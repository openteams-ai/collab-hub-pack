"""An organization of their own for every verified login without one (issue #365).

Under ``orgSource=self_service`` a caller who authenticates with a verified
email and has **no membership row at all** gets a new organization on their
first authenticated request, with themselves as ``owner``. The hub this exists
for charges per person for usage, so anyone may sign up; what it removes is the
dead end where an account exists and every hub request answers
``no_organization``.

What one admission writes, in one audited transaction
------------------------------------------------------
* the ``collab_orgs`` row, marked ``self_service`` and ``name_generated``;
* the caller's ``collab_org_members`` row, role ``owner``;
* one ``org.create`` audit row with the caller as actor and
  ``detail.self_service: true``;
* a ``pending`` row in ``collab_service_access_grants`` for each service group
  the deployment grants (the groups invitation acceptance grants), so the grant
  attempted after commit is reconcilable exactly as an acceptance's is.

A caller who already holds a row of any kind gets it back untouched and nothing
is written: a ``removed`` row stays removed, and an active row anywhere is
never moved. That check runs inside the transaction and is backed by the
membership primary key, so two first requests racing produce one organization.

The generated name
------------------
``Personal: <verified email>``, unique across generated names (a partial unique
index, migration 19). The address is unique at the identity provider, so the
first candidate normally wins; it collides only when an earlier account with
the same address left its organization behind, and then a short suffix from the
subject is tried, then random ones. The Gate B revision of 2026-08-04 kept names
derived from a login out of *invitation-created* organizations; this is a
different kind of organization, and an operator needs to tell thousands of them
apart. The owner page asks for a real name before the first invitation goes
out (``name_generated`` is treated like the placeholder), so the address is not
shown to a second member.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from .orgs import MEMBERSHIP_ACTIVE, ROLE_OWNER, OrgMembership

if TYPE_CHECKING:  # pragma: no cover
    from .auth import DisplayIdentity

logger = logging.getLogger("frames_server.self_service")

GENERATED_NAME_PREFIX = "Personal: "

_MAX_NAME_ATTEMPTS = 8
"""Candidates tried before giving up. The first is the address alone; a
collision past the subject-suffixed second needs a random suffix to collide
too, so running out means something other than chance is wrong."""


class SelfServiceAdmissionError(RuntimeError):
    """No unique generated name could be found. Surfaces as a server error."""


@dataclass(frozen=True)
class SelfServiceAdmission:
    """The row now standing for the caller, and whether this call created it."""

    membership: OrgMembership
    created: bool
    service_groups: tuple[str, ...] = ()
    """The groups this admission owes a grant for. Empty unless *created*."""


def generated_name_candidates(email: str, user_id: str) -> Iterator[str]:
    """The names an admission tries, in order. See the module docstring."""

    base = f"{GENERATED_NAME_PREFIX}{email}"
    yield base
    yield f"{base} ({user_id.replace('-', '')[:8]})"
    for _ in range(_MAX_NAME_ATTEMPTS - 2):
        yield f"{base} ({secrets.token_hex(3)})"


class _ExistingRow(Exception):
    """Aborts the audited transaction: the caller already holds a row."""


def provision_self_service_org(
    db,
    *,
    user_id: str,
    display: DisplayIdentity,
    service_groups: Sequence[str] = (),
) -> SelfServiceAdmission:
    """Create the caller's organization and owner row, unless they hold a row.

    ``display.email`` must be the verified address; the caller checks
    ``display.email_verified`` before calling. Raises whatever the database
    raises: the write is part of an authorization answer and fails closed.
    """

    # Imported here, not at module level: audit imports auth for its types,
    # and auth imports this module's caller (orgs). The same cycle #91 noted.
    from .audit import AUDIT_ACTION_ORG_CREATE, audited
    from .auth import WORKSPACE_DEFAULT, AuthContext
    from .service_access_state import claim_pending

    email = display.email
    if not email:
        raise ValueError("self-service admission needs the caller's verified email")
    new_org_id = uuid4().hex
    groups = tuple(service_groups)
    # The actor's own context, with no organization: at the moment the row is
    # written the caller has none. Same shape as invitation acceptance's.
    ctx = AuthContext(user=user_id, home_org_id=None, workspace_id=WORKSPACE_DEFAULT, display=display)
    try:
        with audited(
            db,
            ctx,
            AUDIT_ACTION_ORG_CREATE,
            target_type="org",
            target_id=new_org_id,
            target_label=None,
            org_id=new_org_id,
            detail={"self_service": True, "role": ROLE_OWNER},
        ) as event:
            conn = event.conn
            # Answers the common case without inserting an organization that
            # then rolls back. The barrier that holds under concurrency is the
            # membership primary key below.
            if conn.execute("SELECT 1 AS present FROM collab_org_members WHERE user_id = %s", (user_id,)).fetchone():
                raise _ExistingRow()
            name = _insert_org(conn, org_id=new_org_id, user_id=user_id, email=email)
            member = conn.execute(
                """
                INSERT INTO collab_org_members (user_id, org_id, role, email, display_name)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (user_id) DO NOTHING
                RETURNING user_id
                """,
                (user_id, new_org_id, ROLE_OWNER, email, display.name),
            ).fetchone()
            if member is None:
                # Lost the race to a concurrent first request. Rolling back
                # takes the organization inserted above with it.
                raise _ExistingRow()
            claim_pending(conn, user_id=user_id, invitation_id=None, group_paths=groups)
    except _ExistingRow:
        return SelfServiceAdmission(membership=_read_membership(db, user_id), created=False)
    logger.info("self_service_org_created", extra={"user": user_id, "org": new_org_id, "generated_name": name})
    return SelfServiceAdmission(
        membership=OrgMembership(user_id=user_id, org_id=new_org_id, role=ROLE_OWNER, status=MEMBERSHIP_ACTIVE),
        created=True,
        service_groups=groups,
    )


def _insert_org(conn, *, org_id: str, user_id: str, email: str) -> str:
    """Insert the organization under the first free generated name."""

    for candidate in generated_name_candidates(email, user_id):
        # No conflict target, so a collision on the partial unique index over
        # generated names answers DO NOTHING instead of aborting the
        # transaction; an id collision (a fresh uuid4) would do the same and
        # simply cost one more candidate.
        row = conn.execute(
            """
            INSERT INTO collab_orgs (id, name, created_by, self_service, name_generated)
            VALUES (%s, %s, %s, true, true)
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            (org_id, candidate, user_id),
        ).fetchone()
        if row is not None:
            return candidate
    raise SelfServiceAdmissionError("no unique generated organization name could be found")


def _read_membership(db, user_id: str) -> OrgMembership:
    with db.connection() as conn:
        row = conn.execute(
            "SELECT user_id, org_id, role, status FROM collab_org_members WHERE user_id = %s",
            (user_id,),
        ).fetchone()
    # Nothing deletes membership rows, and one was seen inside the
    # transaction (or won the race against it), so this read is not empty.
    return OrgMembership(user_id=row["user_id"], org_id=row["org_id"], role=row["role"], status=row["status"])


def grant_owed_service_access(
    *,
    granter,
    record,
    user_id: str,
    display: DisplayIdentity,
    org_id: str,
    groups: Sequence[str],
) -> None:
    """Attempt the grants an admission owes, after its transaction committed.

    The same sequence invitation acceptance follows (#180): the ``pending``
    rows already committed with the organization, so this attempts each group,
    settles its row, and writes the ``service_access.grant`` audit row through
    the invitation service, the one writer of that action. Nothing here can
    undo the admission; every failure is logged and leaves the row
    outstanding for reconciliation.
    """

    from .account_provisioning import ServiceAccessError

    if not groups or granter is None or not getattr(granter, "configured", False):
        return
    for group_path in groups:
        granted = True
        try:
            granter.grant(user_id=user_id, group_path=group_path)
        except ServiceAccessError:
            granted = False
            logger.error(
                "service_access_grant_failed", extra={"self_service": True, "group_path": group_path, "org_id": org_id}
            )
        if record is None:
            continue
        try:
            record.settle_service_access_grant(user_id=user_id, group_path=group_path, granted=granted)
        except Exception:
            logger.exception("service_access_grant_unsettled", extra={"self_service": True, "group_path": group_path})
        try:
            record.record_service_access_grant(
                user_id, display, invitation_id=None, org_id=org_id, group_path=group_path, granted=granted
            )
        except Exception:
            logger.exception("service_access_grant_unrecorded", extra={"self_service": True, "group_path": group_path})


@dataclass(frozen=True)
class SelfServicePolicy:
    """What an admission on this app owes, and what to do once it commits.

    Built per request from the app's state by :func:`self_service_policy`, so
    the auth choke point (and the web surface) can admit without knowing how
    service access is wired.
    """

    service_groups: tuple[str, ...] = ()
    on_created: Callable[[str, DisplayIdentity, SelfServiceAdmission], None] | None = None


def self_service_policy(app_state) -> SelfServicePolicy | None:
    """The policy for an app, or ``None`` when the deployment is not ``self_service``.

    The groups owed are decided before the transaction, as acceptance decides
    them (#180): a deployment that will not attempt a grant must not write rows
    saying one is due.
    """

    from .org_source import org_source_is_self_service

    if not org_source_is_self_service():
        return None
    granter = getattr(app_state, "service_access_granter", None)
    groups = tuple(getattr(app_state, "granted_service_groups", ()) or ())
    owed = groups if groups and granter is not None and getattr(granter, "configured", False) else ()
    record = getattr(app_state, "invitation_service", None)

    def on_created(user_id: str, display: DisplayIdentity, admission: SelfServiceAdmission) -> None:
        grant_owed_service_access(
            granter=granter,
            record=record,
            user_id=user_id,
            display=display,
            org_id=admission.membership.org_id,
            groups=admission.service_groups,
        )

    return SelfServicePolicy(service_groups=owed, on_created=on_created)


def admit_self_service(
    user_id: str, display: DisplayIdentity, org_store, policy: SelfServicePolicy
) -> OrgMembership | None:
    """Give a verified caller with no membership row an organization of their own.

    Returns the row now standing (created, or one that won a race), or
    ``None`` when the caller does not qualify: no email, or an email the
    identity provider did not assert verified. ``email_verified`` must be the
    boolean ``True``, the same strictness invitation acceptance applies.

    The post-commit grant is best-effort by design and never fails the
    request: the organization is committed and correct, and an owed grant is
    a reconcilable row.
    """

    if display.email_verified is not True or not display.email:
        return None
    admission = org_store.provision_self_service_org(user_id, display, service_groups=policy.service_groups)
    if admission.created and policy.on_created is not None:
        try:
            policy.on_created(user_id, display, admission)
        except Exception:
            logger.exception("self_service_post_commit_failed", extra={"user": user_id})
    return admission.membership
