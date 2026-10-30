from __future__ import annotations

import base64
import json

from httpx import ASGITransport, AsyncClient

from collab_hub_api.config import Config
from collab_hub_api.core import make_app


def _jwt(payload: dict) -> str:
    def encode(part: dict) -> str:
        raw = json.dumps(part, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode(payload)}."


def auth_cookie(
    user: str,
    org: str = "org-a",
    workspace: str = "workspace-a",
    email: str | None = None,
) -> dict[str, str]:
    claims = {
        "preferred_username": user,
        "org_id": org,
        "workspace_id": workspace,
    }
    if email is not None:
        claims["email"] = email
    return {"IdToken-test": _jwt(claims)}


async def create_frame(client, user: str = "alice", name: str = "Team Frame"):
    response = await client.post(
        "/v1/frames",
        cookies=auth_cookie(user),
        json={"name": name, "tags": ["team"], "body": "# Body"},
    )
    assert response.status_code == 201
    return response.json()


async def record_chat_created(client, user: str, detail: dict | None = None):
    response = await client.post(
        "/v1/usage/events",
        cookies=auth_cookie(user),
        json={"event": "chat_created", **({"detail": detail} if detail is not None else {})},
    )
    assert response.status_code == 204


async def test_usage_endpoints_require_auth(client):
    for method, path in (
        ("POST", "/v1/usage/events"),
        ("GET", "/v1/usage/summary"),
        ("GET", "/v1/usage/users"),
    ):
        response = await client.request(method, path, json={"event": "chat_created"})
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"


async def test_authenticated_request_records_user_with_email(client):
    response = await client.get(
        "/v1/frames",
        cookies=auth_cookie("alice", email="alice@example.com"),
    )
    assert response.status_code == 200

    response = await client.get("/v1/usage/users", cookies=auth_cookie("bob"))
    assert response.status_code == 200
    users = {entry["user"]: entry for entry in response.json()["users"]}
    # Both callers appear: alice from the frames call, bob from this read.
    assert users["alice"]["email"] == "alice@example.com"
    assert users["alice"]["first_seen"] <= users["alice"]["last_seen"]
    assert users["bob"]["email"] is None


async def test_users_are_scoped_to_workspace(client):
    await client.get("/v1/frames", cookies=auth_cookie("alice"))
    await client.get("/v1/frames", cookies=auth_cookie("carol", org="org-b", workspace="workspace-b"))

    response = await client.get("/v1/usage/users", cookies=auth_cookie("alice"))
    users = [entry["user"] for entry in response.json()["users"]]
    assert "alice" in users
    assert "carol" not in users


async def test_chat_created_events_are_counted_per_user(client):
    await record_chat_created(client, "alice", {"agent_id": "hub-agent"})
    await record_chat_created(client, "alice")
    await record_chat_created(client, "bob")

    response = await client.get("/v1/usage/summary", cookies=auth_cookie("alice"))
    assert response.status_code == 200
    chats = response.json()["chats"]
    assert chats["created"] == 3
    by_user = {entry["user"]: entry["count"] for entry in chats["created_by_user"]}
    assert by_user == {"alice": 2, "bob": 1}


async def test_unknown_event_is_rejected(client):
    response = await client.post(
        "/v1/usage/events",
        cookies=auth_cookie("alice"),
        json={"event": "keyboard_smashed"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_summary_counts_frame_mutations(client):
    frame = await create_frame(client, user="alice")
    response = await client.put(
        f"/v1/frames/{frame['id']}",
        cookies=auth_cookie("alice"),
        json={"name": "Renamed", "tags": ["team"], "body": "# Body v2"},
    )
    assert response.status_code == 200
    await create_frame(client, user="bob", name="Bob Frame")

    response = await client.get("/v1/usage/summary", cookies=auth_cookie("alice"))
    frames = response.json()["frames"]
    assert frames["created"] == 2
    assert frames["updated"] == 1
    created_by_user = {entry["user"]: entry["count"] for entry in frames["created_by_user"]}
    assert created_by_user == {"alice": 1, "bob": 1}
    assert frames["updated_by_user"] == [{"user": "alice", "count": 1}]


async def test_summary_counts_active_frames(client):
    frame = await create_frame(client, user="alice")
    response = await client.put(
        "/v1/active-frames",
        cookies=auth_cookie("alice"),
        json={"frame_ids": [frame["id"]]},
    )
    assert response.status_code == 200

    response = await client.get("/v1/usage/summary", cookies=auth_cookie("alice"))
    assert response.json()["active_frames"] == {"frames": 1, "users": 1}


async def test_summary_window_bounds_event_counts(client):
    await record_chat_created(client, "alice")
    await create_frame(client, user="alice")

    response = await client.get(
        "/v1/usage/summary",
        params={"since": "2020-01-01T00:00:00Z"},
        cookies=auth_cookie("alice"),
    )
    body = response.json()
    assert body["chats"]["created"] == 1
    assert body["frames"]["created"] == 1
    assert body["users"]["active"] == body["users"]["total"]

    response = await client.get(
        "/v1/usage/summary",
        params={"until": "2020-01-01T00:00:00Z"},
        cookies=auth_cookie("alice"),
    )
    body = response.json()
    assert body["chats"]["created"] == 0
    assert body["frames"]["created"] == 0
    assert body["users"]["active"] == 0
    # The roster and active-Frames snapshot are current-state, not windowed.
    assert body["users"]["total"] > 0


async def test_usage_unavailable_without_database(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_IDTOKEN_ALLOW_UNSIGNED", "true")
    config = Config.parse(
        {
            "storage": {"frames_path": str(tmp_path / "frames")},
            "frames": {
                "active_state": {"backend": "memory"},
                "history": {"backend": "memory"},
                # No usage backend and no shared postgres URL -> unavailable.
                "mcp_session_manager_enabled": False,
            },
        }
    )
    app = make_app(config)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Ordinary authenticated calls still work: seen-user recording is
            # a best-effort no-op without a database.
            response = await client.get("/v1/frames", cookies=auth_cookie("alice"))
            assert response.status_code == 200

            response = await client.post(
                "/v1/usage/events",
                cookies=auth_cookie("alice"),
                json={"event": "chat_created"},
            )
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "usage_unavailable"

            response = await client.get("/v1/usage/summary", cookies=auth_cookie("alice"))
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "usage_unavailable"


async def test_summary_user_filter_narrows_per_user_figures(client):
    await record_chat_created(client, "alice")
    await record_chat_created(client, "bob")
    frame = await create_frame(client, user="alice")
    response = await client.put(
        "/v1/active-frames",
        cookies=auth_cookie("alice"),
        json={"frame_ids": [frame["id"]]},
    )
    assert response.status_code == 200

    response = await client.get(
        "/v1/usage/summary",
        params={"user": "alice"},
        cookies=auth_cookie("bob"),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["users"]["total"] == 1
    assert body["chats"]["created"] == 1
    assert body["chats"]["created_by_user"] == [{"user": "alice", "count": 1}]
    assert body["frames"]["created_by_user"] == [{"user": "alice", "count": 1}]
    # Filtered active-frames reflect only the named users' own active sets.
    assert body["active_frames"] == {"frames": 1, "users": 1}

    # The repeatable form accepts several users at once.
    response = await client.get(
        "/v1/usage/summary",
        params=[("user", "alice"), ("user", "bob")],
        cookies=auth_cookie("alice"),
    )
    assert response.json()["chats"]["created"] == 2


async def test_me_returns_only_the_callers_usage(client):
    await record_chat_created(client, "alice")
    await record_chat_created(client, "bob")

    response = await client.get("/v1/usage/me", cookies=auth_cookie("alice"))
    assert response.status_code == 200
    body = response.json()
    assert body["chats"]["created"] == 1
    assert body["chats"]["created_by_user"] == [{"user": "alice", "count": 1}]
    assert body["users"]["total"] == 1
    assert body["active_frames"] == {"frames": 0, "users": 0}


async def test_users_endpoint_supports_user_filter(client):
    await client.get("/v1/frames", cookies=auth_cookie("alice"))
    await client.get("/v1/frames", cookies=auth_cookie("bob"))

    response = await client.get(
        "/v1/usage/users",
        params={"user": "bob"},
        cookies=auth_cookie("alice"),
    )
    users = [entry["user"] for entry in response.json()["users"]]
    assert users == ["bob"]


async def test_usage_dashboard_renders_workspace_usage(client):
    await record_chat_created(client, "alice")
    await create_frame(client, user="alice")

    response = await client.get(
        "/usage",
        cookies=auth_cookie("bob", email="bob@example.com"),
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    page = response.text
    assert "Hub Usage" in page
    assert "alice" in page
    assert "bob@example.com" in page

    response = await client.get("/usage", cookies=auth_cookie("bob"), params={"window": "7d"})
    assert response.status_code == 200


async def test_usage_dashboard_requires_auth(client):
    response = await client.get("/usage")
    assert response.status_code == 401


async def test_root_home_links_to_usage_dashboard(client):
    # Unconfigured, the landing page is served as it always was — gateway
    # installs keep relying on enforceAtGateway. A hardened deployment opts
    # into the protection map; see test_path_protection.
    response = await client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert 'href="./usage"' in response.text
    assert 'href="./docs"' in response.text


async def test_a_summary_window_without_a_timezone_is_refused(client):
    response = await client.get("/v1/usage/summary?since=2026-09-01T00:00:00", cookies=auth_cookie("alice"))

    assert response.status_code == 422
