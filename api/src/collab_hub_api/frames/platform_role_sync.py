"""Who is an operator, decided by the identity provider and copied here.

Until now the only way to grant the operator role was a hand-written ``psql``
insert, recorded by an ``operator.manual`` audit row. That is the right
bootstrap and the wrong steady state: it puts the grant outside the system an
organization actually administers identity in, and it does not scale past the
first person.

So the identity provider owns the fact and this module keeps a copy. Keycloak
is used as a plain OIDC provider here -- nothing in this module calls its admin
API. At sign-in the verified ID token's ``groups`` claim is read and
``collab_platform_roles`` is reconciled against it.

Why a copy at all, rather than reading the claim on every request
----------------------------------------------------------------
Because authorization is resolved from this server's own stores on the request
being authorized (see :mod:`..web.authz`), which is what makes the stateless
session cookie acceptable. A claim read per request would work on the bearer
axis and not on the browser one, where the session deliberately carries
identity and nothing else. One source for both axes is worth more than the
freshness a second mechanism would buy.

The cost is stated rather than hidden: on the browser axis a revocation takes
effect at the holder's next sign-in, not their next request. Before this
module, revoking a row locked its holder out immediately. That is a real
weakening, and it is the price of the identity provider owning the fact.

``source``, and why a column was worth a migration
--------------------------------------------------
Sync must be able to *remove*. A copy that only ever adds is worse than no copy
at all: dropping someone from the admin group would leave their row standing
forever, and the deployment would believe the identity provider had revoked
them. So reconcile revokes as well as grants.

Which immediately endangers the bootstrap operator, whose row was inserted by
hand and whose subject may be in no group at all. If sync could revoke that
row, the first sign-in after this ships would lock the first admin out of the
deployment they just bootstrapped.

Hence ``source``: ``manual`` rows are administered here and never touched by
sync; ``idp`` rows are sync's own and it may grant or revoke them freely. It is
the split any OIDC group sync ends up needing, and for this reason.

Sync never revokes the **last** active operator, whatever the claim says. A
renamed admin group, or a groups mapper switched between ``/group`` and
``group``, would otherwise revoke every synced operator at their next sign-in,
and the last one out leaves psql as the only way back. The refusal is logged
(``platform_role_sync_kept_last_operator``) so the cause gets noticed. The
group name is matched with or without its leading slash for the same reason.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from .audit import (
    AUDIT_ACTION_PLATFORM_ROLE_GRANT,
    AUDIT_ACTION_PLATFORM_ROLE_REVOKE,
    audited,
)
from .auth import WORKSPACE_DEFAULT, AuthContext, DisplayIdentity
from .orgs import PLATFORM_ROLE_ACTIVE, PLATFORM_ROLE_OPERATOR, PLATFORM_ROLE_REVOKED

__all__ = [
    "PLATFORM_ROLE_SOURCE_IDP",
    "PLATFORM_ROLE_SOURCE_MANUAL",
    "DisabledPlatformRoleSync",
    "InMemoryPlatformRoleSync",
    "PostgresPlatformRoleSync",
]

logger = logging.getLogger(__name__)

PLATFORM_ROLE_SOURCE_MANUAL = "manual"
"""Administered by hand (the documented bootstrap insert). Sync never touches these."""

PLATFORM_ROLE_SOURCE_IDP = "idp"
"""Created by this module from a groups claim. Sync owns these rows outright."""


class DisabledPlatformRoleSync:
    """What a deployment that has named no admin group does: nothing.

    The default. A deployment with no ``admin_group`` configured behaves
    exactly as it did before this module existed -- roles come from the table
    and only from the table -- rather than having an empty group name silently
    match nothing and revoke every synced row on first sign-in.
    """

    configured = False

    def reconcile(
        self,
        *,
        user_id: str,
        claim_groups: Sequence[str],
        display: DisplayIdentity,
    ) -> str | None:
        return None


class _PlatformRoleSync:
    """The decision, once, for both backends.

    Only the three storage operations differ between Postgres and memory, so
    only those are left abstract. The rule about what a claim means -- and in
    particular the rule that ``manual`` rows are untouchable in both directions
    -- lives here and nowhere else: a second copy of it is how one backend ends
    up revoking the bootstrap operator that the other protects.
    """

    configured = True

    def __init__(self, *, admin_group: str) -> None:
        self._admin_group = admin_group

    def reconcile(
        self,
        *,
        user_id: str,
        claim_groups: Sequence[str],
        display: DisplayIdentity,
    ) -> str | None:
        """Make this subject's role row match their claim; return the active role.

        Returns the role the subject holds *after* reconciling, or ``None``.
        The return value is the answer the sign-in path wants, so that the
        caller never has to read the row back and never has to decide what a
        revoked row means.
        """

        wanted = _group_name(self._admin_group)
        in_admin_group = any(_group_name(group) == wanted for group in claim_groups)
        current = self._current_row(user_id)

        if in_admin_group and _is_sync_owned(current) and not _is_active(current):
            self._grant(user_id=user_id, display=display)
            return PLATFORM_ROLE_OPERATOR

        if not in_admin_group and current is not None and _is_sync_owned(current) and _is_active(current):
            if self._revoke(user_id=user_id, display=display):
                return None
            logger.warning("platform_role_sync_kept_last_operator", extra={"user": user_id})
            return PLATFORM_ROLE_OPERATOR

        return current["role"] if _is_active(current) else None

    def _current_row(self, user_id: str):
        raise NotImplementedError

    def _grant(self, *, user_id: str, display: DisplayIdentity) -> None:
        raise NotImplementedError

    def _revoke(self, *, user_id: str, display: DisplayIdentity) -> bool:
        """Revoke unless this is the last active operator; say whether it did."""

        raise NotImplementedError


class InMemoryPlatformRoleSync(_PlatformRoleSync):
    """The memory-backed pairing, for dev and for deployments without Postgres.

    Records no audit rows, because ``audited()`` writes to Postgres and there
    is no in-memory audit table to write to. That is the existing shape of
    every memory-backed store in this package rather than a gap introduced
    here, and a deployment that needs the record runs Postgres.
    """

    def __init__(self, org_store, *, admin_group: str) -> None:
        super().__init__(admin_group=admin_group)
        self._store = org_store

    def _current_row(self, user_id: str):
        return self._store.get_platform_role_row(user_id)

    def _grant(self, *, user_id: str, display: DisplayIdentity) -> None:
        self._store.set_platform_role(
            user_id,
            PLATFORM_ROLE_OPERATOR,
            PLATFORM_ROLE_ACTIVE,
            PLATFORM_ROLE_SOURCE_IDP,
        )

    def _revoke(self, *, user_id: str, display: DisplayIdentity) -> bool:
        if self._store.active_operator_ids() == {user_id}:
            return False
        self._store.set_platform_role(
            user_id,
            PLATFORM_ROLE_OPERATOR,
            PLATFORM_ROLE_REVOKED,
            PLATFORM_ROLE_SOURCE_IDP,
        )
        return True


class PostgresPlatformRoleSync(_PlatformRoleSync):
    """The durable pairing: the row and its audit entry in one transaction."""

    def __init__(self, db, *, admin_group: str) -> None:
        super().__init__(admin_group=admin_group)
        self._db = db

    def _current_row(self, user_id: str):
        """This subject's role row as it stands, or ``None``.

        Read on its own connection, outside the audited transaction, because
        the audited primitive always writes a row and most sign-ins change
        nothing: opening it to discover there is nothing to record would make
        every sign-in of every operator a fresh audit entry, and a log that is
        mostly "still an operator" is a log nobody reads.
        """

        with self._db.connection() as conn:
            return conn.execute(
                "SELECT role, status, source FROM collab_platform_roles WHERE user_id = %s",
                (user_id,),
            ).fetchone()

    def _grant(self, *, user_id: str, display: DisplayIdentity) -> None:
        # An upsert, not an insert: a grant is just as often the *return*
        # of someone whose synced row was revoked when they left the group,
        # and ``user_id`` is the primary key, so a plain insert would fail
        # on exactly the second-most-common case this path serves.
        #
        # The ``WHERE`` on the update arm is the same guard the revoke
        # carries, for the same reason: the row was read on a different
        # connection, so a hand-run grant or revocation landing in between
        # must survive a decision taken against the row as it used to be.
        self._write(
            AUDIT_ACTION_PLATFORM_ROLE_GRANT,
            user_id=user_id,
            display=display,
            sql="""
                INSERT INTO collab_platform_roles (user_id, role, status, source)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE
                   SET role = EXCLUDED.role,
                       status = EXCLUDED.status,
                       granted_at = now()
                 WHERE collab_platform_roles.source = %s
                """,
            params=(
                user_id,
                PLATFORM_ROLE_OPERATOR,
                PLATFORM_ROLE_ACTIVE,
                PLATFORM_ROLE_SOURCE_IDP,
                PLATFORM_ROLE_SOURCE_IDP,
            ),
        )

    def _revoke(self, *, user_id: str, display: DisplayIdentity) -> bool:
        # ``source`` is in the WHERE, not only in the decision above: the
        # row is read on a different connection from the one that writes,
        # so a hand-run grant landing in between must not be overwritten by
        # a decision taken against the row as it used to be.
        return (
            self._write(
                AUDIT_ACTION_PLATFORM_ROLE_REVOKE,
                user_id=user_id,
                display=display,
                sql="""
                UPDATE collab_platform_roles
                   SET status = %s
                 WHERE user_id = %s AND source = %s AND status = %s
                """,
                params=(
                    PLATFORM_ROLE_REVOKED,
                    user_id,
                    PLATFORM_ROLE_SOURCE_IDP,
                    PLATFORM_ROLE_ACTIVE,
                ),
                keep_last_operator=True,
            )
            != _KEPT_LAST_OPERATOR
        )

    def _write(
        self,
        action: str,
        *,
        user_id: str,
        display: DisplayIdentity,
        sql: str,
        params: tuple,
        keep_last_operator: bool = False,
    ) -> str | None:
        """Run one guarded role write and record it, or record nothing.

        When the guard suppresses the write -- a hand-run change landed after
        the row was read -- the audit entry is rolled back with it. A log
        saying sync granted a role the table does not show would be believed.

        With *keep_last_operator*, the active operators are read under a lock
        first (:func:`lock_active_operators`) and the write is abandoned if
        *user_id* is the only one; that returns :data:`_KEPT_LAST_OPERATOR`.
        """

        try:
            with audited(
                self._db,
                _actor(user_id, display),
                action,
                target_type="user",
                target_id=user_id,
                target_label=display.email or display.name,
                org_id=None,
                detail={"origin": "oidc", "group": self._admin_group},
            ) as event:
                if keep_last_operator and lock_active_operators(event.conn) == {user_id}:
                    raise _Unchanged(_KEPT_LAST_OPERATOR)
                if event.conn.execute(sql, params).rowcount == 0:
                    raise _Unchanged(None)
        except _Unchanged as unchanged:
            return unchanged.args[0]
        return None


class _Unchanged(Exception):
    """Raised inside ``audited()`` to roll back an audit row for a no-op write."""


_KEPT_LAST_OPERATOR = "kept_last_operator"


def _group_name(group: str) -> str:
    """A group path without its leading slash, so ``/admins`` matches ``admins``."""

    return group.lstrip("/")


def lock_active_operators(conn) -> set[str]:
    """Every active operator, with their rows locked until the transaction ends.

    Both revoke paths -- the panel's and sign-in sync's -- read this before
    writing, so two revokes running at once cannot each see the other still
    active and together remove the last administrator: the second waits for
    the first to commit, then reads the result. ``ORDER BY`` makes every caller
    take the locks in the same order, so two of them cannot deadlock.
    """

    rows = conn.execute(
        """
        SELECT user_id FROM collab_platform_roles
         WHERE role = %s AND status = %s
         ORDER BY user_id
           FOR UPDATE
        """,
        (PLATFORM_ROLE_OPERATOR, PLATFORM_ROLE_ACTIVE),
    ).fetchall()
    return {row["user_id"] for row in rows}


def _actor(user_id: str, display: DisplayIdentity) -> AuthContext:
    """The audit row's actor: the subject whose sign-in caused the change.

    Not an invented service principal. Nothing here acts on its own initiative
    -- a person signed in, and their identity provider said what they are -- so
    the honest actor is that person, with the provider recorded in ``detail``.

    ``home_org_id=None`` is hub scope and fails closed: every org-scoped read
    of this context raises rather than silently acting inside whichever
    organization the subject happens to belong to.
    """

    return AuthContext(
        user=user_id,
        home_org_id=None,
        workspace_id=WORKSPACE_DEFAULT,
        display=display,
        org_role=None,
        platform_role=None,
    )


def _is_active(row) -> bool:
    """Whether a role row (or its absence) currently grants anything."""

    return row is not None and row["status"] == PLATFORM_ROLE_ACTIVE


def _is_sync_owned(row) -> bool:
    """Whether this row is sync's to grant or revoke.

    A missing row counts: the first synced grant has nothing to own yet. A
    ``manual`` row never counts, in either direction -- which is what keeps the
    bootstrap operator signed in, and equally what keeps a deliberate hand
    revocation from being undone by group membership.
    """

    return row is None or row["source"] == PLATFORM_ROLE_SOURCE_IDP
