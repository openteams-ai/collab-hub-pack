"""Tests for the per-path protection map (issue #60)."""

from __future__ import annotations

import base64
import json

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from starlette.requests import Request

from collab_hub_api.config import Config, PathRule, recommended_path_rules
from collab_hub_api.core import make_app
from collab_hub_api.path_protection import request_path, resolve_access


def _jwt(payload: dict) -> str:
    def encode(part: dict) -> str:
        raw = json.dumps(part, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode(payload)}."


def auth_cookie(user: str = "alice") -> dict[str, str]:
    return {
        "IdToken-test": _jwt(
            {
                "preferred_username": user,
                "org_id": "org-a",
                "workspace_id": "workspace-a",
            }
        )
    }


def base_values(tmp_path, security: dict | None = None) -> dict:
    values: dict = {
        "storage": {"frames_path": str(tmp_path / "frames")},
        "frames": {
            "active_state": {"backend": "memory"},
            "history": {"backend": "memory"},
            "usage": {"backend": "memory"},
            "mcp_session_manager_enabled": False,
        },
        "tasks": {"backend": "memory"},
    }
    if security is not None:
        values["security"] = security
    return values


async def make_client(tmp_path, monkeypatch, security: dict | None = None):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_IDTOKEN_ALLOW_UNSIGNED", "true")
    config = Config.parse(base_values(tmp_path, security))
    app = make_app(config)
    return app, AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# --- matching semantics -------------------------------------------------


def test_recommended_map_protects_root_and_metrics_and_leaves_probes_public():
    rules = recommended_path_rules()
    assert resolve_access("/", rules, "authenticated") == "authenticated"
    assert resolve_access("/metrics", rules, "authenticated") == "authenticated"
    assert resolve_access("/health", rules, "authenticated") == "public"
    assert resolve_access("/health/db", rules, "authenticated") == "public"


def test_unmatched_paths_fall_back_to_the_default_access():
    # A route added without its own auth dependency must fail closed.
    assert resolve_access("/newly-added", recommended_path_rules(), "authenticated") == "authenticated"
    assert resolve_access("/newly-added", [], "public") == "public"


def test_exact_rule_beats_a_broader_prefix_rule():
    rules = [
        PathRule(path="/", match="prefix", access="public"),
        PathRule(path="/metrics", match="exact", access="authenticated"),
    ]
    assert resolve_access("/metrics", rules, "authenticated") == "authenticated"
    assert resolve_access("/anything", rules, "authenticated") == "public"


def test_longest_prefix_wins_and_matching_is_segment_aware():
    rules = [
        PathRule(path="/admin", match="prefix", access="authenticated"),
        PathRule(path="/admin/status", match="prefix", access="public"),
    ]
    assert resolve_access("/admin/users", rules, "authenticated") == "authenticated"
    assert resolve_access("/admin/status/live", rules, "authenticated") == "public"
    # /administration is not inside /admin.
    assert resolve_access("/administration", rules, "public") == "public"


def test_last_equally_specific_rule_wins_so_operators_can_append_overrides():
    rules = recommended_path_rules() + [PathRule(path="/metrics", match="exact", access="public")]
    assert resolve_access("/metrics", rules, "authenticated") == "public"


def test_rules_match_against_the_path_below_a_url_prefix():
    # server.root_path deployments: a proxy that does not strip the prefix
    # would otherwise leave every rule unmatched.
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/nexus/metrics",
        "root_path": "/nexus",
        "headers": [],
        "query_string": b"",
    }
    assert request_path(Request(scope)) == "/metrics"


def test_rule_paths_must_be_absolute():
    with pytest.raises(ValidationError):
        PathRule(path="metrics", match="exact", access="public")


def test_protection_map_is_configuration_not_code():
    # The map arrives as data; the future browser surface (issue #88) adds a
    # public page without a code change.
    config = Config.parse(
        {
            "security": {
                "paths": [
                    {"path": "/invite/accept", "match": "prefix", "access": "public"},
                ]
            }
        }
    )
    assert config.security.paths == [PathRule(path="/invite/accept", match="prefix", access="public")]


def test_protection_map_parses_from_a_json_environment_variable(monkeypatch):
    # This is how the Helm chart delivers the map.
    monkeypatch.setenv(
        "COLLAB_HUB_API__SECURITY__PATHS",
        json.dumps([{"path": "/metrics", "match": "exact", "access": "public"}]),
    )
    monkeypatch.setenv("COLLAB_HUB_API__SECURITY__DEFAULT_ACCESS", "authenticated")
    config = Config()
    assert config.security.paths == [PathRule(path="/metrics", match="exact", access="public")]


def test_unknown_access_level_is_rejected_rather_than_silently_allowed():
    with pytest.raises(ValidationError):
        Config.parse({"security": {"paths": [{"path": "/admin", "access": "operator"}]}})


# --- enforcement --------------------------------------------------------


def hardened() -> dict:
    """What the chart renders for standalone/ingress exposure."""

    return {
        "paths": [rule.model_dump() for rule in recommended_path_rules()],
        "default_access": "authenticated",
    }


async def test_an_unconfigured_server_keeps_its_previous_behavior(tmp_path, monkeypatch):
    # The upgrade case: protection is opted into, so a deployment that does not
    # ask for it serves / and /metrics exactly as it did before issue #60.
    app, client = await make_client(tmp_path, monkeypatch)
    async with app.router.lifespan_context(app), client:
        assert Config.parse(base_values(tmp_path)).security.default_access == "public"
        assert (await client.get("/metrics")).status_code == 200
        assert (await client.get("/")).status_code == 200
        # Route dependencies still protect the API, as they always did.
        assert (await client.get("/v1/frames")).status_code == 401


async def test_metrics_and_root_require_auth_when_hardened(tmp_path, monkeypatch):
    app, client = await make_client(tmp_path, monkeypatch, security=hardened())
    async with app.router.lifespan_context(app), client:
        assert (await client.get("/metrics")).status_code == 401
        assert (await client.get("/")).status_code == 401
        assert (await client.get("/metrics", cookies=auth_cookie())).status_code == 200
        assert (await client.get("/", cookies=auth_cookie())).status_code == 200


async def test_health_probes_stay_public_when_hardened(tmp_path, monkeypatch):
    # A hardened map that dropped these would stop the pod passing its probes.
    app, client = await make_client(tmp_path, monkeypatch, security=hardened())
    async with app.router.lifespan_context(app), client:
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/health/db")).status_code == 200


async def test_metrics_can_be_opened_for_an_in_cluster_scraper(tmp_path, monkeypatch):
    # security.metricsAccess=public in the chart: the rule is appended to the
    # hardened map, so opening it does not mean restating the map.
    security = hardened()
    security["paths"].append({"path": "/metrics", "match": "exact", "access": "public"})
    app, client = await make_client(tmp_path, monkeypatch, security=security)
    async with app.router.lifespan_context(app), client:
        response = await client.get("/metrics")
        assert response.status_code == 200
        assert "frames_server_http_requests_total" in response.text
        # Opening one path does not open the rest.
        assert (await client.get("/")).status_code == 401


async def test_api_paths_keep_their_error_envelope_when_the_middleware_rejects(tmp_path, monkeypatch):
    app, client = await make_client(tmp_path, monkeypatch, security=hardened())
    async with app.router.lifespan_context(app), client:
        response = await client.get("/v1/frames")
        assert response.status_code == 401
        assert response.json() == {"error": {"code": "unauthorized", "message": "Authentication required"}}


async def test_default_deny_covers_unrouted_paths_when_hardened(tmp_path, monkeypatch):
    app, client = await make_client(tmp_path, monkeypatch, security=hardened())
    async with app.router.lifespan_context(app), client:
        assert (await client.get("/not-a-route")).status_code == 401


async def test_a_public_default_still_honours_protected_entries(tmp_path, monkeypatch):
    app, client = await make_client(
        tmp_path,
        monkeypatch,
        security={
            "default_access": "public",
            "paths": [{"path": "/metrics", "match": "exact", "access": "authenticated"}],
        },
    )
    async with app.router.lifespan_context(app), client:
        assert (await client.get("/metrics")).status_code == 401
        # Routes keep their own dependencies even where the map is permissive.
        assert (await client.get("/v1/frames")).status_code == 401
        assert (await client.get("/")).status_code == 200


async def test_the_run_api_refuses_a_request_without_credentials_under_the_hardened_map(tmp_path, monkeypatch):
    # /v1/runs/** needs no entry of its own: the hardened default refuses it before any route runs,
    # the event stream included, and a request that carries credentials reaches the routes.
    app, client = await make_client(tmp_path, monkeypatch, security=hardened())
    async with app.router.lifespan_context(app), client:
        for method, path in (("GET", "/v1/runs"), ("POST", "/v1/runs"), ("GET", "/v1/runs/launchable"),
                             ("GET", "/v1/runs/run-000000000000/events"),
                             ("POST", "/v1/runs/run-000000000000/cancel")):
            assert (await client.request(method, path)).status_code == 401, (method, path)
        # Signed in, the request reaches the run API: this hub has no Track, which it says.
        assert (await client.get("/v1/runs", cookies=auth_cookie())).status_code == 503
