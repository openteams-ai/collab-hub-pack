"""``GET /v1/auth/cli`` and ``GET /v1/me``: how the collab-hub CLI signs in, and who the hub says is calling."""

from __future__ import annotations

import base64
import json

from httpx import ASGITransport, AsyncClient

from collab_hub_api.config import Config, recommended_path_rules
from collab_hub_api.core import make_app


def _jwt(payload: dict) -> str:
    def encode(part: dict) -> str:
        raw = json.dumps(part, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode(payload)}."


def _cookie(user: str = "alice", **claims) -> dict[str, str]:
    return {"IdToken-test": _jwt({"preferred_username": user, "org_id": "org-a", "workspace_id": "ws-a", **claims})}


# --- GET /v1/auth/cli ---------------------------------------------------------------------


async def test_the_cli_config_names_the_issuer_and_the_client_without_credentials(client, monkeypatch):
    monkeypatch.setenv("FRAMES_BEARER_ISSUER", "https://id.example.org/realms/nebari/")
    monkeypatch.setenv("COLLAB_HUB_CLI_CLIENT_ID", "hub-cli")
    response = await client.get("/v1/auth/cli")
    assert response.status_code == 200
    assert response.json() == {
        "issuer": "https://id.example.org/realms/nebari",
        "client_id": "hub-cli",
        "dev_auth": False,
    }


async def test_the_cli_config_defaults_the_client_and_says_when_the_hub_runs_dev_auth(dev_client, monkeypatch):
    monkeypatch.delenv("FRAMES_BEARER_ISSUER", raising=False)
    monkeypatch.delenv("COLLAB_HUB_CLI_CLIENT_ID", raising=False)
    assert (await dev_client.get("/v1/auth/cli")).json() == {
        "issuer": None,
        "client_id": "apollo-desktop",
        "dev_auth": True,
    }


async def test_a_hardened_hub_answers_the_cli_config_but_not_me_without_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_IDTOKEN_ALLOW_UNSIGNED", "true")
    config = Config.parse(
        {
            "storage": {"frames_path": str(tmp_path / "frames")},
            "frames": {
                "active_state": {"backend": "memory"},
                "history": {"backend": "memory"},
                "usage": {"backend": "memory"},
                "mcp_session_manager_enabled": False,
            },
            "tasks": {"backend": "memory"},
            "security": {
                "paths": [rule.model_dump() for rule in recommended_path_rules()],
                "default_access": "authenticated",
            },
        }
    )
    app = make_app(config)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            assert (await http.get("/v1/auth/cli")).status_code == 200
            assert (await http.get("/v1/me")).status_code == 401
            # Exact: the public entry opens nothing below it.
            assert (await http.get("/v1/auth/cli/other")).status_code == 401


# --- GET /v1/me ---------------------------------------------------------------------------


async def test_me_reports_a_verified_caller_as_the_hub_resolved_them(client):
    response = await client.get("/v1/me", cookies=_cookie("alice", name="Alice A", email="alice@example.org"))
    assert response.status_code == 200
    me = response.json()
    assert me["user"] == "alice" and me["org_id"] == "org-a" and me["workspace_id"] == "ws-a"
    assert me["name"] == "Alice A" and me["email"] == "alice@example.org"
    assert me["authenticated_by"] == "token"


async def test_me_says_when_the_dev_shortcut_answered(dev_client):
    me = (await dev_client.get("/v1/me")).json()
    assert me["user"] == "dev-user" and me["org_id"] == "dev-org"
    assert me["authenticated_by"] == "dev"


async def test_a_rejected_credential_is_a_401_even_where_dev_auth_would_answer(dev_client):
    # A client that sent a credential meant to be someone: never fall back to the dev user for it.
    response = await dev_client.get("/v1/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert response.status_code == 401


async def test_a_header_the_hub_does_not_read_is_reported_as_the_dev_shortcut(dev_client):
    # get_auth_context ignores these and answers as the dev user, so /v1/me must not call that a token.
    for header in ("Basic Zm9vOmJhcg==", "Bearer ", "Bearer"):
        me = (await dev_client.get("/v1/me", headers={"Authorization": header})).json()
        assert me["user"] == "dev-user" and me["authenticated_by"] == "dev", header


async def test_me_requires_a_caller(client):
    assert (await client.get("/v1/me")).status_code == 401
