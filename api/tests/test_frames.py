from __future__ import annotations

import base64
import json
import logging
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from collab_hub_api.config import Config
from collab_hub_api.core import make_app
from collab_hub_api.frames.models import (
    FRAME_BODY_MAX_LENGTH,
    FRAME_NAME_MAX_LENGTH,
    MAX_OWNERS,
    MAX_TAGS,
    SUGGESTION_BODY_MAX_LENGTH,
)


def _jwt(payload: dict) -> str:
    def encode(part: dict) -> str:
        raw = json.dumps(part, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode(payload)}."


def auth_cookie(user: str, org: str = "org-a", workspace: str = "workspace-a") -> dict[str, str]:
    return {
        "IdToken-test": _jwt(
            {
                "preferred_username": user,
                "org_id": org,
                "workspace_id": workspace,
            }
        )
    }


def auth_header(user: str, org: str = "org-a", workspace: str = "workspace-a") -> dict[str, str]:
    token = _jwt(
        {
            "preferred_username": user,
            "org_id": org,
            "workspace_id": workspace,
        }
    )
    return {"Authorization": f"Bearer {token}"}


def auth_header_without_scope(user: str) -> dict[str, str]:
    token = _jwt({"preferred_username": user})
    return {"Authorization": f"Bearer {token}"}


async def create_frame(
    client,
    user: str = "alice",
    name: str = "Team Frame",
    tags: list[str] | None = None,
    *,
    body: dict | None = None,
):
    payload = {"name": name, "tags": tags or ["team"], "body": "# Body"}
    if body is not None:
        payload.update(body)
    response = await client.post(
        "/v1/frames",
        cookies=auth_cookie(user),
        json=payload,
    )
    assert response.status_code == 201
    return response.json()


async def publish(client, frame_id: str, user: str = "alice"):
    response = await client.post(f"/v1/frames/{frame_id}/publish", cookies=auth_cookie(user))
    assert response.status_code == 200
    return response.json()


async def create_shared_frame(client, user: str = "alice", name: str = "Team Frame", tags: list[str] | None = None):
    """Create a published, internal frame readable by any same-tenant user."""

    frame = await create_frame(client, user=user, name=name, tags=tags, body={"visibility": "internal"})
    await publish(client, frame["id"], user=user)
    return frame


async def test_frames_contract_roundtrip(client):
    created = await create_frame(client, tags=["Brand", "team"])
    frame_id = created["id"]
    assert len(frame_id) == 32
    assert created["owners"] == ["alice"]
    assert created["created_by"] == "alice"
    assert created["visibility"] == "private"
    assert created["published"] is False
    assert created["readers"] == []
    assert created["group_ids"] == []
    assert created["tags"] == ["brand", "team"]
    assert created["body"] == "# Body"

    # A private, unpublished frame is invisible to non-owners (404, never 403).
    assert (await client.get(f"/v1/frames/{frame_id}", cookies=auth_cookie("bob"))).status_code == 404
    assert (await client.get("/v1/frames?tag=brand", cookies=auth_cookie("bob"))).json() == []

    # Publish as internal so same-tenant users can read it.
    await client.put(
        f"/v1/frames/{frame_id}",
        cookies=auth_cookie("alice"),
        json={"name": "Team Frame", "tags": ["brand", "team"], "body": "# Body", "visibility": "internal"},
    )
    await publish(client, frame_id)

    listing = await client.get("/v1/frames?tag=brand&owner=alice", cookies=auth_cookie("bob"))
    assert listing.status_code == 200
    assert [item["id"] for item in listing.json()] == [frame_id]
    assert "body" not in listing.json()[0]

    detail = await client.get(f"/v1/frames/{frame_id}", cookies=auth_cookie("bob"))
    assert detail.status_code == 200
    assert detail.json()["body"] == "# Body"

    rejected = await client.put(
        f"/v1/frames/{frame_id}",
        cookies=auth_cookie("bob"),
        json={"name": "Updated", "tags": [], "body": "updated"},
    )
    assert rejected.status_code == 403

    updated = await client.put(
        f"/v1/frames/{frame_id}",
        cookies=auth_cookie("alice"),
        json={"name": "Updated", "tags": ["legal"], "body": "updated"},
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "Updated"
    assert updated.json()["body"] == "updated"


async def test_suggestions_and_active_frames(client):
    frame = await create_frame(client, body={"visibility": "internal"})
    await publish(client, frame["id"])
    suggestion = await client.post(
        f"/v1/frames/{frame['id']}/suggestions",
        cookies=auth_cookie("bob"),
        json={"body": "try this"},
    )
    assert suggestion.status_code == 201
    suggestion_id = suggestion.json()["id"]

    closed = await client.post(
        f"/v1/frames/{frame['id']}/suggestions/{suggestion_id}/close",
        cookies=auth_cookie("bob"),
    )
    assert closed.status_code == 200
    assert closed.json()["status"] == "closed"

    active = await client.put(
        "/v1/active-frames",
        cookies=auth_cookie("alice"),
        json={"frame_ids": [frame["id"], frame["id"]]},
    )
    assert active.status_code == 200
    assert active.json()["frame_ids"] == [frame["id"]]

    other_user = await client.get("/v1/active-frames", cookies=auth_cookie("bob"))
    assert other_user.status_code == 200
    assert other_user.json()["frame_ids"] == []


async def test_workspace_scoping_and_hidden_aliases(client):
    frame = await create_frame(client)
    # The owner (same user) lists are tenant-scoped, but a *non-owner* in another
    # tenant cannot read a private frame (owners themselves now read anywhere).
    other_workspace = auth_cookie("alice", workspace="workspace-b")
    stranger = auth_cookie("bob", workspace="workspace-b")

    assert (await client.get("/v1/frames", cookies=other_workspace)).json() == []
    assert (await client.get(f"/v1/frames/{frame['id']}", cookies=stranger)).status_code == 404

    compat = await client.get(f"/frames/{frame['id']}", cookies=auth_cookie("alice"))
    assert compat.status_code == 200
    assert compat.json()["id"] == frame["id"]

    schema = (await client.get("/openapi.json", cookies=auth_cookie("alice"))).json()
    assert "/v1/frames" in schema["paths"]
    assert "/v1/active-frames" in schema["paths"]
    assert "/frames" not in schema["paths"]


async def test_owner_from_dev_fallback(dev_client):
    response = await dev_client.post(
        "/v1/frames",
        json={"name": "Dev Frame", "body": "dev", "tags": []},
    )

    assert response.status_code == 201
    assert response.json()["owners"] == ["dev-user"]
    assert response.json()["org_id"] == "dev-org"
    assert response.json()["workspace_id"] == "default"


async def test_real_token_wins_over_dev_fallback(dev_client):
    response = await dev_client.post(
        "/v1/frames",
        json={"name": "Token Frame", "body": "token", "tags": []},
        cookies=auth_cookie("token-user"),
    )

    assert response.status_code == 201
    assert response.json()["owners"] == ["token-user"]


async def test_bearer_token_uses_same_identity_claims(client, monkeypatch):
    monkeypatch.setenv("FRAMES_BEARER_ALLOW_UNSIGNED", "true")
    response = await client.post(
        "/v1/frames",
        json={"name": "Token Frame", "body": "token", "tags": []},
        headers=auth_header("token-user"),
    )

    assert response.status_code == 201
    assert response.json()["owners"] == ["token-user"]
    assert response.json()["org_id"] == "org-a"
    assert response.json()["workspace_id"] == "workspace-a"


async def test_bearer_token_uses_configured_default_scope(client, monkeypatch):
    monkeypatch.setenv("FRAMES_BEARER_ALLOW_UNSIGNED", "true")
    monkeypatch.setenv("FRAMES_AUTH_DEFAULT_ORG", "nebari")
    monkeypatch.setenv("FRAMES_AUTH_DEFAULT_WORKSPACE", "default")

    response = await client.post(
        "/v1/frames",
        json={"name": "Token Frame", "body": "token", "tags": []},
        headers=auth_header_without_scope("token-user"),
    )

    assert response.status_code == 201
    assert response.json()["owners"] == ["token-user"]
    assert response.json()["org_id"] == "nebari"
    assert response.json()["workspace_id"] == "default"


async def test_bearer_token_without_scope_or_defaults_is_rejected(client, monkeypatch):
    monkeypatch.setenv("FRAMES_BEARER_ALLOW_UNSIGNED", "true")
    monkeypatch.delenv("FRAMES_AUTH_DEFAULT_ORG", raising=False)
    monkeypatch.delenv("FRAMES_AUTH_DEFAULT_WORKSPACE", raising=False)

    response = await client.post(
        "/v1/frames",
        json={"name": "Token Frame", "body": "token", "tags": []},
        headers=auth_header_without_scope("token-user"),
    )

    assert response.status_code == 401
    assert response.json()["error"]["message"] == "Invalid bearer token"


async def test_cookie_wins_over_bearer_token(client, monkeypatch):
    monkeypatch.setenv("FRAMES_BEARER_ALLOW_UNSIGNED", "true")
    response = await client.post(
        "/v1/frames",
        json={"name": "Cookie Frame", "body": "token", "tags": []},
        cookies=auth_cookie("cookie-user"),
        headers=auth_header("bearer-user"),
    )

    assert response.status_code == 201
    assert response.json()["owners"] == ["cookie-user"]


async def test_bearer_token_wins_over_dev_fallback(dev_client, monkeypatch):
    monkeypatch.setenv("FRAMES_BEARER_ALLOW_UNSIGNED", "true")
    response = await dev_client.post(
        "/v1/frames",
        json={"name": "Token Frame", "body": "token", "tags": []},
        headers=auth_header("token-user"),
    )

    assert response.status_code == 201
    assert response.json()["owners"] == ["token-user"]


async def test_bearer_token_requires_verification_configuration(client, monkeypatch):
    monkeypatch.delenv("FRAMES_UNSAFE_AUTH_ENABLED", raising=False)
    monkeypatch.delenv("FRAMES_BEARER_ALLOW_UNSIGNED", raising=False)
    monkeypatch.delenv("FRAMES_BEARER_JWKS_URL", raising=False)

    response = await client.get("/v1/frames", headers=auth_header("token-user"))

    assert response.status_code == 401
    assert response.json()["error"]["message"] == "Invalid bearer token"


async def test_id_token_cookie_requires_verification_configuration(config: Config, monkeypatch):
    monkeypatch.delenv("FRAMES_UNSAFE_AUTH_ENABLED", raising=False)
    monkeypatch.delenv("FRAMES_IDTOKEN_ALLOW_UNSIGNED", raising=False)
    monkeypatch.delenv("FRAMES_IDTOKEN_JWKS_URL", raising=False)
    monkeypatch.delenv("FRAMES_BEARER_JWKS_URL", raising=False)
    app = make_app(config)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as isolated_client:
            response = await isolated_client.get("/v1/active-frames", cookies=auth_cookie("forged-user"))

    assert response.status_code == 401
    assert response.json()["error"]["message"] == "Invalid IdToken cookie"


async def test_dev_auth_requires_explicit_unsafe_gate(config: Config, monkeypatch):
    monkeypatch.setenv("DEV_AUTH_USER", "dev-user")
    monkeypatch.delenv("DEV_AUTH_ENABLED", raising=False)
    monkeypatch.delenv("FRAMES_UNSAFE_AUTH_ENABLED", raising=False)
    app = make_app(config)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as isolated_client:
            response = await isolated_client.get("/v1/frames")

    assert response.status_code == 401
    assert response.json()["error"]["message"] == "Authentication required"


async def test_invalid_bearer_token_rejected(client):
    response = await client.get(
        "/v1/frames",
        headers={"Authorization": "Bearer invalid-token"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["message"] == "Invalid bearer token"


async def test_configured_cors_allows_nebari_session_credentials(cors_client):
    response = await cors_client.options(
        "/v1/frames",
        headers={
            "Origin": "https://desktop.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://desktop.example.com"
    assert response.headers["access-control-allow-credentials"] == "true"


async def test_auth_required_without_cookie_or_dev_user(client):
    response = await client.get("/v1/frames")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_suggestions_flow_and_frame_delete_cleanup(client):
    frame = await create_shared_frame(client, user="alice", tags=["sales"])
    frame_id = frame["id"]

    suggestion = await client.post(
        f"/v1/frames/{frame_id}/suggestions",
        cookies=auth_cookie("bob"),
        json={"body": "change the intro"},
    )
    assert suggestion.status_code == 201
    assert suggestion.json()["submitted_by"] == "bob"
    assert suggestion.json()["status"] == "open"

    open_response = await client.get(
        f"/v1/frames/{frame_id}/suggestions?status=open",
        cookies=auth_cookie("alice"),
    )
    assert [item["id"] for item in open_response.json()] == [suggestion.json()["id"]]

    close_response = await client.post(
        f"/v1/frames/{frame_id}/suggestions/{suggestion.json()['id']}/close",
        cookies=auth_cookie("bob"),
    )
    assert close_response.status_code == 200
    assert close_response.json()["status"] == "closed"

    closed_response = await client.get(
        f"/v1/frames/{frame_id}/suggestions?status=closed",
        cookies=auth_cookie("alice"),
    )
    assert [item["id"] for item in closed_response.json()] == [suggestion.json()["id"]]

    assert (await client.delete(f"/v1/frames/{frame_id}", cookies=auth_cookie("alice"))).status_code == 204
    assert (
        await client.get(f"/v1/frames/{frame_id}/suggestions", cookies=auth_cookie("alice"))
    ).status_code == 404


async def test_frame_owner_can_close_any_suggestion(client):
    frame = await create_shared_frame(client, user="alice", tags=["sales"])
    suggestion = await client.post(
        f"/v1/frames/{frame['id']}/suggestions",
        cookies=auth_cookie("bob"),
        json={"body": "try this"},
    )
    assert suggestion.status_code == 201

    close_response = await client.post(
        f"/v1/frames/{frame['id']}/suggestions/{suggestion.json()['id']}/close",
        cookies=auth_cookie("alice"),
    )
    assert close_response.status_code == 200
    assert close_response.json()["status"] == "closed"


async def test_unrelated_user_cannot_close_suggestion(client):
    frame = await create_shared_frame(client, user="alice", tags=["sales"])
    suggestion = await client.post(
        f"/v1/frames/{frame['id']}/suggestions",
        cookies=auth_cookie("bob"),
        json={"body": "try this"},
    )
    assert suggestion.status_code == 201

    close_response = await client.post(
        f"/v1/frames/{frame['id']}/suggestions/{suggestion.json()['id']}/close",
        cookies=auth_cookie("charlie"),
    )
    assert close_response.status_code == 403
    assert close_response.json()["error"]["code"] == "forbidden"


async def test_close_missing_suggestion_returns_not_found(client):
    frame = await create_frame(client, user="alice", tags=["sales"])
    missing_suggestion_id = "0" * 32

    close_response = await client.post(
        f"/v1/frames/{frame['id']}/suggestions/{missing_suggestion_id}/close",
        cookies=auth_cookie("alice"),
    )
    assert close_response.status_code == 404
    assert close_response.json()["error"]["code"] == "suggestion_not_found"


async def test_suggestion_permissions_and_delete_cleanup(client):
    frame = await create_shared_frame(client, user="alice", tags=["sales"])
    frame_id = frame["id"]

    suggestion = await client.post(
        f"/v1/frames/{frame_id}/suggestions",
        cookies=auth_cookie("bob"),
        json={"body": "change the intro"},
    )
    assert suggestion.status_code == 201
    assert suggestion.json()["submitted_by"] == "bob"
    suggestion_id = suggestion.json()["id"]

    rejected = await client.post(
        f"/v1/frames/{frame_id}/suggestions/{suggestion_id}/close",
        cookies=auth_cookie("charlie"),
    )
    assert rejected.status_code == 403
    assert rejected.json()["error"]["code"] == "forbidden"

    closed = await client.post(
        f"/v1/frames/{frame_id}/suggestions/{suggestion_id}/close",
        cookies=auth_cookie("alice"),
    )
    assert closed.status_code == 200
    assert closed.json()["status"] == "closed"

    missing = await client.post(
        f"/v1/frames/{frame_id}/suggestions/{'0' * 32}/close",
        cookies=auth_cookie("alice"),
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "suggestion_not_found"

    deleted = await client.delete(f"/v1/frames/{frame_id}", cookies=auth_cookie("alice"))
    assert deleted.status_code == 204
    suggestions = await client.get(f"/v1/frames/{frame_id}/suggestions", cookies=auth_cookie("alice"))
    assert suggestions.status_code == 404


async def test_active_frame_state_is_per_user_and_validates_ids(client):
    first = await create_frame(client, user="alice", tags=["team"])
    second = await create_frame(client, user="alice", tags=["team"])

    response = await client.put(
        "/v1/active-frames",
        cookies=auth_cookie("alice"),
        json={"frame_ids": [first["id"], second["id"], first["id"]]},
    )
    assert response.status_code == 200
    assert response.json() == {
        "user": "alice",
        "org_id": "org-a",
        "workspace_id": "workspace-a",
        "frame_ids": [first["id"], second["id"]],
    }

    alice = await client.get("/v1/active-frames", cookies=auth_cookie("alice"))
    assert alice.status_code == 200
    assert alice.json()["frame_ids"] == [first["id"], second["id"]]

    bob = await client.get("/v1/active-frames", cookies=auth_cookie("bob"))
    assert bob.status_code == 200
    assert bob.json() == {
        "user": "bob",
        "org_id": "org-a",
        "workspace_id": "workspace-a",
        "frame_ids": [],
    }

    missing = await client.put(
        "/v1/active-frames",
        cookies=auth_cookie("alice"),
        json={"frame_ids": ["0" * 32]},
    )
    assert missing.status_code == 404


async def test_frames_and_active_state_are_workspace_scoped(client):
    frame = await create_frame(client, user="alice")
    other_workspace_cookie = auth_cookie("alice", workspace="workspace-b")
    # A non-owner in another tenant cannot see or pin a private frame.
    stranger_cookie = auth_cookie("bob", workspace="workspace-b")

    assert (await client.get("/v1/frames", cookies=other_workspace_cookie)).json() == []
    get_from_other_workspace = await client.get(
        f"/v1/frames/{frame['id']}",
        cookies=stranger_cookie,
    )
    assert get_from_other_workspace.status_code == 404

    rejected_active = await client.put(
        "/v1/active-frames",
        cookies=stranger_cookie,
        json={"frame_ids": [frame["id"]]},
    )
    assert rejected_active.status_code == 404

    response = await client.put(
        "/v1/active-frames",
        cookies=auth_cookie("alice"),
        json={"frame_ids": [frame["id"]]},
    )
    assert response.status_code == 200

    # Active state is per (org, workspace, user): the same user in another
    # workspace has an independent, empty active set.
    other_active = await client.get("/v1/active-frames", cookies=other_workspace_cookie)
    assert other_active.json()["frame_ids"] == []


async def test_deleted_frame_is_removed_from_active_state(client):
    frame = await create_frame(client)
    active = await client.put(
        "/v1/active-frames",
        cookies=auth_cookie("alice"),
        json={"frame_ids": [frame["id"]]},
    )
    assert active.status_code == 200

    deleted = await client.delete(f"/v1/frames/{frame['id']}", cookies=auth_cookie("alice"))
    assert deleted.status_code == 204

    active = await client.get("/v1/active-frames", cookies=auth_cookie("alice"))
    assert active.status_code == 200
    assert active.json()["frame_ids"] == []


async def test_validation_rejects_invalid_frame_inputs(client):
    empty = await client.post(
        "/v1/frames",
        json={"name": "Empty Body", "body": "", "tags": []},
        cookies=auth_cookie("alice"),
    )
    assert empty.status_code == 422
    assert empty.json()["error"]["code"] == "validation_error"

    too_large = await client.post(
        "/v1/frames",
        json={
            "name": "Large Body",
            "body": "x" * (FRAME_BODY_MAX_LENGTH + 1),
            "tags": [],
        },
        cookies=auth_cookie("alice"),
    )
    assert too_large.status_code == 422

    bad_tag = await client.post(
        "/v1/frames",
        json={"name": "Bad Tag", "body": "body", "tags": ["Invalid Tag"]},
        cookies=auth_cookie("alice"),
    )
    assert bad_tag.status_code == 422

    too_many_tags = await client.post(
        "/v1/frames",
        json={
            "name": "Too Many Tags",
            "body": "body",
            "tags": [f"tag-{index}" for index in range(MAX_TAGS + 1)],
        },
        cookies=auth_cookie("alice"),
    )
    assert too_many_tags.status_code == 422


async def test_tags_and_names_are_normalized(client):
    frame = await create_frame(
        client,
        user="alice",
        name=" Team Playbook ",
        tags=[" Sales ", "sales", "legal.review"],
    )

    assert frame["name"] == "Team Playbook"
    assert frame["tags"] == ["sales", "legal.review"]


async def test_validation_rejects_invalid_names_suggestions_and_ids(client):
    empty_name = await client.post(
        "/v1/frames",
        json={"name": "   ", "body": "body", "tags": []},
        cookies=auth_cookie("alice"),
    )
    assert empty_name.status_code == 422

    too_long_name = await client.post(
        "/v1/frames",
        json={
            "name": "x" * (FRAME_NAME_MAX_LENGTH + 1),
            "body": "body",
            "tags": [],
        },
        cookies=auth_cookie("alice"),
    )
    assert too_long_name.status_code == 422

    frame = await create_frame(client)
    empty_suggestion = await client.post(
        f"/v1/frames/{frame['id']}/suggestions",
        json={"body": ""},
        cookies=auth_cookie("bob"),
    )
    assert empty_suggestion.status_code == 422

    too_large_suggestion = await client.post(
        f"/v1/frames/{frame['id']}/suggestions",
        json={"body": "x" * (SUGGESTION_BODY_MAX_LENGTH + 1)},
        cookies=auth_cookie("bob"),
    )
    assert too_large_suggestion.status_code == 422

    bad_path = await client.get("/v1/frames/not-a-frame-id", cookies=auth_cookie("alice"))
    assert bad_path.status_code == 422
    assert bad_path.json()["error"]["code"] == "validation_error"


async def test_docs_and_openapi_require_auth(client):
    assert (await client.get("/docs")).status_code == 401
    assert (await client.get("/redoc")).status_code == 401
    assert (await client.get("/openapi.json")).status_code == 401

    assert (await client.get("/docs", cookies=auth_cookie("alice"))).status_code == 200
    assert (await client.get("/redoc", cookies=auth_cookie("alice"))).status_code == 200
    assert (await client.get("/openapi.json", cookies=auth_cookie("alice"))).status_code == 200


async def test_openapi_contract_contains_rest_paths_and_frame_schemas(client):
    schema = (await client.get("/openapi.json", cookies=auth_cookie("alice"))).json()
    assert "/v1/frames" in schema["paths"]
    assert "/v1/active-frames" in schema["paths"]
    assert "/v1/frames/{frame_id}" in schema["paths"]
    assert "/v1/frames/{frame_id}/suggestions" in schema["paths"]
    assert "/frames" not in schema["paths"]
    assert schema["components"]["schemas"]["FrameCreate"]["properties"]["body"]["maxLength"] == FRAME_BODY_MAX_LENGTH
    assert schema["components"]["schemas"]["FrameCreate"]["additionalProperties"] is False


async def test_request_id_and_metrics(client):
    response = await client.get(
        "/v1/frames",
        cookies=auth_cookie("alice"),
        headers={"x-request-id": "request-123"},
    )
    assert response.headers["x-request-id"] == "request-123"

    frame = await create_frame(client)
    assert frame["id"]

    # Unconfigured, /metrics stays reachable exactly as it was: the protection
    # map is opted into, not on by default. test_path_protection
    # covers the hardened deployment.
    metrics = await client.get("/metrics")
    assert metrics.status_code == 200
    assert "frames_server_http_requests_total" in metrics.text
    assert "frames_server_http_request_duration_seconds" in metrics.text
    assert "frames_server_audit_events_total" in metrics.text


# --- Spec 1: access control, owners, readers, publish, reconciliation ---


async def set_active(client, user: str, frame_id: str):
    response = await client.put(
        "/v1/active-frames",
        cookies=auth_cookie(user),
        json={"frame_ids": [frame_id]},
    )
    assert response.status_code == 200
    return response.json()


async def active_ids(client, user: str) -> list[str]:
    response = await client.get("/v1/active-frames", cookies=auth_cookie(user))
    assert response.status_code == 200
    return response.json()["frame_ids"]


async def test_create_defaults_and_seeded_owners(client):
    created = await create_frame(client)
    assert created["owners"] == ["alice"]
    assert created["created_by"] == "alice"
    assert created["visibility"] == "private"
    assert created["published"] is False

    # Seeding co-owners always force-includes the caller, deduped, caller first.
    seeded = await client.post(
        "/v1/frames",
        cookies=auth_cookie("alice"),
        json={"name": "Seeded", "tags": [], "body": "# Body", "owners": ["bob", "alice", "bob"]},
    )
    assert seeded.status_code == 201
    assert seeded.json()["owners"] == ["alice", "bob"]
    assert seeded.json()["created_by"] == "alice"


async def test_owner_reads_unpublished_private_frame(client):
    frame = await create_frame(client)
    detail = await client.get(f"/v1/frames/{frame['id']}", cookies=auth_cookie("alice"))
    assert detail.status_code == 200


async def test_internal_published_is_tenant_scoped(client):
    frame = await create_shared_frame(client)

    # Same-tenant non-owner can read.
    assert (await client.get(f"/v1/frames/{frame['id']}", cookies=auth_cookie("bob"))).status_code == 200

    # Cross-workspace user is denied with 404 (existence is not leaked).
    cross = auth_cookie("bob", workspace="workspace-b")
    assert (await client.get(f"/v1/frames/{frame['id']}", cookies=cross)).status_code == 404


async def test_readers_expand_a_private_frame(client):
    # The reader list is an ACL-lite grant that EXPANDS a published private frame
    # to the listed users.
    frame = await create_frame(client)  # private
    await publish(client, frame["id"])

    # A non-reader cannot see a published private frame.
    assert (await client.get(f"/v1/frames/{frame['id']}", cookies=auth_cookie("bob"))).status_code == 404

    add = await client.post(
        f"/v1/frames/{frame['id']}/readers",
        cookies=auth_cookie("alice"),
        json={"email": "bob"},
    )
    assert add.status_code == 200
    assert add.json()["readers"] == ["bob"]

    # The listed reader can now read; a non-listed user still cannot.
    assert (await client.get(f"/v1/frames/{frame['id']}", cookies=auth_cookie("bob"))).status_code == 200
    assert (await client.get(f"/v1/frames/{frame['id']}", cookies=auth_cookie("carol"))).status_code == 404


async def test_readers_do_not_apply_to_internal_frames(client):
    # An internal frame is readable by the whole tenant; readers never narrow it
    # (they only ever apply to private — see the invariant below).
    frame = await create_frame(client, body={"visibility": "internal"})
    await publish(client, frame["id"])
    assert (await client.get(f"/v1/frames/{frame['id']}", cookies=auth_cookie("bob"))).status_code == 200
    assert (await client.get(f"/v1/frames/{frame['id']}", cookies=auth_cookie("carol"))).status_code == 200


async def test_reader_visibility_invariant(client):
    # Writing a non-empty reader list forces visibility=private...
    frame = await create_frame(client, body={"visibility": "internal"})
    add = await client.post(
        f"/v1/frames/{frame['id']}/readers",
        cookies=auth_cookie("alice"),
        json={"email": "bob"},
    )
    assert add.status_code == 200
    detail = await client.get(f"/v1/frames/{frame['id']}", cookies=auth_cookie("alice"))
    assert detail.json()["visibility"] == "private"
    assert detail.json()["readers"] == ["bob"]

    # ...and setting visibility back to internal/public clears the reader list.
    updated = await client.put(
        f"/v1/frames/{frame['id']}",
        cookies=auth_cookie("alice"),
        json={"name": "Team Frame", "tags": [], "body": "# Body", "visibility": "public"},
    )
    assert updated.status_code == 200
    assert updated.json()["visibility"] == "public"
    assert updated.json()["readers"] == []


async def test_owners_management_add_remove_and_last_owner(client):
    # Published internal so a non-owner can *read* it — required to observe the
    # 403 (visible-but-not-owner) rather than the 404 (can't-see-it) branch.
    frame = await create_frame(client, body={"visibility": "internal"})
    await publish(client, frame["id"])
    frame_id = frame["id"]

    # A non-owner who can read the frame but isn't an owner: 403.
    forbidden = await client.put(
        f"/v1/frames/{frame_id}/owners",
        cookies=auth_cookie("bob"),
        json={"owners": ["bob"]},
    )
    assert forbidden.status_code == 403

    added = await client.post(
        f"/v1/frames/{frame_id}/owners",
        cookies=auth_cookie("alice"),
        json={"email": "bob"},
    )
    assert added.status_code == 200
    assert added.json()["owners"] == ["alice", "bob"]

    # bob is now an owner and can manage.
    listed = await client.get(f"/v1/frames/{frame_id}/owners", cookies=auth_cookie("bob"))
    assert listed.json()["owners"] == ["alice", "bob"]

    removed = await client.delete(f"/v1/frames/{frame_id}/owners/alice", cookies=auth_cookie("bob"))
    assert removed.status_code == 200
    assert removed.json()["owners"] == ["bob"]

    # Removing the last owner is refused.
    last = await client.delete(f"/v1/frames/{frame_id}/owners/bob", cookies=auth_cookie("bob"))
    assert last.status_code == 409
    assert last.json()["error"]["code"] == "last_owner"

    # PUT replace must keep >=1 owner.
    empty = await client.put(
        f"/v1/frames/{frame_id}/owners",
        cookies=auth_cookie("bob"),
        json={"owners": []},
    )
    assert empty.status_code == 422


async def test_add_owner_past_max_owners_returns_409_not_500(client):
    # Regression for issue #43: add_owner's unguarded normalize_owners() call
    # raised ValueError inside the handler once a frame held MAX_OWNERS
    # owners, and FastAPI turned that into a bare 500. The boundary itself
    # (49 -> 50) must keep succeeding; only crossing it (50 -> 51) is refused.
    frame = await create_frame(client)
    frame_id = frame["id"]

    # "alice" plus MAX_OWNERS - 2 others = MAX_OWNERS - 1 owners total.
    crowd = [f"owner-{i}" for i in range(MAX_OWNERS - 2)]
    filled = await client.put(
        f"/v1/frames/{frame_id}/owners",
        cookies=auth_cookie("alice"),
        json={"owners": ["alice", *crowd]},
    )
    assert filled.status_code == 200
    assert len(filled.json()["owners"]) == MAX_OWNERS - 1

    # 49 -> 50 succeeds.
    at_cap = await client.post(
        f"/v1/frames/{frame_id}/owners",
        cookies=auth_cookie("alice"),
        json={"email": "owner-at-cap"},
    )
    assert at_cap.status_code == 200
    owners_at_cap = at_cap.json()["owners"]
    assert len(owners_at_cap) == MAX_OWNERS

    # 50 -> 51 is refused in the frames error envelope, not a 500.
    over_cap = await client.post(
        f"/v1/frames/{frame_id}/owners",
        cookies=auth_cookie("alice"),
        json={"email": "one-too-many"},
    )
    assert over_cap.status_code == 409
    assert over_cap.json()["error"]["code"] == "max_owners"
    assert str(MAX_OWNERS) in over_cap.json()["error"]["message"]
    # And the frame is left unchanged at the cap, not partially written:
    # the exact owner list from before the refused add, not merely 50 entries.
    listed = await client.get(f"/v1/frames/{frame_id}/owners", cookies=auth_cookie("alice"))
    assert listed.json()["owners"] == owners_at_cap


async def test_mutation_on_unreadable_frame_is_404_not_403(client):
    # A non-owner who cannot even read a private frame gets 404 from a mutation
    # endpoint (existence is never leaked), not 403 — read-then-manage ordering.
    frame = await create_frame(client)  # private, unpublished
    frame_id = frame["id"]

    put_owners = await client.put(
        f"/v1/frames/{frame_id}/owners",
        cookies=auth_cookie("bob"),
        json={"owners": ["bob"]},
    )
    assert put_owners.status_code == 404
    assert put_owners.json()["error"]["code"] == "frame_not_found"

    assert (
        await client.put(
            f"/v1/frames/{frame_id}",
            cookies=auth_cookie("bob"),
            json={"name": "X", "tags": [], "body": "x"},
        )
    ).status_code == 404
    assert (await client.delete(f"/v1/frames/{frame_id}", cookies=auth_cookie("bob"))).status_code == 404


async def test_readers_management_and_permissions(client):
    frame = await create_frame(client, body={"visibility": "internal"})
    await publish(client, frame["id"])
    frame_id = frame["id"]

    replaced = await client.put(
        f"/v1/frames/{frame_id}/readers",
        cookies=auth_cookie("alice"),
        json={"readers": ["bob", "bob", "carol"]},
    )
    assert replaced.status_code == 200
    assert replaced.json()["readers"] == ["bob", "carol"]

    # Readers may read but never manage the reader list.
    assert (await client.get(f"/v1/frames/{frame_id}", cookies=auth_cookie("bob"))).status_code == 200
    forbidden = await client.get(f"/v1/frames/{frame_id}/readers", cookies=auth_cookie("bob"))
    assert forbidden.status_code == 403

    # Removing bob from a non-empty reader list blocks bob (carol stays the only reader).
    removed = await client.delete(f"/v1/frames/{frame_id}/readers/bob", cookies=auth_cookie("alice"))
    assert removed.status_code == 200
    assert removed.json()["readers"] == ["carol"]
    assert (await client.get(f"/v1/frames/{frame_id}", cookies=auth_cookie("bob"))).status_code == 404


async def test_publish_unpublish_toggles_access(client):
    frame = await create_frame(client, body={"visibility": "internal"})
    frame_id = frame["id"]

    assert (await client.get(f"/v1/frames/{frame_id}", cookies=auth_cookie("bob"))).status_code == 404
    await publish(client, frame_id)
    assert (await client.get(f"/v1/frames/{frame_id}", cookies=auth_cookie("bob"))).status_code == 200

    unpublished = await client.post(f"/v1/frames/{frame_id}/unpublish", cookies=auth_cookie("alice"))
    assert unpublished.status_code == 200
    assert unpublished.json()["published"] is False
    assert (await client.get(f"/v1/frames/{frame_id}", cookies=auth_cookie("bob"))).status_code == 404


async def test_reconcile_on_unpublish(client):
    frame = await create_shared_frame(client)
    await set_active(client, "bob", frame["id"])
    assert await active_ids(client, "bob") == [frame["id"]]

    await client.post(f"/v1/frames/{frame['id']}/unpublish", cookies=auth_cookie("alice"))
    assert await active_ids(client, "bob") == []


async def test_reconcile_on_internal_to_private(client):
    frame = await create_shared_frame(client)
    await set_active(client, "bob", frame["id"])

    # Narrow internal -> private via update; bob is not a reader, so he loses it.
    narrowed = await client.put(
        f"/v1/frames/{frame['id']}",
        cookies=auth_cookie("alice"),
        json={"name": "Team Frame", "tags": ["team"], "body": "# Body", "visibility": "private"},
    )
    assert narrowed.status_code == 200
    assert await active_ids(client, "bob") == []


async def test_reconcile_on_reader_removal(client):
    # Internal frame restricted to [bob, carol]; removing bob narrows him out.
    frame = await create_frame(client, body={"visibility": "internal"})
    await publish(client, frame["id"])
    await client.put(
        f"/v1/frames/{frame['id']}/readers",
        cookies=auth_cookie("alice"),
        json={"readers": ["bob", "carol"]},
    )
    await set_active(client, "bob", frame["id"])
    assert await active_ids(client, "bob") == [frame["id"]]

    await client.delete(f"/v1/frames/{frame['id']}/readers/bob", cookies=auth_cookie("alice"))
    assert await active_ids(client, "bob") == []


async def test_reconcile_on_reader_add_narrowing_internal(client):
    # Adding the first reader to an internal frame narrows it from whole-tenant
    # to listed-only, pruning a previously-eligible holder.
    frame = await create_frame(client, body={"visibility": "internal"})
    await publish(client, frame["id"])
    await set_active(client, "bob", frame["id"])
    assert await active_ids(client, "bob") == [frame["id"]]

    # Restrict to carol only; bob (no longer eligible) is reconciled out.
    await client.post(
        f"/v1/frames/{frame['id']}/readers",
        cookies=auth_cookie("alice"),
        json={"email": "carol"},
    )
    assert await active_ids(client, "bob") == []


async def test_reconcile_on_owner_removal(client):
    frame = await create_frame(client)
    await client.post(
        f"/v1/frames/{frame['id']}/owners",
        cookies=auth_cookie("alice"),
        json={"email": "bob"},
    )
    await set_active(client, "bob", frame["id"])
    assert await active_ids(client, "bob") == [frame["id"]]

    # Removing bob as owner of a private, unpublished frame drops it from his active set.
    await client.delete(f"/v1/frames/{frame['id']}/owners/bob", cookies=auth_cookie("alice"))
    assert await active_ids(client, "bob") == []


async def test_owner_keeps_active_frame_after_unpublish(client):
    frame = await create_shared_frame(client)
    await set_active(client, "alice", frame["id"])

    await client.post(f"/v1/frames/{frame['id']}/unpublish", cookies=auth_cookie("alice"))
    # The owner still reads it, so reconciliation must not prune it.
    assert await active_ids(client, "alice") == [frame["id"]]


async def test_visibility_and_published_list_filters(client):
    private_frame = await create_frame(client, name="Private One")
    internal_frame = await create_shared_frame(client, name="Internal One")

    internal_only = await client.get("/v1/frames?visibility=internal", cookies=auth_cookie("alice"))
    assert {item["id"] for item in internal_only.json()} == {internal_frame["id"]}

    published_only = await client.get("/v1/frames?published=true", cookies=auth_cookie("alice"))
    assert {item["id"] for item in published_only.json()} == {internal_frame["id"]}

    unpublished_only = await client.get("/v1/frames?published=false", cookies=auth_cookie("alice"))
    assert {item["id"] for item in unpublished_only.json()} == {private_frame["id"]}


async def test_legacy_metadata_migration_over_api(client, config):
    # Write a legacy metadata.json that predates owners/visibility/published.
    frame_id = "a" * 32
    frame_dir = Path(config.storage.frames_path) / frame_id
    frame_dir.mkdir(parents=True)
    (frame_dir / "body.md").write_text("legacy body", encoding="utf-8")
    (frame_dir / "metadata.json").write_text(
        json.dumps(
            {
                "id": frame_id,
                "org_id": "org-a",
                "workspace_id": "workspace-a",
                "name": "Legacy Frame",
                "owner": "alice",
                "tags": [],
                "token_estimate": 3,
                "suggestions": [],
                "created_at": "2026-06-01T00:00:00Z",
                "updated_at": "2026-06-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    detail = await client.get(f"/v1/frames/{frame_id}", cookies=auth_cookie("alice"))
    assert detail.status_code == 200
    body = detail.json()
    assert body["owners"] == ["alice"]
    assert body["created_by"] == "alice"
    assert body["visibility"] == "private"
    assert body["published"] is False
    assert body["readers"] == []
    assert body["group_ids"] == []

    # Migrated frames are owner-only until published.
    assert (await client.get(f"/v1/frames/{frame_id}", cookies=auth_cookie("bob"))).status_code == 404


async def test_legacy_internal_with_readers_does_not_widen(client, config):
    # A record written under the earlier "readers restrict internal" semantics
    # (published internal + readers=[bob]) must NOT become whole-tenant readable.
    # normalize_metadata coerces it to private on read, so only the listed reader
    # (and owners) can see it.
    frame_id = "b" * 32
    frame_dir = Path(config.storage.frames_path) / frame_id
    frame_dir.mkdir(parents=True)
    (frame_dir / "body.md").write_text("legacy body", encoding="utf-8")
    (frame_dir / "metadata.json").write_text(
        json.dumps(
            {
                "id": frame_id,
                "org_id": "org-a",
                "workspace_id": "workspace-a",
                "name": "Legacy Restricted",
                "created_by": "alice",
                "owners": ["alice"],
                "visibility": "internal",
                "published": True,
                "readers": ["bob"],
                "tags": [],
                "token_estimate": 3,
                "suggestions": [],
                "created_at": "2026-06-01T00:00:00Z",
                "updated_at": "2026-06-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    # Owner reads it and sees the repaired (private) shape.
    owner_view = await client.get(f"/v1/frames/{frame_id}", cookies=auth_cookie("alice"))
    assert owner_view.status_code == 200
    assert owner_view.json()["visibility"] == "private"
    assert owner_view.json()["readers"] == ["bob"]

    # The listed reader can read; a different same-tenant user CANNOT (no widening).
    assert (await client.get(f"/v1/frames/{frame_id}", cookies=auth_cookie("bob"))).status_code == 200
    assert (await client.get(f"/v1/frames/{frame_id}", cookies=auth_cookie("carol"))).status_code == 404


async def test_set_active_frames_requires_readable_frame(client):
    # Unpublished, private frame: a non-owner cannot pin it (404, never 200).
    private_frame = await create_frame(client)
    rejected = await client.put(
        "/v1/active-frames",
        cookies=auth_cookie("bob"),
        json={"frame_ids": [private_frame["id"]]},
    )
    assert rejected.status_code == 404
    assert await active_ids(client, "bob") == []

    # Published but private: still owners-only, so a non-owner cannot pin it.
    published_private = await create_frame(client)
    await publish(client, published_private["id"])
    rejected_published = await client.put(
        "/v1/active-frames",
        cookies=auth_cookie("bob"),
        json={"frame_ids": [published_private["id"]]},
    )
    assert rejected_published.status_code == 404
    assert await active_ids(client, "bob") == []

    # Published internal restricted to [carol]: bob is excluded and cannot pin it.
    restricted = await create_frame(client, body={"visibility": "internal"})
    await publish(client, restricted["id"])
    await client.put(
        f"/v1/frames/{restricted['id']}/readers",
        cookies=auth_cookie("alice"),
        json={"readers": ["carol"]},
    )
    rejected_restricted = await client.put(
        "/v1/active-frames",
        cookies=auth_cookie("bob"),
        json={"frame_ids": [restricted["id"]]},
    )
    assert rejected_restricted.status_code == 404

    # Adding bob to the reader list makes it readable, hence pinnable.
    await client.post(
        f"/v1/frames/{restricted['id']}/readers",
        cookies=auth_cookie("alice"),
        json={"email": "bob"},
    )
    accepted = await client.put(
        "/v1/active-frames",
        cookies=auth_cookie("bob"),
        json={"frame_ids": [restricted["id"]]},
    )
    assert accepted.status_code == 200
    assert await active_ids(client, "bob") == [restricted["id"]]


async def test_public_frame_readable_cross_tenant(client):
    frame = await create_frame(client, body={"visibility": "public"})

    outsider = auth_cookie("zoe", org="org-z", workspace="workspace-z")
    # Unpublished public is still owner-only.
    assert (await client.get(f"/v1/frames/{frame['id']}", cookies=outsider)).status_code == 404

    await publish(client, frame["id"])
    # Published public is readable by any authenticated user in ANY tenant.
    assert (await client.get(f"/v1/frames/{frame['id']}", cookies=outsider)).status_code == 200
    # ...but cross-tenant public frames are NOT listed (discovery stays scoped).
    assert (await client.get("/v1/frames", cookies=outsider)).json() == []


async def test_reconcile_prunes_cross_tenant_holder_on_narrowing(client):
    # Exercises the GLOBAL active-holder lookup: a public frame can be pinned by a
    # user in another tenant, and narrowing it must reach that cross-tenant holder.
    frame = await create_frame(client, body={"visibility": "public"})
    await publish(client, frame["id"])

    outsider = auth_cookie("zoe", org="org-z", workspace="workspace-z")
    pinned = await client.put("/v1/active-frames", cookies=outsider, json={"frame_ids": [frame["id"]]})
    assert pinned.status_code == 200
    assert (await client.get("/v1/active-frames", cookies=outsider)).json()["frame_ids"] == [frame["id"]]

    # public -> internal narrows the audience; the cross-tenant holder loses access.
    narrowed = await client.put(
        f"/v1/frames/{frame['id']}",
        cookies=auth_cookie("alice"),
        json={"name": "Team Frame", "tags": ["team"], "body": "# Body", "visibility": "internal"},
    )
    assert narrowed.status_code == 200
    assert (await client.get("/v1/active-frames", cookies=outsider)).json()["frame_ids"] == []


async def test_owner_cannot_manage_frame_cross_tenant(client):
    # `public` grants cross-tenant READ by id, never cross-tenant management.
    frame = await create_frame(client, body={"visibility": "public"})
    await publish(client, frame["id"])
    frame_id = frame["id"]

    # Same owner identity, but authenticated in a foreign tenant context.
    cross = auth_cookie("alice", org="org-z", workspace="workspace-z")

    # Reads succeed (public)...
    assert (await client.get(f"/v1/frames/{frame_id}", cookies=cross)).status_code == 200

    # ...but every management verb is refused with 403 from the foreign tenant.
    put = await client.put(
        f"/v1/frames/{frame_id}",
        cookies=cross,
        json={"name": "Hijacked", "tags": [], "body": "x", "visibility": "public"},
    )
    assert put.status_code == 403
    assert (await client.delete(f"/v1/frames/{frame_id}", cookies=cross)).status_code == 403
    assert (
        await client.post(f"/v1/frames/{frame_id}/owners", cookies=cross, json={"email": "mallory"})
    ).status_code == 403
    assert (await client.post(f"/v1/frames/{frame_id}/unpublish", cookies=cross)).status_code == 403

    # The real owner, operating in the frame's own tenant, still manages it.
    owned = await client.put(
        f"/v1/frames/{frame_id}",
        cookies=auth_cookie("alice"),
        json={"name": "Renamed", "tags": [], "body": "x", "visibility": "public"},
    )
    assert owned.status_code == 200
    assert owned.json()["name"] == "Renamed"


async def test_corrupt_metadata_returns_structured_500(client, tmp_path):
    """A corrupt sidecar surfaces as a structured 500, never a 404.

    The object exists but is unreadable; mapping it to 404 would tell the client
    the frame is gone. The codec's FrameDecodeError carries the distinction.
    """

    frame = await create_frame(client)
    metadata_path = tmp_path / "frames" / frame["id"] / "metadata.json"
    metadata_path.write_text("{not valid json", encoding="utf-8")

    response = await client.get(f"/v1/frames/{frame['id']}", cookies=auth_cookie("alice"))

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "frame_decode_error"


async def test_corrupt_frame_500_is_logged_with_the_frame_id_and_cause(client, tmp_path, caplog):
    """The 500 handler must leave a server-side record of what failed to decode.

    Before the typed error, Starlette logged the raw traceback; a registered
    handler suppresses that, so it must log the frame id and cause itself.
    """

    frame = await create_frame(client)
    (tmp_path / "frames" / frame["id"] / "metadata.json").write_text("{not valid json", encoding="utf-8")

    with caplog.at_level(logging.ERROR):
        response = await client.get(f"/v1/frames/{frame['id']}", cookies=auth_cookie("alice"))

    assert response.status_code == 500
    assert response.json()["error"]["details"] == {"frame_id": frame["id"]}
    errors = [r for r in caplog.records if r.levelno == logging.ERROR and frame["id"] in r.getMessage()]
    assert len(errors) == 1
    assert isinstance(errors[0].exc_info[1].__cause__, json.JSONDecodeError)


async def test_an_already_active_corrupt_frame_does_not_block_toggles(client, tmp_path):
    """The UI resends the whole set on every toggle; a corrupt active frame must not lock it.

    Frames already in the stored set were checked when they were added, so a
    decode failure on one of them is tolerated: it stays in the set (nothing
    silently lost, and it is active again once repaired) while the toggle
    applies. Unticking it needs no read at all.
    """

    a = await create_frame(client)
    b = await create_frame(client)
    c = await create_frame(client)
    first = await client.put(
        "/v1/active-frames", cookies=auth_cookie("alice"), json={"frame_ids": [a["id"], b["id"]]}
    )
    assert first.status_code == 200
    metadata_path = tmp_path / "frames" / b["id"] / "metadata.json"
    healthy = metadata_path.read_bytes()
    metadata_path.write_text("{not valid json", encoding="utf-8")

    toggled = await client.put(
        "/v1/active-frames",
        cookies=auth_cookie("alice"),
        json={"frame_ids": [a["id"], b["id"], c["id"]]},
    )
    assert toggled.status_code == 200
    assert toggled.json()["frame_ids"] == [a["id"], b["id"], c["id"]]

    # Once repaired, b is simply active again; no re-adding needed.
    metadata_path.write_bytes(healthy)
    stored = await client.get("/v1/active-frames", cookies=auth_cookie("alice"))
    assert stored.json()["frame_ids"] == [a["id"], b["id"], c["id"]]

    unticked = await client.put(
        "/v1/active-frames", cookies=auth_cookie("alice"), json={"frame_ids": [a["id"], c["id"]]}
    )
    assert unticked.json()["frame_ids"] == [a["id"], c["id"]]


async def test_adding_a_corrupt_frame_to_the_active_set_names_it_in_the_500(client, tmp_path):
    """A frame not yet active has never been checked, so a corrupt one fails loudly."""

    a = await create_frame(client)
    b = await create_frame(client)
    first = await client.put("/v1/active-frames", cookies=auth_cookie("alice"), json={"frame_ids": [a["id"]]})
    assert first.status_code == 200
    (tmp_path / "frames" / b["id"] / "metadata.json").write_text("{not valid json", encoding="utf-8")

    added = await client.put(
        "/v1/active-frames",
        cookies=auth_cookie("alice"),
        json={"frame_ids": [a["id"], b["id"]]},
    )

    assert added.status_code == 500
    assert added.json()["error"] == {
        "code": "frame_decode_error",
        "message": "Stored frame could not be decoded",
        "details": {"frame_id": b["id"]},
    }
    stored = await client.get("/v1/active-frames", cookies=auth_cookie("alice"))
    assert stored.json()["frame_ids"] == [a["id"]]
