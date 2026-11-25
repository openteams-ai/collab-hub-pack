"""Self-service organizations (issue #365).

Under ``orgSource=self_service`` a verified login with no membership row gets
an organization of its own, with a generated unique name; an invitation into
another organization moves the owner out of it while nobody else has joined,
taking their frames along as ``private``.

The unit tests run against the in-memory store. The live tests (opt in with
``COLLAB_HUB_TEST_POSTGRES_URL``) exercise the real transactions.
"""

from __future__ import annotations

import os
import threading

import pytest

from collab_hub_api.frames.auth import (
    WORKSPACE_DEFAULT,
    AuthContext,
    DisplayIdentity,
    NoOrganizationError,
    auth_context_from_membership,
)
from collab_hub_api.frames.identity import IDENTITY_CLAIM_ENV
from collab_hub_api.frames.org_source import (
    ORG_SOURCE_ENV,
    enforce_membership_org_source_preconditions,
    org_source_is_self_service,
    org_source_is_single,
    org_source_resolves_membership,
)
from collab_hub_api.frames.orgs import (
    MEMBERSHIP_REMOVED,
    ROLE_MEMBER,
    ROLE_OWNER,
    InMemoryOrgStore,
    OrgsUnavailableError,
    UnavailableOrgStore,
)
from collab_hub_api.frames.self_service import (
    GENERATED_NAME_PREFIX,
    SelfServicePolicy,
    generated_name_candidates,
    self_service_policy,
)

ALICE = "11111111-1111-4111-8111-111111111111"
BOB = "22222222-2222-4222-8222-222222222222"
CAROL = "33333333-3333-4333-8333-333333333333"
ALICE_EMAIL = "alice@example.test"


@pytest.fixture(autouse=True)
def _self_service_env(monkeypatch):
    monkeypatch.setenv(IDENTITY_CLAIM_ENV, "sub")
    monkeypatch.setenv(ORG_SOURCE_ENV, "self_service")
    for name in ("FRAMES_AUTH_DEFAULT_ORG", "FRAMES_AUTH_DEFAULT_WORKSPACE"):
        monkeypatch.delenv(name, raising=False)


def claims(sub: str, *, email: str | None = ALICE_EMAIL, verified: object = True, name: str | None = "Alice") -> dict:
    out: dict = {"sub": sub, "email_verified": verified}
    if email is not None:
        out["email"] = email
    if name is not None:
        out["name"] = name
    return out


# ---------------------------------------------------------------------------
# The org source
# ---------------------------------------------------------------------------


def test_self_service_is_a_membership_resolving_source():
    assert org_source_resolves_membership() is True
    assert org_source_is_self_service() is True
    assert org_source_is_single() is False


def test_self_service_requires_the_identity_pin(monkeypatch):
    monkeypatch.delenv(IDENTITY_CLAIM_ENV)
    with pytest.raises(RuntimeError, match="self_service requires"):
        enforce_membership_org_source_preconditions()


def test_self_service_refuses_a_leftover_default_fallback(monkeypatch):
    monkeypatch.setenv("FRAMES_AUTH_DEFAULT_ORG", "acme")
    with pytest.raises(RuntimeError, match="must be unset"):
        enforce_membership_org_source_preconditions()


def test_no_policy_off_self_service(monkeypatch):
    monkeypatch.setenv(ORG_SOURCE_ENV, "membership")
    assert self_service_policy(object()) is None


class _Granter:
    def __init__(self, configured: bool = True) -> None:
        self.configured = configured
        self.calls: list[tuple[str, str]] = []

    def grant(self, *, user_id: str, group_path: str) -> None:
        self.calls.append((user_id, group_path))


class _State:
    def __init__(self, granter=None, groups=(), invitation_service=None) -> None:
        self.service_access_granter = granter
        self.granted_service_groups = groups
        self.invitation_service = invitation_service


def test_the_policy_owes_groups_only_where_a_grant_can_be_attempted():
    assert self_service_policy(_State(_Granter(), ("/llm",))).service_groups == ("/llm",)
    assert self_service_policy(_State(_Granter(configured=False), ("/llm",))).service_groups == ()
    assert self_service_policy(_State(None, ("/llm",))).service_groups == ()
    assert self_service_policy(_State(_Granter(), ())).service_groups == ()


def test_generated_names_start_with_the_address_and_stay_distinct():
    names = list(generated_name_candidates(ALICE_EMAIL, ALICE))
    assert names[0] == f"{GENERATED_NAME_PREFIX}{ALICE_EMAIL}"
    assert names[1] == f"{GENERATED_NAME_PREFIX}{ALICE_EMAIL} (11111111)"
    assert len(set(names)) == len(names)


# ---------------------------------------------------------------------------
# Admission at the auth choke point
# ---------------------------------------------------------------------------


def test_a_verified_login_with_no_row_gets_its_own_organization_as_owner():
    store = InMemoryOrgStore()
    context = auth_context_from_membership(claims(ALICE), store, self_service=SelfServicePolicy())

    assert context is not None and context.org_role == ROLE_OWNER
    assert context.org_id in store.self_service_orgs
    roster = store.get_roster(context.org_id)
    assert roster is not None and roster.name == f"{GENERATED_NAME_PREFIX}{ALICE_EMAIL}"


@pytest.mark.parametrize(
    "token",
    [
        claims(ALICE, verified=False),
        claims(ALICE, verified="true"),
        claims(ALICE, email=None),
        {"sub": ALICE},
    ],
)
def test_a_login_without_a_verified_email_is_not_admitted(token):
    store = InMemoryOrgStore()
    with pytest.raises(NoOrganizationError):
        auth_context_from_membership(token, store, self_service=SelfServicePolicy())
    assert store.get_membership(ALICE) is None


def test_admission_happens_once():
    store = InMemoryOrgStore()
    created: list[str] = []
    policy = SelfServicePolicy(
        service_groups=("/llm",), on_created=lambda user, display, admission: created.append(user)
    )

    first = auth_context_from_membership(claims(ALICE), store, self_service=policy)
    second = auth_context_from_membership(claims(ALICE), store, self_service=policy)

    assert first.org_id == second.org_id
    assert created == [ALICE]
    assert store.owed_service_groups[ALICE] == ("/llm",)


def test_an_existing_member_is_never_moved_and_a_removal_is_never_resurrected():
    store = InMemoryOrgStore()
    store.set_membership(BOB, "acme", ROLE_MEMBER)
    context = auth_context_from_membership(claims(BOB), store, self_service=SelfServicePolicy())
    assert context.org_id == "acme" and context.org_role == ROLE_MEMBER

    store.set_membership(CAROL, "acme", ROLE_MEMBER, status=MEMBERSHIP_REMOVED)
    with pytest.raises(NoOrganizationError):
        auth_context_from_membership(claims(CAROL), store, self_service=SelfServicePolicy())
    assert store.get_membership(CAROL).org_id == "acme"


def test_a_failing_post_commit_step_does_not_fail_the_request():
    def explode(*_):
        raise RuntimeError("identity provider down")

    store = InMemoryOrgStore()
    context = auth_context_from_membership(claims(ALICE), store, self_service=SelfServicePolicy(on_created=explode))
    assert context.org_role == ROLE_OWNER


def test_an_unavailable_store_raises_rather_than_admitting():
    with pytest.raises(OrgsUnavailableError):
        auth_context_from_membership(claims(ALICE), UnavailableOrgStore(), self_service=SelfServicePolicy())


def test_the_policy_grants_owed_groups_after_creation():
    granter = _Granter()
    policy = self_service_policy(_State(granter, ("/llm",)))
    store = InMemoryOrgStore()
    auth_context_from_membership(claims(ALICE), store, self_service=policy)
    auth_context_from_membership(claims(ALICE), store, self_service=policy)
    assert granter.calls == [(ALICE, "/llm")]


# ---------------------------------------------------------------------------
# The acceptance page keeps the token across tabs
# ---------------------------------------------------------------------------


def test_the_acceptance_page_banks_the_token_in_local_storage_with_an_expiry():
    from collab_hub_api.frames.invitations import INVITATION_TTL
    from collab_hub_api.web.acceptance import ACCEPTANCE_SCRIPT

    assert "window.localStorage" in ACCEPTANCE_SCRIPT
    assert "sessionStorage" not in ACCEPTANCE_SCRIPT
    assert f"var TOKEN_TTL_MS = {int(INVITATION_TTL.total_seconds() * 1000)};" in ACCEPTANCE_SCRIPT
    assert "write(TOKEN_KEY, fresh, TOKEN_TTL_MS)" in ACCEPTANCE_SCRIPT
    assert "write(OUTCOME_KEY, outcome, OUTCOME_TTL_MS)" in ACCEPTANCE_SCRIPT


# ===========================================================================
# Against a real database
# ===========================================================================

POSTGRES_URL = os.environ.get("COLLAB_HUB_TEST_POSTGRES_URL", "")

live_postgres = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="set COLLAB_HUB_TEST_POSTGRES_URL to a disposable database to run the live self-service tests",
)

COLLAB_TABLES = (
    "collab_frame_moves",
    "frames_server_groups",
    "collab_service_access_grants",
    "collab_provisioned_accounts",
    "collab_invitations",
    "collab_org_members",
    "collab_platform_roles",
    "collab_audit_events",
    "collab_orgs",
    "collab_schema_migrations",
)

TEAM = "team0000000000000000000000000000"
TEAM_OWNER = "44444444-4444-4444-8444-444444444444"


@pytest.fixture
def live_db():
    from collab_hub_api.frames.collab_schema import run_collab_schema_migrations
    from collab_hub_api.frames.db import PostgresDatabase

    database = PostgresDatabase(POSTGRES_URL, min_size=0, max_size=8, timeout_seconds=15.0)

    def drop_all() -> None:
        with database.connection() as conn:
            for table in COLLAB_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

    try:
        drop_all()
        run_collab_schema_migrations(database)
        with database.connection() as conn:
            conn.execute("INSERT INTO collab_orgs (id, name, created_by) VALUES (%s, %s, %s)", (TEAM, "Team", "op"))
            conn.execute(
                "INSERT INTO collab_org_members (user_id, org_id, role) VALUES (%s, %s, %s)",
                (TEAM_OWNER, TEAM, ROLE_OWNER),
            )
        yield database
        drop_all()
    finally:
        database.close()


def _rows(database, sql: str, params=()) -> list[dict]:
    with database.connection() as conn:
        return conn.execute(sql, params).fetchall()


def _display(email: str = ALICE_EMAIL, name: str | None = "Alice") -> DisplayIdentity:
    return DisplayIdentity(name=name, email=email, email_verified=True)


def _ctx(user: str, org_id: str | None, role: str | None) -> AuthContext:
    return AuthContext(
        user=user,
        home_org_id=org_id,
        workspace_id=WORKSPACE_DEFAULT,
        display=_display(f"{user[:4]}@example.test"),
        org_role=role,
    )


def _admit(database, user: str = ALICE, email: str = ALICE_EMAIL, groups=()):
    from collab_hub_api.frames.orgs import PostgresOrgStore

    return PostgresOrgStore(database).provision_self_service_org(user, _display(email), service_groups=groups)


def _accept(service, user: str, secret: str, email: str):
    from collab_hub_api.frames.invitations import hash_invitation_secret

    return service.accept(
        user_id=user,
        display=_display(email),
        token_hash=hash_invitation_secret(secret),
        claim_email=email,
        email_verified=True,
    )


@live_postgres
def test_live_admission_writes_the_org_the_owner_row_the_audit_row_and_the_owed_grant(live_db):
    admission = _admit(live_db, groups=("/llm",))
    assert admission.created is True and admission.membership.role == ROLE_OWNER
    org_id = admission.membership.org_id

    (org,) = _rows(
        live_db, "SELECT name, created_by, self_service, name_generated FROM collab_orgs WHERE id = %s", (org_id,)
    )
    assert org == {
        "name": f"{GENERATED_NAME_PREFIX}{ALICE_EMAIL}",
        "created_by": ALICE,
        "self_service": True,
        "name_generated": True,
    }
    (member,) = _rows(live_db, "SELECT org_id, role, email FROM collab_org_members WHERE user_id = %s", (ALICE,))
    assert member == {"org_id": org_id, "role": ROLE_OWNER, "email": ALICE_EMAIL}
    (audit,) = _rows(live_db, "SELECT actor, action, target_id, org_id, detail FROM collab_audit_events")
    assert audit["action"] == "org.create" and audit["actor"] == ALICE and audit["org_id"] == org_id
    assert audit["detail"]["self_service"] is True
    (owed,) = _rows(live_db, "SELECT group_path, state, invitation FROM collab_service_access_grants")
    assert owed == {"group_path": "/llm", "state": "pending", "invitation": None}

    again = _admit(live_db, groups=("/llm",))
    assert again.created is False and again.membership.org_id == org_id
    assert len(_rows(live_db, "SELECT id FROM collab_orgs WHERE self_service")) == 1
    assert len(_rows(live_db, "SELECT id FROM collab_audit_events")) == 1


@live_postgres
def test_live_a_taken_generated_name_gets_a_suffix(live_db):
    with live_db.connection() as conn:
        conn.execute(
            "INSERT INTO collab_orgs (id, name, created_by, self_service, name_generated)"
            " VALUES (%s, %s, %s, true, true)",
            ("old0000000000000000000000000000a", f"{GENERATED_NAME_PREFIX}{ALICE_EMAIL}", "deleted-account"),
        )
    admission = _admit(live_db)
    (org,) = _rows(live_db, "SELECT name FROM collab_orgs WHERE id = %s", (admission.membership.org_id,))
    assert org["name"] == f"{GENERATED_NAME_PREFIX}{ALICE_EMAIL} (11111111)"


@live_postgres
def test_live_concurrent_first_requests_create_one_organization(live_db):
    results = []
    barrier = threading.Barrier(4)

    def admit():
        barrier.wait()
        results.append(_admit(live_db))

    threads = [threading.Thread(target=admit) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(1 for result in results if result.created) == 1
    assert len({result.membership.org_id for result in results}) == 1
    assert len(_rows(live_db, "SELECT id FROM collab_orgs WHERE self_service")) == 1


@live_postgres
def test_live_a_generated_name_must_be_replaced_before_inviting_and_can_be_replaced_once(live_db):
    from collab_hub_api.frames.invitations import OrganizationAlreadyNamedError, PostgresInvitationService

    service = PostgresInvitationService(live_db)
    org_id = _admit(live_db).membership.org_id
    owner = _ctx(ALICE, org_id, ROLE_OWNER)

    assert service.organization_needs_name(org_id) is True
    assert service.organization_needs_name(TEAM) is False
    assert service.name_organization(owner, org_id=org_id, name="Alice Consulting") == "Alice Consulting"
    assert service.organization_needs_name(org_id) is False
    (org,) = _rows(live_db, "SELECT name, name_generated FROM collab_orgs WHERE id = %s", (org_id,))
    assert org == {"name": "Alice Consulting", "name_generated": False}
    with pytest.raises(OrganizationAlreadyNamedError):
        service.name_organization(owner, org_id=org_id, name="Something Else")


@live_postgres
def test_live_an_invitation_moves_the_owner_out_of_an_untouched_self_service_org(live_db):
    from collab_hub_api.frames.invitations import PostgresInvitationService

    service = PostgresInvitationService(live_db)
    personal = _admit(live_db).membership.org_id
    # A pending invitation issued from the personal organization, and a frame
    # group in it.
    outgoing = service.create(_ctx(ALICE, personal, ROLE_OWNER), email="friend@example.test", org_id=personal)
    with live_db.connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS frames_server_groups (
                id text PRIMARY KEY, org_id text NOT NULL, visibility text NOT NULL DEFAULT 'private',
                updated_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        conn.execute(
            "INSERT INTO frames_server_groups (id, org_id, visibility) VALUES ('g1', %s, 'internal')", (personal,)
        )
    invitation = service.create(_ctx(TEAM_OWNER, TEAM, ROLE_OWNER), email=ALICE_EMAIL, org_id=TEAM)

    acceptance = _accept(service, ALICE, invitation.raw_secret.reveal(), ALICE_EMAIL)

    assert acceptance.org_id == TEAM and acceptance.moved_from_org_id == personal
    (member,) = _rows(live_db, "SELECT org_id, role, status FROM collab_org_members WHERE user_id = %s", (ALICE,))
    assert member == {"org_id": TEAM, "role": ROLE_MEMBER, "status": "active"}
    (retired,) = _rows(live_db, "SELECT retired_at FROM collab_orgs WHERE id = %s", (personal,))
    assert retired["retired_at"] is not None
    (revoked,) = _rows(
        live_db, "SELECT status, revoked_by FROM collab_invitations WHERE id = %s", (outgoing.invitation.id,)
    )
    assert revoked == {"status": "revoked", "revoked_by": ALICE}
    (group,) = _rows(live_db, "SELECT org_id, visibility FROM frames_server_groups WHERE id = 'g1'")
    assert group == {"org_id": TEAM, "visibility": "private"}
    (move,) = _rows(live_db, "SELECT from_org_id, to_org_id, user_id, state FROM collab_frame_moves")
    assert move == {"from_org_id": personal, "to_org_id": TEAM, "user_id": ALICE, "state": "pending"}
    redeem = _rows(live_db, "SELECT detail FROM collab_audit_events WHERE action = 'invitation.redeem'")
    assert redeem[-1]["detail"]["moved_from_org"] == personal


@live_postgres
def test_live_a_self_service_org_someone_else_joined_is_not_left(live_db):
    from collab_hub_api.frames.invitations import AlreadyInOrganizationError, PostgresInvitationService

    service = PostgresInvitationService(live_db)
    personal = _admit(live_db).membership.org_id
    with live_db.connection() as conn:
        conn.execute(
            "INSERT INTO collab_org_members (user_id, org_id, role, status) VALUES (%s, %s, %s, 'removed')",
            (BOB, personal, ROLE_MEMBER),
        )
    invitation = service.create(_ctx(TEAM_OWNER, TEAM, ROLE_OWNER), email=ALICE_EMAIL, org_id=TEAM)

    with pytest.raises(AlreadyInOrganizationError):
        _accept(service, ALICE, invitation.raw_secret.reveal(), ALICE_EMAIL)
    (member,) = _rows(live_db, "SELECT org_id FROM collab_org_members WHERE user_id = %s", (ALICE,))
    assert member["org_id"] == personal
    (pending,) = _rows(live_db, "SELECT status FROM collab_invitations WHERE id = %s", (invitation.invitation.id,))
    assert pending["status"] == "pending"
    assert _rows(live_db, "SELECT * FROM collab_frame_moves") == []


@live_postgres
def test_live_a_member_of_an_ordinary_org_is_still_refused(live_db):
    from collab_hub_api.frames.invitations import AlreadyInOrganizationError, PostgresInvitationService

    service = PostgresInvitationService(live_db)
    other = "other000000000000000000000000000"
    with live_db.connection() as conn:
        conn.execute("INSERT INTO collab_orgs (id, created_by) VALUES (%s, 'op')", (other,))
        conn.execute("INSERT INTO collab_org_members (user_id, org_id, role) VALUES (%s, %s, 'owner')", (CAROL, other))
    invitation = service.create(_ctx(TEAM_OWNER, TEAM, ROLE_OWNER), email="carol@example.test", org_id=TEAM)
    with pytest.raises(AlreadyInOrganizationError):
        _accept(service, CAROL, invitation.raw_secret.reveal(), "carol@example.test")


@live_postgres
def test_live_frames_follow_their_owner_and_become_private(live_db, tmp_path):
    from collab_hub_api.frames.invitations import PostgresInvitationService
    from collab_hub_api.frames.models import Visibility
    from collab_hub_api.frames.store import LocalFsFrameStore

    frames = LocalFsFrameStore(tmp_path / "frames")
    service = PostgresInvitationService(live_db)
    personal = _admit(live_db).membership.org_id
    made = [
        frames.create_frame(personal, WORKSPACE_DEFAULT, ALICE, [ALICE], f"Frame {v.value}", "", v, [], "# Body\n")
        for v in (Visibility.private, Visibility.internal, Visibility.public)
    ]
    elsewhere = frames.create_frame(
        TEAM, WORKSPACE_DEFAULT, TEAM_OWNER, [TEAM_OWNER], "Team frame", "", Visibility.internal, [], "# B\n"
    )
    invitation = service.create(_ctx(TEAM_OWNER, TEAM, ROLE_OWNER), email=ALICE_EMAIL, org_id=TEAM)
    acceptance = _accept(service, ALICE, invitation.raw_secret.reveal(), ALICE_EMAIL)

    assert service.complete_frame_moves(frames, from_org_id=acceptance.moved_from_org_id) == 1

    for frame in made:
        moved = frames.get_frame(frame.id)
        assert moved.org_id == TEAM and moved.visibility == Visibility.private and moved.owners == [ALICE]
    assert frames.get_frame(elsewhere.id).visibility == Visibility.internal
    (move,) = _rows(live_db, "SELECT state, moved FROM collab_frame_moves")
    assert move == {"state": "done", "moved": 3}
    # Done is done: the sweep finds nothing left to do.
    assert service.complete_frame_moves(frames) == 0


@live_postgres
def test_live_a_failed_frame_move_stays_pending_for_the_sweep(live_db, tmp_path):
    from collab_hub_api.frames.invitations import PostgresInvitationService
    from collab_hub_api.frames.models import Visibility
    from collab_hub_api.frames.store import LocalFsFrameStore

    class Flaky(LocalFsFrameStore):
        fail = True

        def move_to_org(self, frame_id, org_id):
            if self.fail:
                raise OSError("object store unavailable")
            return super().move_to_org(frame_id, org_id)

    frames = Flaky(tmp_path / "frames")
    service = PostgresInvitationService(live_db)
    personal = _admit(live_db).membership.org_id
    frame = frames.create_frame(
        personal, WORKSPACE_DEFAULT, ALICE, [ALICE], "Mine", "", Visibility.internal, [], "# B\n"
    )
    invitation = service.create(_ctx(TEAM_OWNER, TEAM, ROLE_OWNER), email=ALICE_EMAIL, org_id=TEAM)
    _accept(service, ALICE, invitation.raw_secret.reveal(), ALICE_EMAIL)

    assert service.complete_frame_moves(frames) == 0
    assert _rows(live_db, "SELECT state FROM collab_frame_moves")[0]["state"] == "pending"

    frames.fail = False
    assert service.complete_frame_moves(frames) == 1
    assert frames.get_frame(frame.id).org_id == TEAM
