from __future__ import annotations

import base64
import json
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from httpx import ASGITransport, AsyncClient, Response
from pydantic import ValidationError

from collab_hub_api.config import Config
from collab_hub_api.connectors.drive_client import GoogleDriveClient, UnsupportedDriveFileType
from collab_hub_api.connectors.models import (
    SLACK_READONLY_SCOPES,
    DriveFileMetadata,
    DriveSearchRequest,
    SlackReadRequest,
    SlackSearchRequest,
)
from collab_hub_api.connectors.slack_client import (
    MAX_CHANNEL_AUTHORIZATION_PAGES,
    SlackClient,
    SlackConversationNotAllowed,
    SlackUpstreamError,
)
from collab_hub_api.connectors.slack_text import sanitize_slack_text
from collab_hub_api.core import make_app


def _install_mock_client(monkeypatch, handler) -> None:
    original_async_client = httpx.AsyncClient

    def mock_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)


def test_slack_search_allows_bounded_discovery_batch() -> None:
    request = SlackSearchRequest(query="after:2026-06-22", limit=100, page=100)
    assert request.limit == 100
    assert request.page == 100
    with pytest.raises(ValidationError):
        SlackSearchRequest(query="after:2026-06-22", limit=101)
    with pytest.raises(ValidationError):
        SlackSearchRequest(query="after:2026-06-22", page=101)


def _auth_test_only_handler(*, ok: bool = True, error: str = "invalid_auth", status_code: int = 200):
    """A Slack mock that only answers ``auth.test`` — used by connector status tests."""

    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/auth.test"):
            if status_code != 200:
                return Response(status_code, json={"ok": False, "error": error})
            if not ok:
                return Response(200, json={"ok": False, "error": error})
            return Response(
                200,
                json={"ok": True, "user_id": "U0001", "team": "Acme", "url": "https://slack.test/"},
                headers={"x-oauth-scopes": ",".join(SLACK_READONLY_SCOPES)},
            )
        return Response(404, json={"ok": False, "error": "unknown_method"})

    return handler


def _jwt(payload: dict) -> str:
    def encode(part: dict) -> str:
        raw = json.dumps(part, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode(payload)}."


def auth_header(user: str = "alice") -> dict[str, str]:
    return {
        "Authorization": "Bearer "
        + _jwt(
            {
                "preferred_username": user,
                "org_id": "org-a",
                "workspace_id": "workspace-a",
            }
        )
    }


def connector_config(tmp_path, **google) -> Config:
    google_config = {
        "static_access_token": "google-token-alice",
        "drive_api_base_url": "https://drive.test/drive/v3",
        **google,
    }
    return Config.parse(
        {
            "storage": {"frames_path": str(tmp_path / "frames")},
            "frames": {
                "active_state": {"backend": "memory"},
                "mcp_session_manager_enabled": False,
            },
            "connectors": {"google": google_config},
        }
    )


def slack_connector_config(tmp_path, **slack) -> Config:
    slack_config = {
        "static_access_token": "slack-token-alice",
        "api_base_url": "https://slack.test/api",
        **slack,
    }
    return Config.parse(
        {
            "storage": {"frames_path": str(tmp_path / "frames")},
            "frames": {
                "active_state": {"backend": "memory"},
                "mcp_session_manager_enabled": False,
            },
            "connectors": {"slack": slack_config},
        }
    )


async def test_google_drive_status_uses_current_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_BEARER_ALLOW_UNSIGNED", "true")
    app = make_app(connector_config(tmp_path))

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/v1/connectors/google-drive/status", headers=auth_header())

    assert response.status_code == 200
    assert response.json() == {
        "id": "google-drive",
        "name": "Google Drive",
        "connected": True,
        "state": "connected",
        "scopes": ["https://www.googleapis.com/auth/drive.readonly"],
        "detail": None,
    }
    assert "google-token" not in response.text


async def test_google_drive_status_reports_not_connected_without_token_source(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_BEARER_ALLOW_UNSIGNED", "true")
    app = make_app(connector_config(tmp_path, static_access_token="", broker_token_url=""))

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/v1/connectors/google-drive/status", headers=auth_header())

    assert response.status_code == 200
    assert response.json()["connected"] is False
    assert response.json()["state"] == "not_connected"
    assert response.json()["detail"] == "Google connector token broker is not configured"


async def test_google_drive_status_reports_reconnect_for_rejected_broker_token(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_BEARER_ALLOW_UNSIGNED", "true")

    def handler(request: httpx.Request) -> Response:
        assert request.url == "https://keycloak.test/realms/nebari/broker/google/token"
        return Response(401)

    original_async_client = httpx.AsyncClient

    def mock_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)
    app = make_app(
        connector_config(
            tmp_path,
            static_access_token="",
            broker_token_url="https://keycloak.test/realms/nebari/broker/google/token",
        )
    )

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/v1/connectors/google-drive/status", headers=auth_header())

    assert response.status_code == 200
    assert response.json()["connected"] is False
    assert response.json()["state"] == "reconnect_required"
    assert response.json()["detail"] == "Google account must be reconnected"


async def test_google_drive_status_reports_broker_permission_error(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_BEARER_ALLOW_UNSIGNED", "true")

    def handler(request: httpx.Request) -> Response:
        assert request.url == "https://keycloak.test/realms/nebari/broker/google/token"
        return Response(403)

    original_async_client = httpx.AsyncClient

    def mock_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)
    app = make_app(
        connector_config(
            tmp_path,
            static_access_token="",
            broker_token_url="https://keycloak.test/realms/nebari/broker/google/token",
        )
    )

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/v1/connectors/google-drive/status", headers=auth_header())

    assert response.status_code == 200
    assert response.json()["connected"] is False
    assert response.json()["state"] == "unavailable"
    assert (
        response.json()["detail"]
        == "Keycloak denied broker token access. Grant the broker read-token role to normal Hub users."
    )


async def test_search_and_read_drive_endpoints_do_not_expose_access_token(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_BEARER_ALLOW_UNSIGNED", "true")
    seen_authorization: list[str] = []

    def handler(request: httpx.Request) -> Response:
        seen_authorization.append(request.headers.get("authorization", ""))
        if request.url.path.endswith("/files") and request.url.params.get("alt") == "media":
            return Response(200, content=b"")
        if request.url.path.endswith("/files"):
            assert "healthcare" in request.url.params["q"]
            return Response(
                200,
                json={
                    "files": [
                        {
                            "id": "file-1",
                            "name": "Healthcare past performance",
                            "mimeType": "application/vnd.google-apps.document",
                            "modifiedTime": "2026-06-01T12:00:00Z",
                            "webViewLink": "https://docs.google.test/file-1",
                            "owners": [{"displayName": "Alice"}],
                        }
                    ]
                },
            )
        if request.url.path.endswith("/files/file-1/export"):
            return Response(200, content=b"Implemented healthcare analytics platform.\nReduced manual review by 30%.")
        if request.url.path.endswith("/files/file-1"):
            return Response(
                200,
                json={
                    "id": "file-1",
                    "name": "Healthcare past performance",
                    "mimeType": "application/vnd.google-apps.document",
                    "modifiedTime": "2026-06-01T12:00:00Z",
                    "webViewLink": "https://docs.google.test/file-1",
                    "owners": [{"displayName": "Alice"}],
                },
            )
        return Response(404)

    original_async_client = httpx.AsyncClient

    def mock_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)
    app = make_app(connector_config(tmp_path))

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            search = await client.post(
                "/v1/connectors/google-drive/search",
                headers=auth_header(),
                json={"query": "healthcare past performance", "limit": 5},
            )
            read = await client.post(
                "/v1/connectors/google-drive/files/file-1/read",
                headers=auth_header(),
                json={"max_chars": 32},
            )

    assert search.status_code == 200
    assert search.json()["files"][0]["name"] == "Healthcare past performance"
    assert "web_url" not in search.json()["files"][0]
    assert "docs.google.test" not in search.text
    assert search.json()["content_trust"] == "external_untrusted"
    assert read.status_code == 200
    assert read.json()["text"] == "Implemented healthcare analytics"
    assert read.json()["truncated"] is True
    assert "google-token-alice" not in search.text
    assert "google-token-alice" not in read.text
    assert seen_authorization
    assert all(value == "Bearer google-token-alice" for value in seen_authorization)


async def test_google_drive_read_reports_upstream_export_detail(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_BEARER_ALLOW_UNSIGNED", "true")

    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/files/sheet-1/export"):
            return Response(403, json={"error": {"message": "The file is not exportable."}})
        if request.url.path.endswith("/files/sheet-1"):
            return Response(
                200,
                json={
                    "id": "sheet-1",
                    "name": "Case Study Catalog",
                    "mimeType": "application/vnd.google-apps.spreadsheet",
                    "modifiedTime": "2026-06-01T12:00:00Z",
                },
            )
        return Response(404)

    original_async_client = httpx.AsyncClient

    def mock_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)
    app = make_app(connector_config(tmp_path))

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/connectors/google-drive/files/sheet-1/read",
                headers=auth_header(),
                json={"max_chars": 100},
            )

    assert response.status_code == 502
    assert response.json()["detail"] == "Google Drive export failed with HTTP 403: The file is not exportable."
    assert "google-token-alice" not in response.text


async def test_google_drive_client_rejects_unsupported_binary_type(monkeypatch):
    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/files/file-1"):
            return Response(
                200,
                json={
                    "id": "file-1",
                    "name": "scan.pdf",
                    "mimeType": "application/pdf",
                    "modifiedTime": "2026-06-01T12:00:00Z",
                },
            )
        return Response(404)

    original_async_client = httpx.AsyncClient

    def mock_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)
    drive = GoogleDriveClient(access_token="token", api_base_url="https://drive.test/drive/v3")

    file = await drive.metadata("file-1")

    try:
        await drive.read_text(file, 100)
    except UnsupportedDriveFileType:
        pass
    else:
        raise AssertionError("expected unsupported PDF to be rejected")


async def test_drive_search_query_includes_modified_time_and_mime_filters(monkeypatch):
    captured_q = ""

    def handler(request: httpx.Request) -> Response:
        nonlocal captured_q
        captured_q = request.url.params["q"]
        return Response(200, json={"files": []})

    original_async_client = httpx.AsyncClient

    def mock_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)
    drive = GoogleDriveClient(access_token="token", api_base_url="https://drive.test/drive/v3")

    await drive.search(
        query="healthcare",
        limit=3,
        modified_after=datetime(2026, 1, 1, tzinfo=UTC),
        mime_types=["application/vnd.google-apps.document"],
    )

    assert "trashed = false" in captured_q
    assert "fullText contains 'healthcare'" in captured_q
    assert "modifiedTime > '2026-01-01T00:00:00+00:00'" in captured_q
    assert "mimeType = 'application/vnd.google-apps.document'" in captured_q


def test_drive_search_request_normalizes_json_encoded_mime_array():
    request = DriveSearchRequest.model_validate(
        {
            "query": "Apollo/Sync",
            "mime_types": ['["application/vnd.google-apps.document"]'],
        }
    )

    assert request.mime_types == ["application/vnd.google-apps.document"]


async def test_drive_search_includes_shared_drive_flags(monkeypatch):
    captured_params = {}

    def handler(request: httpx.Request) -> Response:
        nonlocal captured_params
        captured_params = dict(request.url.params)
        return Response(200, json={"files": []})

    original_async_client = httpx.AsyncClient

    def mock_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)
    drive = GoogleDriveClient(access_token="token", api_base_url="https://drive.test/drive/v3")

    await drive.search(query="catalog", limit=3)

    assert captured_params["supportsAllDrives"] == "true"
    assert captured_params["includeItemsFromAllDrives"] == "true"
    assert captured_params["corpora"] == "allDrives"


async def test_drive_search_expands_multi_term_queries(monkeypatch):
    captured_queries: list[str] = []

    def handler(request: httpx.Request) -> Response:
        captured_queries.append(request.url.params["q"])
        return Response(200, json={"files": []})

    original_async_client = httpx.AsyncClient

    def mock_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)
    drive = GoogleDriveClient(access_token="token", api_base_url="https://drive.test/drive/v3")

    await drive.search(
        query="case study catalog inventory past performance project client work proposal",
        limit=3,
    )

    joined = "\n".join(captured_queries)
    assert "name contains 'case study catalog'" in joined
    assert "name contains 'catalog'" in joined
    assert "name contains 'inventory'" in joined
    assert "fullText contains 'past'" in joined


async def test_drive_search_ranks_name_matches_before_full_text_noise(monkeypatch):
    def handler(request: httpx.Request) -> Response:
        return Response(
            200,
            json={
                "files": [
                    {
                        "id": "doc-1",
                        "name": "Weekly sales meeting",
                        "mimeType": "application/vnd.google-apps.document",
                        "modifiedTime": "2026-06-01T12:00:00Z",
                    },
                    {
                        "id": "sheet-1",
                        "name": "Internal Project Case Study Catalog & Inventory",
                        "mimeType": "application/vnd.google-apps.spreadsheet",
                        "modifiedTime": "2026-06-01T12:00:00Z",
                    },
                ]
            },
        )

    original_async_client = httpx.AsyncClient

    def mock_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)
    drive = GoogleDriveClient(access_token="token", api_base_url="https://drive.test/drive/v3")

    files = await drive.search(query="case study catalog inventory past performance", limit=1)

    assert [file.id for file in files] == ["sheet-1"]


async def test_drive_search_keeps_targeted_name_results_when_broad_query_fails(monkeypatch):
    def handler(request: httpx.Request) -> Response:
        q = request.url.params["q"]
        if "fullText contains" in q:
            return Response(500, json={"error": {"message": "Internal Error"}})
        if "name contains 'Internal Project Case Study Catalog'" in q:
            return Response(
                200,
                json={
                    "files": [
                        {
                            "id": "sheet-1",
                            "name": "Internal Project Case Study Catalog & Inventory",
                            "mimeType": "application/vnd.google-apps.spreadsheet",
                            "modifiedTime": "2026-06-01T12:00:00Z",
                        },
                    ]
                },
            )
        return Response(200, json={"files": []})

    original_async_client = httpx.AsyncClient

    def mock_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)
    drive = GoogleDriveClient(access_token="token", api_base_url="https://drive.test/drive/v3")

    files = await drive.search(query="Internal Project Case Study Catalog", limit=10)

    assert [file.id for file in files] == ["sheet-1"]


async def test_drive_search_skips_broad_queries_when_targeted_name_search_matches(monkeypatch):
    captured_queries: list[str] = []

    def handler(request: httpx.Request) -> Response:
        q = request.url.params["q"]
        captured_queries.append(q)
        if "name contains 'Internal Project Case'" in q:
            return Response(
                200,
                json={
                    "files": [
                        {
                            "id": "sheet-1",
                            "name": "Internal Project Case Study Catalog & Inventory",
                            "mimeType": "application/vnd.google-apps.spreadsheet",
                            "modifiedTime": "2026-06-01T12:00:00Z",
                        },
                    ]
                },
            )
        return Response(200, json={"files": []})

    original_async_client = httpx.AsyncClient

    def mock_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)
    drive = GoogleDriveClient(access_token="token", api_base_url="https://drive.test/drive/v3")

    files = await drive.search(query="Internal Project Case Study Catalog & Inventory", limit=10)

    assert [file.id for file in files] == ["sheet-1"]
    assert all("fullText contains" not in q for q in captured_queries)


async def test_google_drive_client_escapes_file_id_path_segments(monkeypatch):
    captured_paths: list[str] = []

    def handler(request: httpx.Request) -> Response:
        raw_path = request.url.raw_path.decode()
        captured_paths.append(raw_path)
        if request.url.params.get("alt") == "media":
            return Response(200, content=b"notes")
        if raw_path.startswith("/drive/v3/files/file%2F1%3Falt%3Dmedia?"):
            assert request.url.params["supportsAllDrives"] == "true"
            return Response(
                200,
                json={
                    "id": "file/1?alt=media",
                    "name": "notes.txt",
                    "mimeType": "text/plain",
                    "modifiedTime": "2026-06-01T12:00:00Z",
                },
            )
        return Response(404)

    original_async_client = httpx.AsyncClient

    def mock_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)
    drive = GoogleDriveClient(access_token="token", api_base_url="https://drive.test/drive/v3")

    file = await drive.metadata("file/1?alt=media")
    text, truncated = await drive.read_text(file, 20)

    assert text == "notes"
    assert truncated is False
    assert captured_paths == [
        "/drive/v3/files/file%2F1%3Falt%3Dmedia?fields=id%2Cname%2CmimeType%2CmodifiedTime%2Cowners%28displayName%2CemailAddress%29&supportsAllDrives=true",
        "/drive/v3/files/file%2F1%3Falt%3Dmedia?alt=media&supportsAllDrives=true",
    ]


async def test_slack_status_uses_current_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_BEARER_ALLOW_UNSIGNED", "true")
    # Status validates the token against auth.test and reports the scopes Slack
    # actually granted (from the x-oauth-scopes header).
    _install_mock_client(monkeypatch, _auth_test_only_handler())
    app = make_app(slack_connector_config(tmp_path))

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/v1/connectors/slack/status", headers=auth_header())

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == "slack"
    assert payload["name"] == "Slack"
    assert payload["connected"] is True
    assert payload["state"] == "connected"
    assert "search:read" in payload["scopes"]
    assert "im:history" not in payload["scopes"]
    assert "mpim:history" not in payload["scopes"]
    assert all("write" not in scope and "post" not in scope for scope in payload["scopes"])
    assert "slack-token" not in response.text


async def test_slack_status_reports_reconnect_for_identity_token(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_BEARER_ALLOW_UNSIGNED", "true")
    # A linked OpenID sign-in token brokers successfully but auth.test rejects it.
    _install_mock_client(monkeypatch, _auth_test_only_handler(ok=False, error="invalid_auth"))
    app = make_app(slack_connector_config(tmp_path))

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/v1/connectors/slack/status", headers=auth_header())

    assert response.status_code == 200
    payload = response.json()
    assert payload["connected"] is False
    assert payload["state"] == "reconnect_required"
    assert payload["scopes"] == []
    assert "invalid_auth" in payload["detail"]
    assert "user token" in payload["detail"]
    assert "slack-token" not in response.text


async def test_slack_status_stays_connected_when_verification_upstream_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_BEARER_ALLOW_UNSIGNED", "true")
    # A transient Slack outage must not flap the connector from connected to broken.
    _install_mock_client(monkeypatch, _auth_test_only_handler(status_code=503, error="service_unavailable"))
    app = make_app(slack_connector_config(tmp_path))

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/v1/connectors/slack/status", headers=auth_header())

    assert response.status_code == 200
    payload = response.json()
    assert payload["connected"] is True
    assert payload["state"] == "connected"
    assert "channels:history" in payload["scopes"]


async def test_slack_status_reports_not_connected_without_token_source(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_BEARER_ALLOW_UNSIGNED", "true")
    app = make_app(slack_connector_config(tmp_path, static_access_token="", broker_token_url=""))

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/v1/connectors/slack/status", headers=auth_header())

    assert response.status_code == 200
    assert response.json()["connected"] is False
    assert response.json()["state"] == "not_connected"
    assert response.json()["detail"] == "Slack connector token broker is not configured"


async def test_slack_status_reports_reconnect_for_rejected_broker_token(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_BEARER_ALLOW_UNSIGNED", "true")

    def handler(request: httpx.Request) -> Response:
        assert request.url == "https://keycloak.test/realms/nebari/broker/slack/token"
        return Response(401)

    original_async_client = httpx.AsyncClient

    def mock_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)
    app = make_app(
        slack_connector_config(
            tmp_path,
            static_access_token="",
            broker_token_url="https://keycloak.test/realms/nebari/broker/slack/token",
        )
    )

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/v1/connectors/slack/status", headers=auth_header())

    assert response.status_code == 200
    assert response.json()["connected"] is False
    assert response.json()["state"] == "reconnect_required"
    assert response.json()["detail"] == "Slack account must be reconnected"


async def test_slack_status_reports_broker_permission_error(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_BEARER_ALLOW_UNSIGNED", "true")

    def handler(request: httpx.Request) -> Response:
        assert request.url == "https://keycloak.test/realms/nebari/broker/slack/token"
        return Response(403)

    original_async_client = httpx.AsyncClient

    def mock_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)
    app = make_app(
        slack_connector_config(
            tmp_path,
            static_access_token="",
            broker_token_url="https://keycloak.test/realms/nebari/broker/slack/token",
        )
    )

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/v1/connectors/slack/status", headers=auth_header())

    assert response.status_code == 200
    assert response.json()["connected"] is False
    assert response.json()["state"] == "unavailable"
    assert (
        response.json()["detail"]
        == "Keycloak denied broker token access. Grant the broker read-token role to normal Hub users."
    )


async def test_list_connectors_lists_only_what_the_deployment_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_BEARER_ALLOW_UNSIGNED", "true")
    _install_mock_client(monkeypatch, _auth_test_only_handler())
    app = make_app(slack_connector_config(tmp_path))

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/v1/connectors", headers=auth_header())

    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == ["slack"]


def _slack_api_handler(request: httpx.Request, seen_authorization: list[str]) -> Response:
    seen_authorization.append(request.headers.get("authorization", ""))
    path = request.url.path
    if path.endswith("/conversations.list"):
        types = request.url.params.get("types", "")
        if "im" in types.split(","):
            return Response(
                200,
                json={
                    "ok": True,
                    "channels": [
                        {"id": "D0001", "is_im": True, "user": "U0002"},
                        {"id": "G0002", "name": "mpdm-alice--bob", "is_mpim": True},
                    ],
                    "response_metadata": {"next_cursor": ""},
                },
            )
        if types == "private_channel":
            return Response(
                200,
                json={
                    "ok": True,
                    "channels": [
                        {
                            "id": "C0003",
                            "name": "private-proposals",
                            "is_private": True,
                        }
                    ],
                    "response_metadata": {"next_cursor": ""},
                },
            )
        return Response(
            200,
            json={
                "ok": True,
                "channels": [
                    {
                        "id": "C0001",
                        "name": "proposals",
                        "is_private": False,
                        "topic": {"value": "Proposal work"},
                        "num_members": 12,
                    }
                ],
                "response_metadata": {"next_cursor": ""},
            },
        )
    if path.endswith("/search.messages"):
        assert request.url.params["query"] == "healthcare kickoff"
        assert request.url.params["page"] == "1"
        return Response(
            200,
            json={
                "ok": True,
                "messages": {
                    "matches": [
                        {
                            "ts": "1783041866.494000",
                            "user": "U0002",
                            "username": "bob",
                            "text": "Healthcare kickoff notes are in the DM",
                            "permalink": "https://slack.test/archives/D0001/p1783041866494000",
                            "channel": {"id": "D0001", "name": "", "is_im": True},
                        },
                        {
                            "ts": "1783041800.000000",
                            "user": "U0003",
                            "text": "Healthcare kickoff notes are in the channel",
                            "permalink": "https://slack.test/archives/C0001/p1783041800000000",
                            "channel": {"id": "C0001", "name": "proposals", "is_channel": True},
                        },
                        {
                            "ts": "1783041700.000000",
                            "user": "U0004",
                            "text": "Healthcare kickoff notes are in the group DM",
                            "permalink": "https://slack.test/archives/G0002/p1783041700000000",
                            "channel": {"id": "G0002", "name": "mpdm-alice--bob", "is_mpim": True},
                        },
                    ],
                    "paging": {"page": 1, "pages": 2},
                },
            },
        )
    if path.endswith("/conversations.info"):
        channel_id = request.url.params["channel"]
        if channel_id == "D0001":
            return Response(200, json={"ok": False, "error": "missing_scope"})
        return Response(200, json={"ok": True, "channel": {"id": channel_id, "is_channel": True}})
    if path.endswith("/conversations.history"):
        raise AssertionError("DM history must not be requested")
    if path.endswith("/conversations.replies"):
        assert request.url.params["channel"] == "C0001"
        assert request.url.params["ts"] == "1783041866.494000"
        return Response(
            200,
            json={
                "ok": True,
                "messages": [
                    {"ts": "1783041866.494000", "user": "U0001", "text": "root", "reply_count": 1},
                    {"ts": "1783041900.111000", "user": "U0002", "text": "reply", "thread_ts": "1783041866.494000"},
                ],
                "has_more": False,
            },
        )
    return Response(404)


async def test_slack_endpoints_cover_channels_dms_search_and_reads(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_BEARER_ALLOW_UNSIGNED", "true")
    seen_authorization: list[str] = []

    def handler(request: httpx.Request) -> Response:
        return _slack_api_handler(request, seen_authorization)

    original_async_client = httpx.AsyncClient

    def mock_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)
    app = make_app(slack_connector_config(tmp_path))

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            channels = await client.get("/v1/connectors/slack/channels", headers=auth_header())
            dms = await client.get("/v1/connectors/slack/dms", headers=auth_header())
            search = await client.post(
                "/v1/connectors/slack/search",
                headers=auth_header(),
                json={"query": "healthcare kickoff", "limit": 5},
            )
            read = await client.post(
                "/v1/connectors/slack/channels/D0001/read",
                headers=auth_header(),
                json={"limit": 10},
            )
            thread = await client.post(
                "/v1/connectors/slack/channels/C0001/threads/1783041866.494000/read",
                headers=auth_header(),
                json={"limit": 10},
            )

    assert channels.status_code == 200
    assert channels.json()["channels"][0]["id"] == "C0001"
    assert channels.json()["channels"][0]["name"] == "proposals"

    assert dms.status_code == 200
    dm_payload = dms.json()["dms"]
    assert [dm["id"] for dm in dm_payload] == ["D0001", "G0002"]
    assert dm_payload[0]["is_im"] is True
    assert dm_payload[0]["name"] == "dm-U0002"
    assert dm_payload[1]["is_mpim"] is True

    assert search.status_code == 200
    assert [hit["channel_id"] for hit in search.json()["hits"]] == ["C0001"]
    assert all(not hit["is_im"] and not hit["is_mpim"] for hit in search.json()["hits"])
    assert search.json()["next_page"] == 2

    assert read.status_code == 403

    assert thread.status_code == 200
    assert len(thread.json()["messages"]) == 2
    assert thread.json()["next_cursor"] == ""

    for response in (channels, dms, search, read, thread):
        assert "slack-token-alice" not in response.text
    assert seen_authorization
    assert all(value == "Bearer slack-token-alice" for value in seen_authorization)


async def test_slack_read_rejects_invalid_channel_id(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_BEARER_ALLOW_UNSIGNED", "true")
    app = make_app(slack_connector_config(tmp_path))

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/connectors/slack/channels/not-a-channel/read",
                headers=auth_header(),
                json={"limit": 10},
            )

    assert response.status_code == 422


async def test_slack_read_accepts_private_channel_absent_from_channel_list(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_BEARER_ALLOW_UNSIGNED", "true")

    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/conversations.list"):
            raise AssertionError("channel reads must not depend on conversations.list")
        if request.url.path.endswith("/conversations.info"):
            assert request.url.params["channel"] == "C0123456789"
            return Response(
                200,
                json={
                    "ok": True,
                    "channel": {
                        "id": "C0123456789",
                        "is_channel": True,
                        "is_private": True,
                        "is_member": True,
                    },
                },
            )
        if request.url.path.endswith("/conversations.history"):
            assert request.url.params["channel"] == "C0123456789"
            return Response(
                200,
                json={"ok": True, "messages": [], "has_more": False},
            )
        return Response(404)

    _install_mock_client(monkeypatch, handler)
    app = make_app(slack_connector_config(tmp_path))

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/connectors/slack/channels/C0123456789/read",
                headers=auth_header(),
                json={"limit": 10},
            )

    assert response.status_code == 200
    assert response.json()["channel_id"] == "C0123456789"
    assert response.json()["messages"] == []


async def test_slack_read_missing_info_scope_uses_split_channel_lists(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_BEARER_ALLOW_UNSIGNED", "true")
    requested_types: list[str] = []

    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/conversations.info"):
            return Response(200, json={"ok": False, "error": "missing_scope"})
        if request.url.path.endswith("/conversations.list"):
            channel_type = request.url.params["types"]
            requested_types.append(channel_type)
            assert channel_type in {"public_channel", "private_channel"}
            channels = []
            if channel_type == "private_channel":
                channels = [
                    {
                        "id": "C0123456789",
                        "name": "private-release",
                        "is_private": True,
                    }
                ]
            return Response(
                200,
                json={
                    "ok": True,
                    "channels": channels,
                    "response_metadata": {"next_cursor": ""},
                },
            )
        if request.url.path.endswith("/conversations.history"):
            return Response(
                200,
                json={"ok": True, "messages": [], "has_more": False},
            )
        return Response(404)

    _install_mock_client(monkeypatch, handler)
    app = make_app(slack_connector_config(tmp_path))

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/connectors/slack/channels/C0123456789/read",
                headers=auth_header(),
                json={"limit": 10},
            )

    assert response.status_code == 200
    assert requested_types == ["public_channel", "private_channel"]


async def test_slack_read_reports_upstream_error_detail(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_BEARER_ALLOW_UNSIGNED", "true")

    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/conversations.info"):
            return Response(200, json={"ok": True, "channel": {"id": "C0404", "is_channel": True}})
        if request.url.path.endswith("/conversations.history"):
            return Response(200, json={"ok": False, "error": "channel_not_found"})
        return Response(404)

    original_async_client = httpx.AsyncClient

    def mock_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)
    app = make_app(slack_connector_config(tmp_path))

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/connectors/slack/channels/C0404/read",
                headers=auth_header(),
                json={"limit": 10},
            )

    assert response.status_code == 502
    assert response.json()["detail"] == "Slack conversation read failed: channel_not_found"
    assert "slack-token-alice" not in response.text


async def test_slack_search_endpoint_rejected_when_not_connected(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_BEARER_ALLOW_UNSIGNED", "true")
    app = make_app(slack_connector_config(tmp_path, static_access_token="", broker_token_url=""))

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/connectors/slack/search",
                headers=auth_header(),
                json={"query": "anything", "limit": 5},
            )

    assert response.status_code == 409


async def test_slack_client_raises_for_http_error_status(monkeypatch):
    def handler(request: httpx.Request) -> Response:
        return Response(429, json={"ok": False, "error": "ratelimited"})

    original_async_client = httpx.AsyncClient

    def mock_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)
    slack = SlackClient(access_token="token", api_base_url="https://slack.test/api")

    try:
        await slack.search(query="anything", limit=5)
    except SlackUpstreamError as exc:
        assert exc.status_code == 429
        assert "ratelimited" in str(exc)
    else:
        raise AssertionError("expected rate limit error to raise")


async def test_slack_client_paginates_channel_listing(monkeypatch):
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> Response:
        channel_type = request.url.params["types"]
        cursor = request.url.params.get("cursor", "")
        requests.append((channel_type, cursor))
        if channel_type == "public_channel" and not cursor:
            return Response(
                200,
                json={
                    "ok": True,
                    "channels": [{"id": "C0001", "name": "one"}],
                    "response_metadata": {"next_cursor": "page-2"},
                },
            )
        if channel_type == "public_channel":
            return Response(
                200,
                json={
                    "ok": True,
                    "channels": [{"id": "C0002", "name": "two"}],
                    "response_metadata": {"next_cursor": ""},
                },
            )
        return Response(
            200,
            json={
                "ok": True,
                "channels": [{"id": "C0003", "name": "three", "is_private": True}],
                "response_metadata": {"next_cursor": ""},
            },
        )

    original_async_client = httpx.AsyncClient

    def mock_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)
    slack = SlackClient(access_token="token", api_base_url="https://slack.test/api")

    public, next_cursor = await slack.list_channels(limit=10)

    assert [channel.id for channel in public] == ["C0001", "C0002"]
    assert next_cursor == "private:"
    assert requests == [
        ("public_channel", ""),
        ("public_channel", "page-2"),
    ]

    private, next_cursor = await slack.list_channels(limit=10, cursor=next_cursor)

    assert [channel.id for channel in private] == ["C0003"]
    assert next_cursor == ""
    assert requests == [
        ("public_channel", ""),
        ("public_channel", "page-2"),
        ("private_channel", ""),
    ]


async def test_slack_channel_listing_accepts_legacy_raw_cursor(monkeypatch):
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> Response:
        requests.append(
            (request.url.params["types"], request.url.params.get("cursor", ""))
        )
        return Response(
            200,
            json={
                "ok": True,
                "channels": [],
                "response_metadata": {"next_cursor": ""},
            },
        )

    _install_mock_client(monkeypatch, handler)
    slack = SlackClient(access_token="token", api_base_url="https://slack.test/api")

    channels, next_cursor = await slack.list_channels(
        limit=10, cursor="legacy-slack-cursor"
    )

    assert channels == []
    assert next_cursor == "private:"
    assert requests == [("public_channel", "legacy-slack-cursor")]


async def test_slack_missing_scope_channel_fallback_stops_on_cursor_cycle(
    monkeypatch,
):
    list_calls = 0

    def handler(request: httpx.Request) -> Response:
        nonlocal list_calls
        if request.url.path.endswith("/conversations.info"):
            return Response(200, json={"ok": False, "error": "missing_scope"})
        if request.url.path.endswith("/conversations.list"):
            list_calls += 1
            return Response(
                200,
                json={
                    "ok": True,
                    "channels": [],
                    "response_metadata": {"next_cursor": "repeated"},
                },
            )
        raise AssertionError(f"unexpected Slack request: {request.url.path}")

    _install_mock_client(monkeypatch, handler)
    slack = SlackClient(access_token="token", api_base_url="https://slack.test/api")

    with pytest.raises(SlackConversationNotAllowed):
        await slack.read_conversation(channel_id="C0001", limit=10)

    assert list_calls == 2


async def test_slack_missing_scope_channel_fallback_has_page_budget(monkeypatch):
    list_calls = 0

    def handler(request: httpx.Request) -> Response:
        nonlocal list_calls
        if request.url.path.endswith("/conversations.info"):
            return Response(200, json={"ok": False, "error": "missing_scope"})
        if request.url.path.endswith("/conversations.list"):
            list_calls += 1
            return Response(
                200,
                json={
                    "ok": True,
                    "channels": [],
                    "response_metadata": {"next_cursor": f"page-{list_calls}"},
                },
            )
        raise AssertionError(f"unexpected Slack request: {request.url.path}")

    _install_mock_client(monkeypatch, handler)
    slack = SlackClient(access_token="token", api_base_url="https://slack.test/api")

    with pytest.raises(SlackConversationNotAllowed):
        await slack.read_conversation(channel_id="C0001", limit=10)

    assert list_calls == MAX_CHANNEL_AUTHORIZATION_PAGES


async def test_slack_read_forwards_cursor_and_returns_next_cursor(monkeypatch):
    seen_cursors: list[str] = []

    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/conversations.info"):
            return Response(200, json={"ok": True, "channel": {"id": "C0001", "is_channel": True}})
        seen_cursors.append(request.url.params.get("cursor", ""))
        return Response(
            200,
            json={
                "ok": True,
                "messages": [{"ts": "1783041900.111000", "user": "U0002", "text": "page two"}],
                "has_more": True,
                "response_metadata": {"next_cursor": "page-3"},
            },
        )

    _install_mock_client(monkeypatch, handler)
    slack = SlackClient(access_token="token", api_base_url="https://slack.test/api")

    messages, has_more, next_cursor = await slack.read_conversation(channel_id="C0001", limit=10, cursor="page-2")

    assert [message.text for message in messages] == ["page two"]
    assert has_more is True
    assert next_cursor == "page-3"
    assert seen_cursors == ["page-2"]


def test_slack_read_request_derives_timestamps_from_friendly_fields():
    # since_date/until_date convert to Slack's epoch.micros oldest/latest.
    req = SlackReadRequest(since_date=date(2024, 6, 1), until_date=date(2024, 6, 30))
    assert req.oldest == "1717200000.000000"  # 2024-06-01 00:00:00 UTC
    assert req.latest.startswith("1719791999.")  # 2024-06-30 23:59:59 UTC

    # days_back derives an oldest near now - N days and leaves latest unset.
    req = SlackReadRequest(days_back=7)
    expected = datetime.now(UTC) - timedelta(days=7)
    assert abs(float(req.oldest) - expected.timestamp()) < 5
    assert req.latest == ""

    # An explicit oldest/latest always wins over the friendly fields.
    req = SlackReadRequest(oldest="1700000000.000000", since_date=date(2024, 6, 1))
    assert req.oldest == "1700000000.000000"


def test_slack_read_request_rejects_conflicting_time_fields():
    with pytest.raises(ValidationError):
        SlackReadRequest(days_back=7, since_date=date(2024, 6, 1))
    with pytest.raises(ValidationError):
        SlackReadRequest(since_date=date(2024, 6, 30), until_date=date(2024, 6, 1))


async def test_slack_read_forwards_friendly_time_fields_as_slack_params(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_BEARER_ALLOW_UNSIGNED", "true")

    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/conversations.info"):
            return Response(200, json={"ok": True, "channel": {"id": "C0001", "is_channel": True}})
        if request.url.path.endswith("/conversations.history"):
            seen["oldest"] = request.url.params.get("oldest", "")
            seen["latest"] = request.url.params.get("latest", "")
            return Response(200, json={"ok": True, "messages": [], "has_more": False})
        return Response(404)

    _install_mock_client(monkeypatch, handler)
    app = make_app(slack_connector_config(tmp_path))

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            read = await client.post(
                "/v1/connectors/slack/channels/C0001/read",
                headers=auth_header(),
                json={"since_date": "2024-06-01", "until_date": "2024-06-30"},
            )

    assert read.status_code == 200
    assert seen["oldest"] == "1717200000.000000"
    assert seen["latest"].startswith("1719791999.")


async def test_slack_read_sanitizes_link_shaped_message_text(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_BEARER_ALLOW_UNSIGNED", "true")

    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/conversations.info"):
            return Response(200, json={"ok": True, "channel": {"id": "C0001", "is_channel": True}})
        if request.url.path.endswith("/conversations.history"):
            return Response(
                200,
                json={
                    "ok": True,
                    "messages": [
                        {
                            "ts": "1783041900.111000",
                            "user": "U0002",
                            "text": "docs <https://intranet.test/x|here> and https://example.test/y",
                        }
                    ],
                    "has_more": False,
                },
            )
        return Response(404)

    _install_mock_client(monkeypatch, handler)
    app = make_app(slack_connector_config(tmp_path))

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            read = await client.post(
                "/v1/connectors/slack/channels/C0001/read",
                headers=auth_header(),
                json={"limit": 10},
            )

    assert read.status_code == 200
    assert read.json()["messages"][0]["text"] == "docs here and [link]"
    assert "http://" not in read.text
    assert "https://" not in read.text


def test_sanitize_slack_text_neutralizes_links_and_preserves_mentions():
    assert sanitize_slack_text("see <https://x.test/a|the doc>") == "see the doc"
    assert sanitize_slack_text("bare <https://x.test/a>") == "bare [link]"
    assert sanitize_slack_text("go to https://x.test/a, now") == "go to [link], now"
    assert sanitize_slack_text("visit www.x.test") == "visit [link]"
    # Slack auto-links a pasted URL as <url|url>; the label must not leak the URL.
    assert sanitize_slack_text("auto <https://x.test/a|https://x.test/a>") == "auto [link]"
    assert sanitize_slack_text("hi <@U1> in <#C1|general>") == "hi @U1 in #general"
    assert sanitize_slack_text("heads up <!here> and <!subteam^S1|frontend>") == "heads up @here and @frontend"
    assert sanitize_slack_text("email <mailto:bob@x.test|Bob>") == "email Bob"
    assert sanitize_slack_text("plain text, no links") == "plain text, no links"
    assert sanitize_slack_text("") == ""


async def test_google_drive_client_limits_download_before_decoding(monkeypatch):
    def handler(request: httpx.Request) -> Response:
        assert request.url.path.endswith("/files/file-1")
        assert request.url.params.get("alt") == "media"
        return Response(200, content=b"a" * 1000)

    original_async_client = httpx.AsyncClient

    def mock_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)
    drive = GoogleDriveClient(access_token="token", api_base_url="https://drive.test/drive/v3")

    file = DriveFileMetadata(id="file-1", name="notes.txt", mime_type="text/plain")
    text, truncated = await drive.read_text(file, 12)

    assert text == "a" * 12
    assert truncated is True
