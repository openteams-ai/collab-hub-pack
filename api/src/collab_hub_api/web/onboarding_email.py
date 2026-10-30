"""The onboarding email sent with an invitation (#155, #93).

Everything the message needs is known the moment an invitation is minted — the
link, the invited address and the expiry — so the server renders the whole
email and sends it. Nobody composes anything.

It was written for the other order: while delivery was manual (#116) the
invitation pages rendered this text for an operator to paste, with two slots
left visible for them to complete. Delivery is automated now, that display is
deleted, and the same copy is what goes out — which was the point of writing it
this way.

**The copy names no sender.** It is first person plural, signed by the team,
and it points the reader at a support address rather than asking them to reply.
That is not only tone: it is what let one text serve both delivery modes. A
message signed by whoever happened to be signed in, asking for a reply, works
from a human mailbox and breaks the moment the server sends the same words from
an unattended one. So this renderer deliberately does **not** take the session
identity — there is nothing per-operator in the output at all.

**The copy lives in ``onboarding_email.txt``, beside this module, and nowhere
else.** To change what invitees are told, edit that file: it is the text, not
a copy of the text. This module only substitutes the parts that vary per
invitation, and `docs/invitation-onboarding-email.md` explains *why* the copy
says what it says without restating it.

Placeholders are ``[UPPER CASE IN BRACKETS]``, uniformly, and substitution is
plain string replacement — no ``format`` or ``$`` templating, so a brace or a
dollar sign appearing in the copy one day cannot turn into a rendering error
in an email somebody is waiting on.

Two of them cannot be derived from the invitation at all:
:data:`NAME_PLACEHOLDER`, since an invitation records an address rather than a
person, and :data:`APP_INSTRUCTIONS_PLACEHOLDER`, which comes from deployment
configuration. :func:`render_onboarding_email` leaves them standing when they
are not supplied, and a test asserts they are the *only* ones that can survive;
:func:`render_for_automated_delivery` — the only path that sends — supplies
both and then **refuses** a body with any placeholder left in it.

**The temporary step is gone, and this note is its receipt.** The copy used to
carry a step explaining that no verification mail was coming and that an
operator would confirm the address by hand. That was true only while the realm
had no SMTP; it lands `verifyEmail=true` as of 2026-08-18
(an internal issue, #77), so the step was deleted along with
the "write to us as soon as you've finished step 2" clause in the opening.
Leaving it would have told people to ignore a verification email that had in
fact arrived. `docs/invitation-onboarding-email.md` records the same change.
"""

from __future__ import annotations

import re
import textwrap
from datetime import datetime, timezone
from importlib.resources import files

from .data_statement import DATA_STATEMENT_TEXT

TEMPLATE_FILENAME = "onboarding_email.txt"

NAME_PLACEHOLDER = "[NAME]"
"""Left in the rendered text for the sender to replace. An invitation carries
an email address and no name, so this is the one substitution the page cannot
make for them."""

APP_INSTRUCTIONS_PLACEHOLDER = "[DESKTOP APP DOWNLOAD / INSTALL INSTRUCTIONS]"
"""Left in the rendered text: how the beta build is distributed has not been
decided, so there is nothing truthful to render. A candidate for configuration
once it is."""

LINK_PLACEHOLDER = "[INVITATION LINK]"
RECIPIENT_PLACEHOLDER = "[INVITEE EMAIL]"
EXPIRY_PLACEHOLDER = "[EXPIRY]"
DATA_STATEMENT_PLACEHOLDER = "[DATA STATEMENT]"
"""The data statement is substituted rather than written into the template:
#146 makes ``web/data_statement.py`` its single source, and it is also shown
on the acceptance page and its own page. One paragraph, one owner, three
places it appears."""

WRAP_WIDTH = 72
"""Where the substituted data statement wraps, and the width the template
itself is written to. Plain-text email with unwrapped paragraphs renders as
one long line in some clients and is wrapped unpredictably by others; 72
leaves room for a couple of levels of quoting."""

SUBJECT_PREFIX = "Subject: "

CONDITIONAL_MARKER = re.compile(r"\[(?:IF [A-Z_0-9 ]+|END IF)\]")
"""Any conditional marker, for the post-resolution assertion.

Separate from :data:`CONDITIONAL_BLOCK` because it has to match a marker the
block pattern *failed* to match -- which is the whole failure mode.
"""


CONDITIONAL_BLOCK = re.compile(
    r"^\[IF VERIFIED EMAIL REQUIRED\][ \t]*\r?\n(.*?)^\[END IF\][ \t]*\r?\n",
    re.DOTALL | re.MULTILINE,
)
"""A span of copy that belongs in the message only under one configuration.

**Why markers in the template rather than a second template file.** The
divergence this module's history records -- two versions of the copy drifting
apart -- came from composing a second version elsewhere. Two files would
reintroduce exactly that, with 90% shared text and no mechanism keeping the
shared part shared. So the variants live together, and what varies is marked.

**Why not variant strings in Python.** The subject line is read out of the
template file specifically so *all* of the copy lives in one reviewable place.
Moving a paragraph into a Python literal would undo that for the one paragraph
whose wording is most likely to be argued about.

The marker spellings deliberately match the ``[UPPER CASE IN BRACKETS]``
placeholder convention, which means :data:`UNRESOLVED_PLACEHOLDER` matches them
too. That is the safety net rather than a coincidence: a body that reached the
sending path with a marker still in it is refused, so forgetting to resolve a
conditional fails the same way forgetting to substitute ``[NAME]`` does.
"""


CONDITIONAL_MARKERS_ARE_NOT_PLACEHOLDERS = """\
[IF VERIFIED EMAIL REQUIRED] and [END IF] are **selection markers**, not slots.

They mark a span the renderer keeps or drops according to
``frames.invitations.require_verified_email``; nothing substitutes them. They
are spelled like placeholders deliberately, so the same net that refuses an
unsubstituted [NAME] also refuses a marker that survived resolution.

If you reflow step 2, leave those two lines alone. Whitespace and line endings
around them are tolerated -- see :func:`_resolve_conditionals` -- but the
markers themselves must stay on their own lines.

Recorded here rather than in a copy-editing guide because this package no
longer ships one; the module docstring above still refers to a document that
did not come across, which is worth reconciling separately.
"""


UNRESOLVED_PLACEHOLDER = re.compile(r"\[[A-Z][A-Z /]*\]")
"""Matches any ``[UPPER CASE]`` slot still present in a rendered body.

Used by :func:`render_for_automated_delivery` as a **runtime** check, not only
a test one. An operator pasting the message can see and fill a leftover
placeholder; a server sending it cannot, and the invitee would read
``[DESKTOP APP DOWNLOAD / INSTALL INSTRUCTIONS]`` verbatim. So the automated
path refuses to produce a body that still contains one.

**Deliberately narrower than :data:`CONDITIONAL_MARKER`, which is wide enough
for underscores.** This pattern is applied to a body that already contains
operator-supplied ``app_instructions``, so widening it to ``[A-Z_0-9 ]``
made it match bracketed tokens in *their* copy -- ``Download [MACOS_ARM64]
build`` began failing every send with ``invalid_invitation_email``, and the
message is dropped so nobody saw why. The underscore case that widening was
for belongs to the marker pattern, which only ever sees the template.
"""

AUTOMATED_GREETING_NAME = "there"
"""What ``[NAME]`` becomes when nothing can know the person's name.

"Hi there," rather than dropping the salutation, so the automated and pasted
messages stay one template with one greeting rather than two variants that can
drift apart. An invitation records an address; if a name ever accompanies it,
pass it and this is unused.
"""


def _load_template() -> tuple[str, str]:
    """The subject and body out of the template file.

    Read through ``importlib.resources`` rather than ``__file__`` so it
    resolves the same whether the package is installed, zipped, or run from a
    checkout — the file ships inside the wheel, verified against a built
    artifact rather than assumed.

    The subject is the file's first line so that *all* of the copy lives in
    one reviewable file; it is split off here rather than being a constant in
    this module, which would have put one line of the email somewhere
    different from the other fifty.
    """

    text = files(__package__).joinpath(TEMPLATE_FILENAME).read_text(encoding="utf-8")
    first, _, body = text.partition("\n")
    if not first.startswith(SUBJECT_PREFIX):
        raise ValueError(f"{TEMPLATE_FILENAME} must begin with a {SUBJECT_PREFIX!r} line")
    return first[len(SUBJECT_PREFIX) :].strip(), body.lstrip("\n")


def _resolve_conditionals(body: str, *, require_verified_email: bool) -> str:
    r"""Keep or drop each conditional span, leaving no markers behind.

    The kept form is the block's own content, which carries its leading blank
    line, so a paragraph that stays is separated from the one above it and a
    paragraph that goes leaves no double blank behind. Both shapes are asserted
    on the rendered body rather than reasoned about here -- whitespace is
    exactly the kind of thing that reads correct and renders wrong.

    **Trailing whitespace after a marker is tolerated rather than diagnosed.**
    Someone reflowing this copy can leave a space or tab behind, and an exact
    ``\n`` requirement would turn that into a refusal to send.

    Line endings need no tolerance and never did. An earlier review reported
    that a CRLF checkout would make the substitution a no-op and fire the guard
    for both settings; **that mechanism does not exist.** ``Path.read_text``
    and ``importlib.resources``' text mode both perform universal-newline
    translation, so this function only ever sees ``\n`` however the file was
    checked out. The ``\r?`` is harmless and kept for cheapness, not as
    protection against anything reachable. Recorded because the previous
    version of this docstring asserted that history as fact, and it was
    verified by constructing a CRLF string by hand rather than by asking
    whether one could arrive.

    The raise is kept for a marker that survives for a reason the pattern
    cannot absorb, such as an unclosed ``[IF ...]``.
    """

    resolved = CONDITIONAL_BLOCK.sub(r"\1" if require_verified_email else "", body)
    if CONDITIONAL_MARKER.search(resolved):
        # The message names what is observable and stops there. An earlier
        # version guessed "unclosed [IF ...]", which is wrong for the likelier
        # cause: a *second* condition added to the copy, whose marker
        # CONDITIONAL_MARKER matches and CONDITIONAL_BLOCK -- keyed to one
        # literal name -- does not. Reporting the surviving markers lets the
        # reader see which it was.
        surviving = ", ".join(sorted(set(CONDITIONAL_MARKER.findall(resolved))))
        raise ValueError(
            f"{TEMPLATE_FILENAME}: conditional markers survived resolution ({surviving}). Each"
            " must begin its own line and be paired, and only [IF VERIFIED EMAIL REQUIRED] is"
            " understood -- a new condition needs teaching to CONDITIONAL_BLOCK, not just"
            " adding to the copy."
        )
    return resolved


SUBJECT, _BODY_TEMPLATE = _load_template()

BODIES: dict[bool, str] = {
    True: _resolve_conditionals(_BODY_TEMPLATE, require_verified_email=True),
    False: _resolve_conditionals(_BODY_TEMPLATE, require_verified_email=False),
}
"""Both variants, resolved once when this module is first imported.

Resolved here rather than per message for the reason :func:`_load_template`
already establishes for the template itself: unrenderable copy should stop the
pod from starting, not start cleanly, pass health checks, and then fail every
invitation with a message ``deliver`` deliberately drops.

**That property depends on somebody importing this module at startup, and for a
while nothing did.** The only importer was function-local inside
``render_invitation_email``, so the resolution actually happened on the first
send. ``ConfiguredInvitationEmailDelivery.__init__`` now touches this name
during startup validation, beside the check that refuses a missing
``app_instructions`` -- which is where "a deployment that enables invitation
email and cannot render it fails loudly" already lived. Public rather than
underscored because that dependency is real and should be visible.

It also removes a per-send ``DOTALL`` regex over the whole body, which the
result never depended on: one boolean and a module constant fully determine it.
"""


def format_expiry(expires_at: datetime) -> str:
    """The expiry as an absolute UTC timestamp.

    Absolute rather than "48 hours from now" for two reasons: the reader does
    not have to reason about when the clock started (it started at issuance,
    not at whenever the operator got round to sending), and the sentence
    cannot quietly become false if ``INVITATION_TTL`` changes.

    Naive datetimes are refused rather than assumed to be UTC — the same rule
    :func:`~..frames.invitation_email.render_invitation_email` applies, and for
    the same reason: an invitation's expiry is a deadline, and silently
    reinterpreting its timezone moves it.
    """

    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise ValueError("invitation expiry must be timezone-aware")
    return expires_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def render_onboarding_email(
    *,
    link: str,
    recipient: str,
    expires_at: datetime,
    name: str | None = None,
    app_instructions: str | None = None,
    require_verified_email: bool = True,
) -> str:
    """The complete message body, personalised for one invitation.

    ``link`` carries the one-time secret in its fragment, so the returned
    string is credential-bearing: it belongs on the page that already shows
    that link and nowhere else. It is deliberately **not** logged, returned by
    any API, or given a ``__repr__``-safe wrapper here — the caller that has
    the raw link already holds the same secret, and adding a second wrapper
    would suggest this string is safe to move around when it is not.

    ``name`` and ``app_instructions`` default to ``None``, which **leaves their
    placeholders in the output**. That existed for the operator-paste page,
    where a visible ``[NAME]`` was the instruction to fill it in; that page is
    gone, and the default now survives as the shape the placeholder-contract
    test measures. **Do not call this to send anything** — use
    :func:`render_for_automated_delivery`, which supplies both slots and refuses
    a body with any placeholder still in it.
    """

    # `break_on_hyphens=False` is not cosmetic: the default split
    # "collab-support@openteams.com" across two lines at its hyphen, which is
    # a broken address in the one paragraph that tells someone how to ask for
    # their data to be deleted. `break_long_words=False` for the same class of
    # reason — a long token is better overhanging the margin than silently cut
    # in half.
    statement = textwrap.fill(
        DATA_STATEMENT_TEXT,
        width=WRAP_WIDTH,
        break_on_hyphens=False,
        break_long_words=False,
    )
    substitutions = [
        (LINK_PLACEHOLDER, link),
        (RECIPIENT_PLACEHOLDER, recipient),
        (EXPIRY_PLACEHOLDER, format_expiry(expires_at)),
        (DATA_STATEMENT_PLACEHOLDER, statement),
    ]
    # Only when supplied: `None` deliberately leaves the placeholder standing,
    # which is what the contract test measures. Nothing sends this form.
    if name is not None:
        substitutions.append((NAME_PLACEHOLDER, name))
    if app_instructions is not None:
        substitutions.append((APP_INSTRUCTIONS_PLACEHOLDER, app_instructions))

    body = BODIES[bool(require_verified_email)]
    for placeholder, value in substitutions:
        body = body.replace(placeholder, value)
    return body


def render_for_automated_delivery(
    *,
    link: str,
    recipient: str,
    expires_at: datetime,
    app_instructions: str,
    name: str = AUTOMATED_GREETING_NAME,
    require_verified_email: bool = True,
) -> str:
    """The body to **send**, with nothing left for a human to fill in (#93).

    The difference from :func:`render_onboarding_email` is not the substitutions
    — it is the refusal at the end. The message an operator used to paste could
    carry a visible ``[PLACEHOLDER]``, because they were looking at it. A
    message the server sends cannot: the invitee reads whatever is there, and
    nobody proof-reads it.

    So this raises rather than sending a half-finished message, and it raises
    for **any** unresolved slot, not only the two known ones — a placeholder
    added to the copy later is caught by the same check, which is the whole
    reason it is a pattern rather than a list.

    That net covers the conditional markers too, since they are spelled like
    placeholders: a body that reached here with ``[IF VERIFIED EMAIL
    REQUIRED]`` still in it is refused rather than sent. ``require_verified_email``
    defaults to the strict value, matching the server-side check it describes,
    so a caller that forgets to thread it produces copy that over-explains
    rather than copy that lies.
    """

    if not app_instructions.strip():
        raise ValueError(
            "app_instructions must be set to send an invitation email:"
            " the copy tells the invitee how to get the desktop app, and there"
            " is no truthful default"
        )
    body = render_onboarding_email(
        link=link,
        recipient=recipient,
        expires_at=expires_at,
        name=name,
        app_instructions=app_instructions,
        require_verified_email=require_verified_email,
    )
    unresolved = sorted(set(UNRESOLVED_PLACEHOLDER.findall(body)))
    if unresolved:
        raise ValueError("refusing to send an invitation email with unresolved placeholders: " + ", ".join(unresolved))
    return body
