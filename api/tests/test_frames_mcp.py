from __future__ import annotations

import base64
import json
import logging
import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from collab_hub_api.config import Config
from collab_hub_api.core import make_app
from collab_hub_api.frames.active_state import InMemoryActiveFrameStore
from collab_hub_api.frames.auth import AuthContext, current_auth_context
from collab_hub_api.frames.mcp_server import create_mcp_server
from collab_hub_api.frames.models import Visibility
from collab_hub_api.frames.store import LocalFsFrameStore

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from smoke_frames_mcp import assert_mcp_contract  # noqa: E402


def parse_tool_result(result):
    return json.loads(result[0].text)


def free_tcp_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def auth_cookie(user: str = "smoke-user") -> str:
    def encode(part: dict) -> str:
        raw = json.dumps(part, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    token = (
        f"{encode({'alg': 'none'})}."
        f"{encode({'preferred_username': user, 'org_id': 'smoke-org', 'workspace_id': 'smoke-workspace'})}."
    )
    return f"IdToken-smoke={token}"


def start_test_server(app, port: int):
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_config=None,
            lifespan="on",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    with httpx.Client(base_url=base_url, timeout=1) as client:
        for _ in range(100):
            try:
                if client.get("/health").status_code == 200:
                    return server, thread, base_url
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
    server.should_exit = True
    thread.join(timeout=5)
    raise RuntimeError("test server did not become healthy")


@pytest.mark.anyio
async def test_mcp_tools_preserve_auth_scope_active_fallback_and_resource(tmp_path):
    store = LocalFsFrameStore(tmp_path)
    active_store = InMemoryActiveFrameStore()
    auth = AuthContext(user="alice", home_org_id="org-a", workspace_id="workspace-a")
    first = store.create_frame(
        org_id=auth.org_id,
        workspace_id=auth.workspace_id,
        created_by="alice",
        owners=["alice"],
        name="Brand Voice",
        description="",
        visibility=Visibility.private,
        tags=["brand", "sales"],
        body="# Brand\nUse short words.",
    )
    # Owned by bob but published + internal, so alice can read it via can_read.
    second = store.create_frame(
        org_id=auth.org_id,
        workspace_id=auth.workspace_id,
        created_by="bob",
        owners=["bob"],
        name="Legal Review",
        description="",
        visibility=Visibility.internal,
        tags=["legal"],
        body="# Legal\nDo not promise terms.",
    )
    store.set_published(second.id, True)
    # Owned by alice but unpublished + private: alice still reads it (owner),
    # but it must never surface to non-owners.
    hidden = store.create_frame(
        org_id=auth.org_id,
        workspace_id=auth.workspace_id,
        created_by="carol",
        owners=["carol"],
        name="Carol Private",
        description="",
        visibility=Visibility.private,
        tags=["brand"],
        body="# Carol\nInvisible to alice.",
    )
    # Owned by a stranger in another workspace: alice is not an owner and the
    # frame is private, so can_read denies her even cross-tenant.
    other_workspace = store.create_frame(
        org_id=auth.org_id,
        workspace_id="workspace-b",
        created_by="dave",
        owners=["dave"],
        name="Other Workspace",
        description="",
        visibility=Visibility.private,
        tags=["brand"],
        body="# Other\nInvisible.",
    )
    # Public + published in a DIFFERENT org/workspace: alice can read it by id
    # (cross-tenant public), but it must not surface in her scoped list.
    public_other_tenant = store.create_frame(
        org_id="org-b",
        workspace_id="workspace-z",
        created_by="erin",
        owners=["erin"],
        name="Public Elsewhere",
        description="",
        visibility=Visibility.public,
        tags=["brand"],
        body="# Public\nReadable anywhere.",
    )
    store.set_published(public_other_tenant.id, True)
    active_store.set_active_frame_ids(auth.org_id, auth.workspace_id, auth.user, [second.id])
    mcp = create_mcp_server(store, active_store=active_store)

    token = current_auth_context.set(auth)
    try:
        all_frames = parse_tool_result(await mcp.call_tool("list_frames", {}))
        assert {item["id"] for item in all_frames["frames"]} == {first.id, second.id}
        assert "body" not in all_frames["frames"][0]
        assert all_frames["frames"][0]["token_estimate"] >= 1

        brand_frames = parse_tool_result(await mcp.call_tool("list_frames", {"tags": ["brand"]}))
        assert [item["id"] for item in brand_frames["frames"]] == [first.id]

        alice_frames = parse_tool_result(await mcp.call_tool("list_frames", {"owner": "alice"}))
        assert [item["id"] for item in alice_frames["frames"]] == [first.id]

        named_frames = parse_tool_result(await mcp.call_tool("list_frames", {"name": "voice"}))
        assert [item["id"] for item in named_frames["frames"]] == [first.id]

        body = parse_tool_result(await mcp.call_tool("get_frame", {"id": first.id}))
        assert body == {"id": first.id, "body": "# Brand\nUse short words."}

        active = parse_tool_result(await mcp.call_tool("get_active_frames", {"ids": [first.id, second.id]}))
        assert [item["body"] for item in active["frames"]] == [
            "# Brand\nUse short words.",
            "# Legal\nDo not promise terms.",
        ]

        stored_active = parse_tool_result(await mcp.call_tool("get_active_frames", {}))
        assert [item["id"] for item in stored_active["frames"]] == [second.id]

        # An empty `ids` list must behave like an omitted arg (return the active
        # set), not return zero Frames. Models commonly call the tool with
        # `ids: []` instead of omitting it.
        empty_ids_active = parse_tool_result(await mcp.call_tool("get_active_frames", {"ids": []}))
        assert [item["id"] for item in empty_ids_active["frames"]] == [second.id]

        templates = await mcp.list_resource_templates()
        assert {template.uriTemplate for template in templates} == {"frame://{frame_id}"}

        resource = await mcp.read_resource(f"frame://{first.id}")
        assert resource[0].content == "# Brand\nUse short words."

        with pytest.raises(Exception):
            await mcp.call_tool("get_frame", {"id": other_workspace.id})

        # can_read is enforced: a private, unpublished frame alice does not own
        # is invisible to both get_frame and list_frames.
        with pytest.raises(Exception):
            await mcp.call_tool("get_frame", {"id": hidden.id})
        assert hidden.id not in {item["id"] for item in all_frames["frames"]}

        # Cross-tenant `public` read: get_frame reaches a published public frame
        # in another org/workspace by id...
        public_body = parse_tool_result(await mcp.call_tool("get_frame", {"id": public_other_tenant.id}))
        assert public_body == {"id": public_other_tenant.id, "body": "# Public\nReadable anywhere."}
        # ...and the resource too...
        public_resource = await mcp.read_resource(f"frame://{public_other_tenant.id}")
        assert public_resource[0].content == "# Public\nReadable anywhere."
        # ...but list_frames stays tenant-scoped and never discovers it.
        assert public_other_tenant.id not in {item["id"] for item in all_frames["frames"]}
        brand_again = parse_tool_result(await mcp.call_tool("list_frames", {"tags": ["brand"]}))
        assert public_other_tenant.id not in {item["id"] for item in brand_again["frames"]}
    finally:
        current_auth_context.reset(token)

    # The store itself does not apply can_read (that is the MCP/router layer):
    # all three same-workspace frames remain persisted.
    assert len(store.list_frames(auth.org_id, auth.workspace_id)) == 3
    assert store.get_frame(first.id).body == "# Brand\nUse short words."


@pytest.mark.anyio
async def test_get_active_frames_skips_a_corrupt_frame(tmp_path, caplog):
    """One corrupt active Frame must not break the whole get_active_frames call.

    The active set is walked frame by frame; an undecodable sidecar is skipped
    (and logged) so the caller still receives every readable active Frame, the
    same tolerance already applied to a stale id the caller can no longer read.
    """

    store = LocalFsFrameStore(tmp_path)
    active_store = InMemoryActiveFrameStore()
    auth = AuthContext(user="alice", home_org_id="org-a", workspace_id="workspace-a")
    good = store.create_frame(
        org_id=auth.org_id,
        workspace_id=auth.workspace_id,
        created_by="alice",
        owners=["alice"],
        name="Good",
        description="",
        visibility=Visibility.private,
        tags=["brand"],
        body="# Good",
    )
    bad = store.create_frame(
        org_id=auth.org_id,
        workspace_id=auth.workspace_id,
        created_by="alice",
        owners=["alice"],
        name="Bad",
        description="",
        visibility=Visibility.private,
        tags=["brand"],
        body="# Bad",
    )
    active_store.set_active_frame_ids(auth.org_id, auth.workspace_id, auth.user, [good.id, bad.id])
    (tmp_path / bad.id / "metadata.json").write_text("{not valid json", encoding="utf-8")
    mcp = create_mcp_server(store, active_store=active_store)

    token = current_auth_context.set(auth)
    try:
        with caplog.at_level(logging.WARNING, logger="frames_server.codec"):
            active = parse_tool_result(await mcp.call_tool("get_active_frames", {}))
    finally:
        current_auth_context.reset(token)

    assert [item["id"] for item in active["frames"]] == [good.id]
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert bad.id in warnings[0].getMessage()


@pytest.mark.anyio
async def test_mcp_rejects_invalid_frame_ids_before_store_lookup(tmp_path):
    store = LocalFsFrameStore(tmp_path)
    active_store = InMemoryActiveFrameStore()
    auth = AuthContext(user="alice", home_org_id="org-a", workspace_id="workspace-a")
    active_store.set_active_frame_ids(auth.org_id, auth.workspace_id, auth.user, ["../metadata"])
    mcp = create_mcp_server(store, active_store=active_store)
    store_lookup_called = False

    def fail_get_frame(frame_id):
        nonlocal store_lookup_called
        store_lookup_called = True
        msg = f"store lookup should not receive invalid Frame id {frame_id!r}"
        raise AssertionError(msg)

    store.get_frame = fail_get_frame

    token = current_auth_context.set(auth)
    try:
        with pytest.raises(Exception):
            await mcp.call_tool("get_frame", {"id": "../metadata"})
        with pytest.raises(Exception):
            await mcp.call_tool("get_active_frames", {"ids": ["../metadata"]})
        with pytest.raises(Exception):
            await mcp.call_tool("get_active_frames", {})
        with pytest.raises(Exception):
            await mcp.read_resource("frame://../metadata")
    finally:
        current_auth_context.reset(token)

    assert store_lookup_called is False


def test_mcp_http_route_starts_session_manager(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("DEV_AUTH_ENABLED", "true")
    monkeypatch.setenv("DEV_AUTH_USER", "dev-user")
    config = Config.parse(
        {
            "storage": {"frames_path": str(tmp_path / "frames")},
            "frames": {"active_state": {"backend": "memory"}},
        }
    )
    app = make_app(config)

    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers={
                "accept": "application/json, text/event-stream",
                "content-type": "application/json",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )

    assert response.status_code == 200
    assert '"name":"frames"' in response.text or '"name": "frames"' in response.text


# The MCP app used to be mounted at "/", where it matched every path the API did
# not route: its auth middleware answered 401 for a trailing-slash typo, and its
# plain-text 404 answered unmatched paths. It is now registered at the one path
# it serves (#67); these pin what falls through to the app itself.


async def test_mcp_mount_lets_trailing_slash_paths_redirect(client):
    for path, target in (
        ("/v1/usage/summary/", "/v1/usage/summary"),
        ("/v1/frames/", "/v1/frames"),
        ("/v1/tasks/", "/v1/tasks"),
        ("/usage/", "/usage"),
    ):
        # Unauthenticated on purpose: the 401 this replaces came from the MCP
        # mount's auth middleware, which ran before any routing decision.
        response = await client.get(path)
        assert response.status_code == 307, path
        assert response.headers["location"].endswith(target), path


async def test_mcp_mount_lets_unmatched_api_paths_answer_the_frames_envelope(client):
    for path in ("/v1/usage/", "/v1/frames/a/b/c", "/v1/tasks/nope/nope", "/frames/a/b/c"):
        response = await client.get(path)
        assert response.status_code == 404, path
        assert response.json() == {"error": {"code": "not_found", "message": "Not Found"}}, path


async def test_bad_method_on_api_path_answers_the_envelope_and_keeps_allow(client):
    # Bad methods used to reach the MCP mount (401); now Starlette's router
    # raises 405 and the handler must keep the Allow header it attaches.
    response = await client.delete("/v1/frames")
    assert response.status_code == 405
    assert "GET" in response.headers["allow"]
    assert response.json() == {"error": {"code": "http_error", "message": "Method Not Allowed"}}


async def test_unsupported_options_on_api_paths_answers_405_with_allow(client):
    # Not a CORS preflight (no Origin / Access-Control-Request-Method), so the
    # request is routed and the GET-only route refuses it.
    response = await client.options("/v1/frames")
    assert response.status_code == 405
    assert "GET" in response.headers["allow"]


async def test_body_parse_errors_on_api_paths_answer_the_envelope(client):
    # FastAPI raises Starlette's HTTPException(400) for an unparseable body, so
    # registering the handler on the base class brings it into the envelope.
    response = await client.post(
        "/v1/frames",
        content=b"{\"name\": \"\xff\"}",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "http_error"


@pytest.mark.parametrize(
    ("root_path", "request_prefix"),
    [("/nexus", "/nexus"), ("/nexus", ""), ("/v", "")],
)
async def test_routing_errors_keep_the_envelope_under_a_root_path(
    tmp_path, monkeypatch, root_path, request_prefix
):
    # A proxy that keeps the `server.root_path` prefix ("/nexus" requested as
    # "/nexus/..."), one that strips it, and a root path that is a textual but
    # not a segment prefix of the API path ("/v"): in every case the path the
    # router matched on is an API path, and the answer is the envelope.
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_IDTOKEN_ALLOW_UNSIGNED", "true")
    config = Config.parse(
        {
            "storage": {"frames_path": str(tmp_path / "frames")},
            "server": {"root_path": root_path},
            "frames": {
                "active_state": {"backend": "memory"},
                "history": {"backend": "memory"},
                "usage": {"backend": "memory"},
                "mcp_session_manager_enabled": False,
            },
            "tasks": {"backend": "memory"},
        }
    )
    app = make_app(config)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, root_path=root_path)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            unmatched = await client.get(f"{request_prefix}/v1/frames/a/b/c")
            bad_method = await client.delete(f"{request_prefix}/v1/frames")
            # A route-raised 401 goes through the same handler.
            anonymous = await client.get(f"{request_prefix}/v1/frames")
    assert anonymous.status_code == 401
    assert anonymous.json()["error"]["code"] == "unauthorized"
    assert unmatched.status_code == 404
    assert unmatched.json() == {"error": {"code": "not_found", "message": "Not Found"}}
    assert bad_method.status_code == 405
    assert "GET" in bad_method.headers["allow"]
    assert bad_method.json() == {"error": {"code": "http_error", "message": "Method Not Allowed"}}


async def test_a_refusal_has_one_body_whichever_layer_raised_it(tmp_path, monkeypatch):
    # With default_access="authenticated" the protection middleware refuses
    # an anonymous request before routing; with "public" the route's own
    # dependency does.
    # Both classify the path with request_path, so the caller cannot tell the
    # two apart. root_path="/frames" is the case that used to split them: the
    # raw path spells an API prefix, the app-relative /v1/connectors does not.
    monkeypatch.setenv("FRAMES_IDTOKEN_ALLOW_UNSIGNED", "true")
    bodies = []
    for default_access in ("authenticated", "public"):
        config = Config.parse(
            {
                "storage": {"frames_path": str(tmp_path / f"frames-{default_access}")},
                "server": {"root_path": "/frames"},
                "security": {"default_access": default_access},
                "frames": {"active_state": {"backend": "memory"}, "mcp_session_manager_enabled": False},
            }
        )
        app = make_app(config)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, root_path="/frames")
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/frames/v1/connectors")
        assert response.status_code == 401, default_access
        bodies.append(response.json())
    assert bodies[0] == bodies[1]
    assert set(bodies[0]) == {"detail"}


async def test_mcp_mount_removal_leaves_other_response_shapes_alone(client):
    # `/v1/connectors/*` stays out of core._api_path, so it keeps FastAPI's
    # default error body. Converging error shapes app-wide is a contract
    # decision of its own; #67 only moves the routing decision.
    unmatched_connector = await client.get("/v1/connectors/bogus")
    assert unmatched_connector.status_code == 404
    assert unmatched_connector.json() == {"detail": "Not Found"}

    # A path outside every API prefix gets FastAPI's default body too, rather
    # than the MCP app's plain text.
    unmatched_elsewhere = await client.get("/no-such-page")
    assert unmatched_elsewhere.status_code == 404
    assert unmatched_elsewhere.json() == {"detail": "Not Found"}

    assert (await client.get("/health")).status_code == 200
    assert (await client.get("/metrics")).status_code == 200
    assert (await client.get("/")).status_code == 200


def test_mcp_endpoint_stays_reachable_while_other_paths_fall_through(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("DEV_AUTH_ENABLED", "true")
    monkeypatch.setenv("DEV_AUTH_USER", "dev-user")
    config = Config.parse(
        {
            "storage": {"frames_path": str(tmp_path / "frames")},
            "frames": {"active_state": {"backend": "memory"}},
        }
    )
    app = make_app(config)

    with TestClient(app) as client:
        # `/mcp` is the URL the docs, the chart's ingress and MCP clients all
        # use. The catch-all mount became an exact-path registration, so the
        # path the MCP app builds its endpoint on has to be routed to it.
        mcp_path = app.state.mcp_server.settings.streamable_http_path
        assert mcp_path == "/mcp"
        assert mcp_path in {getattr(route, "path", None) for route in app.router.routes}

        initialize = client.post(
            mcp_path,
            headers={
                "accept": "application/json, text/event-stream",
                "content-type": "application/json",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )
        assert initialize.status_code == 200
        assert '"name":"frames"' in initialize.text or '"name": "frames"' in initialize.text

        # The slash form of the MCP path now redirects to it like any other
        # registered path, instead of being refused by the MCP auth middleware.
        slashed = client.post(f"{mcp_path}/", follow_redirects=False)
        assert slashed.status_code == 307
        assert slashed.headers["location"].endswith(mcp_path)

        # ...while the same app now answers for everything else itself.
        redirected = client.get("/v1/usage/summary/", follow_redirects=False)
        assert redirected.status_code == 307
        unmatched = client.get("/v1/usage/")
        assert unmatched.status_code == 404
        assert unmatched.json() == {"error": {"code": "not_found", "message": "Not Found"}}


def test_mcp_http_smoke_helper_parses_tool_json_payloads(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_IDTOKEN_ALLOW_UNSIGNED", "true")
    config = Config.parse(
        {
            "storage": {"frames_path": str(tmp_path / "frames")},
            "frames": {"active_state": {"backend": "memory"}},
        }
    )
    app = make_app(config)
    server, thread, base_url = start_test_server(app, free_tcp_port())
    headers = {"Cookie": auth_cookie()}
    expected_body = "# Smoke\nUpdated frame body"

    try:
        with httpx.Client(base_url=base_url, headers=headers, timeout=5) as client:
            created = client.post(
                "/v1/frames",
                json={
                    "name": "Smoke Frame",
                    "tags": ["smoke"],
                    "body": "# Smoke\nInitial frame body",
                },
            )
            created.raise_for_status()
            frame_id = created.json()["id"]

            updated = client.put(
                f"/v1/frames/{frame_id}",
                json={"name": "Smoke Frame", "tags": ["smoke"], "body": expected_body},
            )
            updated.raise_for_status()

            active = client.put("/v1/active-frames", json={"frame_ids": [frame_id]})
            active.raise_for_status()

        import asyncio

        asyncio.run(
            assert_mcp_contract(
                base_url,
                frame_id,
                expected_body,
                headers=headers,
                require_stored_active=True,
            )
        )
    finally:
        server.should_exit = True
        thread.join(timeout=5)
