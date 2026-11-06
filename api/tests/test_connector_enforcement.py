"""A switched-off connector is actually off, not merely labelled off.

The switch would be theatre if it only changed what the admin screen printed.
What makes it real is that ``get_connectors_config`` -- the single dependency
every connector route already takes -- hands those routes a configuration with
the disabled connector's credentials blanked, so it is indistinguishable from
one that was never set up.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from contextlib import asynccontextmanager

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from collab_hub_api.config import Config, ConnectorsConfig
from collab_hub_api.core import make_app
from collab_hub_api.frames.connector_state import CONNECTOR_KEYS
from collab_hub_api.routers.connectors import get_connectors_config as _get_connectors_config
from collab_hub_api.routers.connectors import router as connectors_router
from collab_hub_api.routers.connectors import switched_off_connectors


class FakeState:
    def __init__(self, connectors, store=None):
        self.connectors_config = connectors
        if store is not None:
            self.connector_store = store


class FakeApp:
    def __init__(self, state):
        self.state = state


class FakeRequest:
    def __init__(self, state):
        self.app = FakeApp(state)


class Store:
    def __init__(self, disabled=(), raises=False):
        self._disabled = set(disabled)
        self._raises = raises

    def disabled(self):
        if self._raises:
            raise RuntimeError("database is unreachable")
        return set(self._disabled)


def get_connectors_config(request):
    """Resolve the dependency as a request would: switch state first."""

    return _get_connectors_config(request, switched_off_connectors(request))


def configured() -> ConnectorsConfig:
    return ConnectorsConfig.model_validate(
        {
            "slack": {"broker_token_url": "https://broker.example.com/token"},
            "github": {"static_access_token": "gh-not-a-real-token"},
        }
    )


def test_a_disabled_connector_reaches_routes_with_no_credentials():
    request = FakeRequest(FakeState(configured(), Store(disabled={"slack"})))

    served = get_connectors_config(request)

    assert served.slack.broker_token_url == ""
    assert served.github.static_access_token == "gh-not-a-real-token"


def test_an_enabled_connector_is_untouched():
    request = FakeRequest(FakeState(configured(), Store()))

    served = get_connectors_config(request)

    assert served.slack.broker_token_url == "https://broker.example.com/token"


def test_a_deployment_with_no_switch_store_behaves_exactly_as_before():
    request = FakeRequest(FakeState(configured()))

    served = get_connectors_config(request)

    assert served is request.app.state.connectors_config


def test_an_unreachable_switch_store_disables_nothing():
    """Failing closed here would take every connector down over a blip.

    The switch is an administrator's preference, not an authorization decision:
    losing sight of it briefly should leave connectors working, which is the
    state they were in a moment earlier.
    """

    request = FakeRequest(FakeState(configured(), Store(raises=True)))

    served = get_connectors_config(request)

    assert served.slack.broker_token_url == "https://broker.example.com/token"


@pytest.mark.parametrize("name", ["slack", "github"])
def test_switching_one_off_never_touches_another(name):
    request = FakeRequest(FakeState(configured(), Store(disabled={name})))

    served = get_connectors_config(request)
    others = {"slack", "github"} - {name}
    (other,) = others

    assert getattr(served, name).broker_token_url == ""
    assert getattr(served, name).static_access_token == ""
    section = getattr(served, other)
    assert section.broker_token_url or section.static_access_token


# --- what a client sees ----------------------------------------------------

STATUS_ROUTES = {
    "google": ["google-drive", "gmail", "google-calendar"],
    "slack": ["slack"],
    "github": ["github"],
}
ALL_STATUS_ROUTES = [route for routes in STATUS_ROUTES.values() for route in routes]


class SwitchableStore(Store):
    def set_enabled(self, connector, enabled):
        (self._disabled.discard if enabled else self._disabled.add)(connector)


def _auth_header() -> dict[str, str]:
    def encode(part: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(part).encode()).decode().rstrip("=")

    claims = {"preferred_username": "alice", "org_id": "org-a", "workspace_id": "workspace-a"}
    return {"Authorization": f"Bearer {encode({'alg': 'none'})}.{encode(claims)}."}


@pytest.fixture
def hub(tmp_path, monkeypatch):
    """An app factory; every upstream provider call fails the same way each time."""

    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_BEARER_ALLOW_UNSIGNED", "true")
    original_async_client = httpx.AsyncClient

    def mock_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(lambda request: httpx.Response(500))
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)

    def build(store=None, *, token="not-a-real-token"):
        app = make_app(
            Config.parse(
                {
                    "storage": {"frames_path": str(tmp_path / "frames")},
                    "frames": {"active_state": {"backend": "memory"}, "mcp_session_manager_enabled": False},
                    "connectors": {name: {"static_access_token": token} for name in STATUS_ROUTES},
                }
            )
        )
        app.extra["connector_store"] = store
        return app

    return build


@asynccontextmanager
async def _client(app):
    async with app.router.lifespan_context(app):
        # After startup, which assigns the deployment's own (absent) store.
        if app.extra["connector_store"] is not None:
            app.state.connector_store = app.extra["connector_store"]
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client


async def _get(app, path):
    async with _client(app) as client:
        return await client.get(path, headers=_auth_header())


async def _statuses(app) -> dict[str, tuple[int, dict]]:
    found = {}
    for route in ALL_STATUS_ROUTES:
        response = await _get(app, f"/v1/connectors/{route}/status")
        found[route] = (response.status_code, response.json())
    return found


@pytest.mark.parametrize("name", sorted(STATUS_ROUTES))
async def test_status_of_a_switched_off_connector_is_not_found(hub, name):
    """``not_connected`` would invite the user to connect something they cannot."""

    found = await _statuses(hub(Store(disabled={name})))

    for route in ALL_STATUS_ROUTES:
        code, body = found[route]
        if route in STATUS_ROUTES[name]:
            assert code == 404
            assert body == {"detail": f"The {name} connector is switched off on this hub"}
        else:
            assert code == 200


@pytest.mark.parametrize("name", sorted(STATUS_ROUTES))
async def test_switching_back_on_restores_the_status_it_had(hub, name):
    store = SwitchableStore()
    app = hub(store)
    before = await _statuses(app)

    store.set_enabled(name, False)
    assert {code for code, _ in (await _statuses(app)).values()} == {200, 404}
    store.set_enabled(name, True)

    assert await _statuses(app) == before
    assert {code for code, _ in before.values()} == {200}


@pytest.mark.parametrize("store", [None, Store(), Store(raises=True)], ids=["no-store", "nothing-off", "unreachable"])
async def test_an_unconfigured_connector_nobody_switched_still_reads_not_connected(hub, store):
    """The 404 is for a deliberate switch, never for missing credentials."""

    found = await _statuses(hub(store, token=""))

    assert {code for code, _ in found.values()} == {200}
    assert {body["state"] for _, body in found.values()} == {"not_connected"}


async def test_the_connector_list_leaves_out_what_is_switched_off(hub):
    listed = await _get(hub(Store(disabled={"google"})), "/v1/connectors")
    everything = await _get(hub(Store()), "/v1/connectors")

    assert [item["id"] for item in listed.json()] == ["slack", "github"]
    assert [item["id"] for item in everything.json()] == ALL_STATUS_ROUTES


async def test_other_routes_of_a_switched_off_connector_refuse_as_unconfigured(hub):
    """Only status changed: a read still answers what an unconfigured one does."""

    app = hub(Store(disabled={"slack"}))

    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/slack/search", headers=_auth_header(), json={"query": "anything", "limit": 5}
        )

    assert response.status_code == 409
    assert response.json() == {"detail": "Slack connector token broker is not configured"}


async def test_a_switched_off_status_is_told_apart_from_a_path_nothing_serves(hub, caplog):
    app = hub(Store(disabled={"slack"}))

    with caplog.at_level(logging.DEBUG, logger="frames_server.connectors"):
        unrouted = await _get(app, "/v1/connectors/slack/no-such-route")
        assert not [r for r in caplog.records if r.getMessage() == "connector_status_switched_off"]
        switched_off = await _get(app, "/v1/connectors/slack/status")

    assert unrouted.status_code == switched_off.status_code == 404
    assert unrouted.text != switched_off.text
    (record,) = [r for r in caplog.records if r.getMessage() == "connector_status_switched_off"]
    assert record.connector == "slack"
    # Clients poll status, so this must not add a line per request at the default level.
    assert record.levelno == logging.DEBUG


async def test_a_switched_off_status_still_asks_for_sign_in_first(hub):
    """Which connectors a hub has switched off is not told to someone who has not signed in."""

    app = hub(Store(disabled={"slack"}))

    async with _client(app) as client:
        switched_off = await client.get("/v1/connectors/slack/status")
        switched_on = await client.get("/v1/connectors/github/status")

    assert switched_off.status_code == switched_on.status_code == 401
    assert switched_off.json() == switched_on.json()


# Status routes of connectors that have no switch. A connector added here is a
# decision that its status may answer 200 while everything switchable is off.
_NOT_SWITCHABLE = {"notion"}


async def test_every_status_route_the_router_serves_honours_the_switch(hub):
    """The 404 is wired per route, so a new connector's status route could forget it.

    The routes are read from the router rather than listed here: a status route
    added later is checked without anyone remembering to add it to a test.
    """

    served = sorted(
        match.group(1)
        for route in connectors_router.routes
        if "GET" in getattr(route, "methods", ())
        and (match := re.fullmatch(r"/connectors/([^/{}]+)/status", route.path))
    )
    assert set(ALL_STATUS_ROUTES) | _NOT_SWITCHABLE == set(served), "STATUS_ROUTES in this file is out of date"

    app = hub(Store(disabled=set(CONNECTOR_KEYS)))
    answered = {}
    async with _client(app) as client:
        for connector in served:
            response = await client.get(f"/v1/connectors/{connector}/status", headers=_auth_header())
            answered[connector] = response.status_code

    assert answered == {connector: 200 if connector in _NOT_SWITCHABLE else 404 for connector in served}
