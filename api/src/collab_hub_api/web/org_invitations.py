"""The owner invitation page (issue #142): copy and markup.

``/web/org/invitations`` — restricted to the caller's own organization's
``role = 'owner'``. It does three things and deliberately not a fourth: it
takes an email address and issues an invitation **into the caller's
organization**, it shows that organization's invitations with their state,
and it revokes one. No member management, no member removal, no org
deletion; the page is the owner half of the invitation lifecycle exactly,
and a page module is where extra capability arrives unnoticed.

The one addition since #142 is bounded to that lifecycle: the **first-invite
naming step** (#92's criterion 4, observed missing on the live hub as #44).
Every organization starts with the neutral placeholder name, and an owner
issuing from here was shown only that placeholder as the destination — so
while the placeholder stands the page shows a naming form *instead of* the
issue form, and the server refuses to issue (a well-formed submission gets a
409 before the issue action; a malformed one fails its usual check first).
Naming happens once; after it the form is gone. Changing a chosen name is a
different action for a different surface, and this page does not offer it.

**The organization is never a form field.** It is implied by the caller's
ownership, resolved server-side per request (:mod:`.owner`) — the same
wrapper rule the ``/v1/orgs/{org_id}/invitations`` routes enforce, where an
owner naming somebody else's organization is refused before the handler
exists. A page with an org field would be a second, unguarded spelling of the
target.

Two delivery modes, one page
----------------------------
Issuing hands the freshly minted secret to the delivery adapter, exactly as
#89's API route does, and the page reports the sanitized outcome (sent /
could not be sent / unconfirmed). One single-use secret travels one route,
never two, so no link is rendered here at all.

Until invitation mail was deliverable this page had a second mode that
rendered the redemption link for an operator to send by hand, reusing
``web/invite_link.py`` at that module's dated width. That module and this
page's link branch are gone (an internal issue completed),
and a deployment with no provider now gets the "could not be sent" notice
rather than a rendered credential — which is the correct answer once mail is
the delivery channel. ``email_configured`` survives only to warn the owner
*before* they issue, since without a provider every issue will fail that way.

No JavaScript, and the POSTs render rather than redirect, for the reasons
:mod:`.admin` documents: the delivery outcome exists for exactly one
response, and the issuance rule behind it — one live invitation per address,
enforced in the service under an advisory lock — is what makes an accidental
resubmit harmless.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from ..frames.invitations import (
    MAX_ORGANIZATION_NAME_LENGTH,
    Invitation,
    effective_status,
)
from .admin import (
    _STATUS_WORDS,
    EMAIL_FIELD,
    INVITATION_ID_FIELD,
    MAX_EMAIL_LENGTH,
    REVOCABLE_STATUSES,
)
from .forms import refused_form_page
from .pages import escape, render_page
from .surface import (
    ORG_INVITATIONS_NAME_PATH,
    ORG_INVITATIONS_PATH,
    ORG_INVITATIONS_REVOKE_PATH,
)

__all__ = [
    "ORG_INVITATIONS_NAME_PATH",
    "ORG_INVITATIONS_PATH",
    "ORG_INVITATIONS_REVOKE_PATH",
    "ORGANIZATION_NAME_FIELD",
    "NOTICES",
    "NOTICE_ALREADY_LIVE",
    "NOTICE_ALREADY_NAMED",
    "NOTICE_INVALID_EMAIL",
    "NOTICE_INVALID_ORGANIZATION_NAME",
    "NOTICE_ISSUED_SEND_FAILED",
    "NOTICE_ISSUED_SEND_UNKNOWN",
    "NOTICE_ISSUED_SENT",
    "NOTICE_NAMED",
    "NOTICE_NOT_FOUND",
    "NOTICE_ORGANIZATION_UNNAMED",
    "NOTICE_REVOKED",
    "NOTICE_REVOKE_REFUSED",
    "NOTICE_UNAVAILABLE",
    "Notice",
    "PAGE_TITLE",
    "invitations_page",
    "request_refused_page",
]

PAGE_TITLE = "Your organization's invitations"

# The form field names and their bounds are the operator page's
# (:mod:`.admin`): one invitation form, one spelling of its fields. The one
# field this page has that the operator page does not is the name.

ORGANIZATION_NAME_FIELD = "organization_name"
"""The first-invite naming form's single field; bounded by
:data:`~..frames.invitations.MAX_ORGANIZATION_NAME_LENGTH`."""

# --- Notices ----------------------------------------------------------------
#
# One fixed sentence per outcome, exactly as on the operator page: constants
# written for a person, whose only dynamic part is the address, composed in
# and escaped with the whole string. Nothing here can carry a token, and
# nothing quotes a provider's error message, which is why the three issue
# outcomes are three fixed sentences rather than one sentence with a status
# pasted in.

NOTICE_ISSUED_SENT = "issued_sent"
NOTICE_ISSUED_SEND_FAILED = "issued_send_failed"
NOTICE_ISSUED_SEND_UNKNOWN = "issued_send_unknown"
NOTICE_ALREADY_LIVE = "already_live"
NOTICE_INVALID_EMAIL = "invalid_email"
NOTICE_REVOKED = "revoked"
NOTICE_NOT_FOUND = "not_found"
NOTICE_REVOKE_REFUSED = "revoke_refused"
NOTICE_UNAVAILABLE = "unavailable"
NOTICE_ORGANIZATION_UNNAMED = "organization_unnamed"
NOTICE_NAMED = "named"
NOTICE_ALREADY_NAMED = "already_named"
NOTICE_INVALID_ORGANIZATION_NAME = "invalid_organization_name"

NOTICES: dict[str, str] = {
    NOTICE_ISSUED_SENT: (
        "Invitation created for {address} and the invitation email has been"
        " handed to the email provider. They join your organization when they"
        " accept it."
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
        " fresh invitation, revoke that one below and issue another."
    ),
    NOTICE_INVALID_EMAIL: (
        "That is not a single email address, so nothing was created. Enter one"
        " plain address, exactly as the invitee will register with — it is matched"
        " exactly, and this page does not correct capitalization."
    ),
    NOTICE_REVOKED: "Invitation for {address} revoked. Its link no longer works.",
    NOTICE_NOT_FOUND: (
        "That invitation no longer exists or does not belong to your organization, so nothing was revoked."
    ),
    NOTICE_REVOKE_REFUSED: (
        "That invitation has already been accepted and can no longer be revoked."
        " Removing someone who has already joined is a different action, and this"
        " page does not do it."
    ),
    NOTICE_UNAVAILABLE: (
        "Invitations are unavailable on this deployment right now, so nothing was"
        " created or changed. This is a problem on our side."
    ),
    NOTICE_ORGANIZATION_UNNAMED: (
        "Your organization needs a name before anyone can be invited to it, so"
        " nothing was created and nothing was sent. Name it below first, so the"
        " organization you are inviting people into is one you have identified."
    ),
    NOTICE_NAMED: ("Your organization is now called {address}. You can invite people to it below."),
    NOTICE_ALREADY_NAMED: (
        "Your organization already has a name, so nothing changed. This page"
        " names an organization once; ask an administrator if it needs to"
        " change."
    ),
    NOTICE_INVALID_ORGANIZATION_NAME: (
        "That is not a usable organization name, so nothing changed. Use one"
        " line of plain text with at least one letter or digit, up to 120"
        " characters — and not the placeholder itself."
    ),
}
"""Every outcome this page can present. A test pins the two directions: every
notice constant has copy, and every copy entry is a constant."""


@dataclass(frozen=True)
class Notice:
    """One outcome to present, and the address it concerns.

    Same shape as the operator page's: ``address`` is whatever the owner
    typed, echoed back so they can see which submission the sentence is
    about, escaped at render time along with the sentence it sits in.
    """

    kind: str
    address: str = ""
    """The one dynamic part. An email address for every invitation outcome;
    for :data:`NOTICE_NAMED` it carries the name the owner just chose — the
    same slot, escaped the same way, rather than a second one that would be
    empty on every other notice."""


def request_refused_page(*, root_path: str = "", status_code: int) -> str:
    """The refused-body page, pointed back at this page's own path."""

    return refused_form_page(root_path=root_path, status_code=status_code, back_path=ORG_INVITATIONS_PATH)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _notice_markup(notice: Notice) -> str:
    template = NOTICES.get(notice.kind)
    if template is None:  # pragma: no cover - a kind not in NOTICES is a bug
        return ""
    return f'<p class="notice">{escape(template.format(address=notice.address))}</p>'


def _issue_form(root_path: str, csrf_token: str) -> str:
    return (
        f'<form method="post" action="{escape(root_path)}{ORG_INVITATIONS_PATH}">'
        f'<input type="hidden" name="csrf_token" value="{escape(csrf_token)}">'
        f'<label for="invite-email">Email address</label>'
        f'<input id="invite-email" type="email" name="{EMAIL_FIELD}" required'
        f' maxlength="{MAX_EMAIL_LENGTH}" autocomplete="off" spellcheck="false"'
        ' inputmode="email" placeholder="person@example.com">'
        '<button type="submit">Create invitation</button>'
        "</form>"
    )


def _name_form(root_path: str, csrf_token: str) -> str:
    return (
        f'<form method="post" action="{escape(root_path)}{ORG_INVITATIONS_NAME_PATH}">'
        f'<input type="hidden" name="csrf_token" value="{escape(csrf_token)}">'
        f'<label for="organization-name">Organization name</label>'
        f'<input id="organization-name" type="text" name="{ORGANIZATION_NAME_FIELD}" required'
        f' maxlength="{MAX_ORGANIZATION_NAME_LENGTH}" autocomplete="organization"'
        ' spellcheck="true" placeholder="Example Ltd">'
        '<button type="submit">Name the organization</button>'
        "</form>"
    )


def _revoke_form(root_path: str, csrf_token: str, invitation_id: str) -> str:
    return (
        f'<form class="inline" method="post"'
        f' action="{escape(root_path)}{ORG_INVITATIONS_REVOKE_PATH}">'
        f'<input type="hidden" name="csrf_token" value="{escape(csrf_token)}">'
        f'<input type="hidden" name="{INVITATION_ID_FIELD}" value="{escape(invitation_id)}">'
        '<button type="submit" class="link">Revoke</button>'
        "</form>"
    )


def _rows(invitations: Sequence[Invitation], now: datetime, root_path: str, csrf_token: str) -> str:
    # No "Joins" column: every row of `list_for_org` joins this organization
    # by construction (org-creating invitations belong to no organization and
    # appear in no organization's list), so the column would print one value.
    rows = []
    for invitation in invitations:
        status = effective_status(invitation, now)
        action = _revoke_form(root_path, csrf_token, invitation.id) if status in REVOCABLE_STATUSES else ""
        rows.append(
            "<tr>"
            f"<td>{escape(invitation.email)}</td>"
            f"<td>{escape(_STATUS_WORDS.get(status, status))}</td>"
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
        return "<p>Your organization has no invitations yet.</p>"
    more = (
        "<p>Only the most recent invitations are shown. Older ones are still listed by the API.</p>" if has_more else ""
    )
    return (
        "<table><thead><tr>"
        "<th>Address</th><th>State</th><th>Created</th>"
        "<th>Expires</th><th></th>"
        "</tr></thead><tbody>"
        f"{_rows(invitations, now, root_path, csrf_token)}"
        f"</tbody></table>{more}"
    )


def invitations_page(
    *,
    root_path: str = "",
    session,
    organization_name: str,
    organization_named: bool = True,
    email_configured: bool,
    invitations: Sequence[Invitation] = (),
    now: datetime,
    has_more: bool = False,
    notice: Notice | None = None,
) -> str:
    """Render the owner invitation page for this request.

    ``organization_name`` is the display name the invitation service resolved
    for the caller's organization — store-derived text, escaped like any
    other. ``organization_named`` is the router's verdict on whether that
    name is real or the placeholder every organization starts with: when it
    is the placeholder the page shows the naming form in place of the issue
    form, so the owner cannot invite anyone into "Unnamed organization" — the
    server refuses that too; hiding the form is the honest rendering of the
    refusal, not the enforcement.

    ``email_configured`` adds a warning when this deployment has no
    invitation email provider, because there is no longer a fallback: issuing
    would create an invitation nobody can be told about. ``now`` is the
    invitation service's clock, not Python's, so a row's derived ``expired``
    state is decided by the same clock that will decide it at redemption.
    """

    identity = session.name or session.email or session.user
    # Composed first, escaped whole — same one-escape rule as the notices.
    if organization_named:
        intro = escape(
            "Invite someone to join {org}. They will receive an email with an"
            " acceptance link; they become a member when they accept.".format(org=organization_name)
            + (
                ""
                if email_configured
                else " This deployment has no invitation email configured, so an"
                " invitation issued here cannot be sent and the invitee will never"
                " hear about it. Ask an administrator to configure invitation email"
                " first."
            )
        )
        form = _issue_form(root_path, session.csrf)
    else:
        intro = escape(
            "Your organization does not have a name yet. Give it one before"
            " inviting anyone, so that you can see which organization you are"
            f" inviting people into rather than \u201c{organization_name}\u201d. The"
            " name appears on this page and in your organization's records; it"
            " is display text, not an identifier, and it is chosen once here."
        )
        form = _name_form(root_path, session.csrf)
    body = (
        f"<h1>{escape(PAGE_TITLE)}</h1>"
        f"<p>{intro}</p>"
        f"{_notice_markup(notice) if notice is not None else ''}"
        f"{form}"
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
