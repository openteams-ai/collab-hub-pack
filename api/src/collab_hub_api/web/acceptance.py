"""The invitation-acceptance page (issue #90): the server's half of it.

The page itself is the registration app, a small React bundle built by
``admin-ui`` (``admin-ui/registration``) and served by
:mod:`..routers.invite`. It was server-rendered markup with one inline script
until the pages of the registration flow moved beside the admin panel; the
copy, the states and the handling of the invitation code now live in that app
and are tested by its own suite.

What stays here is what the two halves have to agree on:

* the paths, and
* the **outcome words** the redemption endpoint answers with, which the app
  turns into copy. They are the wire contract. ``registration/states.json`` in
  the app holds the copy for each, and a test in this suite reads that file to
  check that every word below has a page, so a new terminal state fails the
  suite instead of quietly rendering the generic error.

Why the page needs script at all
--------------------------------
The one-time invitation secret is delivered in the URL **fragment**
(``…/invite/accept#token=…``, minted by
:func:`~..frames.invitation_email.build_setup_url`). A fragment is never put
on the request line, never sent in a ``Referer``, and never reaches the
server at all — that is the whole point of choosing it, and it is what makes
the R3 claim "the token appears in no HTTP request line" true by
construction rather than by discipline. The direct consequence: only
client-side script can read it. The app reads it, strips it from the address
bar, keeps it in ``sessionStorage`` across the sign-in round trip, and sends it
to the server in a **POST body** and nowhere else (``registration/flow.ts``).

Which script may run on the page is decided by path, in
:func:`~.pages.headers_for_path`; see :data:`~.pages.REGISTRATION_APP_HEADERS`.
"""

from __future__ import annotations

from .pages import render_page

ACCEPT_PAGE_PATH = "/invite/accept"
"""The page an invitation link points at. Anonymous by design — see
:data:`~.surface.PUBLIC_WEB_PATHS`."""

ACCEPT_REDEEM_PATH = "/invite/accept/redeem"
"""The page's own POST endpoint, which **does** require a web session.

A separate path rather than a second method on the page, because the guard's
public-path exemption (:data:`~.surface.PUBLIC_WEB_PATHS`) is keyed on the
path and knows nothing about methods. Sharing one path would have made the
redemption endpoint anonymous as a side effect of making the page anonymous.
"""

# --- Outcomes ---------------------------------------------------------------

OUTCOME_ACCEPTED = "accepted"
OUTCOME_NOT_FOUND = "invitation_not_found"
OUTCOME_EXPIRED = "invitation_expired"
OUTCOME_REVOKED = "invitation_revoked"
OUTCOME_ALREADY_USED = "invitation_already_used"
OUTCOME_EMAIL_MISMATCH = "invitation_email_mismatch"
OUTCOME_EMAIL_NOT_VERIFIED = "email_not_verified"
OUTCOME_ALREADY_IN_ORGANIZATION = "already_in_organization"
OUTCOME_ORGANIZATION_MISSING = "organization_missing"
OUTCOME_ORGANIZATION_CREATION_REFUSED = "organization_creation_refused"
OUTCOME_UNAVAILABLE = "invitations_unavailable"
OUTCOME_REAUTHENTICATION_REQUIRED = "reauthentication_required"
"""The session's verified-address assertion is too old to act on.

Deliberately one name for two things (a state the app decides from the
session answer, and an outcome the redemption endpoint can answer), because
they are the same situation and the person's next step is identical. The
endpoint is the control; the app's own check only saves them a click that was
going to fail.
"""

OUTCOME_ERROR = "error"


def bundle_missing_page(*, root_path: str = "") -> str:
    """What the invitation link answers on a deployment built without the app.

    Server-rendered, with the layout the rest of the surface uses, because the
    thing that is missing is the bundle that would otherwise render it. An
    invitee reaching this has done nothing wrong and can do nothing about it,
    so the page says whose problem it is and that the invitation is intact.
    """

    return render_page(
        title="The invitation page is unavailable",
        body=(
            "<h1>The invitation page is unavailable</h1>"
            "<p>This deployment was started without the pages that accept an"
            " invitation. This is a problem on our side.</p>"
            "<p>Your invitation has not been used. Tell whoever invited you, and"
            " open your invitation link again once they say it is fixed.</p>"
        ),
        root_path=root_path,
    )


__all__ = [
    "ACCEPT_PAGE_PATH",
    "ACCEPT_REDEEM_PATH",
    "bundle_missing_page",
]
