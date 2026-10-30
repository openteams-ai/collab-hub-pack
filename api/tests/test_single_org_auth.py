"""The single-organization org source (issue #91).

``orgSource=single`` is membership resolution plus one declared difference:
a caller with **no membership row at all**, whose sign-in arrived through a
*declared identity source*, is auto-admitted — a real ``collab_org_members``
row is written on their first authenticated request. Five things are pinned
here, in this order:

1. **The declaration.** The mode requires an organization id, a name, and at
   least one identity-provider alias, all failing startup rather than the
   first sign-in; every membership-mode precondition (the identity pin, the
   retired defaults, a real organization store) applies unchanged.
2. **The gate.** Admission keys on the token's ``identity_provider`` claim
   matching a declared alias exactly. A missing claim, an undeclared alias,
   and a non-string value all fall through to precisely the membership-mode
   outcomes — authentication is never the boundary, because "every
   authenticated user" includes hand-created realm accounts, the one door
   with no policy on it.
3. **The write.** Insert-if-absent, role ``member``, and never an update: a
   ``removed`` row is not resurrected (removal keeps taking effect on the
   next request), an active row in another organization is not moved, and
   turning the mode off simply stops the writes — every row it made remains
   an ordinary membership row.
4. **The row is real.** The admitted caller is a member the rest of the
   product can see — the same store read that serves the member list — and
   ``internal`` visibility means every admitted user of this hub, by
   declaration.
5. **Failing closed.** The provision write is part of an authorization
   answer: a store failure fails the request, never best-effort.
"""

from __future__ import annotations

import base64
import json
import os

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

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
    ORG_SOURCE_ENV,
    SINGLE_ORG_ID_ENV,
    SINGLE_ORG_MEMBER_SOURCES_ENV,
    SINGLE_ORG_NAME_ENV,
    SingleOrgDeclaration,
    enforce_membership_org_source_preconditions,
    org_source_is_single,
    org_source_resolves_membership,
    single_org_declaration,
)
from collab_hub_api.frames.orgs import (
    MEMBERSHIP_REMOVED,
    PLATFORM_ROLE_OPERATOR,
    ROLE_MEMBER,
    ROLE_OWNER,
    SINGLE_ORG_CREATED_BY,
    InMemoryOrgStore,
    OrgSchemaMissingError,
    OrgsUnavailableError,
    PostgresOrgStore,
    UnavailableOrgStore,
)

psycopg = pytest.importorskip("psycopg")

ALICE = "a1b2c3d4-1111-4111-8111-abcdefabcdef"
BOB = "22222222-2222-4222-8222-b0b0b0b0b0b0"
CAROL = "33333333-3333-4333-8333-cacacacacaca"

THE_ORG = "org-solo-1"
THE_ORG_NAME = "Example Corp"
OTHER_ORG = "org-elsewhere"

DECLARATION = SingleOrgDeclaration(
    org_id=THE_ORG,
    org_name=THE_ORG_NAME,
    member_sources=frozenset({"corp-google", "corp-slack"}),
)


@pytest.fixture(autouse=True)
def _pin_identity(monkeypatch):
    # The pin is a startup precondition of every membership-resolving mode, so
    # every test here runs under it; the one test about the precondition
    # itself removes it explicitly.
    monkeypatch.setenv(IDENTITY_CLAIM_ENV, "sub")


def _jwt(payload: dict) -> str:
    def encode(part: dict) -> str:
        raw = json.dumps(part, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode(payload)}."


def claims_for(sub: str, *, provider: object = "corp-google", **extra) -> dict:
    claims: dict = {"sub": sub, "preferred_username": f"name-{sub[:4]}", **extra}
    if provider is not None:
        claims["identity_provider"] = provider
    return claims


def cookies_for(sub: str, *, provider: object = "corp-google", **extra) -> dict[str, str]:
    return {"IdToken-test": _jwt(claims_for(sub, provider=provider, **extra))}


def _single_env(monkeypatch, *, sources: str = "corp-google,corp-slack") -> None:
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_IDTOKEN_ALLOW_UNSIGNED", "true")
    monkeypatch.setenv(IDENTITY_CLAIM_ENV, "sub")
    monkeypatch.setenv(ORG_SOURCE_ENV, "single")
    monkeypatch.setenv(SINGLE_ORG_ID_ENV, THE_ORG)
    monkeypatch.setenv(SINGLE_ORG_NAME_ENV, THE_ORG_NAME)
    monkeypatch.setenv(SINGLE_ORG_MEMBER_SOURCES_ENV, sources)
    monkeypatch.delenv(DEFAULT_ORG_ENV, raising=False)


class ProvisionRecordingOrgStore(InMemoryOrgStore):
    """In-memory store that records every provision call and its kwargs."""

    def __init__(self) -> None:
        super().__init__()
        self.provisions: list[dict] = []

    def provision_member(self, user_id, org_id, org_name, *, email=None, display_name=None):
        self.provisions.append(
            {
                "user_id": user_id,
                "org_id": org_id,
                "org_name": org_name,
                "email": email,
                "display_name": display_name,
            }
        )
        return super().provision_member(user_id, org_id, org_name, email=email, display_name=display_name)


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


@pytest_asyncio.fixture
async def single_org_client(tmp_path, monkeypatch):
    """A single-organization app, plus the store its memberships land in."""

    _single_env(monkeypatch)
    monkeypatch.setattr(config_module, "InMemoryOrgStore", ProvisionRecordingOrgStore)
    app = make_app(_config(tmp_path))
    async with app.router.lifespan_context(app):
        store = app.state.org_store
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, store


def assert_no_organization(response) -> None:
    assert response.status_code == 403, response.text
    body = response.json()
    assert body["error"]["code"] == error_codes.NO_ORGANIZATION


# --------------------------------------------------------------------------
# The switch, and the declaration's startup contract
# --------------------------------------------------------------------------


def test_single_resolves_membership_and_knows_it_is_single(monkeypatch):
    _single_env(monkeypatch)
    assert org_source_resolves_membership() is True
    assert org_source_is_single() is True


def test_membership_mode_is_not_single_and_has_no_declaration(monkeypatch):
    monkeypatch.setenv(ORG_SOURCE_ENV, "membership")
    # A leftover declaration is inert off `single`: nothing reads it, so a
    # membership deployment cannot auto-admit by accident.
    monkeypatch.setenv(SINGLE_ORG_ID_ENV, THE_ORG)
    assert org_source_is_single() is False
    assert single_org_declaration() is None


def test_the_declaration_parses_and_trims_its_sources(monkeypatch):
    _single_env(monkeypatch, sources=" corp-google , corp-slack ,")
    declaration = single_org_declaration()
    assert declaration == SingleOrgDeclaration(
        org_id=THE_ORG,
        org_name=THE_ORG_NAME,
        member_sources=frozenset({"corp-google", "corp-slack"}),
    )


@pytest.mark.parametrize(
    "missing, message_names",
    [
        (SINGLE_ORG_ID_ENV, SINGLE_ORG_ID_ENV),
        (SINGLE_ORG_NAME_ENV, SINGLE_ORG_NAME_ENV),
        (SINGLE_ORG_MEMBER_SOURCES_ENV, SINGLE_ORG_MEMBER_SOURCES_ENV),
    ],
)
def test_an_incomplete_declaration_fails_startup(monkeypatch, missing, message_names):
    _single_env(monkeypatch)
    monkeypatch.setenv(missing, "  ")
    with pytest.raises(RuntimeError, match=message_names):
        single_org_declaration()


def test_a_wildcard_member_source_is_refused(monkeypatch):
    _single_env(monkeypatch, sources="corp-google,*")
    with pytest.raises(RuntimeError, match=r"does not support '\*'"):
        single_org_declaration()


def test_single_requires_the_identity_pin(monkeypatch):
    _single_env(monkeypatch)
    monkeypatch.delenv(IDENTITY_CLAIM_ENV, raising=False)
    with pytest.raises(RuntimeError, match="sub"):
        enforce_membership_org_source_preconditions()


def test_single_refuses_a_leftover_default_fallback(monkeypatch):
    _single_env(monkeypatch)
    monkeypatch.setenv(DEFAULT_ORG_ENV, "everyone")
    with pytest.raises(RuntimeError, match=DEFAULT_ORG_ENV):
        enforce_membership_org_source_preconditions()


def test_the_preconditions_check_validates_the_declaration_too(monkeypatch):
    _single_env(monkeypatch)
    monkeypatch.setenv(SINGLE_ORG_ID_ENV, "")
    with pytest.raises(RuntimeError, match=SINGLE_ORG_ID_ENV):
        enforce_membership_org_source_preconditions()


def test_startup_fails_when_single_has_no_organization_store(tmp_path, monkeypatch):
    _single_env(monkeypatch)
    with pytest.raises(RuntimeError, match="organization store"):
        make_app(_config(tmp_path, frames={"orgs": {"backend": ""}}))


def test_startup_fails_on_an_incomplete_declaration(tmp_path, monkeypatch):
    _single_env(monkeypatch)
    monkeypatch.delenv(SINGLE_ORG_MEMBER_SOURCES_ENV, raising=False)
    with pytest.raises(RuntimeError, match=SINGLE_ORG_MEMBER_SOURCES_ENV):
        make_app(_config(tmp_path))


def test_invitations_still_mount_under_single(tmp_path, monkeypatch):
    # Invitations carry more than membership — the granted role, and service
    # access on acceptance. On a single-org hub they become optional for
    # *access* and stay useful for *role*, so the router must keep mounting.
    _single_env(monkeypatch)
    app = make_app(_config(tmp_path))
    assert any(getattr(route, "path", "") == "/v1/invitations/accept" for route in app.routes)


# --------------------------------------------------------------------------
# The gate and the write, at the resolution seam
# --------------------------------------------------------------------------


def test_a_declared_sign_in_with_no_row_is_admitted_as_a_member():
    store = InMemoryOrgStore()
    context = auth_context_from_membership(claims_for(ALICE), store, auto_admit=DECLARATION)
    assert context is not None
    assert context.org_id == THE_ORG
    assert context.org_role == ROLE_MEMBER
    assert context.workspace_id == WORKSPACE_DEFAULT
    # The row is real: the same read that serves the member list sees it.
    membership = store.get_membership(ALICE)
    assert membership is not None and membership.is_active
    assert membership.role == ROLE_MEMBER


@pytest.mark.parametrize(
    "provider",
    [
        None,  # no claim at all — a local realm account, or a missing mapper
        "personal-github",  # a provider nobody declared
        ["corp-google"],  # a non-string claim is not an alias
    ],
)
def test_an_undeclared_sign_in_falls_through_to_no_organization(provider):
    store = InMemoryOrgStore()
    with pytest.raises(NoOrganizationError):
        auth_context_from_membership(claims_for(ALICE, provider=provider), store, auto_admit=DECLARATION)
    assert store.get_membership(ALICE) is None


def test_without_a_declaration_membership_mode_is_unchanged():
    store = InMemoryOrgStore()
    with pytest.raises(NoOrganizationError):
        auth_context_from_membership(claims_for(ALICE), store)
    assert store.get_membership(ALICE) is None


def test_a_removed_member_is_never_resurrected():
    store = InMemoryOrgStore()
    store.set_membership(ALICE, THE_ORG, status=MEMBERSHIP_REMOVED)
    with pytest.raises(NoOrganizationError):
        auth_context_from_membership(claims_for(ALICE), store, auto_admit=DECLARATION)
    membership = store.get_membership(ALICE)
    assert membership is not None and membership.status == MEMBERSHIP_REMOVED


def test_an_existing_member_of_another_organization_is_not_moved():
    store = InMemoryOrgStore()
    store.set_membership(ALICE, OTHER_ORG, role=ROLE_OWNER)
    context = auth_context_from_membership(claims_for(ALICE), store, auto_admit=DECLARATION)
    assert context is not None and context.org_id == OTHER_ORG
    assert context.org_role == ROLE_OWNER


def test_an_operator_from_a_declared_source_is_admitted_and_keeps_the_platform_axis():
    store = InMemoryOrgStore()
    store.set_platform_role(ALICE)
    context = auth_context_from_membership(claims_for(ALICE), store, auto_admit=DECLARATION)
    assert context is not None
    assert context.org_id == THE_ORG and context.org_role == ROLE_MEMBER
    assert context.platform_role == PLATFORM_ROLE_OPERATOR


def test_an_operator_from_an_undeclared_source_stays_hub_scoped():
    store = InMemoryOrgStore()
    store.set_platform_role(ALICE)
    context = auth_context_from_membership(claims_for(ALICE, provider="personal-github"), store, auto_admit=DECLARATION)
    assert context is not None
    assert context.home_org_id is None
    assert context.platform_role == PLATFORM_ROLE_OPERATOR


def test_the_email_column_gets_only_a_verified_address():
    store = ProvisionRecordingOrgStore()
    auth_context_from_membership(
        claims_for(ALICE, email="alice@example.test", email_verified=True, name="Alice"),
        store,
        auto_admit=DECLARATION,
    )
    auth_context_from_membership(
        claims_for(BOB, email="bob@example.test", email_verified=False),
        store,
        auto_admit=DECLARATION,
    )
    by_user = {entry["user_id"]: entry for entry in store.provisions}
    assert by_user[ALICE]["email"] == "alice@example.test"
    assert by_user[ALICE]["display_name"] == "Alice"
    assert by_user[BOB]["email"] is None
    assert by_user[ALICE]["org_name"] == THE_ORG_NAME


def test_the_in_memory_store_provisions_once_and_then_returns_the_standing_row():
    store = InMemoryOrgStore()
    first, created = store.provision_member(ALICE, THE_ORG, THE_ORG_NAME)
    again, created_again = store.provision_member(ALICE, THE_ORG, THE_ORG_NAME)
    assert created is True and created_again is False
    assert again == first


def test_an_unavailable_store_raises_on_provision_rather_than_admitting():
    with pytest.raises(OrgsUnavailableError):
        UnavailableOrgStore().provision_member(ALICE, THE_ORG, THE_ORG_NAME)


def test_turning_the_mode_off_keeps_members_and_stops_admitting():
    # The acceptance criterion verbatim: rows written under `single` are
    # ordinary membership rows, so flipping back to `membership` keeps every
    # member working and new sign-ins stop being admitted.
    store = InMemoryOrgStore()
    admitted = auth_context_from_membership(claims_for(ALICE), store, auto_admit=DECLARATION)
    assert admitted is not None and admitted.org_id == THE_ORG

    still_member = auth_context_from_membership(claims_for(ALICE), store)
    assert still_member is not None and still_member.org_id == THE_ORG
    assert still_member.org_role == ROLE_MEMBER
    with pytest.raises(NoOrganizationError):
        auth_context_from_membership(claims_for(BOB), store)


# --------------------------------------------------------------------------
# Through the app: acceptance criteria end to end
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_brand_new_account_is_admitted_on_its_first_request(single_org_client):
    client, store = single_org_client
    listed = await client.get("/v1/frames", cookies=cookies_for(ALICE))
    assert listed.status_code == 200, listed.text
    membership = store.get_membership(ALICE)
    assert membership is not None and membership.org_id == THE_ORG and membership.is_active


@pytest.mark.asyncio
async def test_admission_writes_once_not_per_request(single_org_client):
    client, store = single_org_client
    for _ in range(3):
        response = await client.get("/v1/frames", cookies=cookies_for(ALICE))
        assert response.status_code == 200
    assert len(store.provisions) == 1


@pytest.mark.asyncio
async def test_an_undeclared_login_gets_the_no_organization_envelope(single_org_client):
    client, store = single_org_client
    response = await client.get("/v1/frames", cookies=cookies_for(BOB, provider=None))
    assert_no_organization(response)
    assert store.get_membership(BOB) is None


@pytest.mark.asyncio
async def test_internal_means_every_admitted_user_by_declaration(single_org_client):
    client, store = single_org_client
    created = await client.post(
        "/v1/frames",
        cookies=cookies_for(ALICE),
        json={"name": "Handbook", "tags": ["team"], "body": "# Body", "visibility": "internal"},
    )
    assert created.status_code == 201, created.text
    frame_id = created.json()["id"]
    published = await client.post(f"/v1/frames/{frame_id}/publish", cookies=cookies_for(ALICE))
    assert published.status_code == 200, published.text

    fetched = await client.get(f"/v1/frames/{frame_id}", cookies=cookies_for(BOB, provider="corp-slack"))
    assert fetched.status_code == 200, fetched.text

    # And an undeclared login shares nothing: no membership, no internal reads.
    refused = await client.get(f"/v1/frames/{frame_id}", cookies=cookies_for(CAROL, provider=None))
    assert_no_organization(refused)


@pytest.mark.asyncio
async def test_removal_still_takes_effect_on_the_very_next_request(single_org_client):
    client, store = single_org_client
    admitted = await client.get("/v1/frames", cookies=cookies_for(ALICE))
    assert admitted.status_code == 200
    membership = store.get_membership(ALICE)
    store.set_membership(ALICE, membership.org_id, role=membership.role, status=MEMBERSHIP_REMOVED)
    refused = await client.get("/v1/frames", cookies=cookies_for(ALICE))
    assert_no_organization(refused)
    # Still bound, still removed: the declared source did not re-admit them.
    after = store.get_membership(ALICE)
    assert after is not None and after.status == MEMBERSHIP_REMOVED


# --------------------------------------------------------------------------
# Org-creating invitations are refused: the one write that would mint a
# second organization on a hub that declares exactly one
# --------------------------------------------------------------------------


def _operator_ctx() -> object:
    from collab_hub_api.frames.auth import AuthContext

    return AuthContext(user=CAROL, home_org_id=None, workspace_id=WORKSPACE_DEFAULT)


def test_org_creating_invitations_are_refused_at_issuance(monkeypatch):
    from collab_hub_api.frames.invitations import (
        OrganizationCreationRefusedError,
        PostgresInvitationService,
    )

    _single_env(monkeypatch)
    # The guard answers before the database is touched — a sentinel proves it.
    service = PostgresInvitationService(object())
    with pytest.raises(OrganizationCreationRefusedError):
        service.create(_operator_ctx(), email="new@example.test", org_id=None)
    with pytest.raises(OrganizationCreationRefusedError):
        service.create_unless_live(_operator_ctx(), email="new@example.test", org_id=None)


def test_invitations_into_an_existing_organization_pass_the_guard(monkeypatch):
    from collab_hub_api.frames.invitations import _refuse_org_creation_under_single_org

    _single_env(monkeypatch)
    # No raise: targeted invitations are how a single-org hub grants `owner`.
    _refuse_org_creation_under_single_org(THE_ORG)


def test_org_creating_invitations_stay_allowed_under_membership(monkeypatch):
    from collab_hub_api.frames.invitations import _refuse_org_creation_under_single_org

    monkeypatch.setenv(ORG_SOURCE_ENV, "membership")
    _refuse_org_creation_under_single_org(None)


# --------------------------------------------------------------------------
# The Postgres write, against a stub connection (the live test proves the SQL)
# --------------------------------------------------------------------------


class _StubResult:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _StubConnection:
    """Answers the provision sequence: org insert, member insert, maybe select."""

    def __init__(self, member_insert_row, select_row=None, error=None):
        self._member_insert_row = member_insert_row
        self._select_row = select_row
        self._error = error
        self.statements: list[tuple[str, tuple]] = []

    def execute(self, sql, params=None):
        if self._error is not None:
            raise self._error
        self.statements.append((" ".join(sql.split()), params))
        if sql.lstrip().startswith("INSERT INTO collab_orgs"):
            return _StubResult(None)
        if sql.lstrip().startswith("INSERT INTO collab_org_members"):
            return _StubResult(self._member_insert_row)
        return _StubResult(self._select_row)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _StubDatabase:
    def __init__(self, connection):
        self._connection = connection

    def connection(self):
        return self._connection


def test_postgres_provision_creates_the_org_and_the_member_together():
    row = {"user_id": ALICE, "org_id": THE_ORG, "role": ROLE_MEMBER, "status": "active"}
    connection = _StubConnection(member_insert_row=row)
    store = PostgresOrgStore(_StubDatabase(connection))
    membership, created = store.provision_member(
        ALICE, THE_ORG, THE_ORG_NAME, email="a@example.test", display_name="Alice"
    )
    assert created is True
    assert membership.org_id == THE_ORG and membership.is_active
    org_sql, org_params = connection.statements[0]
    assert org_sql.startswith("INSERT INTO collab_orgs") and "ON CONFLICT (id) DO NOTHING" in org_sql
    assert org_params == (THE_ORG, THE_ORG_NAME, SINGLE_ORG_CREATED_BY)
    member_sql, member_params = connection.statements[1]
    assert "ON CONFLICT (user_id) DO NOTHING" in member_sql
    assert member_params == (ALICE, THE_ORG, ROLE_MEMBER, "a@example.test", "Alice")


def test_postgres_provision_returns_the_standing_row_when_it_lost_the_race():
    standing = {"user_id": ALICE, "org_id": THE_ORG, "role": ROLE_MEMBER, "status": "removed"}
    connection = _StubConnection(member_insert_row=None, select_row=standing)
    store = PostgresOrgStore(_StubDatabase(connection))
    membership, created = store.provision_member(ALICE, THE_ORG, THE_ORG_NAME)
    assert created is False
    assert membership.status == MEMBERSHIP_REMOVED and not membership.is_active


def test_postgres_provision_maps_a_missing_schema_onto_the_503_class():
    connection = _StubConnection(member_insert_row=None, error=psycopg.errors.UndefinedTable())
    store = PostgresOrgStore(_StubDatabase(connection))
    with pytest.raises(OrgSchemaMissingError):
        store.provision_member(ALICE, THE_ORG, THE_ORG_NAME)


# --------------------------------------------------------------------------
# Against a real database (opt in with COLLAB_HUB_TEST_POSTGRES_URL)
# --------------------------------------------------------------------------

POSTGRES_URL = os.environ.get("COLLAB_HUB_TEST_POSTGRES_URL", "")

live_postgres = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="set COLLAB_HUB_TEST_POSTGRES_URL to a disposable database to run the live single-org tests",
)

COLLAB_TABLES = (
    "collab_service_access_grants",
    "collab_provisioned_accounts",
    "collab_invitations",
    "collab_org_members",
    "collab_platform_roles",
    "collab_audit_events",
    "collab_orgs",
    "collab_schema_migrations",
)


@pytest.fixture
def migrated_database():
    from collab_hub_api.frames.collab_schema import run_collab_schema_migrations
    from collab_hub_api.frames.db import PostgresDatabase

    def drop_all() -> None:
        with database.connection() as conn:
            for table in COLLAB_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

    database = PostgresDatabase(POSTGRES_URL, min_size=0, max_size=5, timeout_seconds=10.0)
    try:
        drop_all()
        run_collab_schema_migrations(database)
        yield database
        drop_all()
    finally:
        database.close()


@live_postgres
def test_live_provision_admits_creates_the_org_and_respects_removal(migrated_database):
    """The whole write against real DDL: the FK, both conflicts, and removal."""

    store = PostgresOrgStore(migrated_database)

    membership, created = store.provision_member(
        ALICE, THE_ORG, THE_ORG_NAME, email="alice@example.test", display_name="Alice"
    )
    assert created is True and membership.is_active and membership.role == ROLE_MEMBER

    with migrated_database.connection() as conn:
        org = conn.execute("SELECT name, created_by FROM collab_orgs WHERE id = %s", (THE_ORG,)).fetchone()
    assert org == {"name": THE_ORG_NAME, "created_by": SINGLE_ORG_CREATED_BY}

    # The declared name applies only at creation: a second admission neither
    # renames the organization nor duplicates it.
    again, created_again = store.provision_member(BOB, THE_ORG, "A Different Name")
    assert created_again is True and again.org_id == THE_ORG
    with migrated_database.connection() as conn:
        count = conn.execute("SELECT count(*) AS n, min(name) AS name FROM collab_orgs").fetchone()
    assert count == {"n": 1, "name": THE_ORG_NAME}

    # Re-provisioning an existing member is a no-op that returns the row.
    standing, re_created = store.provision_member(ALICE, THE_ORG, THE_ORG_NAME)
    assert re_created is False and standing.is_active

    # And a removed row stands: no resurrection, exactly as the choke point
    # requires for removal to keep meaning removal.
    with migrated_database.connection() as conn:
        conn.execute("UPDATE collab_org_members SET status = 'removed' WHERE user_id = %s", (ALICE,))
    removed, re_admitted = store.provision_member(ALICE, THE_ORG, THE_ORG_NAME)
    assert re_admitted is False and removed.status == MEMBERSHIP_REMOVED

    # The resolution seam sees exactly what membership mode would.
    with pytest.raises(NoOrganizationError):
        auth_context_from_membership(claims_for(ALICE), store, auto_admit=DECLARATION)
    context = auth_context_from_membership(claims_for(BOB), store, auto_admit=DECLARATION)
    assert context is not None and context.org_id == THE_ORG


@live_postgres
def test_live_a_preexisting_org_creating_invitation_cannot_mint_a_second_org(migrated_database, monkeypatch):
    """The acceptance-side half of the guard, against the real transaction.

    An org-creating invitation issued *before* the deployment flipped to
    `single` is still live; accepting it is the write that would make the
    declaration false. The refusal must consume nothing — flip the source
    back, and the same token accepts.
    """

    from collab_hub_api.frames.auth import DisplayIdentity
    from collab_hub_api.frames.invitations import (
        OrganizationCreationRefusedError,
        PostgresInvitationService,
        hash_invitation_secret,
    )

    service = PostgresInvitationService(migrated_database)
    monkeypatch.setenv(ORG_SOURCE_ENV, "membership")
    issued = service.create(_operator_ctx(), email="invitee@example.test", org_id=None)
    token_hash = hash_invitation_secret(issued.raw_secret.reveal())

    def accept():
        return service.accept(
            user_id=BOB,
            display=DisplayIdentity(name="Invitee", email="invitee@example.test", email_verified=True),
            token_hash=token_hash,
            claim_email="invitee@example.test",
            email_verified=True,
        )

    _single_env(monkeypatch)
    with pytest.raises(OrganizationCreationRefusedError):
        accept()
    with migrated_database.connection() as conn:
        row = conn.execute("SELECT status FROM collab_invitations").fetchone()
        orgs = conn.execute("SELECT count(*) AS n FROM collab_orgs").fetchone()
    assert row == {"status": "pending"} and orgs == {"n": 0}

    # Nothing was consumed: under membership the same token still works.
    monkeypatch.setenv(ORG_SOURCE_ENV, "membership")
    accept()
    with migrated_database.connection() as conn:
        settled = conn.execute("SELECT status FROM collab_invitations").fetchone()
    assert settled == {"status": "accepted"}
