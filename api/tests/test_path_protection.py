"""Tests for the per-path protection map."""

from __future__ import annotations

import base64
import json

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route

from collab_hub_api.config import Config, PathRule, recommended_path_rules
from collab_hub_api.core import make_app
from collab_hub_api.path_protection import PathProtectionMiddleware, request_path, resolve_access


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


@pytest.mark.parametrize("root_path", ["/", "/w/", "/w//"])
def test_a_root_path_ending_in_a_slash_is_refused_at_load(root_path):
    # The router strips root_path only on a segment boundary, so "/w/" is never
    # stripped from /w/metrics: every rule would go unmatched, which under
    # enforcement is a 401 on every request, the probes included. Both spellings
    # also turn the pages' "{root_path}/web" links into "//web". A value with no
    # working reading is a misconfiguration to stop on, not one to resolve.
    with pytest.raises(ValidationError, match="root_path must not end with"):
        Config.model_validate({"server": {"root_path": root_path}})


@pytest.mark.parametrize("root_path", ["w", "a/b"])
def test_a_root_path_without_a_leading_slash_is_refused_at_load(root_path):
    # A request path always starts with a slash, so "w" is never a prefix of
    # one: nothing is stripped and every route 404s.
    with pytest.raises(ValidationError, match="root_path must start with"):
        Config.model_validate({"server": {"root_path": root_path}})


@pytest.mark.parametrize("root_path", ["", "/w", "/a/b"])
def test_a_root_path_prefix_is_accepted(root_path):
    assert Config.model_validate({"server": {"root_path": root_path}}).server.root_path == root_path


# --- the resolved path is the routed path (issue #69) --------------------
#
# Each case is (root_path, request path, how the app is reached). The path the
# protection map resolves must be exactly the path the router dispatches on;
# any difference is a request the map judges as one path while a handler
# serves it as another. "direct" hands the root_path to the app in the scope,
# as a server started with --root-path does; "mounted" reaches the app through
# a real Starlette Mount, which sets root_path itself.

ROUTED_PATH_CASES = [
    pytest.param("", "/metrics", "direct", id="empty-root-path"),
    # The shape that bypassed the web guard: a raw startswith strip turns
    # /metrics into "metrics", which no rule matches.
    pytest.param("/", "/metrics", "direct", id="slash-root-path"),
    pytest.param("/w", "/w/metrics", "direct", id="prefix-not-stripped-by-proxy"),
    pytest.param("/w", "/metrics", "direct", id="prefix-stripped-by-proxy"),
    # Not inside /w at all: a character-wise strip yields "x/metrics".
    pytest.param("/w", "/wx/metrics", "direct", id="prefix-lookalike-segment"),
    pytest.param("/w", "/w/", "direct", id="prefix-with-trailing-slash-request"),
    # ServerConfig refuses a root_path ending in a slash, so these two scopes
    # cannot come from configuration. They stay because the map must agree
    # with the router on any scope: the router does not strip "/w/" from
    # /w/metrics (the next character is not a separator), so neither may it.
    pytest.param("/w/", "/w/metrics", "direct", id="trailing-slash-root-path"),
    pytest.param("/w/", "/w//metrics", "direct", id="trailing-slash-root-path-double-slash"),
    pytest.param("/prefix", "/prefix/metrics", "mounted", id="mounted-prefix"),
    pytest.param("/prefix", "/prefix/", "mounted", id="mounted-prefix-root"),
]


def _routing_probe(seen: list[str]):
    """An app whose one route echoes the path the router matched it on.

    The route pattern captures everything after the leading slash, so the echo
    is the router's own dispatch path, computed by its matching and not by any
    helper this test could share with the code under test. The probe
    middleware records what ``request_path`` resolves for the same scope.
    """

    class Probe:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http":
                seen.append(request_path(Request(scope)))
            await self.app(scope, receive, send)

    async def echo(request: Request) -> PlainTextResponse:
        return PlainTextResponse("/" + request.path_params["rest"])

    return Starlette(routes=[Route("/{rest:path}", echo)], middleware=[Middleware(Probe)])


def _reach(app, root_path: str, how: str) -> AsyncClient:
    if how == "mounted":
        app = Starlette(routes=[Mount(root_path, app=app)])
        root_path = ""
    return AsyncClient(transport=ASGITransport(app=app, root_path=root_path), base_url="http://test")


@pytest.mark.parametrize(("root_path", "path", "how"), ROUTED_PATH_CASES)
async def test_the_protection_map_resolves_the_path_the_router_routes(root_path, path, how):
    seen: list[str] = []
    async with _reach(_routing_probe(seen), root_path, how) as client:
        response = await client.get(path)
    assert response.status_code == 200, "every case must actually be routed to the handler"
    assert seen == [response.text]


@pytest.mark.parametrize(("root_path", "path"), [("/w", "/w"), ("/", "/")])
async def test_a_request_for_the_bare_root_path_resolves_to_no_route(root_path, path):
    # Starlette routes a request for exactly the root_path as the empty path,
    # which no route matches; it answers with a slash redirect rather than
    # serving anything. The map sees the same empty path, not "/". (With
    # root_path="/" that includes a request for "/" itself.)
    seen: list[str] = []
    async with _reach(_routing_probe(seen), root_path, "direct") as client:
        response = await client.get(path)
    assert response.status_code == 307
    assert seen == [""]


def _refuse(_request: Request) -> None:
    raise HTTPException(status_code=401, detail="Authentication required")


@pytest.mark.parametrize(
    ("root_path", "path", "how"),
    [*ROUTED_PATH_CASES, pytest.param("/", "/", "direct", id="slash-root-path-at-root")],
)
async def test_a_protected_entry_is_never_served_anonymously_whatever_the_root_path(root_path, path, how):
    # Public default, one authenticated entry: the configuration under which a
    # mis-resolved path is a bypass rather than a spurious 401. The catch-all
    # route reports which path it served; if that is the protected one, the
    # middleware must have refused the request first.
    async def echo(request: Request) -> PlainTextResponse:
        return PlainTextResponse("/" + request.path_params["rest"])

    app = Starlette(
        routes=[Route("/{rest:path}", echo)],
        middleware=[
            Middleware(
                PathProtectionMiddleware,
                rules=[
                    PathRule(path="/metrics", match="exact", access="authenticated"),
                    PathRule(path="/", match="exact", access="authenticated"),
                ],
                default_access="public",
                authenticate=_refuse,
            )
        ],
    )
    async with _reach(app, root_path, how) as client:
        response = await client.get(path)
    if response.status_code == 200:
        assert response.text not in {"/metrics", "/"}, f"{response.text} was served anonymously"
    else:
        # 401 from the middleware, or the router's slash redirect for a
        # request that resolves to no route at all.
        assert response.status_code in {401, 307}


@pytest.mark.parametrize("root_path", ["/", "/w"])
async def test_metrics_stays_protected_under_a_root_path_end_to_end(tmp_path, monkeypatch, root_path):
    # The real app, with the permissive default and a protected /metrics.
    # Before issue #69, root_path="/" reduced /metrics to "metrics", which fell
    # to the public default: an anonymous 200 from the metrics endpoint.
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_IDTOKEN_ALLOW_UNSIGNED", "true")
    security = {
        "default_access": "public",
        "paths": [{"path": "/metrics", "match": "exact", "access": "authenticated"}],
    }
    app = make_app(Config.parse(base_values(tmp_path, security)))
    prefix = root_path.rstrip("/")
    transport = ASGITransport(app=app, root_path=root_path)
    async with app.router.lifespan_context(app), AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get(f"{prefix}/metrics")).status_code == 401
        served = await client.get(f"{prefix}/metrics", cookies=auth_cookie())
        assert served.status_code == 200
        assert "frames_server_http_requests_total" in served.text


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
    # ask for it serves / and /metrics exactly as it did before the map existed.
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
