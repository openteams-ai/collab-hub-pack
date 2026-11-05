"""The invitation-acceptance page (issue #90): copy, script, and its own CSP.

This is the **one page of the browser surface that runs JavaScript**, and the
reason is structural rather than a preference, so it is written down here
where the next reader will find it.

Why this page needs a script
----------------------------
The one-time invitation secret is delivered in the URL **fragment**
(``…/invite/accept#token=…``, minted by
:func:`~..frames.invitation_email.build_setup_url`). A fragment is never put
on the request line, never sent in a ``Referer``, and never reaches the
server at all — that is the whole point of choosing it, and it is what makes
the R3 claim "the token appears in no HTTP request line" true by
construction rather than by discipline.

The direct consequence: **only client-side script can read it.** A
server-rendered page cannot see the fragment, so there is no zero-JavaScript
design that also keeps the token out of the request line. The two
requirements are mutually exclusive, and R3 is the one that matters.

Why the relaxation is scoped to this path, and how
--------------------------------------------------
:mod:`.pages` sets ``default-src 'none'`` with **no** ``script-src`` at all
for the rest of the surface, and that is load-bearing: every other page is
plain documents and forms, so forbidding script outright costs nothing and
removes a whole class of injection. Widening that policy surface-wide to
serve this one page would trade a real property for a local convenience.

So the relaxation is:

* **Keyed on the path, not on a flag a handler sets.** ``/invite/accept``
  and nothing else gets the script-bearing policy; every other path on the
  surface gets :data:`~.pages.CONTENT_SECURITY_POLICY` unchanged. The
  decision lives in :func:`~.pages.headers_for_path`, which the security-
  header middleware consults — the same "path in, policy out" shape the
  session guard settled on, and for the same reason: a per-response marker
  is authored by whoever might get it wrong, a path is not.
* **Pinned to a hash, not ``'unsafe-inline'`` and not ``'self'``.**
  ``script-src`` names exactly one SHA-256 digest,
  :data:`ACCEPTANCE_SCRIPT_HASH`, computed **from the bytes this module
  serves** — so the policy cannot drift from the script, and an injected
  ``<script>`` (inline *or* same-origin, since ``'self'`` is deliberately
  absent) is refused by the browser. A nonce would have been the other
  acceptable answer; a hash is stronger here because the script is a
  compile-time constant with no per-request part, so there is nothing for a
  nonce to buy and one less thing to generate correctly.
* **Every other header is unchanged.** ``Referrer-Policy: no-referrer``,
  ``Cache-Control: no-store``, ``X-Content-Type-Options: nosniff``,
  ``X-Frame-Options: DENY``, ``frame-ancestors 'none'``, ``base-uri 'none'``
  and ``default-src 'none'`` all still apply. The only additions are the
  hashed ``script-src`` and ``connect-src 'self'``, which the redemption
  ``fetch`` needs and which permits this origin and nothing else.

**If you are here to make the surface consistent: do not widen
:data:`~.pages.CONTENT_SECURITY_POLICY`.** The inconsistency is the design.
Narrow the exception further if you can; never generalize it.

What the script is allowed to do with the token
------------------------------------------------
The token is read from the fragment into a JavaScript variable, stripped
from the address bar with ``history.replaceState`` immediately, kept in
``sessionStorage`` so it survives the Keycloak registration / sign-in round
trip in the same tab, and sent to the server in a **POST body**. It is never
written into the DOM, never an element value or attribute, never a query
parameter, never a form field, and never part of any URL this page
constructs.

The page's own markup is therefore fully static: every outcome is
server-rendered as a hidden ``<section data-state="…">`` and the script only
toggles the ``hidden`` attribute. There is no ``innerHTML``, no string-built
markup, and nothing derived from the token in anything the script writes.

Why redemption needs a click
----------------------------
The page could redeem the moment it loads — the person did, after all, click
a link in their email. It deliberately does not, and the reason is that
**joining an organization is permanent in this beta**: one organization per
login, and it does not change once it is set.

Consider a page load the person did not intend. Anyone who can issue an
invitation to a known address could, instead of emailing it, get that
address's owner to open ``…/invite/accept#token=…`` — an image link, a
redirect, anything. Auto-redemption would then bind that login to the
issuer's organization silently and irreversibly, and the CSRF token is no
defense because the page reads it from its own DOM. A confirmation step makes
the request a deliberate act by the person, and it is what puts the account
they are about to commit in front of them first.
"""

from __future__ import annotations

from base64 import b64encode
from hashlib import sha256
from urllib.parse import urlencode

from .data_statement import DATA_STATEMENT_TEXT
from .pages import SECURITY_HEADERS, escape, render_page
from .surface import DATA_STATEMENT_PATH

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

CONTAINER_ID = "accept"

TOKEN_STORAGE_KEY = "nexus.invite.token"
OUTCOME_STORAGE_KEY = "nexus.invite.outcome"

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

Deliberately one name for two things — a state the page decides at render
time and an outcome the redemption endpoint can answer — because they are the
same situation and the person's next step is identical. The endpoint is the
control; the render-time state only saves them a click that was going to fail.
"""

OUTCOME_ERROR = "error"

CLIENT_STATE_NO_TOKEN = "no_token"
CLIENT_STATE_SIGNIN = "signin"
CLIENT_STATE_READY = "ready"
CLIENT_STATE_WORKING = "working"

ACCEPT_BUTTON_ATTRIBUTE = "data-accept"
"""How the script finds the confirmation button. An attribute rather than an
id or a class, so restyling the page cannot silently unbind it."""

SETTLED_OUTCOMES = (
    OUTCOME_ACCEPTED,
    OUTCOME_NOT_FOUND,
    OUTCOME_EXPIRED,
    OUTCOME_REVOKED,
    OUTCOME_ALREADY_USED,
    OUTCOME_ORGANIZATION_MISSING,
)
"""Outcomes after which the browser throws the token away and remembers the
result, so a reload cannot re-POST it.

These are exactly the states in which the invitation can never work again:
it was redeemed, or it is dead. The states that are **not** here —
mismatched address, unverified email, already in an organization, service
unavailable — did not consume the token and are fixable by the person (sign
in as the invited address, verify the mailbox, use a separate login, wait),
so the browser keeps the token and a reload retries. Dropping it there would
force them to dig the original link out of their email for a problem they
had just been told how to fix.

On a deployment with ``frames.invitations.require_verified_email`` off, the
"unverified email" outcome above means something else -- the signed-in account
carried no usable address -- and is **not** fixable by the person, since that deployment
sends no verification mail. It stays out of this set either way, which is the
right side of the line for a different reason than the one listed: the browser
keeps the token not because a retry will work, but because throwing it away
would cost the invitee their only copy of it while somebody helps them.
:data:`RELAXED_PARAGRAPHS` is what makes the words match.
"""

# --- The page's script ------------------------------------------------------

ACCEPTANCE_SCRIPT = """\
(function () {
  "use strict";
  var TOKEN_KEY = "__TOKEN_KEY__";
  var OUTCOME_KEY = "__OUTCOME_KEY__";
  var TOKEN_SHAPE = /^[A-Za-z0-9_-]{1,512}$/;
  var SETTLED = __SETTLED__;
  var root = document.getElementById("__CONTAINER__");
  if (!root) { return; }

  function show(name) {
    var sections = root.querySelectorAll("section[data-state]");
    var found = false;
    for (var i = 0; i < sections.length; i++) {
      var match = sections[i].getAttribute("data-state") === name;
      sections[i].hidden = !match;
      if (match) { found = true; }
    }
    return found;
  }

  function present(name) { if (!show(name)) { show("__ERROR__"); } }

  function store() { try { return window.sessionStorage; } catch (e) { return null; } }
  function read(key) {
    var s = store();
    if (!s) { return ""; }
    try { return s.getItem(key) || ""; } catch (e) { return ""; }
  }
  function write(key, value) {
    var s = store();
    if (!s) { return; }
    try { s.setItem(key, value); } catch (e) { }
  }
  function drop(key) {
    var s = store();
    if (!s) { return; }
    try { s.removeItem(key); } catch (e) { }
  }

  function takeFragment() {
    var hash = window.location.hash || "";
    var value = "";
    if (hash.length > 1) {
      try { value = new URLSearchParams(hash.slice(1)).get("token") || ""; } catch (e) { value = ""; }
    }
    if (hash) {
      try { window.history.replaceState(null, "", window.location.pathname); }
      catch (e) { window.location.hash = ""; }
    }
    return TOKEN_SHAPE.test(value) ? value : "";
  }

  var fresh = takeFragment();
  if (fresh) { write(TOKEN_KEY, fresh); drop(OUTCOME_KEY); }

  var settled = read(OUTCOME_KEY);
  if (settled) { present(settled); return; }

  var token = fresh || read(TOKEN_KEY);
  if (!TOKEN_SHAPE.test(token)) { present("__NO_TOKEN__"); return; }
  if (root.getAttribute("data-signed-in") !== "true") { present("__SIGNIN__"); return; }
  if (root.getAttribute("data-claims-current") !== "true") { present("__REAUTH__"); return; }

  function submit() {
    present("__WORKING__");
    fetch(root.getAttribute("data-redeem"), {
      method: "POST",
      cache: "no-store",
      credentials: "same-origin",
      redirect: "manual",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": root.getAttribute("data-csrf") || ""
      },
      body: JSON.stringify({ token: token })
    }).then(function (response) {
      if (response.type === "opaqueredirect" || response.status === 0) {
        present("__SIGNIN__");
        return;
      }
      return response.json().then(
        function (data) { return data; },
        function () { return {}; }
      ).then(function (data) {
        var outcome = (data && typeof data.outcome === "string") ? data.outcome : "__ERROR__";
        if (SETTLED.indexOf(outcome) !== -1) { drop(TOKEN_KEY); write(OUTCOME_KEY, outcome); }
        present(outcome);
      });
    }).catch(function () { present("__ERROR__"); });
  }

  var button = root.querySelector("[__BUTTON__]");
  if (!button) { present("__ERROR__"); return; }
  button.addEventListener("click", function () {
    button.disabled = true;
    submit();
  });
  present("__READY__");
})();\
"""

for _placeholder, _value in (
    ("__TOKEN_KEY__", TOKEN_STORAGE_KEY),
    ("__OUTCOME_KEY__", OUTCOME_STORAGE_KEY),
    ("__CONTAINER__", CONTAINER_ID),
    ("__NO_TOKEN__", CLIENT_STATE_NO_TOKEN),
    ("__SIGNIN__", CLIENT_STATE_SIGNIN),
    ("__REAUTH__", OUTCOME_REAUTHENTICATION_REQUIRED),
    ("__READY__", CLIENT_STATE_READY),
    ("__WORKING__", CLIENT_STATE_WORKING),
    ("__BUTTON__", ACCEPT_BUTTON_ATTRIBUTE),
    ("__ERROR__", OUTCOME_ERROR),
    ("__SETTLED__", "[" + ", ".join(f'"{name}"' for name in SETTLED_OUTCOMES) + "]"),
):
    ACCEPTANCE_SCRIPT = ACCEPTANCE_SCRIPT.replace(_placeholder, _value)
del _placeholder, _value
"""Substituted rather than f-string-interpolated so the script above stays
readable as JavaScript, and so every name it shares with the Python side
(storage keys, state names, the settled-outcome list) has exactly one
definition. A state name that exists only in the script would be a state the
tests cannot enumerate."""


def _script_hash(script: str) -> str:
    """The CSP source expression pinning **these** bytes and no others."""

    return "sha256-" + b64encode(sha256(script.encode()).digest()).decode()


ACCEPTANCE_SCRIPT_HASH = _script_hash(ACCEPTANCE_SCRIPT)
"""Derived from :data:`ACCEPTANCE_SCRIPT` at import, never hand-written.

A hash-pinned CSP is only worth anything if the hash is of what is actually
served. Computing it here means editing the script cannot leave a stale
digest behind: the policy follows the code by construction, and a test
re-derives it from the served response body as well.
"""

ACCEPTANCE_CONTENT_SECURITY_POLICY = (
    f"default-src 'none'; style-src 'self'; script-src '{ACCEPTANCE_SCRIPT_HASH}'; "
    "connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'"
)
"""This page's policy — see the module docstring for why it differs.

Note what is **not** in it: no ``'unsafe-inline'``, no ``'unsafe-eval'``, and
no ``'self'`` in ``script-src``. One digest is the entire script budget for
this page. ``connect-src 'self'`` is the smallest grant that lets the
redemption POST reach this origin's own endpoint.
"""

ACCEPTANCE_PAGE_HEADERS = {
    **SECURITY_HEADERS,
    "Content-Security-Policy": ACCEPTANCE_CONTENT_SECURITY_POLICY,
}

# --- Copy -------------------------------------------------------------------

_SECTIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        CLIENT_STATE_READY,
        "Accept your invitation",
        (
            "You are signed in and your invitation is ready. Check the account"
            " shown at the bottom of this page first — in this beta a login joins"
            " one organization and that does not change afterwards, so accept"
            " with the account you mean to use.",
        ),
    ),
    (
        OUTCOME_REAUTHENTICATION_REQUIRED,
        "Confirm your sign-in to continue",
        (
            "You are signed in, but it was long enough ago that we need to"
            " re-check your email address with your identity provider before"
            " accepting an invitation on it. Nothing is wrong with your account,"
            " and your invitation has not been used.",
            "Continue below. If you are still signed in with your identity"
            " provider this takes a moment and asks you nothing.",
        ),
    ),
    (
        CLIENT_STATE_WORKING,
        "Accepting your invitation",
        ("One moment — we are redeeming your invitation.",),
    ),
    (
        CLIENT_STATE_SIGNIN,
        "Create your account to accept your invitation",
        (
            "Your invitation is ready. Create your account with the exact email"
            " address the invitation was sent to — or sign in with it, if you"
            " already have an account here.",
            "Your invitation is held in this browser tab while you do that, so"
            " finish in this tab. If you have to open an email-verification link,"
            " come back here afterwards, or simply open your invitation link"
            " again.",
        ),
    ),
    (
        CLIENT_STATE_NO_TOKEN,
        "Open your invitation link",
        (
            "This page needs the invitation link that was sent to you. The"
            " one-time code lives in the part of the link after the '#', and your"
            " browser never sends that part to us — so a bookmark of this page,"
            " or the address typed without it, will not work.",
            "Open the original link again in this browser. If you no longer have"
            " it, ask whoever invited you to send a new invitation.",
        ),
    ),
    (
        OUTCOME_ACCEPTED,
        "You are in",
        (
            "Your invitation has been accepted and your account now belongs to an organization on this deployment.",
            "Open the Collab desktop app and sign in with this same account to"
            " get started. You do not need this page again.",
        ),
    ),
    (
        OUTCOME_NOT_FOUND,
        "We do not recognize this invitation",
        (
            "The code in your link does not match any invitation. The most common"
            " cause is a link that was cut short — check that you copied all of"
            " it, including everything after the '#'.",
            "If it still does not work, ask whoever invited you to send a new invitation.",
        ),
    ),
    (
        OUTCOME_EXPIRED,
        "This invitation has expired",
        (
            "Invitations are valid for a limited time and this one has passed it."
            " Nothing has changed about your account.",
            "Ask whoever invited you to send a new invitation.",
        ),
    ),
    (
        OUTCOME_REVOKED,
        "This invitation was withdrawn",
        (
            "Whoever issued this invitation has since withdrawn it, so it can no"
            " longer be accepted. Nothing has changed about your account.",
            "If you think that is a mistake, contact the person who invited you.",
        ),
    ),
    (
        OUTCOME_ALREADY_USED,
        "This invitation has already been used",
        (
            "Each invitation can be accepted once, and this one has been.",
            "If you accepted it yourself, you already have access — open the"
            " Collab desktop app and sign in. If you did not, tell whoever"
            " invited you: someone else may have used your link, and they can"
            " check who.",
        ),
    ),
    (
        OUTCOME_EMAIL_MISMATCH,
        "This invitation was sent to a different address",
        (
            "You are signed in with one email address and this invitation was"
            " issued to another. An invitation can only be accepted by the"
            " address it names.",
            "Sign out below, sign in with the address the invitation was sent to,"
            " and this page will finish accepting it. Your invitation has not"
            " been used.",
        ),
    ),
    (
        OUTCOME_EMAIL_NOT_VERIFIED,
        "Confirm your email address first",
        (
            "We can only accept an invitation for an email address your identity"
            " provider has confirmed. Either this account's address is not"
            " confirmed yet, or the sign-in did not carry an address at all.",
            "If a confirmation email was sent to you, follow its link and come"
            " back to this tab — or open your invitation link again. If you were"
            " never asked to confirm an address, ask whoever invited you for"
            " help. Your invitation has not been used.",
        ),
    ),
    (
        OUTCOME_ALREADY_IN_ORGANIZATION,
        "This login already belongs to an organization",
        (
            "In this beta each login belongs to exactly one organization, and"
            " that does not change once it is set. This login already belongs to"
            " one, so it cannot also accept this invitation.",
            "Joining the organization this invitation is for needs a separate"
            " login with its own email address, and the invitation has to be"
            " issued to that address. Ask whoever invited you to reissue it"
            " there.",
            "Nothing has changed: your invitation has not been used, and what"
            " this login can already see is unaffected.",
        ),
    ),
    (
        OUTCOME_ORGANIZATION_MISSING,
        "The organization for this invitation is gone",
        (
            "This invitation names an organization that no longer exists, so"
            " there is nothing to join. Nothing has changed about your account.",
            "Ask whoever invited you to send a new invitation.",
        ),
    ),
    (
        OUTCOME_ORGANIZATION_CREATION_REFUSED,
        "This invitation cannot be accepted here",
        (
            "This invitation would create a new organization, and this"
            " deployment hosts a single organization, so it cannot be"
            " accepted. Nothing has changed about your account, and your"
            " invitation has not been used.",
            "Ask whoever invited you to send a new invitation into the existing organization.",
        ),
    ),
    (
        OUTCOME_UNAVAILABLE,
        "Invitations are temporarily unavailable",
        (
            "We could not process invitations just now. This is a problem on our"
            " side, not with your invitation or your account.",
            "Your invitation has not been used. Reload this page in a few"
            " minutes, and tell whoever invited you if it keeps happening.",
        ),
    ),
    (
        OUTCOME_ERROR,
        "Something went wrong",
        (
            "We could not finish accepting your invitation. Your invitation has not been used.",
            "Reload this page to try again, and tell whoever invited you if it keeps happening.",
        ),
    ),
)

RELAXED_PARAGRAPHS: dict[str, dict[int, str]] = {
    CLIENT_STATE_SIGNIN: {
        1: (
            "Your invitation is held in this browser tab while you do that, so"
            " finish in this tab. If you lose it, simply open your invitation"
            " link again."
        ),
    },
    OUTCOME_EMAIL_NOT_VERIFIED: {
        0: (
            "An invitation can only be accepted for the address it names, so we"
            " have to be able to read an address from the account you signed in"
            " with — and this sign-in did not carry one."
        ),
        1: (
            "That usually means the account has no email address set. Ask"
            " whoever invited you for help. Your invitation has not been used."
        ),
    },
}
"""Individual paragraphs that differ where a verified address is not required.

**Per paragraph, not per section, and that is the point.** The first version
overrode whole sections, which meant the sign-in state's heading and its first
paragraph were duplicated byte-for-byte from :data:`_SECTIONS`. Rewording the
shared text there would have left relaxed deployments on the old wording
forever, with nothing asserting the shared parts stayed shared -- exactly the
failure :data:`CONDITIONAL_BLOCK`'s docstring argues against for the email copy
("90% shared text and no mechanism keeping the shared part shared"). Indexing
into the paragraph tuple keeps one copy of everything that does not vary.

Headings are not overridable for the same reason: the only heading that needed
to change is the not-verified one, and a table that can change headings invites
the same duplication back.

**Why this exists.** With ``requireVerifiedEmail`` false,
``email_not_verified`` is no longer reached because an address is unverified --
it is reached only when the **sign-in** carries no usable address -- the
invitation row's own address is always populated. The strict
copy tells the reader to "follow the verification link that was sent to your
mailbox", and on such a deployment no such mail is ever sent. The sign-in
state's mention is hedged and so was never false, only noise about a step that
does not happen.
"""


RELAXED_HEADINGS: dict[str, str] = {
    OUTCOME_EMAIL_NOT_VERIFIED: "We could not read an email address for this account",
}
"""The one heading that changes, and the only one that should.

``email_not_verified`` is the only state whose *name* is wrong where
verification is not required: it is reached there because no address was
readable, not because one was unverified. Every other heading is true either
way.

Separate from :data:`RELAXED_PARAGRAPHS` so overriding a heading is a
deliberate act with its own name rather than something a paragraph entry can do
by accident -- and held to the same no-duplication rule by the tests, so an
entry restating the strict heading fails instead of freezing relaxed
deployments on wording a later edit moved past."""


def _validate_overrides() -> None:
    """Refuse a table entry that names no state or no paragraph, at import.

    :func:`_with_overrides` also refuses a bad index, but it runs per request
    and only on a relaxed deployment -- so a stale index would be a 500 on the
    invitation page for somebody with no other copy of their link. The same
    argument :data:`BODIES` makes for the email copy: unrenderable copy should
    stop the pod, not wait for the first reader.
    """

    states = {name: paragraphs for name, _, paragraphs in _SECTIONS}
    for table in (RELAXED_PARAGRAPHS, RELAXED_HEADINGS):
        unknown = sorted(set(table) - set(states))
        if unknown:
            raise ValueError(f"relaxed copy names states that do not exist: {unknown}")
    for state, overrides in RELAXED_PARAGRAPHS.items():
        limit = len(states[state])
        bad = sorted(i for i in overrides if not 0 <= i < limit)
        if bad:
            raise ValueError(f"relaxed copy for {state!r} names paragraphs out of range: {bad}")
        # Run the substitution itself, so this is the only place the indices are
        # checked and the code path a request takes is the one startup proved.
        _with_overrides(states[state], overrides)


PAGE_STATES: tuple[str, ...] = tuple(name for name, _, _ in _SECTIONS)
"""Every state the page can render, in document order.

Enumerated so the tests can assert the two directions that matter: every
terminal state the invitation service can produce has copy here, and every
state named by the script exists as a section.
"""


def _with_overrides(paragraphs: tuple[str, ...], overrides: dict[int, str] | None) -> tuple[str, ...]:
    """One section's paragraphs, with individual ones replaced.

    No range check here. :func:`_validate_overrides` runs this function over
    every section at import, so by the time a request reaches it the indices are
    known good -- and a second check would be unreachable code that also happens
    to be the uncovered line. One implementation, exercised at startup.
    """

    if not overrides:
        return paragraphs
    return tuple(overrides.get(i, text) for i, text in enumerate(paragraphs))


# Called here rather than beside its definition: it exercises `_with_overrides`,
# which is defined just above, so this is the earliest point at which the check
# can run the code path a request takes.
_validate_overrides()


def _section(name: str, heading: str, paragraphs: tuple[str, ...], extra: str = "") -> str:
    """One outcome, rendered hidden.

    ``extra`` is trusted markup composed in this module (a link, never
    anything request-derived); the paragraphs and the heading are escaped
    like every other string on this surface even though they are constants,
    because the next person to add copy should not have to notice.
    """

    body = "".join(f"<p>{escape(paragraph)}</p>" for paragraph in paragraphs)
    return f'<section data-state="{escape(name)}" hidden><h1>{escape(heading)}</h1>{body}{extra}</section>'


def signin_link(root_path: str, *, renew: bool = False, register: bool = False) -> str:
    """Where the page sends someone who is not signed in — or not recently.

    ``next`` is the app-relative acceptance path, so the sign-in flow returns
    them here — with the fragment already banked in ``sessionStorage``,
    because the script ran before they followed this link. The fragment
    itself is deliberately **not** replayed through ``next``: it would then
    be a query parameter, on a request line, in every log between here and
    Keycloak.

    ``renew`` adds the flag that makes the sign-in route run the flow even
    though a session already exists. That is the only way this surface can
    obtain **current** ``email_verified`` claims: it holds no token it could
    ask the IdP with, so it asks for a new ID token instead.

    ``register`` starts the flow on the IdP's registration form instead of
    its login form (#144). The invitee is this page's primary audience and by
    definition has no account yet; sending them to a login form whose way
    forward is a small "Register" link made the common case the hidden one.
    Keycloak's registration page carries its own back-to-sign-in link, so the
    path is a default, not a wall.
    """

    params = {"next": ACCEPT_PAGE_PATH}
    if renew:
        params["renew"] = "1"
    if register:
        params["register"] = "1"
    return f"{root_path}/web/signin?{urlencode(params)}"


def acceptance_page(
    *,
    root_path: str = "",
    session=None,
    claims_current: bool = False,
    require_verified_email: bool = True,
) -> str:
    """Render the acceptance page for this request.

    The server knows two things the script does not: whether this browser
    holds a web session, and whether that session's verified-address claims
    are recent enough to redeem on. Everything else — is there a token, what
    did redemption answer — is decided in the browser, so every outcome ships
    as hidden markup and the script chooses which one is visible.

    ``claims_current`` is a **hint**, not a control: it saves someone a click
    that was going to be refused. The redemption endpoint re-checks it, and
    that check is the one that decides, because the window can lapse between
    this render and the click.
    """

    signed_in = session is not None
    attributes = (
        f'id="{CONTAINER_ID}"'
        f' data-signed-in="{"true" if signed_in else "false"}"'
        f' data-claims-current="{"true" if signed_in and claims_current else "false"}"'
        f' data-redeem="{escape(root_path)}{ACCEPT_REDEEM_PATH}"'
    )
    if signed_in:
        # The CSRF token, not the invitation secret: it is bound to this
        # session, useless without the HttpOnly session cookie, and this is
        # the same value the layout's sign-out form already carries.
        attributes += f' data-csrf="{escape(session.csrf)}"'

    # The sign-in link is rendered *inside* the section the script reveals,
    # not beside it: the script's only DOM power is toggling `hidden` on
    # `section[data-state]`, so anything a state needs has to live in that
    # state's section or it is markup nobody can ever see.
    # Two links, registration first: the invitee is the primary audience and
    # has no account yet, so the create path is the default and sign-in is
    # the exception — the reverse of what a bare login form implies.
    link = (
        f'<p><a href="{escape(signin_link(root_path, register=True))}">Create your account</a></p>'
        f'<p><a href="{escape(signin_link(root_path))}">Already have an account? Sign in</a></p>'
    )
    # The confirmation control. `type="button"`, in no form, with no action:
    # the only thing that submits anything on this page is the script's fetch,
    # so there is no markup path by which the token could ride a form GET.
    # The data statement (#146), in full, immediately above the control that
    # performs the one irreversible act. Rendered from the same constant the
    # canonical page serves, so what the invitee agreed next to and what the
    # statement page says can never be two different texts.
    statement = (
        f'<p class="data-statement">{escape(DATA_STATEMENT_TEXT)}</p>'
        f'<p><a href="{escape(root_path)}{DATA_STATEMENT_PATH}">Read the data statement</a></p>'
    )
    button = f'<p><button type="button" {ACCEPT_BUTTON_ATTRIBUTE}>Accept invitation</button></p>'
    renew = f'<p><a href="{escape(signin_link(root_path, renew=True))}">Continue</a></p>'
    extras = {
        CLIENT_STATE_SIGNIN: link,
        CLIENT_STATE_READY: statement + button,
        OUTCOME_REAUTHENTICATION_REQUIRED: renew,
    }
    # Copy only: neither table adds or removes a state, so the wire contract and
    # PAGE_STATES are identical either way -- asserted in the tests.
    #
    # **Built per request, and it has to be.** Review suggested memoizing this on
    # the deployment boolean, on the reading that the sections depend on no
    # per-request input. They do: `extras` carries `link`, `statement` and
    # `renew`, each of which interpolates `root_path` -- read from
    # `request.scope` precisely because a prefixed deployment changes it. Caching
    # on the boolean alone would serve one request's prefix to another's page,
    # which is a correctness bug traded for a string join.
    para_overrides = {} if require_verified_email else RELAXED_PARAGRAPHS
    head_overrides = {} if require_verified_email else RELAXED_HEADINGS
    sections = "".join(
        _section(
            name,
            head_overrides.get(name, heading),
            _with_overrides(paragraphs, para_overrides.get(name)),
            extras.get(name, ""),
        )
        for name, heading, paragraphs in _SECTIONS
    )
    noscript = (
        "<noscript><h1>JavaScript is required on this page</h1>"
        "<p>Your invitation code travels in the part of the link after the '#',"
        " which browsers never send to a server — that is what keeps it out of"
        " web-server logs. Reading it therefore has to happen in your browser, so"
        " this one page needs JavaScript enabled. It is the only page here that"
        " does.</p></noscript>"
    )
    body = f"<div {attributes}>{sections}</div>{noscript}<script>{ACCEPTANCE_SCRIPT}</script>"
    identity = None
    if signed_in:
        identity = session.name or session.email or session.user
    return render_page(
        title="Accept your invitation",
        body=body,
        root_path=root_path,
        identity_label=identity,
        csrf_token=session.csrf if signed_in else None,
    )


__all__ = [
    "ACCEPTANCE_CONTENT_SECURITY_POLICY",
    "ACCEPTANCE_PAGE_HEADERS",
    "ACCEPTANCE_SCRIPT",
    "ACCEPTANCE_SCRIPT_HASH",
    "ACCEPT_PAGE_PATH",
    "ACCEPT_REDEEM_PATH",
    "PAGE_STATES",
    "SETTLED_OUTCOMES",
    "acceptance_page",
]
