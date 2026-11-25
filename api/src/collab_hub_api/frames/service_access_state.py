"""The durable record of what an acceptance owes somebody (issue #180).

The audit table records what happened; this table records **what is still
owed**. They are different jobs and the difference is load-bearing here.

An invitation's acceptance commits, and only then is the accepter added to the
configured identity-provider groups — deliberately, because a group write
cannot be rolled back and an outage must not cost somebody a membership they
hold a valid invitation and a verified address for. That ordering leaves a
window, and the first version of this feature left the window open: a fault
between the group call and the audit insert lost the fact that anything had
been attempted at all, so nothing could find the invitee who ended up with no
service access.

**What closes it is writing the intent first, inside the acceptance's own
transaction.** The acceptance already requires this database — if it were
unreachable there would be no acceptance to reconcile — so the intent row
costs no new failure mode, and being in that transaction means it lands if and
only if the acceptance does. Afterwards the row is *settled* to its outcome.
Every way the process can die now resolves to the same safe state:

===============================  ===========================================
Where it stops                   What the row says, and what happens next
===============================  ===========================================
before the group call            ``pending`` -> retried, and the add is
                                 idempotent
after the call, before settling  ``pending`` -> retried, idempotent again
settling fails                   ``pending`` -> same
the call itself fails            ``failed`` -> retried
nothing fails                    ``granted`` -> nothing to do
===============================  ===========================================

So "outstanding" is a single row's state rather than an inference over ordered
audit rows — which also removes the question of whether a ``bigserial``
reflects commit order (it does not; it is allocated at ``INSERT``).

**One row per person and group, forever.** The primary key is
``(user_id, group_path)`` because that pair *is* the grant: a second
invitation to the same person does not create a second thing to do. The
invitation recorded is the first one that wanted it, kept for context.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

STATE_PENDING = "pending"
STATE_GRANTED = "granted"
STATE_FAILED = "failed"

SERVICE_ACCESS_STATES = (STATE_PENDING, STATE_GRANTED, STATE_FAILED)
"""The stored vocabulary, CHECK-constrained by migration v6.

``pending`` and ``failed`` are both outstanding and differ only in what is
known: ``pending`` means nobody has seen an answer, ``failed`` means the
provider refused. A reader wants that distinction — one is a crash, the other
is a real error — and a reconciler treats them identically.
"""


@dataclass(frozen=True)
class OutstandingGrant:
    """A person-and-group pair this deployment owes and has not delivered.

    Carries no address. What a retry needs is the subject and the group, and a
    reconciliation list holding addresses is one more place they can leak from;
    the invitation id is here so a reader can join for one when a human has to
    be told something.
    """

    user_id: str
    group_path: str
    state: str
    invitation_id: str | None
    """The invitation whose acceptance owes this grant, or ``None`` for a
    self-service admission (issue #365), which has no invitation behind it."""
    created_at: datetime
    updated_at: datetime

    @property
    def never_attempted(self) -> bool:
        """``pending``: no attempt has been seen through to an answer.

        Distinguished from ``failed`` for the reader, not for the retry — an
        unanswered attempt is usually a restart mid-acceptance, while a failure
        is the identity provider saying no, and those want different attention
        even though they want the same action.
        """

        return self.state == STATE_PENDING


def claim_pending(conn, *, user_id: str, invitation_id: str | None, group_paths: Sequence[str]) -> None:
    """Record, inside the caller's transaction, what this acceptance owes.

    ``conn`` must be the audited transaction's guarded connection. That is the
    whole point: this row and the acceptance commit together, so there is no
    instant at which somebody has accepted and nothing knows a grant is due.

    ``DO NOTHING`` on conflict rather than an upsert, because the conflicting
    row is either already ``granted`` — in which case resetting it to
    ``pending`` would report somebody who holds their access, and re-granting
    is pointless — or already outstanding, in which case it is already going
    to be retried and the earlier invitation is the truer context for why.

    ``invitation_id`` is ``None`` when the grant is owed by a self-service
    admission (issue #365) rather than an acceptance.
    """

    if not group_paths:
        return
    for group_path in group_paths:
        conn.execute(
            """
            INSERT INTO collab_service_access_grants (user_id, group_path, state, invitation)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (user_id, group_path) DO NOTHING
            """,
            (user_id, group_path, STATE_PENDING, invitation_id),
        )


class ServiceAccessStateStore:
    """Settle and read the durable grant state."""

    def __init__(self, db) -> None:
        self._db = db

    def settle(self, *, user_id: str, group_path: str, granted: bool) -> None:
        """Move one row to its outcome, and never out of ``granted``.

        A success is unconditional: it is the terminal state and re-asserting
        it is free. A failure is conditional on the row not already being
        ``granted``, because a failed *re-*attempt does not remove a membership
        somebody already holds — treating it as a regression would put a person
        who has their access back on the outstanding list, and keep them there.

        The guard is in the ``WHERE`` clause rather than in a read before the
        write, so two settlements racing cannot interleave into a lost update.
        No row matching is not an error: nothing owes this pair, which is what
        a deployment that granted nothing looks like.
        """

        with self._db.connection() as conn:
            if granted:
                conn.execute(
                    """
                    UPDATE collab_service_access_grants
                    SET state = %s, updated_at = now()
                    WHERE user_id = %s AND group_path = %s
                    """,
                    (STATE_GRANTED, user_id, group_path),
                )
            else:
                conn.execute(
                    """
                    UPDATE collab_service_access_grants
                    SET state = %s, updated_at = now()
                    WHERE user_id = %s AND group_path = %s AND state <> %s
                    """,
                    (STATE_FAILED, user_id, group_path, STATE_GRANTED),
                )

    def outstanding(self) -> list[OutstandingGrant]:
        """Everything owed and not delivered, oldest first.

        A plain read of stored state — no ordering over history, no "latest
        row" to determine, and no window in which a crash makes an owed grant
        invisible. Oldest first because age is what makes one of these worth
        attention: a pair unsettled for a week is a different problem from one
        unsettled for a second.
        """

        with self._db.connection() as conn:
            rows = conn.execute(
                """
                SELECT user_id, group_path, state, invitation, created_at, updated_at
                FROM collab_service_access_grants
                WHERE state <> %s
                ORDER BY created_at, user_id, group_path
                """,
                (STATE_GRANTED,),
            ).fetchall()
        return [
            OutstandingGrant(
                user_id=row["user_id"],
                group_path=row["group_path"],
                state=row["state"],
                invitation_id=row["invitation"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]
