from __future__ import annotations

import base64
import json
from contextlib import asynccontextmanager

import httpx
import pytest
from httpx import ASGITransport, AsyncClient, Response
from pydantic import ValidationError

from collab_hub_api.config import Config
from collab_hub_api.connectors.models import (
    NOTION_READONLY_CAPABILITIES,
    NotionDatabaseQueryRequest,
    NotionPageReadRequest,
    NotionSearchRequest,
)
from collab_hub_api.core import make_app

STATIC_TOKEN = "secret_notion-bot-token-alice"
PAGE_ID = "0123456789abcdef0123456789abcdef"
DATABASE_ID = "fedcba9876543210fedcba9876543210"


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


def _config(tmp_path, **notion) -> Config:
    notion_config = {
        "static_access_token": STATIC_TOKEN,
        "api_base_url": "https://notion.test",
        "notion_version": "2022-06-28",
        **notion,
    }
    return Config.parse(
        {
            "storage": {"frames_path": str(tmp_path / "frames")},
            "frames": {"active_state": {"backend": "memory"}, "mcp_session_manager_enabled": False},
            "connectors": {"notion": notion_config},
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


def _title_property(text: str, *, href: str | None = None) -> dict:
    return {"Name": {"type": "title", "title": [{"plain_text": text, "href": href}]}}


def _page_hit(page_id: str, title: str, last_edited: str) -> dict:
    return {
        "object": "page",
        "id": page_id,
        "url": "https://www.notion.so/should-be-dropped",
        "last_edited_time": last_edited,
        "properties": _title_property(title),
    }


def _database_hit(database_id: str, title: str, last_edited: str) -> dict:
    return {
        "object": "database",
        "id": database_id,
        "url": "https://www.notion.so/db-dropped",
        "last_edited_time": last_edited,
        "title": [{"plain_text": title, "href": None}],
    }


def _paragraph(text: str, *, href: str | None = None) -> dict:
    return {
        "object": "block",
        "type": "paragraph",
        "has_children": False,
        "paragraph": {"rich_text": [{"plain_text": text, "href": href}]},
    }


def _ok_status_handler(request: httpx.Request) -> Response:
    path = request.url.path
    if path.endswith("/v1/users/me"):
        assert request.headers.get("Notion-Version") == "2022-06-28"
        return Response(200, json={"object": "user", "type": "bot", "bot": {"workspace_name": "Acme HQ"}})
    if path.endswith("/v1/search"):
        return Response(200, json={"object": "list", "results": [], "has_more": False, "next_cursor": None})
    return Response(404, json={"object": "error", "status": 404, "code": "not_found", "message": "Not Found"})


# --- request model bounds -------------------------------------------------


def test_notion_search_request_bounds() -> None:
    request = NotionSearchRequest(query="notes", limit=100, time_zone="America/New_York")
    assert request.limit == 100
    assert request.time_zone == "America/New_York"
    with pytest.raises(ValidationError):
        NotionSearchRequest(limit=101)
    with pytest.raises(ValidationError):
        NotionSearchRequest(time_zone="Mars/Phobos")
    with pytest.raises(ValidationError):
        NotionSearchRequest(since_date="2026-02-01", until_date="2026-01-01")


def test_notion_read_and_query_request_bounds() -> None:
    assert NotionPageReadRequest().max_chars == 20_000
    with pytest.raises(ValidationError):
        NotionPageReadRequest(max_chars=0)
    with pytest.raises(ValidationError):
        NotionDatabaseQueryRequest(limit=0)


# --- status ---------------------------------------------------------------


async def test_notion_status_connected_reports_workspace(tmp_path, monkeypatch):
    _install_mock_client(monkeypatch, _ok_status_handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.get("/v1/connectors/notion/status", headers=_auth_header())
    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is True
    assert body["state"] == "connected"
    assert body["account"] == "Acme HQ"
    assert body["scopes"] == NOTION_READONLY_CAPABILITIES
    assert STATIC_TOKEN not in response.text


async def test_notion_status_not_connected_without_token_source(tmp_path, monkeypatch):
    _install_mock_client(monkeypatch, _ok_status_handler)
    app = make_app(_config(tmp_path, static_access_token="", broker_token_url=""))
    async with _client(app) as client:
        response = await client.get("/v1/connectors/notion/status", headers=_auth_header())
    body = response.json()
    assert body["connected"] is False
    assert body["state"] == "not_connected"


async def test_notion_status_reconnect_when_token_rejected(tmp_path, monkeypatch):
    # A token that brokers but cannot read (401 on the capability probe) must
    # report reconnect_required, not connected (contract 4).
    def handler(request: httpx.Request) -> Response:
        return Response(401, json={"object": "error", "status": 401, "code": "unauthorized", "message": "bad token"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.get("/v1/connectors/notion/status", headers=_auth_header())
    body = response.json()
    assert body["connected"] is False
    assert body["state"] == "reconnect_required"


async def test_notion_status_unavailable_on_server_error(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/v1/users/me"):
            return Response(500, json={"object": "error", "status": 500, "code": "internal", "message": "boom"})
        return _ok_status_handler(request)

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.get("/v1/connectors/notion/status", headers=_auth_header())
    body = response.json()
    assert body["connected"] is False
    assert body["state"] == "unavailable"


# --- config permutations --------------------------------------------------


async def test_notion_static_token_wins_over_broker(tmp_path, monkeypatch):
    # When both a static token and a broker URL are configured the static token
    # is used and the broker is never called. This is intentional (dev/CI).
    def handler(request: httpx.Request) -> Response:
        assert "broker" not in request.url.path, "static token must short-circuit the broker"
        assert request.headers.get("Authorization") == f"Bearer {STATIC_TOKEN}"
        return _ok_status_handler(request)

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path, broker_token_url="https://keycloak.test/broker/token"))
    async with _client(app) as client:
        response = await client.get("/v1/connectors/notion/status", headers=_auth_header())
    assert response.json()["state"] == "connected"


# --- search ---------------------------------------------------------------


async def test_notion_search_returns_hits_and_drops_urls(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/v1/search"):
            body = json.loads(request.content)
            assert body["sort"] == {"timestamp": "last_edited_time", "direction": "descending"}
            return Response(
                200,
                json={
                    "results": [
                        _page_hit(PAGE_ID, "Roadmap", "2026-02-01T00:00:00.000Z"),
                        _database_hit(DATABASE_ID, "Tasks", "2026-01-15T00:00:00.000Z"),
                    ],
                    "has_more": False,
                    "next_cursor": None,
                },
            )
        return Response(404, json={"object": "error", "message": "nope"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post("/v1/connectors/notion/search", headers=_auth_header(), json={"query": "roadmap"})
    assert response.status_code == 200
    body = response.json()
    assert body["content_trust"] == "external_untrusted"
    assert [hit["title"] for hit in body["hits"]] == ["Roadmap", "Tasks"]
    assert [hit["object"] for hit in body["hits"]] == ["page", "database"]
    # No URL field leaks through anywhere in the response.
    assert "url" not in response.text
    assert "notion.so" not in response.text


async def test_notion_search_object_type_filter_and_page_token(tmp_path, monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> Response:
        captured["body"] = json.loads(request.content)
        return Response(
            200,
            json={
                "results": [_page_hit(PAGE_ID, "Only pages", "2026-02-01T00:00:00.000Z")],
                "has_more": True,
                "next_cursor": "cursor-2",
            },
        )

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/notion/search",
            headers=_auth_header(),
            json={"query": "pages", "object_type": "page", "limit": 1, "page_token": "cursor-1"},
        )
    body = response.json()
    assert captured["body"]["filter"] == {"value": "page", "property": "object"}
    assert captured["body"]["start_cursor"] == "cursor-1"
    # limit=1 reached with has_more -> echo the provider cursor to continue.
    assert body["next_page_token"] == "cursor-2"


async def test_notion_search_stops_paging_at_lower_bound(tmp_path, monkeypatch):
    # Newest-first sort + a since_date lower bound: the client must stop paging
    # as soon as it sees an item older than the bound and not fetch further pages.
    pages_served: list[int] = []

    def handler(request: httpx.Request) -> Response:
        body = json.loads(request.content)
        cursor = body.get("start_cursor")
        pages_served.append(1)
        if cursor is None:
            return Response(
                200,
                json={
                    "results": [
                        _page_hit(PAGE_ID, "Recent", "2026-02-10T00:00:00.000Z"),
                        _page_hit("11111111111111111111111111111111", "Too old", "2026-01-01T00:00:00.000Z"),
                    ],
                    "has_more": True,
                    "next_cursor": "cursor-2",
                },
            )
        raise AssertionError("client should have stopped before requesting a second page")

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/notion/search",
            headers=_auth_header(),
            json={"query": "x", "since_date": "2026-02-01", "time_zone": "UTC"},
        )
    body = response.json()
    assert [hit["title"] for hit in body["hits"]] == ["Recent"]
    assert body["next_page_token"] == ""
    assert len(pages_served) == 1


async def test_notion_search_stale_cursor_is_422(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        return Response(
            400,
            json={"object": "error", "status": 400, "code": "validation_error", "message": "invalid start_cursor"},
        )

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/notion/search",
            headers=_auth_header(),
            json={"query": "x", "page_token": "garbage"},
        )
    assert response.status_code == 422
    assert STATIC_TOKEN not in response.text


# --- read page ------------------------------------------------------------


async def test_notion_read_page_assembles_text_and_drops_href(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        path = request.url.path
        if path.endswith(f"/v1/pages/{PAGE_ID}"):
            return Response(
                200,
                json={"object": "page", "id": PAGE_ID, "properties": _title_property("Design Notes")},
            )
        if path.endswith(f"/v1/blocks/{PAGE_ID}/children"):
            return Response(
                200,
                json={
                    "results": [
                        _paragraph("First line with a ", href="https://evil.example/link"),
                        _paragraph("Second line"),
                    ],
                    "has_more": False,
                    "next_cursor": None,
                },
            )
        return Response(404, json={"object": "error", "message": "nope"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(f"/v1/connectors/notion/pages/{PAGE_ID}/read", headers=_auth_header(), json={})
    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Design Notes"
    assert "First line with a" in body["text"]
    assert "Second line" in body["text"]
    assert body["truncated"] is False
    # href never leaks into assembled text.
    assert "evil.example" not in response.text


async def test_notion_read_page_truncates_and_flags_more(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        path = request.url.path
        if path.endswith(f"/v1/pages/{PAGE_ID}"):
            return Response(200, json={"object": "page", "id": PAGE_ID, "properties": _title_property("Big")})
        if path.endswith(f"/v1/blocks/{PAGE_ID}/children"):
            return Response(
                200,
                json={"results": [_paragraph("x" * 50)], "has_more": False, "next_cursor": None},
            )
        return Response(404, json={"object": "error", "message": "nope"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            f"/v1/connectors/notion/pages/{PAGE_ID}/read",
            headers=_auth_header(),
            json={"max_chars": 10},
        )
    body = response.json()
    assert len(body["text"]) == 10
    assert body["truncated"] is True


async def test_notion_read_page_rejects_bad_id(tmp_path, monkeypatch):
    _install_mock_client(monkeypatch, _ok_status_handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post("/v1/connectors/notion/pages/not-a-real-id/read", headers=_auth_header(), json={})
    assert response.status_code == 422


# --- query database -------------------------------------------------------


async def test_notion_query_database_emits_native_timestamp_filter(tmp_path, monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith(f"/v1/databases/{DATABASE_ID}/query"):
            captured["body"] = json.loads(request.content)
            return Response(
                200,
                json={
                    "results": [_page_hit(PAGE_ID, "Row 1", "2026-02-05T00:00:00.000Z")],
                    "has_more": False,
                    "next_cursor": None,
                },
            )
        return Response(404, json={"object": "error", "message": "nope"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            f"/v1/connectors/notion/databases/{DATABASE_ID}/query",
            headers=_auth_header(),
            json={"since_date": "2026-02-01", "until_date": "2026-02-28", "time_zone": "UTC"},
        )
    assert response.status_code == 200
    body = response.json()
    assert [row["title"] for row in body["rows"]] == ["Row 1"]
    # Unlike search, the window is sent to the provider as a native filter.
    filt = captured["body"]["filter"]
    assert filt["timestamp"] == "last_edited_time"
    assert "on_or_after" in filt["last_edited_time"]
    assert "on_or_before" in filt["last_edited_time"]
    assert STATIC_TOKEN not in response.text


async def test_notion_query_database_rejects_bad_id(tmp_path, monkeypatch):
    _install_mock_client(monkeypatch, _ok_status_handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post("/v1/connectors/notion/databases/bad/query", headers=_auth_header(), json={})
    assert response.status_code == 422


async def test_notion_search_provider_timeout_is_502(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post("/v1/connectors/notion/search", headers=_auth_header(), json={"query": "x"})
    assert response.status_code == 502


# --- more error mappings + client edges (coverage) ------------------------

BROKER_URL = "https://keycloak.test/realms/nebari/broker/notion/token"


def _broker_config(tmp_path, broker_status: int):
    """Config with no static token, so the broker path runs, plus a handler
    whose broker response has ``broker_status`` and whose Notion calls succeed."""

    def handler(request: httpx.Request) -> Response:
        if "broker" in request.url.path:
            if broker_status == 200:
                return Response(200, json={"access_token": "ntn_brokered"})
            return Response(broker_status, json={"object": "error", "message": "broker"})
        return _ok_status_handler(request)

    return _config(tmp_path, static_access_token="", broker_token_url=BROKER_URL), handler


async def test_notion_search_broker_not_connected_is_409(tmp_path, monkeypatch):
    config, handler = _broker_config(tmp_path, 404)
    _install_mock_client(monkeypatch, handler)
    app = make_app(config)
    async with _client(app) as client:
        response = await client.post("/v1/connectors/notion/search", headers=_auth_header(), json={"query": "x"})
    assert response.status_code == 409


async def test_notion_search_broker_reconnect_is_409(tmp_path, monkeypatch):
    config, handler = _broker_config(tmp_path, 401)
    _install_mock_client(monkeypatch, handler)
    app = make_app(config)
    async with _client(app) as client:
        response = await client.post("/v1/connectors/notion/search", headers=_auth_header(), json={"query": "x"})
    assert response.status_code == 409


async def test_notion_search_broker_token_error_is_503(tmp_path, monkeypatch):
    config, handler = _broker_config(tmp_path, 500)
    _install_mock_client(monkeypatch, handler)
    app = make_app(config)
    async with _client(app) as client:
        response = await client.post("/v1/connectors/notion/search", headers=_auth_header(), json={"query": "x"})
    assert response.status_code == 503


async def test_notion_read_page_upstream_error_is_502(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith(f"/v1/pages/{PAGE_ID}"):
            return Response(500, json={"object": "error", "status": 500, "message": "boom"})
        return Response(404, json={"object": "error", "message": "nope"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(f"/v1/connectors/notion/pages/{PAGE_ID}/read", headers=_auth_header(), json={})
    assert response.status_code == 502


async def test_notion_read_page_transport_error_is_502(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        raise httpx.ConnectTimeout("t", request=request)

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(f"/v1/connectors/notion/pages/{PAGE_ID}/read", headers=_auth_header(), json={})
    assert response.status_code == 502


async def test_notion_query_database_stale_cursor_is_422(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        return Response(
            400,
            json={"object": "error", "status": 400, "code": "validation_error", "message": "bad cursor"},
        )

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            f"/v1/connectors/notion/databases/{DATABASE_ID}/query",
            headers=_auth_header(),
            json={"page_token": "garbage"},
        )
    assert response.status_code == 422


async def test_notion_query_database_provider_error_is_502(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        return Response(500, json={"object": "error", "status": 500, "message": "boom"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            f"/v1/connectors/notion/databases/{DATABASE_ID}/query", headers=_auth_header(), json={}
        )
    assert response.status_code == 502


async def test_notion_query_database_transport_error_is_502(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        raise httpx.ConnectTimeout("t", request=request)

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            f"/v1/connectors/notion/databases/{DATABASE_ID}/query", headers=_auth_header(), json={}
        )
    assert response.status_code == 502


async def test_notion_search_invalid_json_is_502(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        return Response(200, content=b"<html>not json</html>")

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post("/v1/connectors/notion/search", headers=_auth_header(), json={"query": "x"})
    assert response.status_code == 502


async def test_notion_search_skips_non_content_results(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        return Response(
            200,
            json={
                "results": [
                    "not-a-dict",
                    {"object": "user", "id": "u1"},
                    {"object": "page"},  # missing id
                    _page_hit(PAGE_ID, "Real", "2026-02-01T00:00:00.000Z"),
                ],
                "has_more": False,
                "next_cursor": None,
            },
        )

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post("/v1/connectors/notion/search", headers=_auth_header(), json={"query": "x"})
    body = response.json()
    assert [hit["title"] for hit in body["hits"]] == ["Real"]


async def test_notion_search_skips_items_newer_than_window(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        return Response(
            200,
            json={
                "results": [
                    _page_hit(PAGE_ID, "Future", "2026-03-15T00:00:00.000Z"),
                    _page_hit("22222222222222222222222222222222", "In window", "2026-02-10T00:00:00.000Z"),
                ],
                "has_more": False,
                "next_cursor": None,
            },
        )

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/notion/search",
            headers=_auth_header(),
            json={"query": "x", "until_date": "2026-02-28", "time_zone": "UTC"},
        )
    body = response.json()
    assert [hit["title"] for hit in body["hits"]] == ["In window"]


async def test_notion_search_stuck_cursor_is_502(tmp_path, monkeypatch):
    # Provider keeps handing back the same cursor while claiming has_more -> the
    # client must break the loop as a 502, not spin forever.
    def handler(request: httpx.Request) -> Response:
        return Response(200, json={"results": [], "has_more": True, "next_cursor": "loop"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post("/v1/connectors/notion/search", headers=_auth_header(), json={"query": "x"})
    assert response.status_code == 502


async def test_notion_search_days_back_window(tmp_path, monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> Response:
        captured["body"] = json.loads(request.content)
        return Response(200, json={"results": [], "has_more": False, "next_cursor": None})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/notion/search",
            headers=_auth_header(),
            json={"query": "x", "days_back": 7, "time_zone": "UTC"},
        )
    assert response.status_code == 200
    assert captured["body"]["sort"] == {
        "timestamp": "last_edited_time",
        "direction": "descending",
    }
    assert captured["body"]["query"] == "x"


async def test_notion_read_page_walks_nested_and_paginated_blocks(tmp_path, monkeypatch):
    child_id = "33333333333333333333333333333333"

    def handler(request: httpx.Request) -> Response:
        path = request.url.path
        if path.endswith(f"/v1/pages/{PAGE_ID}"):
            return Response(200, json={"object": "page", "id": PAGE_ID, "properties": _title_property("Nested")})
        if path.endswith(f"/v1/blocks/{PAGE_ID}/children"):
            if request.url.params.get("start_cursor") == "b2":
                return Response(
                    200,
                    json={"results": [_paragraph("Page two")], "has_more": False, "next_cursor": None},
                )
            parent = {
                "object": "block",
                "type": "paragraph",
                "has_children": True,
                "id": child_id,
                "paragraph": {"rich_text": [{"plain_text": "Parent"}]},
            }
            return Response(200, json={"results": [parent], "has_more": True, "next_cursor": "b2"})
        if path.endswith(f"/v1/blocks/{child_id}/children"):
            return Response(
                200,
                json={"results": [_paragraph("Child line")], "has_more": False, "next_cursor": None},
            )
        return Response(404, json={"object": "error", "message": "nope"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(f"/v1/connectors/notion/pages/{PAGE_ID}/read", headers=_auth_header(), json={})
    body = response.json()
    assert "Parent" in body["text"]
    assert "Child line" in body["text"]
    assert "Page two" in body["text"]


async def test_notion_query_database_cursor_and_limit(tmp_path, monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> Response:
        captured["body"] = json.loads(request.content)
        return Response(
            200,
            json={
                "results": [
                    _page_hit(PAGE_ID, "R1", "2026-02-05T00:00:00.000Z"),
                    _page_hit("44444444444444444444444444444444", "R2", "2026-02-04T00:00:00.000Z"),
                ],
                "has_more": True,
                "next_cursor": "c2",
            },
        )

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            f"/v1/connectors/notion/databases/{DATABASE_ID}/query",
            headers=_auth_header(),
            json={"limit": 1, "page_token": "c0"},
        )
    body = response.json()
    assert captured["body"]["start_cursor"] == "c0"
    assert [row["title"] for row in body["rows"]] == ["R1"]
    assert body["next_page_token"] == "c2"


async def test_notion_search_preserves_unconsumed_results_across_pages(tmp_path, monkeypatch):
    # limit=2 with an until_date filter: newest-first, page 1 opens with a
    # too-new item that is skipped, then A; B and C follow. The connector must
    # return [A, B] AND a cursor that still yields C -- never drop C by echoing
    # the whole-page cursor after stopping mid-page.
    id_a = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    id_b = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    id_c = "cccccccccccccccccccccccccccccccc"
    items = [
        _page_hit(PAGE_ID, "TooNew", "2026-03-15T00:00:00.000Z"),
        _page_hit(id_a, "A", "2026-02-10T00:00:00.000Z"),
        _page_hit(id_b, "B", "2026-02-09T00:00:00.000Z"),
        _page_hit(id_c, "C", "2026-02-08T00:00:00.000Z"),
    ]

    def handler(request: httpx.Request) -> Response:
        body = json.loads(request.content)
        size = body["page_size"]
        start = int(body.get("start_cursor") or 0)
        page = items[start : start + size]
        nxt = start + size
        has_more = nxt < len(items)
        return Response(
            200,
            json={"results": page, "has_more": has_more, "next_cursor": str(nxt) if has_more else None},
        )

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        first = await client.post(
            "/v1/connectors/notion/search",
            headers=_auth_header(),
            json={"query": "x", "limit": 2, "until_date": "2026-02-28", "time_zone": "UTC"},
        )
        body = first.json()
        assert [hit["title"] for hit in body["hits"]] == ["A", "B"]
        # C must remain reachable, not silently dropped.
        assert body["next_page_token"] != ""
        second = await client.post(
            "/v1/connectors/notion/search",
            headers=_auth_header(),
            json={
                "query": "x",
                "limit": 2,
                "until_date": "2026-02-28",
                "time_zone": "UTC",
                "page_token": body["next_page_token"],
            },
        )
    assert [hit["title"] for hit in second.json()["hits"]] == ["C"]


async def test_notion_search_sanitizes_urls_in_titles(tmp_path, monkeypatch):
    # A URL typed into the visible title text (not an href/url field) must be
    # neutralized like any other connector text -- dropping href is not enough.
    def handler(request: httpx.Request) -> Response:
        return Response(
            200,
            json={
                "results": [
                    _page_hit(PAGE_ID, "Notes https://example.com/x", "2026-02-01T00:00:00.000Z"),
                    _database_hit(DATABASE_ID, "Board https://foo.example/y", "2026-01-15T00:00:00.000Z"),
                ],
                "has_more": False,
                "next_cursor": None,
            },
        )

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post("/v1/connectors/notion/search", headers=_auth_header(), json={"query": "x"})
    body = response.json()
    assert [hit["title"] for hit in body["hits"]] == ["Notes [link]", "Board [link]"]
    assert "example.com" not in response.text
    assert "foo.example" not in response.text


async def test_notion_read_page_sanitizes_url_in_title(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        path = request.url.path
        if path.endswith(f"/v1/pages/{PAGE_ID}"):
            return Response(
                200,
                json={"object": "page", "id": PAGE_ID, "properties": _title_property("Spec https://example.com/s")},
            )
        if path.endswith(f"/v1/blocks/{PAGE_ID}/children"):
            return Response(200, json={"results": [], "has_more": False, "next_cursor": None})
        return Response(404, json={"object": "error", "message": "nope"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(f"/v1/connectors/notion/pages/{PAGE_ID}/read", headers=_auth_header(), json={})
    body = response.json()
    assert body["title"] == "Spec [link]"
    assert "example.com" not in response.text
