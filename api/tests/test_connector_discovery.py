"""``GET /v1/connectors`` is the list a client renders from.

A client that asks this route what connectors exist has no list of its own, so
the route has to be right about two things the per-connector status routes
never had to say: which connectors this deployment offers at all, and what a
user needs in order to see one and connect it.
"""

from __future__ import annotations

import asyncio
import base64
import json
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import HTTPException, status
from httpx import ASGITransport, AsyncClient

from collab_hub_api.config import Config, ConnectorsConfig
from collab_hub_api.connectors.catalog import CATALOG, connector_link, is_offered
from collab_hub_api.connectors.models import ConnectorSummary
from collab_hub_api.core import make_app
from collab_hub_api.routers import connectors as connectors_router

BROKER = "https://keycloak.test/realms/hub/broker/{alias}/token"
PROVIDERS = ("google", "slack", "github", "notion")
IDS_BY_PROVIDER = {
    "google": ["google-drive", "gmail", "google-calendar"],
    "slack": ["slack"],
    "github": ["github"],
    "notion": ["notion"],
}


class Store:
    def __init__(self, disabled=()):
        self._disabled = set(disabled)

    def disabled(self):
        return set(self._disabled)

    def set_enabled(self, connector, enabled):
        (self._disabled.discard if enabled else self._disabled.add)(connector)


def _auth_header() -> dict[str, str]:
    def encode(part: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(part).encode()).decode().rstrip("=")

    claims = {"preferred_username": "alice", "org_id": "org-a", "workspace_id": "workspace-a"}
    return {"Authorization": f"Bearer {encode({'alg': 'none'})}.{encode(claims)}."}


def _provider_api(request: httpx.Request) -> httpx.Response:
    """Every broker hands out a token, and every provider accepts it."""

    host, path = request.url.host, request.url.path
    if host == "keycloak.test":
        return httpx.Response(200, json={"access_token": "brokered-not-a-real-token"})
    if host == "api.github.com":
        if path == "/user":
            return httpx.Response(200, json={"login": "octo-alice"}, headers={"X-OAuth-Scopes": "repo, read:org"})
        return httpx.Response(200, json=[])
    if host == "api.notion.com":
        if path == "/v1/users/me":
            return httpx.Response(200, json={"bot": {"workspace_name": "Acme Wiki"}})
        return httpx.Response(200, json={"results": []})
    if host == "slack.com":
        return httpx.Response(200, json={"ok": True}, headers={"X-OAuth-Scopes": "search:read"})
    if host == "gmail.googleapis.com":
        return httpx.Response(200, json={"messages": []})
    if host == "www.googleapis.com":
        return httpx.Response(200, json={"items": []})
    return httpx.Response(500)


@pytest.fixture
def hub(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_BEARER_ALLOW_UNSIGNED", "true")
    original_async_client = httpx.AsyncClient

    def mock_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(_provider_api)
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)

    def build(connectors: dict, store=None):
        app = make_app(
            Config.parse(
                {
                    "storage": {"frames_path": str(tmp_path / "frames")},
                    "frames": {"active_state": {"backend": "memory"}, "mcp_session_manager_enabled": False},
                    "connectors": connectors,
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


async def _listed(app) -> list[dict]:
    async with _client(app) as client:
        response = await client.get("/v1/connectors", headers=_auth_header())
    assert response.status_code == 200, response.text
    return response.json()


def _brokered(*providers: str) -> dict:
    return {name: {"broker_token_url": BROKER.format(alias=name)} for name in providers}


# --- which connectors are listed -------------------------------------------


async def test_a_hub_with_nothing_configured_offers_nothing(hub):
    assert await _listed(hub({})) == []


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_only_a_configured_provider_is_listed(hub, provider):
    listed = await _listed(hub(_brokered(provider)))

    assert [item["id"] for item in listed] == IDS_BY_PROVIDER[provider]
    assert {item["provider"] for item in listed} == {provider}


async def test_everything_configured_is_listed_in_catalog_order(hub):
    listed = await _listed(hub(_brokered(*PROVIDERS)))

    assert [item["id"] for item in listed] == [descriptor.id for descriptor in CATALOG]


async def test_a_static_token_counts_as_configured(hub):
    listed = await _listed(hub({"github": {"static_access_token": "gh-not-a-real-token"}}))

    assert [item["id"] for item in listed] == ["github"]


@pytest.mark.parametrize("provider", ["google", "slack", "github"])
async def test_a_switched_off_provider_leaves_the_list_and_comes_back(hub, provider):
    store = Store()
    app = hub(_brokered(*PROVIDERS), store)
    before = await _listed(app)

    store.set_enabled(provider, False)
    without = await _listed(app)
    store.set_enabled(provider, True)

    assert [item["id"] for item in without] == [
        descriptor.id for descriptor in CATALOG if descriptor.provider != provider
    ]
    assert await _listed(app) == before


async def test_an_unlisted_connector_answers_its_own_status_route_by_why_it_is_unlisted(hub):
    """Never configured stays 200 ``not_connected``; switched off is 404. The list omits both."""

    app = hub(_brokered("slack", "github"), Store(disabled={"github"}))

    assert [item["id"] for item in await _listed(app)] == ["slack"]
    async with _client(app) as client:
        never_configured = await client.get("/v1/connectors/notion/status", headers=_auth_header())
        switched_off = await client.get("/v1/connectors/github/status", headers=_auth_header())

    assert never_configured.status_code == 200
    assert never_configured.json()["state"] == "not_connected"
    assert switched_off.status_code == 404
    assert switched_off.json() == {"detail": "The github connector is switched off on this hub"}


# --- what each entry carries -----------------------------------------------


async def test_every_entry_carries_what_a_client_renders(hub):
    listed = await _listed(hub(_brokered(*PROVIDERS)))
    by_id = {descriptor.id: descriptor for descriptor in CATALOG}

    assert len(listed) == len(CATALOG)
    for item in listed:
        descriptor = by_id[item["id"]]
        assert item["name"] == descriptor.name
        assert item["short_name"] == descriptor.short_name
        assert item["description"] == descriptor.description
        assert item["provider"] == descriptor.provider
        assert item["link"]["type"] == "identity_provider"
        assert item["state"] == "connected"
        assert item["connected"] is True


async def test_the_link_names_the_alias_in_the_configured_broker_url(hub):
    """A realm that called its identity provider something else is described as it is."""

    listed = await _listed(hub({"slack": {"broker_token_url": BROKER.format(alias="acme-slack")}}))

    assert listed[0]["link"] == {"type": "identity_provider", "alias": "acme-slack", "prompt": None}


async def test_google_connectors_ask_for_consent_again_and_the_others_do_not(hub):
    listed = await _listed(hub(_brokered(*PROVIDERS)))

    prompts = {item["id"]: item["link"]["prompt"] for item in listed}
    assert prompts == {
        "google-drive": "consent",
        "gmail": "consent",
        "google-calendar": "consent",
        "slack": None,
        "github": None,
        "notion": None,
    }


async def test_only_slack_carries_a_hint_to_read_before_connecting(hub):
    listed = await _listed(hub(_brokered(*PROVIDERS)))

    hints = {item["id"]: item["connect_hint"] for item in listed}
    assert hints.pop("slack").startswith("Before authorizing Slack, confirm the workspace")
    assert set(hints.values()) == {None}


@pytest.mark.parametrize("broker", ["", BROKER.format(alias="github")], ids=["token-only", "token-and-broker"])
async def test_a_static_token_leaves_the_user_nothing_to_link(hub, broker):
    """The token serves every caller, so it wins over a broker configured beside it."""

    section = {"static_access_token": "gh-not-a-real-token", "broker_token_url": broker}
    listed = await _listed(hub({"github": section}))

    assert listed[0]["link"] is None


async def test_the_linked_account_is_listed_where_the_connector_knows_it(hub):
    listed = {item["id"]: item for item in await _listed(hub(_brokered(*PROVIDERS)))}

    assert listed["github"]["account"] == "octo-alice"
    assert listed["notion"]["account"] == "Acme Wiki"
    assert listed["slack"]["account"] == ""


# --- one connector's trouble stays its own ----------------------------------


async def test_a_status_read_that_raises_lists_that_connector_as_unavailable(hub, monkeypatch, caplog):
    async def boom(request, config):
        raise RuntimeError("status helper bug")

    monkeypatch.setitem(connectors_router._STATUS_READERS, "slack", boom)

    listed = {item["id"]: item for item in await _listed(hub(_brokered(*PROVIDERS)))}

    assert list(listed) == [descriptor.id for descriptor in CATALOG]
    assert listed["slack"]["state"] == "unavailable"
    assert listed["slack"]["connected"] is False
    assert listed["slack"]["detail"] == "Slack status check failed."
    assert "status helper bug" not in json.dumps(listed)
    assert listed["github"]["state"] == "connected"
    (record,) = [r for r in caplog.records if r.getMessage() == "connector_list_status_failed"]
    assert record.connector == "slack"


async def test_a_reader_that_refuses_with_an_http_error_is_also_contained(hub, monkeypatch):
    async def refuse(request, config):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "broker is down")

    monkeypatch.setitem(connectors_router._STATUS_READERS, "github", refuse)

    listed = {item["id"]: item for item in await _listed(hub(_brokered(*PROVIDERS)))}

    assert listed["github"]["state"] == "unavailable"
    assert listed["slack"]["state"] == "connected"


async def test_status_reads_run_together_and_only_for_what_is_offered(hub, monkeypatch):
    """Sequential reads would deadlock here: each waits for the other to start."""

    started = {"slack": asyncio.Event(), "github": asyncio.Event()}
    asked = []

    def reader(own, other):
        async def read(request, config):
            asked.append(own)
            started[own].set()
            await asyncio.wait_for(started[other].wait(), timeout=5)
            return ConnectorSummary(id=own, name=own, connected=True, state="connected")

        return read

    async def never(request, config):
        asked.append("not offered")
        raise AssertionError("a connector that is not offered had its status read")

    monkeypatch.setitem(connectors_router._STATUS_READERS, "slack", reader("slack", "github"))
    monkeypatch.setitem(connectors_router._STATUS_READERS, "github", reader("github", "slack"))
    for unoffered in ("google-drive", "gmail", "google-calendar", "notion"):
        monkeypatch.setitem(connectors_router._STATUS_READERS, unoffered, never)

    listed = await _listed(hub(_brokered("slack", "github")))

    assert [item["state"] for item in listed] == ["connected", "connected"]
    assert sorted(asked) == ["github", "slack"]


async def test_a_broker_url_that_is_not_a_url_does_not_sink_the_list(hub):
    not_a_url = {"slack": {"broker_token_url": "https://[bad/broker/acme/token"}}
    listed = await _listed(hub({**not_a_url, **_brokered("github")}))

    assert [item["id"] for item in listed] == ["slack", "github"]
    assert listed[1]["link"]["alias"] == "github"


# --- the configuration-derived halves, directly -----------------------------


@pytest.mark.parametrize(
    ("url", "alias"),
    [
        ("https://kc.example.com/realms/hub/broker/google/token", "google"),
        ("https://kc.example.com/auth/realms/hub/broker/google-workspace/token/", "google-workspace"),
        ("https://kc.example.com/realms/broker/broker/notion_v2/token", "notion_v2"),
        ("https://kc.example.com/realms/hub/broker/Acme.Slack/token?x=/broker/other/token", "Acme.Slack"),
        ("https://kc.example.com/realms/hub/broker/acme%2Dslack/token", "acme-slack"),
        ("https://kc.example.com/realms/hub/broker/" + "a" * 64 + "/token", "a" * 64),
        # Not Keycloak-shaped: the URL does not name the alias, so the provider key stands in.
        ("https://tokens.example.com/slack", "slack"),
        ("https://kc.example.com/realms/hub/BROKER/acme/TOKEN", "slack"),
        # Nothing here depends on the rest of the URL being one a parser accepts.
        ("https://[bad/broker/acme/token", "acme"),
        ("not a url", "slack"),
    ],
)
def test_alias_is_read_from_the_broker_url_or_falls_back_to_the_provider(url, alias):
    slack = next(descriptor for descriptor in CATALOG if descriptor.id == "slack")
    section = ConnectorsConfig.model_validate({"slack": {"broker_token_url": url}}).slack

    assert connector_link(slack, section).alias == alias


@pytest.mark.parametrize(
    "alias",
    [
        "bad%20alias",
        "",
        "_leading",
        "a" * 65,
        "acme%2Fslack",
        "acme&kc_action=x",
        "%C3%A9cole",
        # A URL parser would drop these and report `acmeslack`.
        "acme\tslack",
        "acme\nslack",
        "acme\rslack",
        "acme slack",
    ],
)
def test_an_alias_a_client_would_not_link_is_no_link_rather_than_a_guess(alias):
    """Naming a different provider than the one configured would link the wrong account."""

    slack = next(descriptor for descriptor in CATALOG if descriptor.id == "slack")
    url = f"https://kc.example.com/realms/hub/broker/{alias}/token"
    section = ConnectorsConfig.model_validate({"slack": {"broker_token_url": url}}).slack

    assert connector_link(slack, section) is None


def test_every_catalog_entry_has_a_configuration_section_and_a_status_reader():
    config = ConnectorsConfig()

    for descriptor in CATALOG:
        assert not is_offered(getattr(config, descriptor.provider))
        assert descriptor.id in connectors_router._STATUS_READERS
    assert len({descriptor.id for descriptor in CATALOG}) == len(CATALOG) == len(connectors_router._STATUS_READERS)
