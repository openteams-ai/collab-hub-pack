"""The operator invitation page (issue #91): copy and markup.

``/admin/invitations`` — restricted to ``platform_role = 'operator'``. It does
three things and deliberately not a fourth: it takes an email address and
issues an invitation, it shows the invitations on this deployment with their
state, and it revokes one. No cross-org browsing, no member management, no org
deletion; Gate E scoped the operator surface to invitation issuance exactly,
and a page module is where extra capability arrives unnoticed.

**The page does not create the organization.** Every invitation issued here
carries a null ``org_id``, and the organization is created atomically on first
acceptance with the accepter as its owner (Gate B, revised 2026-08-04). There
is no request shape on this page that can name an organization: pre-creating
one would leave an orphan behind every invitation that is revoked, expires, or
is simply never accepted, and would move the ``org.create`` actor from the
accepter to the operator.

No JavaScript
-------------
Plain documents and forms, like the rest of the surface — the acceptance page
(#90) is the single exception and it exists for a reason this page does not
share. So ``/admin/*`` keeps :data:`~.pages.CONTENT_SECURITY_POLICY` unchanged:
``default-src 'none'`` with no ``script-src`` at all. That is checked rather
than assumed; see the tests.

Why the issue ``POST`` renders instead of redirecting
-----------------------------------------------------
Every other mutating form on an operational surface should redirect after
posting, so a reload does not repeat the action. This one answers with the
page directly, because the redemption link exists for exactly one response and
carrying it through a redirect would mean putting a live secret in a URL, a
cookie, or server-side session state — three worse places than the body it is
already in. The cost is a browser "resubmit?" prompt on reload, and the
issuance rule behind it (one live invitation per address, enforced in the
service under an advisory lock) is what makes an accidental resubmit harmless.

Revoke posts to its own path and also answers with the page, for consistency
of shape rather than necessity: revoking twice is a no-op success in #89 and
writes no second audit row.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from ..frames.invitations import (
    STATUS_ACCEPTED,
    STATUS_EXPIRED,
    STATUS_PENDING,
    STATUS_REVOKED,
    Invitation,
    effective_status,
)
from .forms import refused_form_page
from .pages import escape, render_page
from .surface import ADMIN_INVITATIONS_PATH, ADMIN_INVITATIONS_REVOKE_PATH

__all__ = [
    "ADMIN_INVITATIONS_PATH",
    "ADMIN_INVITATIONS_REVOKE_PATH",
    "EMAIL_FIELD",
    "INVITATION_ID_FIELD",
    "MAX_EMAIL_LENGTH",
    "MAX_INVITATION_ID_LENGTH",
    "NOTICES",
    "NOTICE_ALREADY_LIVE",
    "NOTICE_INVALID_EMAIL",
    "NOTICE_ISSUED_SEND_FAILED",
    "NOTICE_ISSUED_SEND_UNKNOWN",
    "NOTICE_ISSUED_SENT",
    "NOTICE_NOT_FOUND",
    "NOTICE_REVOKED",
    "NOTICE_REVOKE_REFUSED",
    "NOTICE_UNAVAILABLE",
    "Notice",
    "PAGE_TITLE",
    "REVOCABLE_STATUSES",
    "invitations_page",
    "request_refused_page",
]

PAGE_TITLE = "Invitations"

EMAIL_FIELD = "email"
INVITATION_ID_FIELD = "invitation_id"

MAX_EMAIL_LENGTH = 254
"""The longest address RFC 5321 admits. Bounded before validation so an
oversized field is refused by shape rather than parsed."""

MAX_INVITATION_ID_LENGTH = 64
"""Ids are ``uuid4().hex`` — 32 characters. Bounded generously, and bounded at
all so a hostile field cannot become a long value in an error path."""

# --- Notices ----------------------------------------------------------------
#
# One fixed sentence per outcome. Each is a constant written for a person; the
# only dynamic part is the address, which is composed in and then escaped with
# the whole string (see `_notice_markup`). Nothing here can carry a token, and
# nothing quotes a provider's error message — which is why the three issue
# outcomes are three fixed sentences rather than one with a status pasted in.

NOTICE_ISSUED_SENT = "issued_sent"
NOTICE_ISSUED_SEND_FAILED = "issued_send_failed"
NOTICE_ISSUED_SEND_UNKNOWN = "issued_send_unknown"
NOTICE_ALREADY_LIVE = "already_live"
NOTICE_INVALID_EMAIL = "invalid_email"
NOTICE_REVOKED = "revoked"
NOTICE_NOT_FOUND = "not_found"
NOTICE_REVOKE_REFUSED = "revoke_refused"
NOTICE_UNAVAILABLE = "unavailable"

NOTICES: dict[str, str] = {
    NOTICE_ISSUED_SENT: (
        "Invitation created for {address} and the invitation email has been"
        " handed to the email provider. No organization exists yet — one is"
        " created when the invitation is accepted, with the invitee as its owner."
    ),
    NOTICE_ISSUED_SEND_FAILED: (
        "The invitation for {address} was created, but the invitation email"
        " could not be sent, so they have no way to accept it. Revoke it below"
        " and issue a fresh one; tell an administrator if this keeps happening."
    ),
    NOTICE_ISSUED_SEND_UNKNOWN: (
        "The invitation for {address} was created, but the email provider did"
        " not confirm sending the invitation email. If it does not arrive,"
        " revoke the invitation below and issue a fresh one."
    ),
    NOTICE_ALREADY_LIVE: (
        "{address} already has an invitation that has not been accepted, revoked,"
        " or expired, so no new one was created and nothing was sent. To send a"
        " fresh invitation, revoke that invitation below and issue another."
    ),
    NOTICE_INVALID_EMAIL: (
        "That is not a single email address, so nothing was created. Enter one"
        " plain address, exactly as the invitee will register with — it is matched"
        " exactly, and this page does not correct capitalization."
    ),
    NOTICE_REVOKED: ("Invitation for {address} revoked. The link in the email already sent to them no longer works."),
    NOTICE_NOT_FOUND: "That invitation no longer exists, so nothing was revoked.",
    NOTICE_REVOKE_REFUSED: (
        "That invitation has already been accepted and can no longer be revoked."
        " Removing someone who has already joined is a different action, and this"
        " page does not do it."
    ),
    NOTICE_UNAVAILABLE: (
        "Invitations are unavailable on this deployment right now, so nothing was"
        " created or changed. This is a problem on our side."
    ),
}
"""Every outcome this page can present. A test pins the two directions: every
notice constant has copy, and every copy entry is a constant."""

REVOCABLE_STATUSES = frozenset({STATUS_PENDING, STATUS_EXPIRED})
"""Which rows get a revoke control. #89 revokes a lapsed invitation as happily
as a live one — retiring a link whose expiry you do not want to wait out is
exactly what revoke is for — and refuses an accepted one, which is member
removal and a different action with different authority."""

_STATUS_WORDS = {
    STATUS_PENDING: "Awaiting acceptance",
    STATUS_ACCEPTED: "Accepted",
    STATUS_REVOKED: "Revoked",
    STATUS_EXPIRED: "Expired",
}


@dataclass(frozen=True)
class Notice:
    """One outcome to present, and the address it concerns.

    ``address`` is whatever the operator typed, echoed back so they can see
    which submission the sentence is about. It is escaped at render time along
    with the sentence it sits in.
    """

    kind: str
    address: str = ""


def request_refused_page(*, root_path: str = "", status_code: int) -> str:
    """The refused-body page, pointed back at this page's own path.

    The copy and the reasoning live in :func:`~.forms.refused_form_page`,
    shared with the owner page (#142); what this page owns is only where
    "back" leads.
    """

    return refused_form_page(root_path=root_path, status_code=status_code, back_path=ADMIN_INVITATIONS_PATH)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _notice_markup(notice: Notice) -> str:
    """Render one notice, escaping the composed sentence as a whole.

    The address is formatted into the constant *before* escaping rather than
    after, so there is one escape call covering both and no way to interpolate
    an already-escaped fragment into a string that is then escaped again.
    """

    template = NOTICES.get(notice.kind)
    if template is None:  # pragma: no cover - a kind not in NOTICES is a bug
        return ""
    return f'<p class="notice">{escape(template.format(address=notice.address))}</p>'


def _issue_form(root_path: str, csrf_token: str) -> str:
    return (
        f'<form method="post" action="{escape(root_path)}{ADMIN_INVITATIONS_PATH}">'
        f'<input type="hidden" name="csrf_token" value="{escape(csrf_token)}">'
        f'<label for="invite-email">Email address</label>'
        f'<input id="invite-email" type="email" name="{EMAIL_FIELD}" required'
        f' maxlength="{MAX_EMAIL_LENGTH}" autocomplete="off" spellcheck="false"'
        ' inputmode="email" placeholder="person@example.com">'
        '<button type="submit">Create invitation</button>'
        "</form>"
    )


def _revoke_form(root_path: str, csrf_token: str, invitation_id: str) -> str:
    return (
        f'<form class="inline" method="post"'
        f' action="{escape(root_path)}{ADMIN_INVITATIONS_REVOKE_PATH}">'
        f'<input type="hidden" name="csrf_token" value="{escape(csrf_token)}">'
        f'<input type="hidden" name="{INVITATION_ID_FIELD}" value="{escape(invitation_id)}">'
        '<button type="submit" class="link">Revoke</button>'
        "</form>"
    )


def _rows(invitations: Sequence[Invitation], now: datetime, root_path: str, csrf_token: str) -> str:
    rows = []
    for invitation in invitations:
        status = effective_status(invitation, now)
        action = _revoke_form(root_path, csrf_token, invitation.id) if status in REVOCABLE_STATUSES else ""
        # `org_id` is null for everything this page issues; an invitation into
        # an existing organization can only come from the owner surface or the
        # API, and saying which is which keeps the operator from reading one
        # as the other.
        kind = "New organization" if invitation.creates_organization else "Existing organization"
        rows.append(
            "<tr>"
            f"<td>{escape(invitation.email)}</td>"
            f"<td>{escape(_STATUS_WORDS.get(status, status))}</td>"
            f"<td>{escape(kind)}</td>"
            f"<td>{escape(_timestamp(invitation.created_at))}</td>"
            f"<td>{escape(_timestamp(invitation.expires_at))}</td>"
            f"<td>{action}</td>"
            "</tr>"
        )
    return "".join(rows)


def _listing(
    invitations: Sequence[Invitation],
    now: datetime,
    *,
    has_more: bool,
    root_path: str,
    csrf_token: str,
) -> str:
    if not invitations:
        return "<p>No invitations have been issued on this deployment yet.</p>"
    more = (
        "<p>Only the most recent invitations are shown. Older ones are still listed by the operator API.</p>"
        if has_more
        else ""
    )
    return (
        "<table><thead><tr>"
        "<th>Address</th><th>State</th><th>Joins</th><th>Created</th>"
        "<th>Expires</th><th></th>"
        "</tr></thead><tbody>"
        f"{_rows(invitations, now, root_path, csrf_token)}"
        f"</tbody></table>{more}"
    )


def invitations_page(
    *,
    root_path: str = "",
    session,
    invitations: Sequence[Invitation] = (),
    now: datetime,
    has_more: bool = False,
    notice: Notice | None = None,
) -> str:
    """Render the operator invitation page for this request.

    ``session`` supplies the identity footer and the CSRF token every form on
    this surface carries. ``now`` is the invitation service's clock, not
    Python's, so a row's derived ``expired`` state is decided by the same clock
    that will decide it at redemption.
    """

    identity = session.name or session.email or session.user
    body = (
        f"<h1>{escape(PAGE_TITLE)}</h1>"
        "<p>Invite someone to this deployment. Accepting creates their"
        " organization, with them as its owner; nothing exists until they"
        " accept.</p>"
        f"{_notice_markup(notice) if notice is not None else ''}"
        f"{_issue_form(root_path, session.csrf)}"
        "<h2>Issued invitations</h2>"
        f"{_listing(invitations, now, has_more=has_more, root_path=root_path, csrf_token=session.csrf)}"
    )
    return render_page(
        title=PAGE_TITLE,
        body=body,
        root_path=root_path,
        identity_label=identity,
        csrf_token=session.csrf,
    )
