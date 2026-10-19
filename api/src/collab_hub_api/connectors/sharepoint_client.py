from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote, unquote, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .connector_text import sanitize_connector_text, sanitize_github_api_text
from .http_stream import read_capped
from .models import SharePointItemMetadata, SharePointReadResponse, SharePointSearchHit
from .sharepoint_text import (
    ExtractionBusy,
    PdfExtractionError,
    extract_pdf_text_async,
    extraction_permit,
)

# Microsoft Search pages by ``from``/``size`` offsets (no cursor). Graph refuses
# deep offsets anyway; this cap keeps a page token from walking forever.
_MAX_SEARCH_OFFSET = 10_000
# Read bound on extraction work, independent of the byte cap: a 25 MB PDF can
# still be thousands of near-empty pages.
_MAX_PDF_PAGES = 200
# ``/content`` answers 302 to a pre-authenticated download host; one hop is the
# norm. Tiny cap so a redirect loop is a 502, not a hang.
_MAX_REDIRECTS = 3
# Bytes to download for a text file per character of budget (UTF-8 worst case),
# mirroring drive_client.
_BYTES_PER_CHAR = 4

# Source types Graph converts to PDF server-side (``/content?format=pdf``, v1.0),
# by extension (Graph's own list) and by the MIME types SharePoint reports for
# them, so a file whose name was masked or lacks an extension is still handled.
_PDF_CONVERTIBLE_EXTENSIONS = {
    "doc",
    "docx",
    "odp",
    "ods",
    "odt",
    "pot",
    "potm",
    "potx",
    "pps",
    "ppsx",
    "ppsxm",
    "ppt",
    "pptm",
    "pptx",
    "rtf",
    "xls",
    "xlsx",
}
_PDF_CONVERTIBLE_MIME_TYPES = {
    "application/msword",
    "application/rtf",
    "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint",
    "application/vnd.oasis.opendocument.presentation",
    "application/vnd.oasis.opendocument.spreadsheet",
    "application/vnd.oasis.opendocument.text",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/rtf",
}
_DIRECT_TEXT_MIME_TYPES = {
    "application/json",
    "application/xml",
    "text/csv",
    "text/markdown",
    "text/plain",
}
_DIRECT_TEXT_EXTENSIONS = {"csv", "json", "log", "markdown", "md", "tsv", "txt", "xml", "yaml", "yml"}


class SharePointUpstreamError(RuntimeError):
    def __init__(
        self,
        *,
        operation: str,
        status_code: int | None = None,
        message: str = "",
        retry_after: str = "",
    ) -> None:
        self.operation = operation
        self.status_code = status_code
        self.message = message
        self.retry_after = retry_after
        detail = f"SharePoint {operation} failed"
        if status_code is not None:
            detail = f"{detail} with HTTP {status_code}"
        if message:
            detail = f"{detail}: {message}"
        super().__init__(detail)


class SharePointSearchError(ValueError):
    """Raised for caller-correctable SharePoint parameters -> 422."""


class SharePointBusy(RuntimeError):
    """A document read was not admitted within the bounded wait; the caller should retry shortly."""

    retry_after_seconds = 5


class SharePointUnsupportedItem(RuntimeError):
    """The item exists but cannot be returned as text; the reason is the message."""


@dataclass
class SharePointAccessCheck:
    account: str = ""


class SharePointClient:
    """Read-only Microsoft Graph client for SharePoint files. Only read methods exist.

    ``POST /search/query`` is a read expressed as POST (Microsoft Search's only
    shape); there is still no write path. Every call carries the brokered,
    delegated Graph token, so results are bounded by what the signed-in person
    may already see.
    """

    def __init__(
        self,
        *,
        access_token: str,
        api_base_url: str,
        timeout_seconds: float = 15.0,
        download_timeout_seconds: float = 30.0,
        max_download_bytes: int = 25 * 1024 * 1024,
        include_personal_onedrive: bool = False,
        extraction_timeout_seconds: float = 30.0,
        extraction_memory_limit_bytes: int = 256 * 1024 * 1024,
        extraction_wait_seconds: float = 10.0,
    ):
        self.access_token = access_token
        self.api_base_url = api_base_url.rstrip("/")
        self.timeout = httpx.Timeout(timeout_seconds)
        self.download_timeout_seconds = download_timeout_seconds
        self.max_download_bytes = max_download_bytes
        self.include_personal_onedrive = include_personal_onedrive
        self.extraction_timeout_seconds = extraction_timeout_seconds
        self.extraction_memory_limit_bytes = extraction_memory_limit_bytes
        self.extraction_wait_seconds = extraction_wait_seconds

    async def verify_access(self) -> SharePointAccessCheck:
        # Two-step capability probe (contract 4): /me proves the token brokers;
        # a one-result search proves it can actually read SharePoint content
        # (Sites.Read.All / Files.Read.All granted). Zero hits is still a pass.
        # The two are independent, so they run concurrently: status is polled
        # often (Hub Management, task runs, the Hermes plugin's 4 s probe), and
        # Graph round trips dominate its latency.
        me, _search = await asyncio.gather(
            self._request("GET", "/me", operation="access check"),
            self._request(
                "POST",
                "/search/query",
                json=_search_body("*", offset=0, size=1, sort="relevance"),
                operation="access check",
            ),
        )
        return SharePointAccessCheck(account=_account_label(me))

    async def search(
        self,
        *,
        query: str,
        limit: int,
        lower: date | None,
        upper: date | None,
        time_zone: str,
        sort: str,
        page_token: str,
    ) -> tuple[list[SharePointSearchHit], str, int]:
        """One Microsoft Search page over driveItems, filtered to the window.

        The date window is sent to Graph as a KQL ``LastModifiedTime`` clause
        AND re-checked here per hit (in the caller's zone), so a query string
        that tries to widen the clause, or Graph's day-granular filter, cannot
        leak a file from outside the window. Paging is by offset: the echoed
        token wraps the next ``from`` plus a fingerprint of this search.
        """
        fingerprint = _fingerprint(query, lower, upper, sort, limit)
        offset = _decode_page_token(page_token, fingerprint)
        zone = _time_zone(time_zone)
        payload = await self._request(
            "POST",
            "/search/query",
            json=_search_body(_kql(query, lower, upper), offset=offset, size=limit, sort=sort),
            operation="search",
        )
        container = _hits_container(payload)
        raw_hits = container.get("hits")
        if not isinstance(raw_hits, list):
            raw_hits = []
        hits: list[SharePointSearchHit] = []
        for raw in raw_hits:
            hit = self._search_hit(raw, lower=lower, upper=upper, zone=zone)
            if hit is not None:
                hits.append(hit)
        next_offset = offset + len(raw_hits)
        more = bool(container.get("moreResultsAvailable")) and raw_hits and next_offset <= _MAX_SEARCH_OFFSET
        next_token = _encode_page_token(next_offset, fingerprint) if more else ""
        return hits, next_token, _non_negative_int(container.get("total"))

    async def read_item(self, *, drive_id: str, item_id: str, max_chars: int) -> SharePointReadResponse:
        resource = await self._request(
            "GET",
            f"/drives/{_path_segment(drive_id)}/items/{_path_segment(item_id)}",
            operation="item metadata",
        )
        item = _item_metadata(resource)
        if not item.drive_id:
            item = item.model_copy(update={"drive_id": drive_id})
        try:
            text, truncated = await self._read_text(item, max_chars)
        except SharePointUnsupportedItem as exc:
            # The reason can carry a MIME type or Graph's own message: mask
            # link shapes in it like every other text field (code-aware, so a
            # dotted MIME type survives).
            return SharePointReadResponse(
                item=item, unsupported=True, unsupported_reason=sanitize_github_api_text(str(exc))
            )
        return SharePointReadResponse(item=item, text=text, truncated=truncated)

    def _search_hit(
        self, raw: object, *, lower: date | None, upper: date | None, zone: ZoneInfo
    ) -> SharePointSearchHit | None:
        if not isinstance(raw, dict):
            return None
        resource = raw.get("resource")
        if not isinstance(resource, dict) or not _string(resource.get("id")):
            return None
        _site, personal = _site_from_web_url(_string(resource.get("webUrl")))
        if personal and not self.include_personal_onedrive:
            return None
        item = _item_metadata(resource)
        if lower is not None or upper is not None:
            # A hit with no usable modified time cannot be placed in the window,
            # so it is dropped rather than trusted to Graph's own filtering.
            if item.last_modified is None:
                return None
            modified_day = item.last_modified.astimezone(zone).date()
            if lower is not None and modified_day < lower:
                return None
            if upper is not None and modified_day > upper:
                return None
        summary = sanitize_connector_text(_strip_search_markup(_string(raw.get("summary"))))
        return SharePointSearchHit(**item.model_dump(), summary=summary)

    async def _read_text(self, item: SharePointItemMetadata, max_chars: int) -> tuple[str, bool]:
        if item.is_folder:
            raise SharePointUnsupportedItem("This item is a folder, not a file.")
        if item.size > self.max_download_bytes:
            limit_mb = self.max_download_bytes // (1024 * 1024)
            raise SharePointUnsupportedItem(f"File is larger than the {limit_mb} MB read limit.")
        extension = item.extension
        mime_type = item.mime_type.lower()
        content_path = f"/drives/{_path_segment(item.drive_id)}/items/{_path_segment(item.id)}/content"

        if (
            mime_type in _DIRECT_TEXT_MIME_TYPES
            or mime_type.startswith("text/")
            or extension in _DIRECT_TEXT_EXTENSIONS
        ):
            body, byte_truncated = await self._download(
                content_path, max_bytes=(max_chars * _BYTES_PER_CHAR) + 1, operation="download"
            )
            text = sanitize_connector_text(_normalize_text(body.decode("utf-8", errors="replace")))
            return text[:max_chars], byte_truncated or len(text) > max_chars

        if mime_type == "application/pdf" or extension == "pdf":
            return await self._document_text(
                content_path, max_chars, operation="download", too_large="File is larger than the read limit."
            )

        if extension in _PDF_CONVERTIBLE_EXTENSIONS or mime_type in _PDF_CONVERTIBLE_MIME_TYPES:
            return await self._document_text(
                content_path,
                max_chars,
                params={"format": "pdf"},
                operation="convert",
                too_large="The converted document is larger than the read limit.",
            )

        raise SharePointUnsupportedItem(
            f"This file type cannot be read as text ({mime_type or extension or 'unknown type'})."
        )

    async def _document_text(
        self,
        content_path: str,
        max_chars: int,
        *,
        operation: str,
        too_large: str,
        params: dict[str, str] | None = None,
    ) -> tuple[str, bool]:
        # One admission permit spans the download (or conversion) and the parse,
        # so a read that is waiting its turn holds no body: the pod's cost for
        # document reads is one body plus one worker at a time. A read that is
        # not admitted within the bounded wait answers busy instead of queueing.
        try:
            async with extraction_permit(self.extraction_wait_seconds):
                body, byte_truncated = await self._download(
                    content_path, params=params, max_bytes=self.max_download_bytes, operation=operation
                )
                if byte_truncated:
                    raise SharePointUnsupportedItem(too_large)
                return await self._pdf_text(body, max_chars)
        except ExtractionBusy as exc:
            raise SharePointBusy("The hub is busy reading other documents. Try again in a few seconds.") from exc

    async def _pdf_text(self, data: bytes, max_chars: int) -> tuple[str, bool]:
        try:
            text, truncated = await extract_pdf_text_async(
                data,
                max_chars=max_chars,
                max_pages=_MAX_PDF_PAGES,
                timeout_seconds=self.extraction_timeout_seconds,
                memory_limit_bytes=self.extraction_memory_limit_bytes,
            )
        except PdfExtractionError as exc:
            raise SharePointUnsupportedItem(f"The file could not be read as a PDF ({exc}).") from exc
        except TimeoutError as exc:
            raise SharePointUnsupportedItem("Reading this file took too long.") from exc
        text = sanitize_connector_text(text)
        if not text.strip():
            raise SharePointUnsupportedItem("No text could be extracted (the file may contain only images).")
        return text[:max_chars], truncated or len(text) > max_chars

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict[str, str] | None = None,
        operation: str,
    ) -> dict:
        headers = {"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"}
        if json is not None:
            headers["Content-Type"] = "application/json"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.request(
                method,
                self.api_base_url + path,
                headers=headers,
                json=json,
                params=params,
            )
        _raise_for_graph_status(response, operation=operation)
        try:
            payload = response.json()
        except ValueError as exc:
            raise SharePointUpstreamError(
                operation=operation,
                status_code=response.status_code,
                message="provider returned invalid JSON",
            ) from exc
        return payload if isinstance(payload, dict) else {}

    async def _download(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        max_bytes: int,
        operation: str,
    ) -> tuple[bytes, bool]:
        """Stream ``/content`` (optionally converted) with a hard byte cap.

        Graph answers 302 to a pre-authenticated download URL on another host.
        Redirects are followed by hand, not by httpx: each hop is checked BEFORE
        it is requested (https, or the configured Graph origin for local fakes)
        and the Graph token is sent only to Graph -- the download host gets the
        pre-authenticated URL and nothing else. The body is streamed through
        ``read_capped`` so a huge or gzip-bombed file never sits in memory whole.
        """
        url = httpx.URL(self.api_base_url + path).copy_merge_params(params or {})
        headers: dict[str, str] = {"Authorization": f"Bearer {self.access_token}"}
        timeout = httpx.Timeout(self.download_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            for _hop in range(_MAX_REDIRECTS + 1):
                async with client.stream("GET", url, headers=headers) as response:
                    if response.is_redirect and response.headers.get("Location"):
                        next_url = response.url.join(response.headers["Location"])
                        if next_url.scheme != "https" and not self._same_origin_as_api(next_url):
                            raise SharePointUpstreamError(
                                operation=operation, message="download redirected to a non-https location"
                            )
                        url = next_url
                        headers = {}
                        continue
                    if response.status_code >= 400:
                        await response.aread()
                        if operation == "convert" and response.status_code in {400, 404, 415, 422}:
                            reason = _graph_error_message(response) or "unsupported source format"
                            raise SharePointUnsupportedItem(f"Microsoft could not convert this file to PDF ({reason}).")
                        _raise_for_graph_status(response, operation=operation)
                    return await read_capped(response, max_bytes)
        raise SharePointUpstreamError(operation=operation, message="too many download redirects")

    def _same_origin_as_api(self, url: httpx.URL) -> bool:
        """True when ``url`` shares scheme, host and port with the configured Graph base.

        Production Graph redirects to an https download host. A local fake Graph
        (smoke tests) redirects to itself over plain http; that is the configured,
        trusted endpoint, so it is allowed without weakening the https rule for
        any other host.
        """
        base = urlparse(self.api_base_url)
        return url.scheme == base.scheme and url.host == (base.hostname or "") and url.port == base.port


def sharepoint_time_bounds(
    *,
    days_back: int,
    since_date: date | None,
    until_date: date | None,
    time_zone: str,
) -> tuple[date | None, date | None]:
    """Resolve friendly date fields to inclusive calendar-day bounds in the zone.

    Microsoft Search's KQL ``LastModifiedTime`` restriction works in whole days,
    so the window is expressed as dates in the caller's zone (and every hit is
    re-checked against the same dates). ``(None, None)`` means no window.
    """
    if not days_back and since_date is None and until_date is None:
        return None, None
    today = datetime.now(_time_zone(time_zone)).date()
    lower: date | None
    if since_date is not None:
        lower = since_date
    elif days_back > 0:
        lower = today - timedelta(days=days_back)
    else:
        lower = None
    return lower, until_date


def _search_body(query_string: str, *, offset: int, size: int, sort: str) -> dict:
    request: dict = {
        "entityTypes": ["driveItem"],
        "query": {"queryString": query_string},
        "from": offset,
        "size": size,
    }
    if sort == "modified":
        request["sortProperties"] = [{"name": "lastModifiedDateTime", "isDescending": True}]
    return {"requests": [request]}


def _kql(query: str, lower: date | None, upper: date | None) -> str:
    """Build the KQL query string: ``(user words) AND LastModifiedTime>=... ``.

    The user's words are cleaned of parentheses and unbalanced quotes and then
    wrapped in their own parentheses, so no operator inside them can escape the
    group and cancel the date clause. ``LastModifiedTime`` is the managed
    property name KQL understands (not the ``lastModifiedDateTime`` field name).
    """
    parts: list[str] = []
    words = _clean_query(query)
    if words:
        parts.append(f"({words})")
    if lower is not None:
        parts.append(f"LastModifiedTime>={lower.isoformat()}")
    if upper is not None:
        parts.append(f"LastModifiedTime<={upper.isoformat()}")
    return " AND ".join(parts) if parts else "*"


def _clean_query(query: str) -> str:
    text = "".join(ch for ch in query if ch.isprintable() or ch == " ")
    text = text.replace("(", " ").replace(")", " ")
    if text.count('"') % 2:
        text = text.replace('"', " ")
    return " ".join(text.split())


def _fingerprint(query: str, lower: date | None, upper: date | None, sort: str, limit: int) -> str:
    key = "|".join([" ".join(query.lower().split()), str(lower), str(upper), sort, str(limit)])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _encode_page_token(offset: int, fingerprint: str) -> str:
    raw = json.dumps({"from": offset, "fp": fingerprint}, separators=(",", ":")).encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_page_token(token: str, fingerprint: str) -> int:
    value = token.strip()
    if not value:
        return 0
    try:
        padded = value + ("=" * (-len(value) % 4))
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, TypeError) as exc:
        raise SharePointSearchError("page_token is not a SharePoint continuation token") from exc
    offset = payload.get("from") if isinstance(payload, dict) else None
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0 or offset > _MAX_SEARCH_OFFSET:
        raise SharePointSearchError("page_token is not a SharePoint continuation token")
    if payload.get("fp") != fingerprint:
        raise SharePointSearchError(
            "page_token belongs to a different search; repeat the original query, dates, sort and limit, "
            "or omit page_token to start over"
        )
    return offset


def _hits_container(payload: dict) -> dict:
    value = payload.get("value")
    if isinstance(value, list) and value and isinstance(value[0], dict):
        containers = value[0].get("hitsContainers")
        if isinstance(containers, list) and containers and isinstance(containers[0], dict):
            return containers[0]
    return {}


def _item_metadata(resource: dict) -> SharePointItemMetadata:
    parent = resource.get("parentReference")
    if not isinstance(parent, dict):
        parent = {}
    modified_by = resource.get("lastModifiedBy")
    user = modified_by.get("user") if isinstance(modified_by, dict) else None
    file_facet = resource.get("file")
    raw_name = _string(resource.get("name"))
    site_name, personal = _site_from_web_url(_string(resource.get("webUrl")))
    return SharePointItemMetadata(
        id=_string(resource.get("id")),
        drive_id=_string(parent.get("driveId")),
        # Names, sites and people are identifiers: the code-aware sanitizer keeps
        # "report.docx" readable while still masking real URLs and emails.
        name=sanitize_github_api_text(raw_name),
        extension=raw_name.rsplit(".", 1)[-1].lower() if "." in raw_name else "",
        site_name="OneDrive" if personal else sanitize_github_api_text(site_name),
        mime_type=_string(file_facet.get("mimeType")) if isinstance(file_facet, dict) else "",
        size=_non_negative_int(resource.get("size")),
        last_modified=_parse_time(_string(resource.get("lastModifiedDateTime"))),
        last_modified_by=sanitize_github_api_text(_string(user.get("displayName")) if isinstance(user, dict) else ""),
        is_folder=isinstance(resource.get("folder"), dict),
    )


def _site_from_web_url(web_url: str) -> tuple[str, bool]:
    """``(site name, is personal OneDrive)`` from a SharePoint item URL.

    The URL itself is never surfaced (contract 7); it is inspected here only to
    name the site (``/sites/<name>/`` or ``/teams/<name>/``) and to recognise a
    personal OneDrive, which lives on the ``<tenant>-my.sharepoint.com`` host.
    """
    if not web_url:
        return "", False
    try:
        parsed = urlparse(web_url)
    except ValueError:
        return "", False
    host = (parsed.hostname or "").lower()
    personal = "-my.sharepoint." in host or "/personal/" in parsed.path.lower()
    segments = [segment for segment in parsed.path.split("/") if segment]
    for index, segment in enumerate(segments[:-1]):
        if segment.lower() in {"sites", "teams"}:
            return unquote(segments[index + 1]), personal
    return "", personal


def _strip_search_markup(summary: str) -> str:
    """Remove Microsoft Search's ``<c0>...</c0>`` highlights and ``<ddd/>`` ellipses."""
    if not summary:
        return ""
    text = re.sub(r"<ddd\s*/?>", " ... ", summary)
    text = re.sub(r"</?c\d+>", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    return " ".join(html.unescape(text).split())


def _account_label(me: dict) -> str:
    """Who is signed in, without an email-shaped token (contract 7)."""
    display_name = _string(me.get("displayName"))
    if display_name:
        return sanitize_github_api_text(display_name)
    principal = _string(me.get("userPrincipalName"))
    return sanitize_github_api_text(principal.split("@", 1)[0]) if principal else ""


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _time_zone(value: str) -> ZoneInfo:
    name = value.strip() or "UTC"
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise SharePointSearchError(f"Unknown IANA time_zone {name!r}.") from exc


def _normalize_text(value: str) -> str:
    lines = [line.rstrip() for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    compact: list[str] = []
    for line in lines:
        if not line and compact and compact[-1] == "":
            continue
        compact.append(line)
    return "\n".join(compact).strip()


def _path_segment(value: str) -> str:
    return quote(value, safe="")


def _string(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _non_negative_int(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _raise_for_graph_status(response: httpx.Response, *, operation: str) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise SharePointUpstreamError(
            operation=operation,
            status_code=response.status_code,
            message=_graph_error_message(response),
            retry_after=response.headers.get("Retry-After", "") if response.status_code == 429 else "",
        ) from exc


def _graph_error_message(response: httpx.Response) -> str:
    """Graph errors are ``{"error": {"code", "message", "innerError": {...}}}``.

    Only the structured top-level ``message`` is surfaced. ``innerError`` carries
    request ids and tenant hints, and a non-JSON body (an intermediary's HTML
    error page) may carry infrastructure detail; neither is echoed.
    """
    try:
        payload = response.json()
    except ValueError:
        return ""
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str):
            return message[:240].strip()
    return ""
