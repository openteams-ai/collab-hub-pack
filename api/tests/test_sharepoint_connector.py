from __future__ import annotations

import asyncio
import base64
import io
import json
import sys
import time
import types
import zlib
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone

import httpx
import pytest
from httpx import ASGITransport, AsyncClient, Response
from pydantic import ValidationError

from collab_hub_api.config import Config
from collab_hub_api.connectors import sharepoint_client, sharepoint_pdf_worker, sharepoint_text
from collab_hub_api.connectors.models import (
    SHAREPOINT_READONLY_SCOPES,
    SharePointReadRequest,
    SharePointSearchRequest,
)
from collab_hub_api.connectors.sharepoint_client import _kql, _site_from_web_url, _strip_search_markup
from collab_hub_api.connectors.sharepoint_text import PdfExtractionError, extract_pdf_text, extract_pdf_text_async
from collab_hub_api.core import make_app

STATIC_TOKEN = "eyJ-static-graph-token-alice"
DRIVE_ID = "b!abc123-XYZ_456"
ITEM_ID = "01BYE5RZ6QN3ZWBTUFOFD3GSPGOHDJD36K"
GRAPH = "https://graph.test/v1.0"
DOWNLOAD_HOST = "https://contoso.sharepoint.test"
SITE_URL = "https://contoso.sharepoint.test/sites/Marketing/Shared%20Documents"
PERSONAL_URL = "https://contoso-my.sharepoint.test/personal/alice_contoso/Documents"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.fixture(autouse=True)
def _allow_unsigned_bearer(monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_BEARER_ALLOW_UNSIGNED", "true")


def _jwt(payload: dict) -> str:
    def encode(part: dict) -> str:
        raw = json.dumps(part, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode(payload)}."


def _auth_header() -> dict[str, str]:
    token = _jwt({"preferred_username": "alice", "org_id": "org-a", "workspace_id": "workspace-a"})
    return {"Authorization": f"Bearer {token}"}


def _config(tmp_path, **sharepoint) -> Config:
    sharepoint_config = {
        "static_access_token": STATIC_TOKEN,
        "api_base_url": GRAPH,
        **sharepoint,
    }
    return Config.parse(
        {
            "storage": {"frames_path": str(tmp_path / "frames")},
            "frames": {"active_state": {"backend": "memory"}, "mcp_session_manager_enabled": False},
            "connectors": {"sharepoint": sharepoint_config},
        }
    )


@asynccontextmanager
async def _client(app):
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c


def _install_mock_client(monkeypatch, handler) -> None:
    original_async_client = httpx.AsyncClient

    def mock_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)


# --- Graph shapes -----------------------------------------------------------


def _resource(
    item_id: str,
    name: str,
    *,
    drive_id: str = DRIVE_ID,
    web_url: str | None = None,
    size: int = 1234,
    modified: str = "2026-10-06T10:00:00Z",
    mime: str = DOCX_MIME,
    folder: bool = False,
) -> dict:
    resource = {
        "@odata.type": "#microsoft.graph.driveItem",
        "id": item_id,
        "name": name,
        "size": size,
        "webUrl": web_url if web_url is not None else f"{SITE_URL}/{name}",
        "lastModifiedDateTime": modified,
        "lastModifiedBy": {"user": {"displayName": "Bob Example", "email": "bob@contoso.test"}},
        "parentReference": {"driveId": drive_id, "siteId": "contoso.sharepoint.test,guid1,guid2"},
    }
    if folder:
        resource["folder"] = {"childCount": 3}
    else:
        resource["file"] = {"mimeType": mime}
    return resource


def _hit(resource: dict, summary: str = "") -> dict:
    return {"hitId": resource["id"], "rank": 1, "summary": summary, "resource": resource}


def _search_response(hits: list[dict], *, total: int | None = None, more: bool = False) -> dict:
    return {
        "value": [
            {
                "searchTerms": ["x"],
                "hitsContainers": [
                    {"hits": hits, "total": len(hits) if total is None else total, "moreResultsAvailable": more}
                ],
            }
        ]
    }


def _graph_error(status: int, message: str) -> Response:
    return Response(
        status,
        json={"error": {"code": "someCode", "message": message, "innerError": {"request-id": "SECRET-REQ-ID"}}},
    )


def _ok_handler(request: httpx.Request) -> Response:
    path = request.url.path
    if path.endswith("/me"):
        return Response(
            200,
            json={"displayName": "Alice Example", "userPrincipalName": "alice@contoso.onmicrosoft.com"},
        )
    if path.endswith("/search/query"):
        return Response(200, json=_search_response([]))
    return _graph_error(404, "Item not found")


def _minimal_pdf(text: str) -> bytes:
    """A hand-built one-page PDF with ``text`` in Helvetica, enough for PDFium."""
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content = f"BT /F1 24 Tf 72 700 Td ({escaped}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n".encode()
    return bytes(out)


def _read_handler(
    resource: dict,
    *,
    content: bytes = b"",
    converted: bytes | None = None,
    convert_status: int = 200,
    convert_message: str = "InputFormatNotSupported",
):
    """Metadata + download handler: ``/content`` 302s to another host like Graph does."""
    seen: dict = {"download_authorization": None, "download_calls": 0, "convert_params": None}

    def handler(request: httpx.Request) -> Response:
        path = request.url.path
        if request.url.host == "contoso.sharepoint.test":
            seen["download_authorization"] = request.headers.get("Authorization")
            if path.endswith("/converted"):
                return Response(200, content=converted or b"", headers={"Content-Type": "application/pdf"})
            return Response(200, content=content, headers={"Content-Type": "application/octet-stream"})
        if path.endswith("/content"):
            seen["download_calls"] += 1
            if request.url.params.get("format") == "pdf":
                seen["convert_params"] = dict(request.url.params)
                if convert_status != 200:
                    return _graph_error(convert_status, convert_message)
                return Response(302, headers={"Location": f"{DOWNLOAD_HOST}/download/converted"})
            return Response(302, headers={"Location": f"{DOWNLOAD_HOST}/download/raw"})
        if f"/items/{ITEM_ID}" in path:
            return Response(200, json=resource)
        return _ok_handler(request)

    return handler, seen


# --- request model bounds -------------------------------------------------


def test_sharepoint_search_request_bounds() -> None:
    request = SharePointSearchRequest(query="plan", limit=25, time_zone="America/New_York", sort="modified")
    assert request.limit == 25
    assert request.time_zone == "America/New_York"
    with pytest.raises(ValidationError):
        SharePointSearchRequest(query="plan", limit=26)
    with pytest.raises(ValidationError):
        SharePointSearchRequest(query="plan", time_zone="Mars/Phobos")
    with pytest.raises(ValidationError):
        SharePointSearchRequest(query="plan", since_date="2026-02-01", until_date="2026-01-01")
    with pytest.raises(ValidationError):
        SharePointSearchRequest(query="plan", sort="newest")
    # An empty query needs a date window ("what changed yesterday"); on its own it is refused.
    with pytest.raises(ValidationError):
        SharePointSearchRequest(query="")
    assert SharePointSearchRequest(query="", days_back=1).days_back == 1


def test_sharepoint_read_request_bounds() -> None:
    assert SharePointReadRequest().max_chars == 12_000
    with pytest.raises(ValidationError):
        SharePointReadRequest(max_chars=0)
    with pytest.raises(ValidationError):
        SharePointReadRequest(max_chars=50_001)


# --- pure helpers ---------------------------------------------------------


def test_kql_wraps_words_and_appends_date_clause() -> None:
    assert _kql("roadmap", None, None) == "(roadmap)"
    assert _kql("", None, None) == "*"
    assert _kql("", date(2026, 10, 1), None) == "LastModifiedTime>=2026-10-01"
    assert (
        _kql("q3 plan", date(2026, 10, 1), date(2026, 10, 7))
        == "(q3 plan) AND LastModifiedTime>=2026-10-01 AND LastModifiedTime<=2026-10-07"
    )
    # Parentheses in the user's words cannot close the group early; an odd quote is dropped.
    assert _kql('x) OR "y', date(2026, 10, 1), None) == "(x OR y) AND LastModifiedTime>=2026-10-01"


def test_strip_search_markup_and_site_name() -> None:
    assert _strip_search_markup("<c0>Marketing</c0> plan &amp; budget<ddd/>") == "Marketing plan & budget ..."
    assert _site_from_web_url(f"{SITE_URL}/plan.docx") == ("Marketing", False)
    assert _site_from_web_url("https://t.sharepoint.test/teams/Eng%20Team/Docs/a.txt") == ("Eng Team", False)
    assert _site_from_web_url(f"{PERSONAL_URL}/draft.docx") == ("", True)
    assert _site_from_web_url("") == ("", False)


def test_extract_pdf_text_bounds_and_rejects_garbage() -> None:
    assert extract_pdf_text(_minimal_pdf("Hello SharePoint"), max_chars=100, max_pages=10) == (
        "Hello SharePoint",
        False,
    )
    text, truncated = extract_pdf_text(_minimal_pdf("Hello SharePoint"), max_chars=5, max_pages=10)
    assert (text, truncated) == ("Hello", True)
    with pytest.raises(PdfExtractionError):
        extract_pdf_text(b"this is not a pdf at all", max_chars=100, max_pages=10)


# --- status ---------------------------------------------------------------


async def test_sharepoint_status_connected_reports_account(tmp_path, monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/search/query"):
            captured["probe"] = json.loads(request.content)
        return _ok_handler(request)

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.get("/v1/connectors/sharepoint/status", headers=_auth_header())
    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is True
    assert body["state"] == "connected"
    assert body["account"] == "Alice Example"
    assert body["scopes"] == SHAREPOINT_READONLY_SCOPES
    assert STATIC_TOKEN not in response.text
    # Capability probe: a bounded one-result search, not just /me (contract 4).
    assert captured["probe"]["requests"][0]["size"] == 1
    assert captured["probe"]["requests"][0]["entityTypes"] == ["driveItem"]


async def test_sharepoint_status_not_connected_without_token_source(tmp_path, monkeypatch):
    _install_mock_client(monkeypatch, _ok_handler)
    app = make_app(_config(tmp_path, static_access_token="", broker_token_url=""))
    async with _client(app) as client:
        response = await client.get("/v1/connectors/sharepoint/status", headers=_auth_header())
    body = response.json()
    assert body["connected"] is False
    assert body["state"] == "not_connected"


@pytest.mark.parametrize("status_code", [401, 403])
async def test_sharepoint_status_reconnect_when_token_rejected(tmp_path, monkeypatch, status_code):
    # A token that brokers but cannot read (consent revoked, expired) must report
    # reconnect_required, not connected (contract 4).
    def handler(request: httpx.Request) -> Response:
        return _graph_error(status_code, "Access token has expired or is not yet valid.")

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.get("/v1/connectors/sharepoint/status", headers=_auth_header())
    body = response.json()
    assert body["connected"] is False
    assert body["state"] == "reconnect_required"
    assert "SECRET-REQ-ID" not in response.text


async def test_sharepoint_status_unavailable_on_server_error(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/me"):
            return _graph_error(500, "boom")
        return _ok_handler(request)

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.get("/v1/connectors/sharepoint/status", headers=_auth_header())
    body = response.json()
    assert body["connected"] is False
    assert body["state"] == "unavailable"


async def test_sharepoint_static_token_wins_over_broker(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        assert "broker" not in request.url.path, "static token must short-circuit the broker"
        assert request.headers.get("Authorization") == f"Bearer {STATIC_TOKEN}"
        return _ok_handler(request)

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path, broker_token_url="https://keycloak.test/broker/microsoft/token"))
    async with _client(app) as client:
        response = await client.get("/v1/connectors/sharepoint/status", headers=_auth_header())
    assert response.json()["state"] == "connected"


# --- search ---------------------------------------------------------------


async def test_sharepoint_search_returns_hits_and_drops_urls(tmp_path, monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/search/query"):
            captured["body"] = json.loads(request.content)
            return Response(
                200,
                json=_search_response(
                    [
                        _hit(
                            _resource(ITEM_ID, "Q3 Marketing Plan.docx"),
                            summary="<c0>Marketing</c0> plan for Q3 see www.evil.example<ddd/>",
                        ),
                        _hit(_resource("02FOLDER", "Archive", folder=True)),
                    ],
                    total=2,
                ),
            )
        return _ok_handler(request)

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/sharepoint/search", headers=_auth_header(), json={"query": "marketing plan"}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["content_trust"] == "external_untrusted"
    assert body["total"] == 2
    assert body["next_page_token"] == ""
    first, folder = body["hits"]
    assert first["id"] == ITEM_ID
    assert first["drive_id"] == DRIVE_ID
    assert first["name"] == "Q3 Marketing Plan.docx"
    assert first["site_name"] == "Marketing"
    assert first["mime_type"] == DOCX_MIME
    assert first["size"] == 1234
    assert first["last_modified_by"] == "Bob Example"
    assert first["last_modified"].startswith("2026-10-06T10:00:00")
    assert first["is_folder"] is False
    # Highlight/ellipsis markup is gone and the link inside the snippet is masked.
    assert first["summary"] == "Marketing plan for Q3 see [link] ..."
    assert folder["is_folder"] is True
    # Request shape: driveItem only, offset paging, the user's words in their own group.
    sent = captured["body"]["requests"][0]
    assert sent["entityTypes"] == ["driveItem"]
    assert sent["query"]["queryString"] == "(marketing plan)"
    assert sent["from"] == 0
    assert sent["size"] == 10
    assert "sortProperties" not in sent
    # No URL, OData link, email, or markup leaks through anywhere in the response.
    assert "sharepoint.test" not in response.text
    assert "@odata" not in response.text
    assert "webUrl" not in response.text
    assert "bob@" not in response.text
    assert "<c0>" not in response.text
    assert STATIC_TOKEN not in response.text


async def test_sharepoint_search_sort_modified_and_date_window(tmp_path, monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/search/query"):
            captured["body"] = json.loads(request.content)
            return Response(
                200,
                json=_search_response(
                    [
                        _hit(_resource(ITEM_ID, "Recent.docx", modified="2026-10-06T10:00:00Z")),
                        _hit(_resource("03OLD", "Ancient.docx", modified="2020-01-01T10:00:00Z")),
                        _hit(_resource("04FUTURE", "Future.docx", modified="2026-10-09T10:00:00Z")),
                        _hit(_resource("05UNDATED", "Undated.docx", modified="")),
                    ]
                ),
            )
        return _ok_handler(request)

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/sharepoint/search",
            headers=_auth_header(),
            json={
                "query": "",
                "since_date": "2026-10-01",
                "until_date": "2026-10-07",
                "time_zone": "UTC",
                "sort": "modified",
            },
        )
    assert response.status_code == 200
    sent = captured["body"]["requests"][0]
    assert sent["query"]["queryString"] == "LastModifiedTime>=2026-10-01 AND LastModifiedTime<=2026-10-07"
    assert sent["sortProperties"] == [{"name": "lastModifiedDateTime", "isDescending": True}]
    # Hits Graph returned from outside the window (too old, too new) and a hit
    # with no modified time are all dropped Hub-side as well.
    assert [hit["name"] for hit in response.json()["hits"]] == ["Recent.docx"]


async def test_sharepoint_search_days_back_uses_callers_zone(tmp_path, monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/search/query"):
            captured["body"] = json.loads(request.content)
            return Response(200, json=_search_response([]))
        return _ok_handler(request)

    # 20:00 UTC on Oct 15 is already Oct 16 in Auckland, so "7 days back" in the
    # caller's zone is Oct 9; computed from UTC it would be Oct 8.
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 15, 20, 0, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(sharepoint_client, "datetime", FrozenDatetime)
    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/sharepoint/search",
            headers=_auth_header(),
            json={
                "query": "x) OR LastModifiedTime>=1900-01-01 AND (y",
                "days_back": 7,
                "time_zone": "Pacific/Auckland",
            },
        )
    assert response.status_code == 200
    query_string = captured["body"]["requests"][0]["query"]["queryString"]
    # The user's parentheses were neutralised, the outer date clause is still
    # ANDed on, and the day comes from the caller's zone, not UTC.
    assert query_string == "(x OR LastModifiedTime>=1900-01-01 AND y) AND LastModifiedTime>=2026-10-09"
    assert query_string.count("(") == 1


async def test_sharepoint_search_excludes_personal_onedrive_by_default(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/search/query"):
            return Response(
                200,
                json=_search_response(
                    [
                        _hit(_resource(ITEM_ID, "Team plan.docx")),
                        _hit(_resource("04PERSONAL", "My draft.docx", web_url=f"{PERSONAL_URL}/My%20draft.docx")),
                    ]
                ),
            )
        return _ok_handler(request)

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post("/v1/connectors/sharepoint/search", headers=_auth_header(), json={"query": "plan"})
    assert [hit["name"] for hit in response.json()["hits"]] == ["Team plan.docx"]

    app = make_app(_config(tmp_path, include_personal_onedrive=True))
    async with _client(app) as client:
        response = await client.post("/v1/connectors/sharepoint/search", headers=_auth_header(), json={"query": "plan"})
    hits = response.json()["hits"]
    assert [hit["name"] for hit in hits] == ["Team plan.docx", "My draft.docx"]
    assert hits[1]["site_name"] == "OneDrive"


async def test_sharepoint_search_page_token_roundtrip_and_rejections(tmp_path, monkeypatch):
    offsets: list[int] = []

    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/search/query"):
            sent = json.loads(request.content)["requests"][0]
            offsets.append(sent["from"])
            hits = [
                _hit(_resource(f"0{index}ITEM", f"file{index}.txt", mime="text/plain")) for index in range(sent["size"])
            ]
            return Response(200, json=_search_response(hits, total=50, more=True))
        return _ok_handler(request)

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        first = await client.post(
            "/v1/connectors/sharepoint/search", headers=_auth_header(), json={"query": "file", "limit": 5}
        )
        token = first.json()["next_page_token"]
        assert token
        second = await client.post(
            "/v1/connectors/sharepoint/search",
            headers=_auth_header(),
            json={"query": "file", "limit": 5, "page_token": token},
        )
        assert second.status_code == 200
        # The same token with a different query is a stale cursor.
        stale = await client.post(
            "/v1/connectors/sharepoint/search",
            headers=_auth_header(),
            json={"query": "other", "limit": 5, "page_token": token},
        )
        garbage = await client.post(
            "/v1/connectors/sharepoint/search",
            headers=_auth_header(),
            json={"query": "file", "limit": 5, "page_token": "not-a-token"},
        )
    assert offsets == [0, 5]
    assert stale.status_code == 422
    assert garbage.status_code == 422
    assert "SharePoint continuation token" in garbage.json()["detail"]


async def test_sharepoint_search_graph_400_is_422_and_hides_inner_error(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        return _graph_error(400, "The specified query is invalid.")

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post("/v1/connectors/sharepoint/search", headers=_auth_header(), json={"query": "x"})
    assert response.status_code == 422
    assert "The specified query is invalid." in response.json()["detail"]
    assert "SECRET-REQ-ID" not in response.text


async def test_sharepoint_search_throttled_surfaces_retry_after(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        return Response(429, json={"error": {"message": "Too many requests"}}, headers={"Retry-After": "7"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post("/v1/connectors/sharepoint/search", headers=_auth_header(), json={"query": "x"})
    assert response.status_code == 429
    assert response.headers.get("Retry-After") == "7"


async def test_sharepoint_search_provider_timeout_is_502(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post("/v1/connectors/sharepoint/search", headers=_auth_header(), json={"query": "x"})
    assert response.status_code == 502


# --- read -----------------------------------------------------------------


def _read_path(item_id: str = ITEM_ID, drive_id: str = DRIVE_ID) -> str:
    return f"/v1/connectors/sharepoint/drives/{drive_id}/items/{item_id}/read"


async def test_sharepoint_read_text_file_follows_redirect_without_token(tmp_path, monkeypatch):
    handler, seen = _read_handler(
        _resource(ITEM_ID, "readme.txt", mime="text/plain"), content=b"Hello\r\n\r\n\r\nWorld www.evil.example\r\n"
    )
    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(_read_path(), headers=_auth_header(), json={})
    assert response.status_code == 200
    body = response.json()
    assert body["content_trust"] == "external_untrusted"
    assert body["item"]["name"] == "readme.txt"
    assert body["item"]["drive_id"] == DRIVE_ID
    assert body["item"]["site_name"] == "Marketing"
    assert body["text"] == "Hello\n\nWorld [link]"
    assert body["truncated"] is False
    assert body["unsupported"] is False
    # Graph 302s to the download host; the Graph token must not follow it there.
    assert seen["download_authorization"] is None
    assert STATIC_TOKEN not in response.text
    assert "sharepoint.test" not in response.text


async def test_sharepoint_read_text_file_truncates(tmp_path, monkeypatch):
    handler, _seen = _read_handler(_resource(ITEM_ID, "notes.md", mime="text/markdown"), content=b"0123456789" * 10)
    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(_read_path(), headers=_auth_header(), json={"max_chars": 5})
    body = response.json()
    assert body["text"] == "01234"
    assert body["truncated"] is True


async def test_sharepoint_read_docx_converts_to_pdf_and_extracts_text(tmp_path, monkeypatch):
    handler, seen = _read_handler(
        _resource(ITEM_ID, "Q3 Marketing Plan.docx"), converted=_minimal_pdf("Hello SharePoint")
    )
    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(_read_path(), headers=_auth_header(), json={})
    assert response.status_code == 200
    body = response.json()
    assert body["unsupported"] is False
    assert "Hello SharePoint" in body["text"]
    assert seen["convert_params"] == {"format": "pdf"}
    assert seen["download_authorization"] is None


async def test_sharepoint_read_pdf_downloads_as_is(tmp_path, monkeypatch):
    handler, seen = _read_handler(
        _resource(ITEM_ID, "policy.pdf", mime="application/pdf"), content=_minimal_pdf("Policy text")
    )
    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(_read_path(), headers=_auth_header(), json={})
    body = response.json()
    assert "Policy text" in body["text"]
    assert seen["convert_params"] is None


@pytest.mark.parametrize(
    ("resource", "expected"),
    [
        (_resource(ITEM_ID, "logo.png", mime="image/png"), "cannot be read as text"),
        (_resource(ITEM_ID, "Archive", folder=True), "folder"),
        (_resource(ITEM_ID, "huge.docx", size=10**9), "read limit"),
    ],
    ids=["image", "folder", "too-large"],
)
async def test_sharepoint_read_unsupported_types_return_metadata_without_download(
    tmp_path, monkeypatch, resource, expected
):
    handler, seen = _read_handler(resource)
    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(_read_path(), headers=_auth_header(), json={})
    assert response.status_code == 200
    body = response.json()
    assert body["unsupported"] is True
    assert expected in body["unsupported_reason"]
    assert body["text"] == ""
    assert body["item"]["name"] == resource["name"]
    assert seen["download_calls"] == 0


async def test_sharepoint_read_conversion_failure_is_unsupported_not_502(tmp_path, monkeypatch):
    handler, _seen = _read_handler(_resource(ITEM_ID, "weird.docx"), convert_status=415)
    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(_read_path(), headers=_auth_header(), json={})
    assert response.status_code == 200
    body = response.json()
    assert body["unsupported"] is True
    assert "InputFormatNotSupported" in body["unsupported_reason"]
    assert "SECRET-REQ-ID" not in response.text


async def test_sharepoint_read_image_only_pdf_is_unsupported(tmp_path, monkeypatch):
    # A valid PDF with no text layer (a scan) extracts to nothing: say so.
    handler, _seen = _read_handler(_resource(ITEM_ID, "scan.pdf", mime="application/pdf"), content=_minimal_pdf(""))
    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(_read_path(), headers=_auth_header(), json={})
    body = response.json()
    assert body["unsupported"] is True
    assert "No text could be extracted" in body["unsupported_reason"]


@pytest.mark.parametrize(
    ("drive_id", "item_id"),
    [("b!good", "bad id with spaces"), ("b!good", "item!bang"), ("drive~tilde", ITEM_ID)],
    ids=["item-with-spaces", "item-with-bang", "drive-with-tilde"],
)
async def test_sharepoint_read_rejects_bad_ids_before_any_graph_call(tmp_path, monkeypatch, drive_id, item_id):
    # These reach the route (unlike a slash, which is a routing 404), so the
    # id validators must answer 422 themselves and Graph must see no request.
    graph_calls: list[str] = []

    def handler(request: httpx.Request) -> Response:
        graph_calls.append(request.url.path)
        return _ok_handler(request)

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            f"/v1/connectors/sharepoint/drives/{httpx.URL(path=drive_id).path}/items/{httpx.URL(path=item_id).path}/read",
            headers=_auth_header(),
            json={},
        )
    assert response.status_code == 422
    assert graph_calls == []


async def test_sharepoint_read_with_a_slash_in_the_id_is_a_routing_404(tmp_path, monkeypatch):
    _install_mock_client(monkeypatch, _ok_handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            f"/v1/connectors/sharepoint/drives/drive/with/slash/items/{ITEM_ID}/read",
            headers=_auth_header(),
            json={},
        )
    assert response.status_code == 404


async def test_sharepoint_read_not_found_is_404_with_guidance(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        return _graph_error(404, "The resource could not be found.")

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(_read_path(), headers=_auth_header(), json={})
    assert response.status_code == 404
    assert "not be visible to your linked Microsoft account" in response.json()["detail"]


async def test_sharepoint_read_upstream_error_is_502(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        return _graph_error(500, "boom")

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(_read_path(), headers=_auth_header(), json={})
    assert response.status_code == 502
    assert "SECRET-REQ-ID" not in response.text


async def test_sharepoint_read_transport_error_is_502(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        raise httpx.ConnectTimeout("t", request=request)

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(_read_path(), headers=_auth_header(), json={})
    assert response.status_code == 502


# --- broker mappings --------------------------------------------------------

BROKER_URL = "https://keycloak.test/realms/nebari/broker/microsoft/token"


def _broker_config(tmp_path, broker_status: int):
    def handler(request: httpx.Request) -> Response:
        if "broker" in request.url.path:
            if broker_status == 200:
                return Response(200, json={"access_token": "eyJ-brokered", "refresh_token": "rt", "expires_in": 3599})
            return Response(broker_status, json={"error": "broker"})
        assert request.headers.get("Authorization") == "Bearer eyJ-brokered"
        return _ok_handler(request)

    return _config(tmp_path, static_access_token="", broker_token_url=BROKER_URL), handler


async def test_sharepoint_brokered_token_is_used_for_graph(tmp_path, monkeypatch):
    config, handler = _broker_config(tmp_path, 200)
    _install_mock_client(monkeypatch, handler)
    app = make_app(config)
    async with _client(app) as client:
        response = await client.get("/v1/connectors/sharepoint/status", headers=_auth_header())
    assert response.json()["state"] == "connected"
    assert "eyJ-brokered" not in response.text


@pytest.mark.parametrize(("broker_status", "expected"), [(404, 409), (401, 409), (403, 503), (500, 503)])
async def test_sharepoint_search_broker_errors_map(tmp_path, monkeypatch, broker_status, expected):
    config, handler = _broker_config(tmp_path, broker_status)
    _install_mock_client(monkeypatch, handler)
    app = make_app(config)
    async with _client(app) as client:
        response = await client.post("/v1/connectors/sharepoint/search", headers=_auth_header(), json={"query": "x"})
    assert response.status_code == expected


async def test_sharepoint_read_refuses_non_https_download_on_another_host(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        path = request.url.path
        if request.url.host == "evil.example":
            raise AssertionError("the download must never follow a plain-http redirect to another host")
        if path.endswith("/content"):
            return Response(302, headers={"Location": "http://evil.example/download/raw"})
        if f"/items/{ITEM_ID}" in path:
            return Response(200, json=_resource(ITEM_ID, "readme.txt", mime="text/plain"))
        return _ok_handler(request)

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(_read_path(), headers=_auth_header(), json={})
    assert response.status_code == 502
    assert "non-https" in response.json()["detail"]


async def test_sharepoint_read_allows_plain_http_download_on_the_configured_origin(tmp_path, monkeypatch):
    # A local fake Graph (smoke tests) redirects to itself over http; that is the
    # configured endpoint, so it is trusted without weakening the https rule.
    def handler(request: httpx.Request) -> Response:
        path = request.url.path
        if path.endswith("/download/raw"):
            return Response(200, content=b"plain http on the configured origin")
        if path.endswith("/content"):
            return Response(302, headers={"Location": "http://graph.local:8976/download/raw"})
        if f"/items/{ITEM_ID}" in path:
            return Response(200, json=_resource(ITEM_ID, "readme.txt", mime="text/plain"))
        return _ok_handler(request)

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path, api_base_url="http://graph.local:8976/v1.0"))
    async with _client(app) as client:
        response = await client.post(_read_path(), headers=_auth_header(), json={})
    assert response.status_code == 200
    assert response.json()["text"] == "plain http on the configured origin"


async def test_sharepoint_routes_require_hub_bearer(tmp_path, monkeypatch):
    _install_mock_client(monkeypatch, _ok_handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        status_response = await client.get("/v1/connectors/sharepoint/status")
        search_response = await client.post("/v1/connectors/sharepoint/search", json={"query": "x"})
        read_response = await client.post(_read_path(), json={})
    assert status_response.status_code == 401
    assert search_response.status_code == 401
    assert read_response.status_code == 401


# --- extraction safeguards --------------------------------------------------


async def test_pdf_parses_never_overlap(monkeypatch):
    # One parse at a time across the API process: with a memory ceiling per
    # worker, that is also the bound on what extraction can cost the pod at once.
    active = 0
    peak = 0

    async def fake_worker(data, **_kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        return "text", False

    monkeypatch.setattr(sharepoint_text, "_run_worker", fake_worker)
    results = await asyncio.gather(
        *[
            extract_pdf_text_async(b"%PDF-", max_chars=10, max_pages=1, timeout_seconds=5, memory_limit_bytes=1 << 20)
            for _ in range(4)
        ]
    )
    assert results == [("text", False)] * 4
    assert peak == 1


async def test_sharepoint_read_masks_links_inside_converted_documents(tmp_path, monkeypatch):
    # Document text is untrusted: a link inside a converted Word file must come
    # back masked, like every other connector text field.
    handler, _seen = _read_handler(
        _resource(ITEM_ID, "Q3 Marketing Plan.docx"),
        converted=_minimal_pdf("See https://evil.example/x for the plan"),
    )
    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(_read_path(), headers=_auth_header(), json={})
    body = response.json()
    assert body["unsupported"] is False
    assert "evil.example" not in response.text
    assert "[link]" in body["text"]


# --- adverse documents stay inside their worker's budget ---------------------


def _flate_pdf(pages: int, content: bytes) -> bytes:
    """``pages`` page objects sharing one FlateDecode content stream of ``content``.

    Small on disk, large when inflated: the shape of a file that is cheap to
    upload and expensive to parse.
    """
    compressed = zlib.compress(content, 9)
    kids = b" ".join(f"{4 + index} 0 R".encode() for index in range(pages))
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [" + kids + b"] /Count " + str(pages).encode() + b" >>",
        b"<< /Length "
        + str(len(compressed)).encode()
        + b" /Filter /FlateDecode >>\nstream\n"
        + compressed
        + b"\nendstream",
    ]
    objects += [b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 3 0 R >>"] * pages
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n".encode()
    return bytes(out)


def _memory_hungry_pdf() -> bytes:
    """One page whose content stream inflates to 32 MiB of path operators."""
    return _flate_pdf(1, b"0 0 m 1 1 l S\n" * (32 * 1024 * 1024 // 14))


def _slow_pdf() -> bytes:
    """200 pages sharing an 8 MiB content stream: a long parse that yields no text."""
    return _flate_pdf(200, b"q Q\n" * (8 * 1024 * 1024 // 4))


def _spy_on_workers(monkeypatch) -> list:
    workers: list = []
    real_exec = asyncio.create_subprocess_exec

    async def spying_exec(*args, **kwargs):
        process = await real_exec(*args, **kwargs)
        workers.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spying_exec)
    return workers


@pytest.mark.parametrize("adverse", [_memory_hungry_pdf, _slow_pdf], ids=["memory-hungry", "slow"])
async def test_sharepoint_read_of_an_adverse_pdf_fails_within_budget_and_the_hub_keeps_serving(
    tmp_path, monkeypatch, adverse
):
    # The parse runs in a worker with a memory ceiling and a deadline. Whatever
    # the file does to PDFium, the route answers "unsupported" inside the budget,
    # the worker is gone (killed or finished, and reaped), the API answered other
    # requests meanwhile, and the next ordinary read works.
    workers = _spy_on_workers(monkeypatch)
    served = {"content": adverse()}
    assert len(served["content"]) < 100 * 1024  # cheap to upload
    resource = _resource(ITEM_ID, "report.pdf", mime="application/pdf", size=len(served["content"]))

    def handler(request: httpx.Request) -> Response:
        if request.url.host == "contoso.sharepoint.test":
            return Response(200, content=served["content"], headers={"Content-Type": "application/pdf"})
        if request.url.path.endswith("/content"):
            return Response(302, headers={"Location": f"{DOWNLOAD_HOST}/download/report.pdf"})
        if f"/items/{ITEM_ID}" in request.url.path:
            return Response(200, json=resource)
        return _ok_handler(request)

    _install_mock_client(monkeypatch, handler)
    budget = 3.0
    app = make_app(_config(tmp_path, extraction_timeout_seconds=budget, extraction_memory_limit_bytes=64 * 1024 * 1024))
    async with _client(app) as client:
        started = time.monotonic()
        read = asyncio.create_task(client.post(_read_path(), headers=_auth_header(), json={}))
        await asyncio.sleep(0.5)  # let the worker start
        health = await asyncio.wait_for(client.get("/health"), 5)  # the API answers while the parse runs
        response = await read
        elapsed = time.monotonic() - started
        assert health.status_code == 200
        assert response.status_code == 200
        body = response.json()
        assert body["unsupported"] is True
        assert body["item"]["name"] == "report.pdf"
        assert elapsed < budget + 15
        assert workers and all(process.returncode is not None for process in workers)

    # The next ordinary read works: no worker, permit or body is left behind.
    # (On a default budget: this proves clean state, not speed under test load.)
    served["content"] = _minimal_pdf("Policy text")
    async with _client(make_app(_config(tmp_path))) as client:
        response = await client.post(_read_path(), headers=_auth_header(), json={})
    body = response.json()
    assert body["unsupported"] is False
    assert "Policy text" in body["text"]


async def test_sharepoint_read_kills_and_reaps_a_stalled_worker(tmp_path, monkeypatch):
    # A worker that never answers is ended by the deadline, not waited on forever.
    workers = _spy_on_workers(monkeypatch)
    monkeypatch.setattr(
        sharepoint_text,
        "worker_command",
        lambda **_kwargs: [sys.executable, "-c", "import sys, time; sys.stdin.buffer.read(); time.sleep(60)"],
    )
    handler, _seen = _read_handler(
        _resource(ITEM_ID, "policy.pdf", mime="application/pdf"), content=_minimal_pdf("Policy text")
    )
    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path, extraction_timeout_seconds=1))
    started = time.monotonic()
    async with _client(app) as client:
        response = await client.post(_read_path(), headers=_auth_header(), json={})
    elapsed = time.monotonic() - started
    body = response.json()
    assert body["unsupported"] is True
    assert "took too long" in body["unsupported_reason"]
    assert elapsed < 10
    assert len(workers) == 1
    assert workers[0].returncode is not None


async def test_sharepoint_unsupported_reason_masks_links_from_graph(tmp_path, monkeypatch):
    handler, _seen = _read_handler(
        _resource(ITEM_ID, "weird.docx"),
        convert_status=415,
        convert_message="Unsupported source; see https://aka.ms/graph-convert",
    )
    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(_read_path(), headers=_auth_header(), json={})
    body = response.json()
    assert body["unsupported"] is True
    assert "aka.ms" not in response.text
    assert "[link]" in body["unsupported_reason"]


# --- the worker process itself ------------------------------------------------


def test_worker_main_answers_text_or_an_error_on_stdout(monkeypatch, capsys):
    limits = []
    monkeypatch.setattr(sharepoint_pdf_worker, "_apply_limits", lambda memory, cpu: limits.append((memory, cpu)))

    def feed(data: bytes) -> None:
        monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(data)))

    feed(_minimal_pdf("Hello worker"))
    assert sharepoint_pdf_worker.main(["worker", "100", "5", "1048576", "7"]) == 0
    assert json.loads(capsys.readouterr().out) == {"text": "Hello worker", "truncated": False}
    assert limits == [(1048576, 7)]  # applied before the parse, from argv

    feed(b"not a pdf")
    assert sharepoint_pdf_worker.main(["worker", "100", "5", "1048576", "7"]) == 0
    assert json.loads(capsys.readouterr().out) == {"error": "not a readable PDF"}

    def exhausted(data, **_kwargs):
        raise MemoryError

    monkeypatch.setattr(sharepoint_text, "extract_pdf_text", exhausted)
    feed(_minimal_pdf("Hello worker"))
    assert sharepoint_pdf_worker.main(["worker", "100", "5", "1048576", "7"]) == 0
    assert json.loads(capsys.readouterr().out) == {"error": "the file needs more memory than a read is allowed"}


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no rlimits")
def test_worker_limits_stay_under_the_hard_ceiling(monkeypatch):
    import resource

    applied = {}
    monkeypatch.setattr(
        resource,
        "getrlimit",
        lambda kind: (resource.RLIM_INFINITY, 1000 if kind == resource.RLIMIT_AS else resource.RLIM_INFINITY),
    )
    monkeypatch.setattr(resource, "setrlimit", lambda kind, pair: applied.__setitem__(kind, pair))
    sharepoint_pdf_worker._apply_limits(5000, 3)
    assert applied[resource.RLIMIT_AS] == (1000, 1000)  # asked above the hard limit: clamp, never fail
    assert applied[resource.RLIMIT_CPU] == (3, resource.RLIM_INFINITY)


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        ("print('nope')", "no result"),
        ("print('[]')", "no result"),
        ("print('{\"text\": 5}')", "no result"),
        ('print(\'{"error": "broken"}\')', "broken"),
        ("import sys; sys.exit(3)", "within the extraction limits"),
    ],
)
async def test_a_misbehaving_worker_reply_reads_as_unsupported(monkeypatch, reply, error):
    monkeypatch.setattr(sharepoint_text, "worker_command", lambda **_kwargs: [sys.executable, "-c", reply])
    with pytest.raises(PdfExtractionError, match=error):
        await extract_pdf_text_async(b"%PDF-", max_chars=10, max_pages=1, timeout_seconds=5, memory_limit_bytes=1 << 20)


async def test_a_worker_that_floods_stdout_is_cut_off_before_the_deadline(monkeypatch):
    # The reply ceiling, not the deadline, ends a flood: the API never buffers it.
    flood = "import sys; sys.stdout.write('x' * (9 * 1024 * 1024)); sys.stdout.flush(); sys.stdin.buffer.read()"
    monkeypatch.setattr(sharepoint_text, "worker_command", lambda **_kwargs: [sys.executable, "-c", flood])
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await extract_pdf_text_async(
            b"%PDF-", max_chars=10, max_pages=1, timeout_seconds=30, memory_limit_bytes=1 << 20
        )
    assert time.monotonic() - started < 10  # cut off by the ceiling, not by the deadline or the reap cap


async def test_a_reply_larger_than_one_pipe_read_is_assembled_whole(monkeypatch):
    big = "import json, sys; sys.stdin.buffer.read(); print(json.dumps({'text': 'y' * 300000, 'truncated': True}))"
    monkeypatch.setattr(sharepoint_text, "worker_command", lambda **_kwargs: [sys.executable, "-c", big])
    text, truncated = await extract_pdf_text_async(
        b"%PDF-", max_chars=250000, max_pages=1, timeout_seconds=30, memory_limit_bytes=1 << 20
    )
    assert (len(text), truncated) == (250000, True)


# --- admission: one document read at a time, decided before any download ----


def _document_handler(bodies: dict[str, bytes | None]):
    """Metadata + download for several PDF items; records every download with its time.

    A ``None`` body makes that item's download fail with a Graph 500.
    """
    downloads: list[tuple[str, float]] = []

    def handler(request: httpx.Request) -> Response:
        path = request.url.path
        if request.url.host == "contoso.sharepoint.test":
            item_id = path.rsplit("/", 1)[-1]
            downloads.append((item_id, time.monotonic()))
            body = bodies[item_id]
            if body is None:
                return Response(500, content=b"storage error")
            return Response(200, content=body, headers={"Content-Type": "application/pdf"})
        for item_id in bodies:
            if f"/items/{item_id}/content" in path:
                return Response(302, headers={"Location": f"{DOWNLOAD_HOST}/download/{item_id}"})
            if f"/items/{item_id}" in path:
                return Response(200, json=_resource(item_id, f"doc-{item_id[-1]}.pdf", mime="application/pdf"))
        return _ok_handler(request)

    return handler, downloads


def _hold_worker(monkeypatch, seconds: float) -> dict:
    """Stand-in worker that keeps its permit for ``seconds``; ``hold["on"] = False`` restores the real one."""
    real = sharepoint_text.worker_command
    hold = {"on": True}
    script = (
        "import json, sys, time; sys.stdin.buffer.read(); "
        f"time.sleep({seconds}); print(json.dumps({{'text': 'Held text', 'truncated': False}}))"
    )
    monkeypatch.setattr(
        sharepoint_text,
        "worker_command",
        lambda **kwargs: [sys.executable, "-c", script] if hold["on"] else real(**kwargs),
    )
    return hold


def _item_ids(suffixes: str) -> list[str]:
    return [ITEM_ID[:-1] + suffix for suffix in suffixes]


async def test_document_reads_are_admitted_one_at_a_time_before_any_download(tmp_path, monkeypatch):
    # With one read holding the worker, concurrent document reads must not each
    # download a body and wait with it. Past the bounded wait they answer busy
    # (503 + Retry-After) having downloaded nothing; the worker is reaped; an
    # ordinary read and another Hub route work afterwards.
    workers = _spy_on_workers(monkeypatch)
    hold = _hold_worker(monkeypatch, 2.0)
    ids = _item_ids("ABC")
    bodies = {item_id: _minimal_pdf(f"Document {item_id[-1]}") for item_id in ids}
    handler, downloads = _document_handler(bodies)
    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path, extraction_wait_seconds=0.5))
    async with _client(app) as client:
        responses = await asyncio.gather(
            *[client.post(_read_path(item_id), headers=_auth_header(), json={}) for item_id in ids]
        )
        assert sorted(r.status_code for r in responses) == [200, 503, 503]
        for refused in (r for r in responses if r.status_code == 503):
            assert refused.headers["Retry-After"] == "5"
            assert "busy" in refused.json()["detail"]
        admitted = next(r for r in responses if r.status_code == 200)
        assert admitted.json()["text"] == "Held text"
        assert len(downloads) == 1  # the refused reads stockpiled nothing
        assert len(workers) == 1 and workers[0].returncode is not None

        hold["on"] = False
        later = await client.post(_read_path(ids[1]), headers=_auth_header(), json={})
        assert later.status_code == 200
        assert f"Document {ids[1][-1]}" in later.json()["text"]
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/v1/connectors", headers=_auth_header())).status_code == 200


async def test_a_waiting_document_read_downloads_only_once_admitted(tmp_path, monkeypatch):
    # Inside the wait, a read is queued without a body: its download starts
    # only after the current read's parse has ended.
    _hold_worker(monkeypatch, 1.0)
    ids = _item_ids("AB")
    handler, downloads = _document_handler({item_id: _minimal_pdf("x") for item_id in ids})
    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path, extraction_wait_seconds=10))
    async with _client(app) as client:
        first = asyncio.create_task(client.post(_read_path(ids[0]), headers=_auth_header(), json={}))
        await asyncio.sleep(0.3)  # the first read holds the permit and the worker
        second = await client.post(_read_path(ids[1]), headers=_auth_header(), json={})
        assert (await first).status_code == 200
    assert second.status_code == 200
    assert [item_id for item_id, _ in downloads] == ids
    assert downloads[1][1] - downloads[0][1] >= 0.9  # fetched only after the first parse ended


async def test_a_read_that_gives_up_waiting_releases_the_permit(tmp_path, monkeypatch):
    hold = _hold_worker(monkeypatch, 1.0)
    ids = _item_ids("ABC")
    handler, downloads = _document_handler({item_id: _minimal_pdf("x") for item_id in ids})
    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path, extraction_wait_seconds=10))
    async with _client(app) as client:
        first = asyncio.create_task(client.post(_read_path(ids[0]), headers=_auth_header(), json={}))
        await asyncio.sleep(0.3)
        with pytest.raises(TimeoutError):  # the caller gives up while waiting its turn
            await asyncio.wait_for(client.post(_read_path(ids[1]), headers=_auth_header(), json={}), 0.3)
        assert (await first).status_code == 200
        hold["on"] = False
        third = await client.post(_read_path(ids[2]), headers=_auth_header(), json={})
        assert third.status_code == 200  # the permit the abandoned read waited for is free
    assert [item_id for item_id, _ in downloads] == [ids[0], ids[2]]


async def test_a_failed_download_releases_the_permit(tmp_path, monkeypatch):
    ids = _item_ids("AB")
    handler, _downloads = _document_handler({ids[0]: None, ids[1]: _minimal_pdf("After failure")})
    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path, extraction_wait_seconds=0))  # a leaked permit would answer 503 at once
    async with _client(app) as client:
        failed = await client.post(_read_path(ids[0]), headers=_auth_header(), json={})
        assert failed.status_code == 502
        after = await client.post(_read_path(ids[1]), headers=_auth_header(), json={})
    assert after.status_code == 200
    assert "After failure" in after.json()["text"]


async def test_extraction_permit_is_one_at_a_time_with_a_bounded_wait():
    async with sharepoint_text.extraction_permit(0):
        with pytest.raises(sharepoint_text.ExtractionBusy):
            async with sharepoint_text.extraction_permit(0.05):
                pass
    async with sharepoint_text.extraction_permit(0):  # released once the body ends
        pass


# --- the worker's environment ---------------------------------------------------


_SENTINEL = "COLLAB_HUB_API__TEST_SECRET"


async def test_the_worker_never_sees_the_api_environment(monkeypatch):
    # A synthetic secret in the API process must not reach the parse worker.
    # The launch path is the production one (extract_pdf_text_async -> _run_worker
    # -> env=worker_environment()); only the argv is a stand-in that reports
    # the names it can see, never their values.
    monkeypatch.setenv(_SENTINEL, "synthetic-secret-" + "x" * 16)
    report = (
        "import json, os, sys; sys.stdin.buffer.read(); "
        "print(json.dumps({'text': ' '.join(sorted(os.environ)), 'truncated': False}))"
    )
    monkeypatch.setattr(sharepoint_text, "worker_command", lambda **_kwargs: [sys.executable, "-c", report])
    names, _ = await extract_pdf_text_async(
        b"%PDF-", max_chars=10000, max_pages=1, timeout_seconds=30, memory_limit_bytes=1 << 20
    )
    seen = set(names.split())
    assert _SENTINEL not in seen
    assert not {name for name in seen if name.startswith("COLLAB_HUB_API__") or name.startswith("FRAMES_")}
    # Nothing beyond the fixed allowlist (plus what the venv launcher itself adds).
    assert seen <= set(sharepoint_text.worker_environment()) | {"__PYVENV_LAUNCHER__"}


async def test_the_real_worker_parses_with_only_the_minimal_environment(monkeypatch):
    # The real worker command, with the sentinel set in the parent: the spawn
    # receives exactly worker_environment(), and PDFium still loads and parses.
    monkeypatch.setenv(_SENTINEL, "synthetic-secret-" + "y" * 16)
    spawned: list[dict | None] = []
    real_exec = asyncio.create_subprocess_exec

    async def spying_exec(*args, **kwargs):
        spawned.append(kwargs.get("env"))
        return await real_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spying_exec)
    text, truncated = await extract_pdf_text_async(
        _minimal_pdf("Hello worker"), max_chars=100, max_pages=5, timeout_seconds=30, memory_limit_bytes=256 << 20
    )
    assert (text, truncated) == ("Hello worker", False)
    assert spawned == [sharepoint_text.worker_environment()]
    assert _SENTINEL not in spawned[0]
    assert set(spawned[0]) >= {"PATH", "LANG"}
