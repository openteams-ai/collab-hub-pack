"""The account-provisioning claim and its phases (issue #172).

Setting an invitee's account up spans four systems that cannot commit
together: this database, Keycloak's user store, Keycloak's mail, and the
invitation email. So the workflow is written down as it goes, and every
interruption resumes from what was written rather than from what a caller
remembers.

Why a table, and why keyed this way
-----------------------------------
This row is two things at once, and the second is the reason it is a table
rather than a column on ``collab_invitations``.

**It is the durable phase**, so an interrupted run is resumable: an ambiguous
``POST /users`` is settled by reading, not by retrying blind.

**It is the claim that serializes provisioning.** The primary key is the
folded address, so the first writer wins and a second concurrent request's
insert conflicts — it learns it is not the creator and reads this row instead
of creating a second account. ``pg_advisory_xact_lock`` cannot do that job
here: the invitation row commits *before* the external calls, deliberately,
because an audited claim without an account is the recoverable partial state
and the reverse is not — and a transaction-scoped lock is gone at commit. The
window this row closes is exactly the window that lock leaves open.

**Keyed per address, not per invitation.** Two live invitations for one
address are deliberate on the ``/v1`` routes and pinned by a regression test.
Provisioning happens at most once per address; issuance may repeat. Keyed per
invitation, two invitations would own one account.

**Keyed on the folded address** (:func:`~.invitations.ascii_folded_bytes`, the
form Gate B matches since #157). Storing the raw address would let ``Alice@``
and ``alice@`` each provision an account when only one of them can ever accept.

What this module deliberately does not do
-----------------------------------------
It talks to no identity provider. It records phases and hands out claims; the
Keycloak calls live behind ``InvitationAccountProvisioner``, so this module
stays testable against a database alone and the authority to write accounts
sits in one named place rather than diffused through the workflow.
"""

from __future__ import annotations

from dataclasses import dataclass

from .invitations import ascii_folded_bytes

PHASE_CLAIMED = "claimed"
"""The claim exists and nothing external has happened. The only phase whose
partial state is "an audited invitation with no account", which is the one
commit-first ordering deliberately accepts."""

PHASE_ACCOUNT_CREATED = "account_created"
"""The account exists and carries its ownership marker. No setup mail yet, so
the account has **not** been marked complete — which is what keeps cleanup off
it, because cleanup selects on completion and the credential's authority over
an account disappears with its group membership."""

PHASE_SETUP_SENT = "setup_sent"
"""The required-action email was accepted by the identity provider. Resuming
from here re-sends it: at-least-once, not idempotent — the effect is the same
required action, but each retry is a real message in somebody's inbox."""

PHASE_COMPLETE = "complete"
"""Finished. Nothing writes to this account again. Cleanup may have removed it
from the staging group, which removes the credential's authority over it, so
"complete" is the only safe predicate for *do not touch*."""

PHASES = (PHASE_CLAIMED, PHASE_ACCOUNT_CREATED, PHASE_SETUP_SENT, PHASE_COMPLETE)

TABLE = "collab_provisioned_accounts"


def folded_key(email: str) -> str:
    """The primary key for an address: its ASCII-folded form, as text.

    Folded through the same function Gate B matches on, then decoded back to
    text for storage. `surrogatepass` on the way out mirrors the way in, so a
    lone surrogate that survived validation round-trips rather than raising
    here — this is a key, not a place to start rejecting addresses that were
    already accepted upstream.
    """

    return ascii_folded_bytes(email).decode("utf-8", "surrogatepass")


@dataclass(frozen=True)
class ProvisioningRecord:
    """One address's provisioning state, as stored."""

    email_folded: str
    email: str
    phase: str
    idp_user_id: str | None
    first_invitation: str

    @property
    def is_complete(self) -> bool:
        return self.phase == PHASE_COMPLETE

    @property
    def may_be_written_to(self) -> bool:
        """Whether this workflow may still make identity-provider writes.

        False once complete, and that is not a courtesy: cleanup removes a
        completed account from the staging group, and the boundary proof on
        #172 established that the credential's authority is evaluated against
        *current* membership. A write attempted here would be refused, so the
        caller must classify on completion **before** deciding to resume.
        """

        return not self.is_complete


@dataclass(frozen=True)
class Claim:
    """The outcome of asking to provision an address.

    ``mine`` is the whole point: exactly one caller per address is told it may
    create an account. Everyone else is handed the existing record and decides
    what to do from its phase.
    """

    record: ProvisioningRecord
    mine: bool


class ProvisioningStore:
    """Claims and phases over the shared frames Postgres pool.

    Not part of :class:`~.invitations.PostgresInvitationService` on purpose.
    That class's contract is that every mutation runs inside ``audited()`` on
    the connection that primitive yields, because #87 makes the audit row and
    the change atomic. A claim is not an audited action -- it is bookkeeping
    for a workflow whose *audited* event is the invitation itself -- and giving
    it its own store keeps that rule from being bent to accommodate it.
    """

    def __init__(self, db) -> None:
        self._db = db

    def claim(self, *, email: str, invitation_id: str, conn=None) -> Claim:
        """Claim this address for provisioning, or report who already holds it.

        ``INSERT ... ON CONFLICT DO NOTHING`` and then read: the insert decides
        the winner and the read tells the loser what state it walked into. One
        statement cannot do both, because ``DO NOTHING`` returns nothing on
        conflict -- and ``DO UPDATE`` would make every caller a writer, which
        is precisely the property this method exists to deny.

        Pass ``conn`` to enlist in a caller's transaction: the claim then
        commits with the invitation row it belongs to, so a committed
        invitation always has a claim to resume from. Called without one it
        takes its own pooled connection, which is the recovery path -- there is
        no invitation to be atomic with by then.
        """

        key = folded_key(email)
        if conn is not None:
            return self._claim_on(conn, key=key, email=email, invitation_id=invitation_id)
        with self._db.connection() as own:
            return self._claim_on(own, key=key, email=email, invitation_id=invitation_id)

    def _claim_on(self, conn, *, key: str, email: str, invitation_id: str) -> Claim:
        inserted = conn.execute(
            f"""
            INSERT INTO {TABLE} (email_folded, email, phase, first_invitation)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (email_folded) DO NOTHING
            RETURNING email_folded
            """,
            (key, email, PHASE_CLAIMED, invitation_id),
        ).fetchone()
        record = self._read_on(conn, key)
        if record is None:  # pragma: no cover - the row was just read or written
            raise RuntimeError(f"provisioning claim vanished for {key!r}")
        return Claim(record=record, mine=inserted is not None)

    def get(self, email: str) -> ProvisioningRecord | None:
        with self._db.connection() as conn:
            return self._read_on(conn, folded_key(email))

    def _read_on(self, conn, key: str) -> ProvisioningRecord | None:
        row = conn.execute(
            f"""
            SELECT email_folded, email, phase, idp_user_id, first_invitation
            FROM {TABLE} WHERE email_folded = %s
            """,
            (key,),
        ).fetchone()
        if row is None:
            return None
        return ProvisioningRecord(
            email_folded=row["email_folded"],
            email=row["email"],
            phase=row["phase"],
            idp_user_id=row["idp_user_id"],
            first_invitation=row["first_invitation"],
        )

    def advance(self, *, email: str, phase: str, idp_user_id: str | None = None) -> ProvisioningRecord:
        """Move an address to ``phase``, forwards only, in one statement.

        **The guard is in the write, not in front of it.** An earlier version of
        this method read the phase, compared positions in Python, and then wrote
        without a predicate -- which loses the race it exists to referee. Two
        advancers interleave, the one holding the older snapshot commits last,
        and a record that reached ``complete`` is stamped back to
        ``setup_sent``. That is designed traffic rather than abuse: this module
        invites concurrent advancers (any caller may push a stalled record
        along, with no lease and no liveness protocol) and re-asserting the
        current phase is the documented way to resume.

        A rewind is the worst possible failure for this table, because
        ``may_be_written_to`` is read from the phase: it would report an account
        as writable after cleanup had already removed it from the staging group,
        and the resulting refusal is exactly the puzzling 403 the guard exists
        to prevent.

        So the ordering lives in the ``WHERE`` clause, evaluated against the row
        as it is at write time. There is no read to be stale, and the phase
        order is still :data:`PHASES` -- passed as a parameter rather than
        spelled again in SQL.

        Re-asserting the current phase remains a no-op on the phase, so a
        resumed send need not special-case itself. ``idp_user_id`` is
        **first-write-wins**: recovery reads that column to decide adopt or
        create, so a second, different value is refused loudly rather than
        silently replacing the evidence.
        """

        if phase not in PHASES:
            raise ValueError(f"unknown provisioning phase {phase!r}")
        key = folded_key(email)
        order = list(PHASES)
        with self._db.connection() as conn:
            row = conn.execute(
                f"""
                UPDATE {TABLE}
                SET phase = %(phase)s::text,
                    idp_user_id = COALESCE(idp_user_id, %(idp_user_id)s::text),
                    updated_at = now()
                WHERE email_folded = %(key)s::text
                  -- Forwards only, decided against the stored row rather than
                  -- against a snapshot this transaction read earlier.
                  -- Casts are not decoration: without them psycopg cannot infer
                  -- a type for a parameter that may be NULL and appears in
                  -- several positions, and raises AmbiguousParameter.
                  AND array_position(%(order)s::text[], phase)
                      <= array_position(%(order)s::text[], %(phase)s::text)
                  -- First-write-wins on the account id: a different non-null
                  -- value must not overwrite the one recovery reads.
                  AND (
                        idp_user_id IS NULL
                        OR %(idp_user_id)s::text IS NULL
                        OR idp_user_id = %(idp_user_id)s::text
                      )
                RETURNING email_folded, email, phase, idp_user_id, first_invitation
                """,
                {"phase": phase, "idp_user_id": idp_user_id, "key": key, "order": order},
            ).fetchone()
            if row is not None:
                return ProvisioningRecord(
                    email_folded=row["email_folded"],
                    email=row["email"],
                    phase=row["phase"],
                    idp_user_id=row["idp_user_id"],
                    first_invitation=row["first_invitation"],
                )
            # Zero rows means one of three things, and they are different
            # mistakes. Read to say which, rather than raising one error that
            # sends the reader looking in the wrong place.
            current = self._read_on(conn, key)
        if current is None:
            raise LookupError(f"no provisioning claim for {key!r}")
        if PHASES.index(phase) < PHASES.index(current.phase):
            raise ValueError(f"refusing to move provisioning backwards for {key!r}: {current.phase!r} -> {phase!r}")
        raise ValueError(
            f"refusing to replace the account id for {key!r}: stored {current.idp_user_id!r}, given {idp_user_id!r}"
        )

    def unfinished(self, *, older_than_seconds: int = 0) -> list[ProvisioningRecord]:
        """Claims that are not complete, oldest first.

        The queue this design creates on purpose. Cleanup selects on
        completion, so an interrupted provisioning is never swept away -- it
        sits here instead, which makes "started and never finished" a thing to
        alert on rather than a stranded person to discover from a support mail.
        """

        with self._db.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT email_folded, email, phase, idp_user_id, first_invitation
                FROM {TABLE}
                WHERE phase <> %s
                  AND updated_at < now() - make_interval(secs => %s)
                ORDER BY updated_at
                """,
                (PHASE_COMPLETE, older_than_seconds),
            ).fetchall()
        return [
            ProvisioningRecord(
                email_folded=r["email_folded"],
                email=r["email"],
                phase=r["phase"],
                idp_user_id=r["idp_user_id"],
                first_invitation=r["first_invitation"],
            )
            for r in rows
        ]
