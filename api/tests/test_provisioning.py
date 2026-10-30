"""The account-provisioning claim (issue #172).

What these prove: that exactly one caller per address is told it may create an
account, that the winner is decided by the **folded** address so case variants
collide, that phases only move forwards, and that an unfinished claim stays
visible instead of being swept away.

The load-bearing test is
:func:`test_live_two_requests_for_one_address_yield_one_creator`. #172 records
per-address serialization as a contract, and the review that approved the
design named this as its acceptance proof — because the primitive the codebase
already had, ``pg_advisory_xact_lock``, cannot provide it: invitation rows
commit *before* provisioning starts, and that lock is gone at commit.
"""

from __future__ import annotations

import os
import threading

import pytest

from collab_hub_api.frames.provisioning import (
    PHASE_ACCOUNT_CREATED,
    PHASE_CLAIMED,
    PHASE_COMPLETE,
    PHASE_SETUP_SENT,
    PHASES,
    ProvisioningStore,
    folded_key,
)

POSTGRES_URL = os.environ.get("COLLAB_HUB_TEST_POSTGRES_URL", "")

live_postgres = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="set COLLAB_HUB_TEST_POSTGRES_URL to a disposable database to run the provisioning tests",
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

INVITEE = "Provision.Me@Example.COM"


def test_the_key_is_the_folded_address_gate_b_matches_on():
    """Case variants must be one key, or two accounts get provisioned for an
    address only one of which can ever accept (#157's amendment, applied here)."""

    assert folded_key("Alice@Example.COM") == folded_key("alice@example.com")
    assert folded_key("alice@example.com") == "alice@example.com"
    # Folding is ASCII-only, deliberately: the Kelvin sign is not "k".
    assert folded_key("Klelvin@example.com") != "kelvin@example.com"


def test_phases_are_ordered_because_recovery_reads_them():
    assert PHASES == (PHASE_CLAIMED, PHASE_ACCOUNT_CREATED, PHASE_SETUP_SENT, PHASE_COMPLETE)


@pytest.fixture
def live_db():
    from collab_hub_api.frames.collab_schema import run_collab_schema_migrations
    from collab_hub_api.frames.db import PostgresDatabase

    database = PostgresDatabase(POSTGRES_URL, min_size=0, max_size=8, timeout_seconds=15.0)

    def drop():
        with database.connection() as conn:
            for table in COLLAB_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

    try:
        drop()
        run_collab_schema_migrations(database)
        yield database
        drop()
    finally:
        database.close()


def _an_invitation(db, invitation_id: str = "inv-provisioning") -> str:
    """A row to point `first_invitation` at. The FK is the point: a claim
    without an invitation is not a state this workflow can reach."""

    with db.connection() as conn:
        conn.execute(
            """
            INSERT INTO collab_invitations (id, email, token_hash, created_by, expires_at)
            VALUES (%s, %s, %s, %s, now() + interval '48 hours')
            ON CONFLICT (id) DO NOTHING
            """,
            (invitation_id, INVITEE, f"hash-{invitation_id}", "operator-subject"),
        )
    return invitation_id


@live_postgres
def test_live_a_claim_is_recorded_at_the_claimed_phase(live_db):
    inv = _an_invitation(live_db)
    store = ProvisioningStore(live_db)

    claim = store.claim(email=INVITEE, invitation_id=inv)

    assert claim.mine is True
    assert claim.record.phase == PHASE_CLAIMED
    assert claim.record.idp_user_id is None
    assert claim.record.email == INVITEE, "the address is stored as typed, for display"
    assert claim.record.email_folded == folded_key(INVITEE), "and keyed folded, for matching"
    assert claim.record.may_be_written_to is True


@live_postgres
def test_live_the_second_caller_is_not_the_creator(live_db):
    """Sequential version of the concurrency proof: same address, two calls."""

    inv = _an_invitation(live_db)
    store = ProvisioningStore(live_db)

    first = store.claim(email=INVITEE, invitation_id=inv)
    second = store.claim(email="provision.me@example.com", invitation_id=inv)

    assert first.mine is True
    assert second.mine is False, "a case variant is the same address, so the claim is held"
    assert second.record.first_invitation == inv


@live_postgres
def test_live_two_requests_for_one_address_yield_one_creator(live_db):
    """**The acceptance proof for #172's serialization contract.**

    Two threads claim the same folded address at once, after their invitation
    rows exist — which is the state the real workflow is in, because the
    invitation commits *before* provisioning starts. Exactly one may create an
    account; the other must be handed the existing record.

    A transaction-scoped advisory lock cannot make this pass, which is why the
    claim is a row with a primary key rather than a lock: by the time
    provisioning begins, the transaction that held such a lock has committed
    and released it.
    """

    _an_invitation(live_db, "inv-race-a")
    _an_invitation(live_db, "inv-race-b")
    store = ProvisioningStore(live_db)

    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, bool]] = []
    lock = threading.Lock()

    def attempt(label: str, address: str, invitation_id: str) -> None:
        barrier.wait(timeout=10)
        claim = store.claim(email=address, invitation_id=invitation_id)
        with lock:
            outcomes.append((label, claim.mine))

    threads = [
        threading.Thread(target=attempt, args=("upper", "Provision.Me@Example.COM", "inv-race-a")),
        threading.Thread(target=attempt, args=("lower", "provision.me@example.com", "inv-race-b")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
        assert not t.is_alive(), "a claim blocked instead of conflicting"

    assert len(outcomes) == 2
    creators = [label for label, mine in outcomes if mine]
    assert len(creators) == 1, f"exactly one creator per address, got {outcomes}"

    # And both invitations remain valid: serialization bounds *provisioning*,
    # not issuance. Two live invitations for one address stay legal (#91's
    # scope, pinned in test_operator_invite_page.py).
    with live_db.connection() as conn:
        rows = conn.execute("SELECT id FROM collab_invitations WHERE id IN ('inv-race-a', 'inv-race-b')").fetchall()
    assert len(rows) == 2


@live_postgres
def test_live_a_claim_can_enlist_in_the_callers_transaction(live_db):
    """The claim commits with the invitation it belongs to, so a committed
    invitation always has something to resume from."""

    inv = _an_invitation(live_db, "inv-enlisted")
    store = ProvisioningStore(live_db)

    with live_db.connection() as conn:
        claim = store.claim(email=INVITEE, invitation_id=inv, conn=conn)
        assert claim.mine is True

    assert store.get(INVITEE) is not None, "the claim committed with the caller's transaction"


@live_postgres
def test_live_phases_move_forwards_only(live_db):
    inv = _an_invitation(live_db)
    store = ProvisioningStore(live_db)
    store.claim(email=INVITEE, invitation_id=inv)

    created = store.advance(email=INVITEE, phase=PHASE_ACCOUNT_CREATED, idp_user_id="kc-user-1")
    assert created.phase == PHASE_ACCOUNT_CREATED
    assert created.idp_user_id == "kc-user-1"

    sent = store.advance(email=INVITEE, phase=PHASE_SETUP_SENT)
    assert sent.idp_user_id == "kc-user-1", "the account id survives a phase that does not carry one"

    # Re-asserting the current phase is a no-op, so a resumed send need not
    # special-case itself.
    assert store.advance(email=INVITEE, phase=PHASE_SETUP_SENT).phase == PHASE_SETUP_SENT

    done = store.advance(email=INVITEE, phase=PHASE_COMPLETE)
    assert done.is_complete
    assert done.may_be_written_to is False, "cleanup may have taken it out of reach"

    with pytest.raises(ValueError, match="backwards"):
        store.advance(email=INVITEE, phase=PHASE_SETUP_SENT)


@live_postgres
def test_live_a_decision_made_from_a_stale_read_cannot_rewind(live_db):
    """A caller's stale decision cannot commit over a newer phase.

    A caller reads, the phase moves on underneath it, and only then does that
    caller write. Its snapshot says the write is a harmless re-assert of the
    current phase; the row says otherwise, and the row wins.

    **What this does not prove:** it is not the regression test for the
    check-then-act defect the review of #178 found. That version re-read inside
    ``advance()``, so it refuses this case too -- measured, not assumed. The
    structural guard above is what fails if the conditional ever leaves the
    write statement.
    """

    inv = _an_invitation(live_db)
    store = ProvisioningStore(live_db)
    store.claim(email=INVITEE, invitation_id=inv)
    store.advance(email=INVITEE, phase=PHASE_ACCOUNT_CREATED, idp_user_id="kc-user-3")
    store.advance(email=INVITEE, phase=PHASE_SETUP_SENT)

    read_done = threading.Event()
    moved_on = threading.Event()
    outcome: dict[str, object] = {}

    def stale_writer() -> None:
        snapshot = store.get(INVITEE)
        assert snapshot.phase == PHASE_SETUP_SENT, "the stale read must see the pre-move phase"
        read_done.set()
        moved_on.wait(timeout=10)
        try:
            # From this caller's snapshot this is the documented no-op re-assert
            # of the current phase. It is not, any more.
            store.advance(email=INVITEE, phase=snapshot.phase)
            outcome["result"] = "wrote"
        except ValueError as exc:
            outcome["result"] = "refused"
            outcome["error"] = str(exc)

    worker = threading.Thread(target=stale_writer)
    worker.start()
    try:
        assert read_done.wait(timeout=5), "the writer never took its snapshot"
        store.advance(email=INVITEE, phase=PHASE_COMPLETE)
    finally:
        moved_on.set()
        worker.join(timeout=20)
    assert not worker.is_alive()

    final = store.get(INVITEE)
    assert final.phase == PHASE_COMPLETE, f"rewound to {final.phase!r}: the guard did not hold"
    assert final.may_be_written_to is False
    assert outcome.get("result") == "refused", outcome
    assert "backwards" in str(outcome.get("error", ""))


def test_the_forwards_only_guard_is_in_the_write_not_in_front_of_it():
    """The actual regression test for the review finding on #178.

    The defect was check-then-act: ``advance()`` read the phase, compared
    positions in Python, then wrote without a predicate, so a decision made
    against a snapshot could commit over a newer phase.

    **A behavioural test cannot distinguish that.** Both shapes refuse a
    backwards move when nothing interleaves, and forcing the interleave means
    pausing *inside* the read-then-write window -- which the fixed
    implementation does not have, because it does not read before writing.
    Measured rather than assumed: reintroducing the check-then-act shape
    faithfully leaves every behavioural test in this module green.

    So the property is pinned structurally, the way the migration guards pin
    v4's primary key by name. If someone rewrites this as a read, a comparison
    and an unconditional write, this fails.
    """

    import inspect

    source = inspect.getsource(ProvisioningStore.advance)
    # Split on the docstring's own pair of triple quotes only: the SQL below is
    # itself triple-quoted, so an unbounded split dismembers the body.
    body = source.split('"""', 2)[-1]

    assert "UPDATE" in body, "advance must write with a single statement"
    assert "array_position" in body, (
        "the phase ordering must be evaluated in the WHERE clause, against the stored row "
        "at write time -- not in Python, against a row read earlier"
    )

    where_clause = body[body.index("WHERE email_folded") :]
    assert "phase" in where_clause, "the write must be conditional on the stored phase"
    assert "idp_user_id" in where_clause, "and on the stored account id, for first-write-wins"

    before_update = body[: body.index("UPDATE")]
    assert "_read_on" not in before_update, (
        "advance must not read before it writes -- a snapshot taken here is what the defect was made of"
    )


@live_postgres
def test_live_concurrent_advances_settle_on_the_furthest_phase(live_db):
    """Whatever order two advancers commit in, the stored phase is the furthest
    of them -- the invariant the acceptance criterion asks for."""

    inv = _an_invitation(live_db)
    store = ProvisioningStore(live_db)
    store.claim(email=INVITEE, invitation_id=inv)
    store.advance(email=INVITEE, phase=PHASE_ACCOUNT_CREATED, idp_user_id="kc-user-4")

    barrier = threading.Barrier(2)
    errors: list[str] = []
    lock = threading.Lock()

    def advance_to(phase: str) -> None:
        barrier.wait(timeout=10)
        try:
            store.advance(email=INVITEE, phase=phase)
        except ValueError as exc:
            with lock:
                errors.append(str(exc))

    threads = [
        threading.Thread(target=advance_to, args=(PHASE_COMPLETE,)),
        threading.Thread(target=advance_to, args=(PHASE_SETUP_SENT,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
        assert not t.is_alive()

    assert store.get(INVITEE).phase == PHASE_COMPLETE
    # The loser either ran first (no error) or was refused as backwards. Both
    # are correct; a silent rewind is the only wrong answer.
    assert all("backwards" in message for message in errors)


@live_postgres
def test_live_the_account_id_is_first_write_wins(live_db):
    """Recovery reads this column to decide adopt-or-create, so a second,
    different value is refused rather than silently replacing the evidence."""

    inv = _an_invitation(live_db)
    store = ProvisioningStore(live_db)
    store.claim(email=INVITEE, invitation_id=inv)
    store.advance(email=INVITEE, phase=PHASE_ACCOUNT_CREATED, idp_user_id="kc-first")

    # Re-asserting the same id is fine: a resumed run should not have to know
    # whether it already recorded it.
    assert store.advance(email=INVITEE, phase=PHASE_ACCOUNT_CREATED, idp_user_id="kc-first").idp_user_id == "kc-first"
    # And a phase advance that carries no id leaves it alone.
    assert store.advance(email=INVITEE, phase=PHASE_SETUP_SENT).idp_user_id == "kc-first"

    with pytest.raises(ValueError, match="replace the account id"):
        store.advance(email=INVITEE, phase=PHASE_SETUP_SENT, idp_user_id="kc-second")
    assert store.get(INVITEE).idp_user_id == "kc-first"


@live_postgres
def test_live_an_enlisted_claim_rolls_back_with_the_caller(live_db):
    """The other half of enlistment: if the invitation transaction fails, the
    claim goes with it, so a rolled-back issuance leaves no address claimed."""

    inv = _an_invitation(live_db, "inv-rollback")
    store = ProvisioningStore(live_db)

    class Deliberate(RuntimeError):
        pass

    with pytest.raises(Deliberate):
        with live_db.connection() as conn:
            claim = store.claim(email="rollback@example.com", invitation_id=inv, conn=conn)
            assert claim.mine is True
            raise Deliberate("the caller's transaction fails after claiming")

    assert store.get("rollback@example.com") is None, "the claim outlived a rolled-back transaction"


@live_postgres
def test_live_the_schema_refuses_a_phase_past_claimed_with_no_account(live_db):
    """The shape a half-written recovery produces, refused by the database
    rather than tolerated — the recovery path reads this column to decide
    whether to adopt or create."""

    import psycopg

    inv = _an_invitation(live_db)
    with live_db.connection() as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            """
            INSERT INTO collab_provisioned_accounts
                (email_folded, email, phase, first_invitation)
            VALUES (%s, %s, %s, %s)
            """,
            ("nobody@example.com", "nobody@example.com", PHASE_ACCOUNT_CREATED, inv),
        )


@live_postgres
def test_live_unfinished_claims_stay_visible(live_db):
    """The queue cleanup deliberately does not touch: completion-gated sweeping
    means an interrupted provisioning is alertable rather than lost."""

    inv = _an_invitation(live_db)
    store = ProvisioningStore(live_db)
    store.claim(email=INVITEE, invitation_id=inv)
    store.advance(email=INVITEE, phase=PHASE_ACCOUNT_CREATED, idp_user_id="kc-user-2")

    unfinished = store.unfinished()
    assert [r.email_folded for r in unfinished] == [folded_key(INVITEE)]

    store.advance(email=INVITEE, phase=PHASE_COMPLETE)
    assert store.unfinished() == [], "a finished claim is not a backlog item"
