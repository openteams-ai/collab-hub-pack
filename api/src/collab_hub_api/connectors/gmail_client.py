from __future__ import annotations

import asyncio
import base64
import html
import unicodedata
from datetime import date, datetime, timedelta, timezone
from email.utils import formataddr, getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .connector_text import sanitize_connector_text
from .models import GmailMessageMetadata

_MAX_METADATA_CONCURRENCY = 5
# Recipients kept per search hit. Every hit repeats its full To/Cc list, so a
# large distribution list dominates the response a model receives (#140).
# The message read is uncapped, so the full list stays reachable.
_SEARCH_MAX_RECIPIENTS = 10


class GmailUpstreamError(RuntimeError):
    def __init__(self, *, operation: str, status_code: int | None = None, message: str = "") -> None:
        self.operation = operation
        self.status_code = status_code
        self.message = message
        detail = f"Gmail {operation} failed"
        if status_code is not None:
            detail = f"{detail} with HTTP {status_code}"
        if message:
            detail = f"{detail}: {message}"
        super().__init__(detail)


class GmailSearchError(ValueError):
    """Raised for caller-correctable Gmail search parameters."""


class GmailClient:
    def __init__(self, *, access_token: str, api_base_url: str, timeout_seconds: float = 10.0):
        self.access_token = access_token
        self.api_base_url = api_base_url.rstrip("/")
        self.timeout = httpx.Timeout(timeout_seconds)

    async def verify_access(self) -> None:
        # Exercise the real list+query contract. users.getProfile can succeed
        # with scopes (for example gmail.metadata) that cannot execute q-based
        # searches, producing a false-positive connected status.
        await self._get_json(
            "/users/me/messages",
            params={"maxResults": "1", "q": "newer_than:1d"},
            operation="access check",
        )

    async def search(
        self,
        *,
        query: str,
        limit: int,
        label_ids: list[str] | None = None,
        include_spam_trash: bool = False,
        days_back: int = 0,
        since_date: date | None = None,
        until_date: date | None = None,
        time_zone: str = "UTC",
        page_token: str = "",
    ) -> tuple[list[GmailMessageMetadata], str, int]:
        """Fetch one Gmail result page and expand each hit into safe metadata.

        Gmail's list endpoint returns only ids, so metadata reads are deliberate
        bounded fan-out (at most ``limit``). ``nextPageToken`` is passed through
        for the caller, with a no-progress guard for repeated provider tokens.
        """
        normalized_query = _gmail_query(
            query,
            days_back=days_back,
            since_date=since_date,
            until_date=until_date,
            time_zone=time_zone,
        )
        params: list[tuple[str, str]] = [
            ("maxResults", str(limit)),
            ("includeSpamTrash", "true" if include_spam_trash else "false"),
        ]
        if normalized_query:
            params.append(("q", normalized_query))
        params.extend(("labelIds", value) for value in label_ids or [] if value.strip())
        if page_token.strip():
            params.append(("pageToken", page_token.strip()))
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            payload = await self._get_json(
                "/users/me/messages",
                params=params,
                operation="search",
                client=client,
            )
            messages = payload.get("messages", [])
            if not isinstance(messages, list):
                messages = []
            message_ids = [
                message_id
                for item in messages[:limit]
                if isinstance(item, dict) and isinstance(message_id := item.get("id"), str) and message_id
            ]
            semaphore = asyncio.Semaphore(_MAX_METADATA_CONCURRENCY)

            async def fetch_metadata(message_id: str) -> GmailMessageMetadata:
                async with semaphore:
                    detail = await self._get_json(
                        f"/users/me/messages/{_path_segment(message_id)}",
                        params=[
                            ("format", "metadata"),
                            ("metadataHeaders", "Subject"),
                            ("metadataHeaders", "From"),
                            ("metadataHeaders", "To"),
                            ("metadataHeaders", "Cc"),
                            ("metadataHeaders", "Date"),
                        ],
                        operation="message metadata",
                        client=client,
                    )
                return _message_metadata(detail, max_recipients=_SEARCH_MAX_RECIPIENTS)

            results = list(await asyncio.gather(*(fetch_metadata(message_id) for message_id in message_ids)))
        next_page_token = _string(payload.get("nextPageToken"))
        if page_token.strip() and next_page_token == page_token.strip():
            raise GmailUpstreamError(
                operation="search",
                message="provider pagination did not advance",
            )
        return results, next_page_token, _non_negative_int(payload.get("resultSizeEstimate"))

    async def read(
        self, message_id: str, max_chars: int
    ) -> tuple[GmailMessageMetadata, str, bool, Literal["plain_text", "html", "multipart", "empty"], int]:
        payload = await self._get_json(
            f"/users/me/messages/{_path_segment(message_id)}",
            params={"format": "full"},
            operation="message read",
        )
        message = _message_metadata(payload)
        message_payload = payload.get("payload")
        text = _message_text(message_payload)
        returned_text = text[:max_chars]
        if message.snippet and _snippet_is_repeated(message.snippet, returned_text):
            # Gmail's snippet usually previews the body's opening, which the
            # returned text then already carries (#140). It is not guaranteed
            # to: Gmail may build it from a part this read did not select (an
            # HTML body behind a plain-text stub), and max_chars may cut the
            # text short. So it is dropped only when it is verifiably repeated.
            message = message.model_copy(update={"snippet": ""})
        body_format, attachment_count = _message_content_info(message_payload)
        truncated = len(text) > max_chars
        return message, returned_text, truncated, body_format, attachment_count

    async def _get_json(
        self,
        path: str,
        params: dict[str, str] | list[tuple[str, str]],
        *,
        operation: str,
        client: httpx.AsyncClient | None = None,
    ) -> dict:
        if client is None:
            async with httpx.AsyncClient(timeout=self.timeout) as owned_client:
                return await self._get_json(
                    path,
                    params,
                    operation=operation,
                    client=owned_client,
                )
        response = await client.get(
            self.api_base_url + path,
            headers={"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"},
            params=params,
        )
        _raise_for_gmail_status(response, operation=operation)
        try:
            payload = response.json()
        except ValueError as exc:
            raise GmailUpstreamError(
                operation=operation,
                status_code=response.status_code,
                message="provider returned invalid JSON",
            ) from exc
        return payload if isinstance(payload, dict) else {}


def _gmail_query(
    query: str,
    *,
    days_back: int,
    since_date: date | None,
    until_date: date | None,
    time_zone: str,
) -> str:
    parts = [query.strip()]
    zone = _time_zone(time_zone)
    now = datetime.now(zone)
    if since_date is not None:
        start = datetime.combine(since_date, datetime.min.time(), tzinfo=zone)
    elif days_back > 0:
        start = now - timedelta(days=days_back)
    else:
        start = None
    if start is not None:
        parts.append(f"after:{int(start.timestamp())}")
    if until_date is not None:
        # Gmail's before: operator is exclusive. Resolve the next local
        # midnight to epoch seconds so Gmail cannot reinterpret the date in its
        # documented PST default timezone.
        exclusive_end = datetime.combine(until_date + timedelta(days=1), datetime.min.time(), tzinfo=zone)
        parts.append(f"before:{int(exclusive_end.timestamp())}")
    return " ".join(part for part in parts if part)


def _message_metadata(payload: dict, *, max_recipients: int | None = None) -> GmailMessageMetadata:
    """Build safe message metadata. ``max_recipients`` caps the recipient list; None keeps all."""
    headers = _headers(payload.get("payload"))
    recipients = _recipient_headers(headers)
    # Slice before sanitizing so dropped addresses are never processed; a None
    # cap slices nothing off.
    shown = recipients[:max_recipients]
    raw_label_ids = payload.get("labelIds", [])
    if not isinstance(raw_label_ids, list):
        raw_label_ids = []
    return GmailMessageMetadata(
        id=_string(payload.get("id")),
        thread_id=_string(payload.get("threadId")),
        subject=sanitize_connector_text(headers.get("subject", "")),
        sender=sanitize_connector_text(headers.get("from", "")),
        recipients=[sanitize_connector_text(value) for value in shown],
        recipients_omitted=len(recipients) - len(shown),
        sent_at=_sent_at(payload, headers),
        snippet=sanitize_connector_text(_string(payload.get("snippet"))),
        label_ids=[value for value in raw_label_ids if isinstance(value, str)],
    )


def _headers(message_payload: object) -> dict[str, str]:
    if not isinstance(message_payload, dict):
        return {}
    raw_headers = message_payload.get("headers", [])
    if not isinstance(raw_headers, list):
        return {}
    result: dict[str, str] = {}
    for item in raw_headers:
        if not isinstance(item, dict):
            continue
        name = _string(item.get("name")).lower()
        value = _string(item.get("value"))
        if name and value:
            result[name] = value
    return result


def _recipient_headers(headers: dict[str, str]) -> list[str]:
    values = [headers.get(name, "") for name in ("to", "cc") if headers.get(name, "")]
    recipients: list[str] = []
    for display_name, address in getaddresses(values):
        if address:
            recipients.append(formataddr((display_name, address)) if display_name else address)
        elif display_name:
            recipients.append(display_name)
    return recipients


def _time_zone(value: str) -> ZoneInfo:
    name = value.strip() or "UTC"
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise GmailSearchError(f"Unknown IANA time_zone {name!r}.") from exc


def _sent_at(payload: dict, headers: dict[str, str]) -> datetime | None:
    internal_date = _string(payload.get("internalDate"))
    try:
        return datetime.fromtimestamp(int(internal_date) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        pass
    try:
        parsed = parsedate_to_datetime(headers.get("date", ""))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _message_text(message_payload: object) -> str:
    """Extract normalized, attachment-free, renderer-safe message text."""
    plain_parts: list[str] = []
    html_parts: list[str] = []
    _collect_text_parts(message_payload, plain_parts=plain_parts, html_parts=html_parts)
    selected = plain_parts if plain_parts else [_html_to_text(value) for value in html_parts]
    normalized = _normalize_text("\n\n".join(value for value in selected if value.strip()))
    return sanitize_connector_text(normalized)


def _message_content_info(
    message_payload: object,
) -> tuple[Literal["plain_text", "html", "multipart", "empty"], int]:
    formats: set[str] = set()
    attachment_count = _collect_content_info(message_payload, formats=formats)
    if {"plain_text", "html"}.issubset(formats):
        return "multipart", attachment_count
    if "plain_text" in formats:
        return "plain_text", attachment_count
    if "html" in formats:
        return "html", attachment_count
    return "empty", attachment_count


def _collect_content_info(message_payload: object, *, formats: set[str]) -> int:
    if not isinstance(message_payload, dict):
        return 0
    filename = _string(message_payload.get("filename"))
    mime_type = _string(message_payload.get("mimeType")).lower()
    body = message_payload.get("body")
    attachment_id = _string(body.get("attachmentId")) if isinstance(body, dict) else ""
    if not filename and not attachment_id:
        if mime_type == "text/plain":
            formats.add("plain_text")
        elif mime_type == "text/html":
            formats.add("html")
    attachment_count = 1 if filename or attachment_id else 0
    parts = message_payload.get("parts", [])
    if isinstance(parts, list):
        attachment_count += sum(_collect_content_info(part, formats=formats) for part in parts)
    return attachment_count


def _collect_text_parts(message_payload: object, *, plain_parts: list[str], html_parts: list[str]) -> None:
    if not isinstance(message_payload, dict):
        return
    filename = _string(message_payload.get("filename"))
    mime_type = _string(message_payload.get("mimeType")).lower()
    body = message_payload.get("body")
    if not filename and isinstance(body, dict):
        data = body.get("data")
        if isinstance(data, str) and data:
            decoded = _decode_body(data)
            if mime_type == "text/plain":
                plain_parts.append(decoded)
            elif mime_type == "text/html":
                html_parts.append(decoded)
    parts = message_payload.get("parts", [])
    if isinstance(parts, list):
        for part in parts:
            _collect_text_parts(part, plain_parts=plain_parts, html_parts=html_parts)


def _snippet_is_repeated(snippet: str, text: str) -> bool:
    """Whether ``text`` already says everything ``snippet`` does.

    Strict on purpose, so doubt keeps the snippet: only Gmail's HTML escaping,
    whitespace, invisible characters and accent encoding are forgiven.
    """
    return _comparable(html.unescape(snippet)) in _comparable(text)


def _comparable(value: str) -> str:
    composed = unicodedata.normalize("NFC", value)
    # Zero-width and other format characters (and the combining grapheme joiner
    # that marketing mail pads its preview with) render as nothing.
    visible = "".join(ch for ch in composed if ch != "\u034f" and unicodedata.category(ch) != "Cf")
    return " ".join(visible.split())


def _decode_body(value: str) -> str:
    try:
        padded = value + ("=" * (-len(value) % 4))
        return base64.urlsafe_b64decode(padded.encode()).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return ""


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, _attrs) -> None:
        if tag in {"br", "div", "li", "p", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"div", "li", "p", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _html_to_text(value: str) -> str:
    parser = _HTMLTextExtractor()
    try:
        parser.feed(value)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed email HTML should not fail the read
        return value
    return "".join(parser.parts)


def _normalize_text(value: str) -> str:
    lines = [" ".join(line.split()) for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
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


def _raise_for_gmail_status(response: httpx.Response, *, operation: str) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise GmailUpstreamError(
            operation=operation,
            status_code=response.status_code,
            message=_google_error_message(response),
        ) from exc


def _google_error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        # A non-JSON body (e.g. an intermediary proxy's HTML error page) may carry
        # infrastructure detail; never echo it to the caller. Only Google's own
        # structured error.message is surfaced below.
        return ""
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str):
            return message[:240].strip()
    return ""
