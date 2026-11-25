"""Granting and revoking the operator role from the admin panel.

Distinct from the identity-provider sync next door: that reconciles rows it
owns, this is a person deciding. The two must not fight, which is what the
``source`` column is for -- an admin's grant is ``manual`` and sync never
touches it.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("psycopg")

from fake_audit_db import FakeAuditDatabase  # noqa: E402

from collab_hub_api.frames.auth import WORKSPACE_DEFAULT, AuthContext, DisplayIdentity  # noqa: E402
from collab_hub_api.frames.platform_role_admin import (  # noqa: E402
    PlatformRoleChangeRefused,
    PostgresPlatformRoleAdmin,
)

OPERATOR = AuthContext(
    user="u-1",
    home_org_id=None,
    workspace_id=WORKSPACE_DEFAULT,
    display=DisplayIdentity(name="Alice", email="alice@example.com"),
    org_role=None,
    platform_role="operator",
)


def statements(db):
    return [entry for conn in db.connections for entry in conn.statements]


def role_write(db):
    """The statement that changes a role row, skipping the operator-count read."""

    return next(
        (sql, params)
        for sql, params in statements(db)
        if "collab_platform_roles" in sql and not sql.startswith("SELECT")
    )


# What the operator-count read answers: two active operators, so revoking one
# of them leaves the deployment administrable.
TWO_OPERATORS = [[{"user_id": "u-1"}, {"user_id": "u-2"}]]


def audit_row(db):
    for sql, params in statements(db):
        if "INSERT INTO collab_audit_events" in sql:
            return params
    return None


def test_granting_writes_a_hand_administered_row_and_records_it():
    """`manual`, so the provider sync leaves it alone -- see the module note."""

    db = FakeAuditDatabase()
    admin = PostgresPlatformRoleAdmin(db)

    admin.grant(OPERATOR, user_id="u-2", user_label="bob@example.com")

    write = next(sql for sql, _ in statements(db) if "collab_platform_roles" in sql)
    assert "INSERT" in write and "ON CONFLICT (user_id) DO UPDATE" in write
    params = next(p for sql, p in statements(db) if "collab_platform_roles" in sql)
    assert "manual" in params

    actor, _label, action, target_type, target_id, _tl, org_id, _detail = audit_row(db)
    assert (actor, action, target_type, target_id, org_id) == (
        "u-1",
        "platform_role.grant",
        "user",
        "u-2",
        None,
    )


def test_revoking_records_its_own_action():
    db = FakeAuditDatabase(rows=TWO_OPERATORS)
    admin = PostgresPlatformRoleAdmin(db)

    admin.revoke(OPERATOR, user_id="u-2", user_label="bob@example.com")

    write, _params = role_write(db)
    assert "UPDATE" in write
    _actor, _label, action, *_rest = audit_row(db)
    assert action == "platform_role.revoke"


def test_an_admin_revoke_reaches_provider_granted_rows_too():
    """Unlike sync, a person may revoke a row the provider created.

    Taking authority away must never be blocked by where it came from. The row
    stays `idp`, so if that person is still in the group their next sign-in
    restores it -- which is correct, and is why the panel says so.
    """

    db = FakeAuditDatabase(rows=TWO_OPERATORS)
    admin = PostgresPlatformRoleAdmin(db)

    admin.revoke(OPERATOR, user_id="u-2", user_label="bob@example.com")

    write, _params = role_write(db)
    assert "source" not in write


def test_an_operator_cannot_revoke_their_own_role():
    """One mis-click on their own row would lock them out, and the only way
    back is psql."""

    db = FakeAuditDatabase(rows=TWO_OPERATORS)
    admin = PostgresPlatformRoleAdmin(db)

    with pytest.raises(PlatformRoleChangeRefused) as refused:
        admin.revoke(OPERATOR, user_id="u-1")

    assert refused.value.reason == "self_revoke"
    assert statements(db) == []


def test_the_last_active_operator_cannot_be_revoked():
    """Read under a row lock in the revoke's own transaction, so two operators
    revoking each other at once cannot both succeed and leave nobody."""

    db = FakeAuditDatabase(rows=[[{"user_id": "u-2"}]])
    admin = PostgresPlatformRoleAdmin(db)

    with pytest.raises(PlatformRoleChangeRefused) as refused:
        admin.revoke(OPERATOR, user_id="u-2")

    assert refused.value.reason == "last_operator"
    read, _params = statements(db)[0]
    assert read.startswith("SELECT") and "FOR UPDATE" in read
    assert not any(sql.startswith("UPDATE") for sql, _ in statements(db))
    assert audit_row(db) is None
    assert db.connections[0].outcome == "rollback"


# --------------------------------------------------------------------------
# Against a real database (opt in with COLLAB_HUB_TEST_POSTGRES_URL)
# --------------------------------------------------------------------------

POSTGRES_URL = os.environ.get("COLLAB_HUB_TEST_POSTGRES_URL", "")

live_postgres = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="set COLLAB_HUB_TEST_POSTGRES_URL to a disposable database to run the live role tests",
)

COLLAB_TABLES = (
    "collab_frame_moves",
    "collab_connector_state",
    "collab_service_access_grants",
    "collab_provisioned_accounts",
    "collab_invitations",
    "collab_audit_events",
    "collab_platform_roles",
    "collab_org_members",
    "collab_orgs",
    "collab_schema_migrations",
)


@pytest.fixture
def live_database():
    from collab_hub_api.frames.collab_schema import run_collab_schema_migrations
    from collab_hub_api.frames.db import PostgresDatabase

    database = PostgresDatabase(POSTGRES_URL, min_size=0, max_size=4, timeout_seconds=10.0)

    def drop_all() -> None:
        with database.connection() as conn:
            for table in COLLAB_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

    try:
        drop_all()
        run_collab_schema_migrations(database)
        yield database
        drop_all()
    finally:
        database.close()


def _active_operators(database) -> set[str]:
    with database.connection() as conn:
        rows = conn.execute(
            "SELECT user_id FROM collab_platform_roles WHERE role = 'operator' AND status = 'active'"
        ).fetchall()
    return {row["user_id"] for row in rows}


@live_postgres
def test_live_revoke_keeps_the_last_operator_and_writes_no_audit_row(live_database):
    admin = PostgresPlatformRoleAdmin(live_database)
    admin.grant(OPERATOR, user_id="u-1")
    admin.grant(OPERATOR, user_id="u-2")

    admin.revoke(OPERATOR, user_id="u-2")
    assert _active_operators(live_database) == {"u-1"}

    other = AuthContext(
        user="u-3",
        home_org_id=None,
        workspace_id=WORKSPACE_DEFAULT,
        display=DisplayIdentity(name="Carol", email="carol@example.com"),
        org_role=None,
        platform_role="operator",
    )
    with pytest.raises(PlatformRoleChangeRefused):
        admin.revoke(other, user_id="u-1")

    assert _active_operators(live_database) == {"u-1"}
    with live_database.connection() as conn:
        revokes = conn.execute(
            "SELECT count(*) AS n FROM collab_audit_events WHERE action = 'platform_role.revoke'"
        ).fetchone()["n"]
    assert revokes == 1


@live_postgres
def test_live_role_rows_for_a_page_of_people_come_back_in_one_read(live_database):
    from collab_hub_api.frames.orgs import PostgresOrgStore

    admin = PostgresPlatformRoleAdmin(live_database)
    admin.grant(OPERATOR, user_id="u-1")
    admin.grant(OPERATOR, user_id="u-2")
    admin.revoke(OPERATOR, user_id="u-2")

    rows = PostgresOrgStore(live_database).get_platform_role_rows(["u-1", "u-2", "u-9"])

    assert rows == {
        "u-1": {"role": "operator", "status": "active", "source": "manual"},
        "u-2": {"role": "operator", "status": "revoked", "source": "manual"},
    }


def _oidc_audit_rows(database) -> list[str]:
    with database.connection() as conn:
        rows = conn.execute(
            "SELECT action FROM collab_audit_events WHERE detail->>'origin' = 'oidc' ORDER BY id"
        ).fetchall()
    return [row["action"] for row in rows]


def _stale_sync(database, stale_row):
    """A sync whose read of the row predates a hand-run change.

    The sync reads the row on one connection and writes on another, so a
    grant or revoke from the panel can land in between. The write is guarded
    against that; this reproduces the window without timing threads.
    """

    from collab_hub_api.frames.platform_role_sync import PostgresPlatformRoleSync

    sync = PostgresPlatformRoleSync(database, admin_group="/hub-admins")
    sync._current_row = lambda _user_id: stale_row
    return sync


@live_postgres
def test_live_sync_records_no_grant_when_a_hand_revoke_won_the_race(live_database):
    admin = PostgresPlatformRoleAdmin(live_database)
    admin.grant(OPERATOR, user_id="u-1")
    admin.grant(OPERATOR, user_id="u-2")
    admin.revoke(OPERATOR, user_id="u-2")
    sync = _stale_sync(live_database, {"role": "operator", "status": "revoked", "source": "idp"})

    sync.reconcile(user_id="u-2", claim_groups=["/hub-admins"], display=OPERATOR.display)

    assert _active_operators(live_database) == {"u-1"}
    assert _oidc_audit_rows(live_database) == []


@live_postgres
def test_live_sync_records_no_revoke_when_a_hand_grant_won_the_race(live_database):
    admin = PostgresPlatformRoleAdmin(live_database)
    admin.grant(OPERATOR, user_id="u-2")
    sync = _stale_sync(live_database, {"role": "operator", "status": "active", "source": "idp"})

    sync.reconcile(user_id="u-2", claim_groups=[], display=OPERATOR.display)

    assert _active_operators(live_database) == {"u-2"}
    assert _oidc_audit_rows(live_database) == []


def test_revoking_someone_who_holds_no_active_role_is_refused_and_unrecorded():
    """Nothing would change, so nothing may be recorded: an audit row saying a
    role was revoked when none was held would be believed."""

    db = FakeAuditDatabase(rows=[[{"user_id": "u-1"}, {"user_id": "u-3"}]])
    admin = PostgresPlatformRoleAdmin(db)

    with pytest.raises(PlatformRoleChangeRefused) as refused:
        admin.revoke(OPERATOR, user_id="u-2")

    assert refused.value.reason == "not_operator"
    assert audit_row(db) is None
    assert not any(sql.startswith("UPDATE") for sql, _ in statements(db))


@live_postgres
def test_live_a_second_revoke_of_the_same_person_records_nothing(live_database):
    admin = PostgresPlatformRoleAdmin(live_database)
    admin.grant(OPERATOR, user_id="u-1")
    admin.grant(OPERATOR, user_id="u-2")
    admin.revoke(OPERATOR, user_id="u-2")

    with pytest.raises(PlatformRoleChangeRefused):
        admin.revoke(OPERATOR, user_id="u-2")

    with live_database.connection() as conn:
        revokes = conn.execute(
            "SELECT count(*) AS n FROM collab_audit_events WHERE action = 'platform_role.revoke'"
        ).fetchone()["n"]
    assert revokes == 1


@live_postgres
def test_live_sync_keeps_the_last_operator_after_a_panel_revoke(live_database):
    """The race from the review, serialized: the panel revokes B while A's
    sign-in carries no admin group. Whichever commits second must find it
    would remove the last operator."""

    from collab_hub_api.frames.platform_role_sync import PostgresPlatformRoleSync

    sync = PostgresPlatformRoleSync(live_database, admin_group="/hub-admins")
    sync.reconcile(user_id="u-1", claim_groups=["/hub-admins"], display=OPERATOR.display)
    sync.reconcile(user_id="u-2", claim_groups=["/hub-admins"], display=OPERATOR.display)

    PostgresPlatformRoleAdmin(live_database).revoke(OPERATOR, user_id="u-2")
    role = sync.reconcile(user_id="u-1", claim_groups=[], display=OPERATOR.display)

    assert role == "operator"
    assert _active_operators(live_database) == {"u-1"}
