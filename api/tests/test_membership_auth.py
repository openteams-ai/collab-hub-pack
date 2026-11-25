"""Organization resolution at the auth choke point (issue #63).

Four things are pinned here, in this order:

1. **The switch.** ``FRAMES_AUTH_ORG_SOURCE`` parses exactly, its preconditions
   are enforced at startup, and ``claims`` deployments are untouched.
2. **The resolution itself.** ``(org_id, role)`` come from the one membership
   row, the workspace is a constant, and org claims in the token are ignored.
3. **Removal.** An ``active`` → ``removed`` row yields ``no_organization`` on
   the next request — the release-bar evidence for removal semantics — and does
   *not* delete explicit reader grants.
4. **Failing closed.** An unreachable or unconfigured organization backend is a
   503 on every authenticated surface, never a 401 and never
   ``no_organization``.
"""

from __future__ import annotations

import base64
import json
import os

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from starlette.testclient import TestClient

from collab_hub_api import config as config_module
from collab_hub_api.config import Config
from collab_hub_api.core import make_app
from collab_hub_api.frames import error_codes
from collab_hub_api.frames.auth import (
    WORKSPACE_DEFAULT,
    NoOrganizationError,
    auth_context_from_membership,
)
from collab_hub_api.frames.identity import IDENTITY_CLAIM_ENV
from collab_hub_api.frames.org_source import (
    DEFAULT_ORG_ENV,
    DEFAULT_WORKSPACE_ENV,
    ORG_SOURCE_ENV,
    enforce_membership_org_source_preconditions,
    org_source_resolves_membership,
)
from collab_hub_api.frames.orgs import (
    MEMBERSHIP_ACTIVE,
    MEMBERSHIP_REMOVED,
    ROLE_MEMBER,
    ROLE_OWNER,
    InMemoryOrgStore,
    OrgMembership,
    OrgsUnavailableError,
    UnavailableOrgStore,
)

psycopg = pytest.importorskip("psycopg")

ALICE = "a1b2c3d4-1111-4111-8111-abcdefabcdef"
BOB = "22222222-2222-4222-8222-b0b0b0b0b0b0"
CAROL = "33333333-3333-4333-8333-cacacacacaca"

ORG_ONE = "org-1111"
ORG_TWO = "org-2222"


def _jwt(payload: dict) -> str:
    def encode(part: dict) -> str:
        raw = json.dumps(part, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode(payload)}."


def cookies_for(sub: str, *, org: str | None = None, workspace: str | None = None) -> dict[str, str]:
    """An IdToken cookie for *sub*, optionally asserting a tenancy it must not get."""

    claims: dict[str, str] = {"sub": sub, "preferred_username": f"name-{sub[:4]}"}
    if org is not None:
        claims["org_id"] = org
    if workspace is not None:
        claims["workspace_id"] = workspace
    return {"IdToken-test": _jwt(claims)}


class RecordingOrgStore(InMemoryOrgStore):
    """In-memory membership that can count lookups and be made to fail.

    Installed by monkeypatching the class ``build_org_store`` instantiates, so
    the outer app and the mounted MCP app share the one instance exactly as
    they share the real store.
    """

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.error: Exception | None = None

    def get_membership(self, user_id: str) -> OrgMembership | None:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return super().get_membership(user_id)


def _config(tmp_path, **overrides) -> Config:
    frames = {
        "active_state": {"backend": "memory"},
        "history": {"backend": "memory"},
        "groups": {"backend": "memory"},
        "usage": {"backend": "memory"},
        "orgs": {"backend": "memory"},
        "mcp_session_manager_enabled": False,
    }
    frames.update(overrides.pop("frames", {}))
    payload = {
        "storage": {"frames_path": str(tmp_path / "frames")},
        "frames": frames,
        "tasks": {"backend": "memory"},
    }
    payload.update(overrides)
    return Config.parse(payload)


def _membership_env(monkeypatch) -> None:
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_IDTOKEN_ALLOW_UNSIGNED", "true")
    monkeypatch.setenv(IDENTITY_CLAIM_ENV, "sub")
    monkeypatch.setenv(ORG_SOURCE_ENV, "membership")
    monkeypatch.delenv(DEFAULT_ORG_ENV, raising=False)
    monkeypatch.delenv(DEFAULT_WORKSPACE_ENV, raising=False)


def _make_membership_app(tmp_path, monkeypatch, **overrides):
    _membership_env(monkeypatch)
    monkeypatch.setattr(config_module, "InMemoryOrgStore", RecordingOrgStore)
    return make_app(_config(tmp_path, **overrides))


@pytest_asyncio.fixture
async def membership_client(tmp_path, monkeypatch):
    """A membership-resolving app, plus the store its memberships live in."""

    app = _make_membership_app(tmp_path, monkeypatch)
    async with app.router.lifespan_context(app):
        store = app.state.org_store
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, store


@pytest_asyncio.fixture
async def enforced_membership_client(tmp_path, monkeypatch):
    """The same app with the protection map on — the standalone permutation.

    Worth its own fixture because the credential check then runs in the
    path-protection middleware, which sits outside the app's exception handlers
    and has to render every outcome itself.
    """

    app = _make_membership_app(
        tmp_path,
        monkeypatch,
        security={
            "paths": [
                {"path": "/health", "match": "exact", "access": "public"},
                {"path": "/", "match": "exact", "access": "authenticated"},
            ],
            "default_access": "authenticated",
        },
    )
    async with app.router.lifespan_context(app):
        store = app.state.org_store
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, store


async def create_frame(client, cookies, *, name="Frame", visibility="private", readers=None, publish=False):
    payload: dict = {"name": name, "tags": ["team"], "body": "# Body", "visibility": visibility}
    response = await client.post("/v1/frames", cookies=cookies, json=payload)
    assert response.status_code == 201, response.text
    frame = response.json()
    if readers is not None:
        granted = await client.put(f"/v1/frames/{frame['id']}/readers", cookies=cookies, json={"readers": readers})
        assert granted.status_code == 200, granted.text
    if publish:
        # `published` is the master read gate, so anything testing visibility or
        # a reader grant has to get past it first.
        published = await client.post(f"/v1/frames/{frame['id']}/publish", cookies=cookies)
        assert published.status_code == 200, published.text
    return frame


def assert_no_organization(response) -> None:
    """The exact envelope apollo-desktop#637 recognises: 403 plus the code."""

    assert response.status_code == 403, response.text
    body = response.json()
    assert body["error"]["code"] == error_codes.NO_ORGANIZATION
    assert body["error"]["message"]


# --------------------------------------------------------------------------
# The switch, and its startup preconditions
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "claims"])
def test_unset_or_claims_keeps_the_historical_org_source(monkeypatch, value):
    if value is None:
        monkeypatch.delenv(ORG_SOURCE_ENV, raising=False)
    else:
        monkeypatch.setenv(ORG_SOURCE_ENV, value)
    assert org_source_resolves_membership() is False


def test_membership_selects_membership_resolution(monkeypatch):
    monkeypatch.setenv(ORG_SOURCE_ENV, "membership")
    assert org_source_resolves_membership() is True


@pytest.mark.parametrize("value", ["Membership", "MEMBERSHIP", " membership ", "org", "true", "collab_org_members"])
def test_unrecognized_org_source_values_fail_loudly(monkeypatch, value):
    # Exact match, same contract as the identity pin: guessing 'claims' would
    # resurrect the retired fallback, guessing 'membership' would lock out a hub
    # that has no membership rows.
    monkeypatch.setenv(ORG_SOURCE_ENV, value)
    with pytest.raises(RuntimeError, match=ORG_SOURCE_ENV):
        org_source_resolves_membership()


def test_membership_requires_the_identity_pin(monkeypatch):
    monkeypatch.setenv(ORG_SOURCE_ENV, "membership")
    monkeypatch.delenv(IDENTITY_CLAIM_ENV, raising=False)
    with pytest.raises(RuntimeError, match=IDENTITY_CLAIM_ENV):
        enforce_membership_org_source_preconditions()


@pytest.mark.parametrize("retired", [DEFAULT_ORG_ENV, DEFAULT_WORKSPACE_ENV])
def test_membership_refuses_a_leftover_default_fallback(monkeypatch, retired):
    # Nothing reads these in membership mode, which is exactly the danger: left
    # set they are invisible until someone flips the source back to claims.
    monkeypatch.setenv(ORG_SOURCE_ENV, "membership")
    monkeypatch.setenv(IDENTITY_CLAIM_ENV, "sub")
    monkeypatch.setenv(retired, "collab-hub")
    with pytest.raises(RuntimeError, match=retired):
        enforce_membership_org_source_preconditions()


def test_claims_mode_leaves_the_default_fallback_alone(monkeypatch):
    # The deployed configuration this issue retires must keep working until the
    # source is flipped — the two changes ship together, and neither half may
    # break on its own.
    monkeypatch.delenv(ORG_SOURCE_ENV, raising=False)
    monkeypatch.setenv(DEFAULT_ORG_ENV, "collab-hub")
    enforce_membership_org_source_preconditions()


def test_startup_fails_on_an_unrecognized_org_source(tmp_path, monkeypatch):
    monkeypatch.setenv(ORG_SOURCE_ENV, "Membership")
    with pytest.raises(RuntimeError, match=ORG_SOURCE_ENV):
        make_app(_config(tmp_path))


def test_startup_fails_when_membership_has_no_organization_store(tmp_path, monkeypatch):
    # Membership is an authorization input: an unavailable store would 503 every
    # authenticated request for the life of the pod. Refuse the rollout instead.
    _membership_env(monkeypatch)
    config = _config(tmp_path, frames={"orgs": {"backend": ""}})
    with pytest.raises(RuntimeError, match="organization store"):
        make_app(config)


def test_claims_mode_starts_without_an_organization_store(tmp_path, monkeypatch):
    monkeypatch.delenv(ORG_SOURCE_ENV, raising=False)
    app = make_app(_config(tmp_path, frames={"orgs": {"backend": ""}}))
    assert app is not None


UNREACHABLE_POSTGRES_URL = "postgresql://nexus:nexus@127.0.0.1:1/nexus"


def _unreachable_postgres_config(tmp_path, *, auto_migrate: bool) -> Config:
    return _config(
        tmp_path,
        frames={
            "orgs": {"backend": ""},
            "postgres": {
                "url": UNREACHABLE_POSTGRES_URL,
                "auto_migrate": auto_migrate,
                "pool": {"min_size": 0, "timeout_seconds": 0.5},
            },
        },
    )


def test_startup_survives_an_unreachable_database_without_auto_migration(tmp_path, monkeypatch):
    # The pool is built so a Postgres outage at startup does not crash the app,
    # and the version preflight must not quietly introduce that dependency: it
    # could not run, so it asserts nothing and the pod comes up to answer 503.
    _membership_env(monkeypatch)
    app = make_app(_unreachable_postgres_config(tmp_path, auto_migrate=False))
    assert app is not None


def test_startup_still_fails_on_an_unreachable_database_when_auto_migrating(tmp_path, monkeypatch):
    """The limit of the previous test, pinned rather than left implied.

    With ``auto_migrate`` on, the migration runner has already raised before the
    preflight is reached — unchanged from issue #62 and deliberate: migrations
    run only at startup, so a pod that skipped its own migration would keep
    failing after the database came back.
    """

    _membership_env(monkeypatch)
    with pytest.raises(psycopg.OperationalError):
        make_app(_unreachable_postgres_config(tmp_path, auto_migrate=True))


# --------------------------------------------------------------------------
# Resolution: the row decides, the token does not
# --------------------------------------------------------------------------


def test_membership_row_supplies_org_role_and_the_constant_workspace(monkeypatch):
    monkeypatch.setenv(IDENTITY_CLAIM_ENV, "sub")
    store = InMemoryOrgStore()
    store.set_membership(ALICE, ORG_ONE, role=ROLE_OWNER)

    context = auth_context_from_membership({"sub": ALICE, "email": "alice@example.com"}, store)

    assert context.user == ALICE
    assert context.org_id == ORG_ONE
    assert context.org_role == ROLE_OWNER
    assert context.workspace_id == WORKSPACE_DEFAULT
    # Display claims still travel; they are simply never the principal.
    assert context.email == "alice@example.com"


def test_token_org_claims_are_ignored(monkeypatch):
    monkeypatch.setenv(IDENTITY_CLAIM_ENV, "sub")
    store = InMemoryOrgStore()
    store.set_membership(ALICE, ORG_ONE)

    context = auth_context_from_membership(
        {"sub": ALICE, "org_id": ORG_TWO, "workspace_id": "smuggled"},
        store,
    )

    assert (context.org_id, context.workspace_id) == (ORG_ONE, WORKSPACE_DEFAULT)


def test_no_membership_row_is_no_organization(monkeypatch):
    monkeypatch.setenv(IDENTITY_CLAIM_ENV, "sub")
    with pytest.raises(NoOrganizationError) as raised:
        auth_context_from_membership({"sub": ALICE}, InMemoryOrgStore())
    assert raised.value.status_code == 403
    assert raised.value.error_code == error_codes.NO_ORGANIZATION


def test_removed_membership_is_no_organization_but_stays_bound(monkeypatch):
    monkeypatch.setenv(IDENTITY_CLAIM_ENV, "sub")
    store = InMemoryOrgStore()
    store.set_membership(ALICE, ORG_ONE, status=MEMBERSHIP_REMOVED)

    with pytest.raises(NoOrganizationError):
        auth_context_from_membership({"sub": ALICE}, store)
    # The row is retained, so the binding is still enforceable.
    assert store.get_membership(ALICE).org_id == ORG_ONE


def test_a_token_without_a_subject_is_not_an_identity(monkeypatch):
    # None, not NoOrganizationError: the caller turns this into a 401, exactly
    # as on the claims path. "Unauthenticated" and "unaffiliated" are different
    # answers and the desktop renders them differently.
    monkeypatch.setenv(IDENTITY_CLAIM_ENV, "sub")
    assert auth_context_from_membership({"preferred_username": "alice"}, InMemoryOrgStore()) is None


def test_store_failures_propagate_rather_than_denying(monkeypatch):
    monkeypatch.setenv(IDENTITY_CLAIM_ENV, "sub")
    store = RecordingOrgStore()
    store.error = psycopg.OperationalError("connection refused")
    with pytest.raises(psycopg.OperationalError):
        auth_context_from_membership({"sub": ALICE}, store)


def test_unavailable_store_raises_rather_than_reporting_no_membership():
    # The distinction the whole fail-closed decision rests on: "no backend" must
    # never be indistinguishable from "this user has no organization".
    with pytest.raises(OrgsUnavailableError):
        UnavailableOrgStore().get_membership(ALICE)


def test_membership_is_active_only_for_active_rows():
    active = OrgMembership(user_id=ALICE, org_id=ORG_ONE, role=ROLE_MEMBER, status=MEMBERSHIP_ACTIVE)
    removed = OrgMembership(user_id=ALICE, org_id=ORG_ONE, role=ROLE_MEMBER, status=MEMBERSHIP_REMOVED)
    assert active.is_active is True
    assert removed.is_active is False


# --------------------------------------------------------------------------
# Over HTTP
# --------------------------------------------------------------------------


async def test_unaffiliated_user_gets_no_organization_and_no_frames(membership_client):
    client, _store = membership_client

    listed = await client.get("/v1/frames", cookies=cookies_for(ALICE))
    assert_no_organization(listed)

    created = await client.post(
        "/v1/frames",
        cookies=cookies_for(ALICE),
        json={"name": "Frame", "body": "# Body"},
    )
    assert_no_organization(created)


async def test_no_organization_survives_a_spoofed_org_claim(membership_client):
    # The fallback this issue retires worked by *believing* a value; a token
    # claiming an org must not resurrect that behavior.
    client, _store = membership_client
    response = await client.get("/v1/frames", cookies=cookies_for(ALICE, org=ORG_ONE, workspace="default"))
    assert_no_organization(response)


async def test_a_member_works_normally(membership_client):
    client, store = membership_client
    store.set_membership(ALICE, ORG_ONE, role=ROLE_OWNER)

    frame = await create_frame(client, cookies_for(ALICE), name="Team Frame")
    listed = await client.get("/v1/frames", cookies=cookies_for(ALICE))
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [frame["id"]]


async def test_internal_frames_do_not_cross_organizations(membership_client):
    client, store = membership_client
    store.set_membership(ALICE, ORG_ONE, role=ROLE_OWNER)
    store.set_membership(BOB, ORG_TWO, role=ROLE_OWNER)

    frame = await create_frame(client, cookies_for(ALICE), visibility="internal", publish=True)

    # Even with the other organization asserted in the token.
    outsider = await client.get(f"/v1/frames/{frame['id']}", cookies=cookies_for(BOB, org=ORG_ONE))
    assert outsider.status_code == 404, outsider.text
    assert (await client.get(f"/v1/frames/{frame['id']}", cookies=cookies_for(ALICE))).status_code == 200


async def test_removed_membership_is_refused_on_the_very_next_request(membership_client):
    """The acceptance evidence for removal semantics.

    Removal is a manual database action with no endpoint, so this is what
    demonstrates that flipping the row takes effect — per request, with no
    cache to wait out and no session to expire.
    """

    client, store = membership_client
    store.set_membership(ALICE, ORG_ONE, role=ROLE_OWNER)
    before = await client.get("/v1/frames", cookies=cookies_for(ALICE))
    assert before.status_code == 200

    store.set_membership(ALICE, ORG_ONE, role=ROLE_OWNER, status=MEMBERSHIP_REMOVED)

    after = await client.get("/v1/frames", cookies=cookies_for(ALICE))
    assert_no_organization(after)


async def test_removal_strips_internal_access_but_not_an_explicit_reader_grant(membership_client):
    """R4, stated precisely: removal is *not* total revocation.

    Losing the organization mechanically removes ``internal`` access, because
    ``internal`` is scoped by organization and a removed caller has none. An
    explicit ``readers`` grant is a different thing — it names the subject, is
    not evaluated against membership, and survives untouched. Describing
    removal as revoking a user's access would therefore be wrong.
    """

    client, store = membership_client
    store.set_membership(ALICE, ORG_ONE, role=ROLE_OWNER)
    store.set_membership(BOB, ORG_TWO, role=ROLE_MEMBER)

    internal = await create_frame(client, cookies_for(ALICE), name="Internal", visibility="internal", publish=True)
    granted = await create_frame(
        client,
        cookies_for(ALICE),
        name="Shared",
        visibility="private",
        readers=[BOB],
        publish=True,
    )

    # Bob reads what he was granted across the organization boundary, and not
    # Alice's internal frame.
    assert (await client.get(f"/v1/frames/{granted['id']}", cookies=cookies_for(BOB))).status_code == 200
    assert (await client.get(f"/v1/frames/{internal['id']}", cookies=cookies_for(BOB))).status_code == 404

    store.set_membership(BOB, ORG_TWO, role=ROLE_MEMBER, status=MEMBERSHIP_REMOVED)
    assert_no_organization(await client.get(f"/v1/frames/{granted['id']}", cookies=cookies_for(BOB)))

    # The grant itself was never touched: restoring the membership restores the
    # read, with nobody having re-shared anything.
    store.set_membership(BOB, ORG_TWO, role=ROLE_MEMBER)
    assert (await client.get(f"/v1/frames/{granted['id']}", cookies=cookies_for(BOB))).status_code == 200


async def test_an_invalid_token_is_still_a_401(membership_client):
    # Membership resolution must not turn "not signed in" into "no organization".
    client, _store = membership_client
    response = await client.get("/v1/frames", cookies={"IdToken-test": "not-a-jwt"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == error_codes.UNAUTHORIZED


# --------------------------------------------------------------------------
# Failing closed (R15): an outage is 503 everywhere, never 401
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error, expected_code",
    [
        (psycopg.OperationalError("connection refused"), error_codes.DATABASE_UNAVAILABLE),
        (OrgsUnavailableError("Organization storage is not configured"), error_codes.ORGANIZATIONS_UNAVAILABLE),
    ],
)
async def test_an_unavailable_membership_backend_is_503_on_the_api(membership_client, error, expected_code):
    client, store = membership_client
    store.set_membership(ALICE, ORG_ONE)
    store.error = error

    response = await client.get("/v1/frames", cookies=cookies_for(ALICE))

    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == expected_code


async def test_an_unavailable_membership_backend_is_503_behind_the_protection_map(enforced_membership_client):
    # The path-protection middleware runs outside the app's exception handlers,
    # so without explicit handling this is a 500 while the same lookup inside a
    # route answers 503.
    client, store = enforced_membership_client
    store.set_membership(ALICE, ORG_ONE)
    store.error = psycopg.OperationalError("connection refused")

    response = await client.get("/", cookies=cookies_for(ALICE))

    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == error_codes.DATABASE_UNAVAILABLE


async def test_no_organization_keeps_its_code_behind_the_protection_map(enforced_membership_client):
    # The middleware's own renderer must not flatten this into `unauthorized`:
    # the page surfaces are not API paths, and the code is the whole message.
    client, _store = enforced_membership_client

    response = await client.get("/", cookies=cookies_for(ALICE))

    assert_no_organization(response)


async def test_the_protection_map_still_answers_401_without_credentials(enforced_membership_client):
    client, _store = enforced_membership_client
    response = await client.get("/")
    assert response.status_code == 401


async def test_membership_is_resolved_once_per_request(enforced_membership_client):
    # Both the protection middleware and the route dependency authenticate. One
    # database round trip per request, not two.
    client, store = enforced_membership_client
    store.set_membership(ALICE, ORG_ONE)

    store.calls = 0
    response = await client.get("/v1/frames", cookies=cookies_for(ALICE))

    assert response.status_code == 200
    assert store.calls == 1


def test_mcp_reports_no_organization_in_the_api_envelope(tmp_path, monkeypatch):
    # The mounted MCP app never sees the outer app's exception handlers, so its
    # auth middleware has to produce this envelope itself.
    app = _make_membership_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers={
                "accept": "application/json, text/event-stream",
                "content-type": "application/json",
                "Cookie": f"IdToken-test={cookies_for(ALICE)['IdToken-test']}",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == error_codes.NO_ORGANIZATION


def test_mcp_reports_an_unavailable_membership_backend_as_503(tmp_path, monkeypatch):
    app = _make_membership_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        app.state.org_store.error = psycopg.OperationalError("connection refused")
        response = client.post(
            "/mcp",
            headers={
                "accept": "application/json, text/event-stream",
                "content-type": "application/json",
                "Cookie": f"IdToken-test={cookies_for(ALICE)['IdToken-test']}",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )

    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == error_codes.DATABASE_UNAVAILABLE


# --------------------------------------------------------------------------
# The dev-auth shortcut is not a way around any of this
# --------------------------------------------------------------------------


async def test_dev_auth_is_resolved_through_membership_too(tmp_path, monkeypatch):
    # DEV_AUTH_ORG names its own organization, which is precisely the
    # claim-asserted tenancy this mode stops honoring. Behind a single
    # environment variable, that would be a tenancy escape hatch.
    monkeypatch.setenv("DEV_AUTH_ENABLED", "true")
    monkeypatch.setenv("DEV_AUTH_USER", CAROL)
    monkeypatch.setenv("DEV_AUTH_ORG", "smuggled-org")
    app = _make_membership_app(tmp_path, monkeypatch)

    async with app.router.lifespan_context(app):
        store = app.state.org_store
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            assert_no_organization(await client.get("/v1/frames"))

            store.set_membership(CAROL, ORG_ONE, role=ROLE_OWNER)
            listed = await client.get("/v1/frames")
            assert listed.status_code == 200

            frame = await create_frame(client, {}, name="Dev Frame")
            assert frame["created_by"] == CAROL


# --------------------------------------------------------------------------
# Against a real database (opt in with COLLAB_HUB_TEST_POSTGRES_URL)
# --------------------------------------------------------------------------

POSTGRES_URL = os.environ.get("COLLAB_HUB_TEST_POSTGRES_URL", "")

live_postgres = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="set COLLAB_HUB_TEST_POSTGRES_URL to a disposable database to run the live membership tests",
)

COLLAB_TABLES = (
    "collab_frame_moves",
    "collab_service_access_grants",
    "collab_provisioned_accounts",
    "collab_invitations",
    "collab_org_members",
    "collab_orgs",
    "collab_schema_migrations",
)


@pytest_asyncio.fixture
async def live_membership_client(tmp_path, monkeypatch):
    """A membership-resolving app reading real ``collab_org_members`` rows.

    The in-memory store proves the decision logic; this proves the query, the
    migration wiring, and that the ``status`` column really is what removal
    flips. Yields the client and a connection factory for the test to write
    rows the way an operator would — by hand, since there is no endpoint.
    """

    _membership_env(monkeypatch)
    config = _config(
        tmp_path,
        frames={"orgs": {"backend": ""}, "postgres": {"url": POSTGRES_URL, "auto_migrate": True}},
    )

    def drop_all(database) -> None:
        with database.connection() as conn:
            for table in COLLAB_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

    from collab_hub_api.frames.db import PostgresDatabase

    scratch = PostgresDatabase(POSTGRES_URL, min_size=0, max_size=2, timeout_seconds=10.0)
    try:
        drop_all(scratch)
        app = make_app(config)
        async with app.router.lifespan_context(app):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                yield client, scratch
        drop_all(scratch)
    finally:
        scratch.close()


@live_postgres
async def test_live_membership_resolution_and_removal(live_membership_client):
    client, database = live_membership_client

    # No row: authenticated, unaffiliated.
    assert_no_organization(await client.get("/v1/frames", cookies=cookies_for(ALICE)))

    with database.connection() as conn:
        conn.execute("INSERT INTO collab_orgs (id, created_by) VALUES (%s, %s)", (ORG_ONE, ALICE))
        conn.execute(
            "INSERT INTO collab_org_members (user_id, org_id, role) VALUES (%s, %s, %s)",
            (ALICE, ORG_ONE, ROLE_OWNER),
        )

    frame = await create_frame(client, cookies_for(ALICE), name="Live Frame")
    assert (await client.get(f"/v1/frames/{frame['id']}", cookies=cookies_for(ALICE))).status_code == 200

    # Removal is a manual UPDATE — there is no endpoint — and it must bite on
    # the next request, with no cache or session to wait out.
    with database.connection() as conn:
        conn.execute("UPDATE collab_org_members SET status = %s WHERE user_id = %s", (MEMBERSHIP_REMOVED, ALICE))

    assert_no_organization(await client.get(f"/v1/frames/{frame['id']}", cookies=cookies_for(ALICE)))

    # And the row is still there, still bound to the same organization.
    with database.connection() as conn:
        row = conn.execute("SELECT org_id, status FROM collab_org_members WHERE user_id = %s", (ALICE,)).fetchone()
    assert row == {"org_id": ORG_ONE, "status": MEMBERSHIP_REMOVED}

    with database.connection() as conn:
        conn.execute("UPDATE collab_org_members SET status = %s WHERE user_id = %s", (MEMBERSHIP_ACTIVE, ALICE))
    assert (await client.get(f"/v1/frames/{frame['id']}", cookies=cookies_for(ALICE))).status_code == 200


@live_postgres
async def test_live_missing_collab_schema_is_503_not_500(live_membership_client):
    """The narrow gap the startup preflight cannot close, closed at the query.

    A pod that starts while Postgres is unreachable (with auto-migration off)
    tolerates an unknown schema version rather than crash-looping. If the
    database then returns and nobody ever ran the migration, the first
    membership query hits a table that does not exist — an ``UndefinedTable``,
    which is a ``ProgrammingError`` and therefore not one of the "database
    unavailable" classes. Unmapped it would answer 500 at request time, which
    is exactly the failure the preflight exists to prevent. Dropping the tables
    under a running app reproduces that state directly.
    """

    client, database = live_membership_client
    with database.connection() as conn:
        conn.execute("DROP TABLE IF EXISTS collab_org_members CASCADE")

    response = await client.get("/v1/frames", cookies=cookies_for(ALICE))

    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == error_codes.ORGANIZATIONS_UNAVAILABLE


@live_postgres
async def test_live_startup_refuses_a_schema_behind_this_build(tmp_path, monkeypatch):
    """The #96 preflight, end to end: no migration, no serving.

    ``auto_migrate`` off against a database nobody migrated is the exact
    situation that used to start cleanly and fail on the first authenticated
    request.
    """

    from collab_hub_api.frames.collab_schema import CollabSchemaVersionError
    from collab_hub_api.frames.db import PostgresDatabase

    scratch = PostgresDatabase(POSTGRES_URL, min_size=0, max_size=2, timeout_seconds=10.0)
    try:
        with scratch.connection() as conn:
            for table in COLLAB_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

        _membership_env(monkeypatch)
        config = _config(
            tmp_path,
            frames={"orgs": {"backend": ""}, "postgres": {"url": POSTGRES_URL, "auto_migrate": False}},
        )
        with pytest.raises(CollabSchemaVersionError):
            make_app(config)
    finally:
        scratch.close()
