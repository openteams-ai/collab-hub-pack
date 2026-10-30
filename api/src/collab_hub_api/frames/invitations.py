"""The invitation lifecycle: issue, list, revoke, redeem (issue #89, Gate B/E).

One operator sending one signup link is the whole point of the public-hub
invitation track, and this module is where that link becomes state. It owns
three things and deliberately not a fourth:

- **The secret.** Minted here (256 random bits), hashed here, compared here.
  What is persisted is the SHA-256 hex; the raw secret appears in no column,
  no log line, no audit row, no error message, and no response body, and its
  only egress is the invited mailbox.

  Keeping it out of logs, dumps, and responses is **structural**, not a list
  of patched exits. Three rounds of review established that suppressing
  ``__repr__`` and special-casing ``model_dump`` and ``__getstate__`` only
  ever closes the routes someone remembered; ``dataclasses.asdict``,
  ``astuple``, ``vars()``, ``json.dumps(..., default=vars)`` and — decisively
  — FastAPI's own response encoder all read the **field value** and would
  hand it out regardless. So no type here stores a raw ``str`` credential:
  every carrier holds a :class:`~.credentials.InvitationSecret`, which
  redacts itself under every rendering, refuses copying, pickling, and
  mutation, carries no ``__dict__`` to flatten, cannot be subclassed, and
  serializes to a redaction under pydantic. A carrier added later inherits
  all of it by holding that type.

  The claim is deliberately narrow, and stated the narrow way here because a
  broader one is falsifiable in a line: ``.reveal()`` is the only *supported*
  accessor, so every supported escape is one greppable call, and what the
  wrapper defends against is **accidental** escape through machinery that
  walks objects generically. Code that deliberately reads the slot can still
  read it — and could equally have called ``.reveal()`` — so nothing here
  tries to stop that. See :class:`~.credentials.InvitationSecret`.

  The one frame that does hold the raw string,
  :func:`hash_invitation_secret`, cannot raise **for the inputs the accept
  route can deliver** — pydantic has already validated the value as a ``str``
  matching a strict ASCII pattern — so on that path it never appears in a
  traceback. That is a claim about the route, not a claim of totality over
  arbitrary Python values; see the function's own docstring.
- **The state machine.** ``pending``/``accepted``/``revoked`` at rest, with
  ``expired`` **derived** from ``pending`` + ``expires_at`` so no sweeper has
  to keep a fourth stored state true. Every surface presents
  :func:`effective_status`, never the bare column.
- **The acceptance transaction.** Redeeming a token creates a membership row
  — and, for an org-creating invitation, the organization too — atomically
  with consuming the token and with the audit row that records it.

What it does **not** own: authorization (the two wrappers in
:mod:`.authorization` decide who may issue and revoke what) and the audit
insert (:func:`.audit.audited` is the only writer of ``collab_audit_events``).
This module *composes* both; it re-implements neither.

One audited action per mutation, and the action names the consequential thing
-----------------------------------------------------------------------------
``audited()`` writes exactly one row per invocation, and a second invocation
would be a second transaction — which is precisely what the primitive exists
to prevent. So each mutation here declares one action:

- issuing → ``invitation.send``;
- revoking → ``invitation.revoke`` (a revoke that changes nothing writes no
  row at all: it is not an action);
- accepting into an **existing** organization → ``invitation.redeem``;
- accepting an org-creating invitation → ``org.create``, with the accepter as
  actor, in the same transaction that creates the organization and the
  membership — the ratified requirement;
- an owner giving their placeholder-named organization its name →
  ``org.rename`` (:meth:`PostgresInvitationService.name_organization`, the
  first-invite naming flow of #92, reopened as #44). It lives on this
  service because it is the write half of
  :meth:`~PostgresInvitationService.organization_name` and exists for one
  caller: the owner invitation page, which refuses to issue while the
  placeholder stands, so an owner has identified the organization they are
  inviting people into before the first invitation leaves. The invitation
  email itself is organization-neutral by decision
  (:func:`.invitation_email.render_invitation_email` discards the name, and
  a regression pins that); the name is for the owner's page, the
  organization's record, and the audit row.

That last case therefore produces no ``invitation.redeem`` row, which is a
real asymmetry and is stated rather than hidden: both acceptance rows carry
``detail.invitation_id``, so following one invitation end to end is a query on
that key (documented in ``docs/frames-operations.md``) rather than on the
action alone.

Address matching (Gate B, ratified 2026-08-03; amended on #157)
---------------------------------------------------------------
Acceptance requires the caller's ``email`` claim to equal the invited address
**but for ASCII case**. Gate B chose exact match over canonicalization, and that
still holds for everything except case: there is no plus-tag stripping, no
dot-folding, no provider-specific rule, and no canonical column.

Gate B's *other* half — that the caller's OIDC ``email_verified`` claim is
boolean ``true`` — is a deployment setting since #190:
``frames.invitations.require_verified_email``, default on. Where it is off, the
invitation token stands in for the proof of mailbox control and **the address
match above still applies unchanged**. This paragraph exists because the
unconditional version of it was the canonical description of Gate B, and a
reader reasoning from it would conclude a claim with ``email_verified: false``
can never reach :func:`emails_match` — and might remove the ``require_verified``
plumbing as dead code.

The original rule was byte-exact, and it was wrong for one specific reason.
**Keycloak lowercases the email on every account it holds**, so the claim it
asserts is ``alice@example.com`` however the address was typed — and an
invitation issued to ``Alice@example.com`` could therefore never be redeemed
by anyone. That was documented here as a consequence "real and accepted"; it
was neither, and it blocked a real invitee on the public hub. Preserving case
never made the check stricter, because the identity provider had already
decided case does not distinguish accounts.

So the service folds ASCII case at both ends — :func:`validate_invited_email`
stores the lowered form, :func:`emails_match` compares the lowered forms — and
folds nothing else. :func:`ascii_folded_bytes` is the single definition, and
its docstring records why the ASCII bound is a safety property rather than an
implementation detail.

A genuine mismatch is still its own terminal state with its own wire code, and
it still does **not** consume the token, so the issuer can revoke and reissue.

Two consequences of matching the *claim at accept time* rather than a
snapshot taken at issuance, both tested:

- if the login's verified email changes to something else between issuance
  and acceptance, acceptance fails with ``invitation_email_mismatch``;
- if it changes *to* the invited address, acceptance succeeds. There is no
  issuance-time snapshot to fall back to and no memory of a previous claim.

Single-issuer assumption (R12)
------------------------------
Identity is the OIDC ``sub``, and a bare ``sub`` is unique only within one
issuer. Everything below — ``created_by``, ``accepted_by``, the membership
primary key, the audit ``actor`` — is a bare ``sub``, so this whole service
is correct only for a deployment that trusts exactly one issuer. That is a
startup precondition already, enforced by
:func:`.identity.enforce_single_issuer_for_pin`, not merely a note. Supporting
a second issuer later needs an explicit account model keyed on
``(issuer, sub)``; it is not a configuration change.

Not here, and deliberately: abuse controls
------------------------------------------
There is **no cap on how many pending invitations an actor or an organization
may hold**, and no rate limit on issuance. That is a deferral, not an
oversight, and it belongs to a specific place: issue #93 (``s07``, invitation
email delivery, register R3/R6) owns "invitation-email rate limits (per-org
and per-operator)", with the acceptance criterion that they "bound both a
compromised owner account and an operator script". #93 depends on this issue,
so the ordering is intended. Two neighbouring pieces of the same deferral live
there too: durable delivery state, and re-send that rotates the secret rather
than minting a second live token.

Separately, rate limiting the *redemption* surface — the one endpoint an
unauthenticated-adjacent caller can reach — is Phase 5 abuse-control work
(``s02``), not #93's.

One live invitation per address, where the caller asks for it (issue #91)
------------------------------------------------------------------------
**Scope first, because the obvious reading of this is wrong.** This is a
property of *one call*, not of the deployment. Nothing here stops a second live
invitation to the same address from existing — it stops **this call** from
creating one. :meth:`PostgresInvitationService.create` still mints
unconditionally, and that is unchanged: the API surface's contract (both the
operator routes and the owner routes in ``routers.invitations``) is #89's and
this module does not rewrite it. So an address can hold two live invitations
today, if one of them came from the API.

That is deliberate for this milestone rather than an oversight. The case the
rule exists for is a human retrying an issuance on the operator page after an
ambiguous send, and unifying the two paths means changing the semantics of a
merged, shipped ``create()`` that a wire contract and the desktop are built
against. #93 owns re-send policy properly, including rotation. Anyone stating
this guarantee — in code, docs, or a release note — must state it scoped to the
page.

:meth:`PostgresInvitationService.create_unless_live` is the *opt-in* variant
#91's operator page calls: it refuses to mint while a ``pending``, unexpired
invitation for the same address exists, and returns that invitation instead.

It is a separate method rather than a flag on ``create`` because the two have
different contracts (one always returns a secret, one may return no secret at
all), and a boolean parameter would put the difference somewhere a reader of a
call site cannot see it.

The rule is enforced **inside the audited transaction**, behind a
transaction-scoped advisory lock keyed on the address. A check-then-insert in
two statements is not enough here: a row that does not exist yet cannot be
locked, so two concurrent issuances for one address would both read "none" and
both insert. The advisory lock is what serializes them, and it is why an
operator double-submitting a form gets one token rather than two — which is
exactly the case the milestone cares about, because a human retries after an
ambiguous send.

Matching folds ASCII case, like everything else about an address here since
the amendment on #157: ``Alice@example.com`` and ``alice@example.com`` are one
address, so they cannot each hold a live invitation. That falls out of
:func:`validate_invited_email` storing the lowered form — the check, the
advisory lock key, and the stored row all see the same string — rather than
from a second comparison rule. Nothing beyond case is folded, so the plus-tag
and dot spellings Gate B refused to unify are still distinct addresses.

Listings are bounded here, though, because that is a response-size and
correctness question rather than a policy one: see
:meth:`PostgresInvitationService.list_all`.

Backend
-------
Postgres only. The guarantees this module exists to provide — one org exactly
once under concurrent acceptance, a token that cannot be redeemed twice, a
mutation that commits with its audit row or not at all — are the row lock,
the primary key, and the transaction. An in-memory backend could imitate the
API and none of the guarantees, so a deployment without the shared frames
Postgres gets :class:`InvitationsUnavailableError` (503) instead of a
plausible-looking fake.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from .audit import (
    AUDIT_ACTION_INVITATION_REDEEM,
    AUDIT_ACTION_INVITATION_REVOKE,
    AUDIT_ACTION_INVITATION_SEND,
    AUDIT_ACTION_ORG_CREATE,
    AUDIT_ACTION_ORG_RENAME,
    AUDIT_ACTION_SERVICE_ACCESS_GRANT,
    audited,
)
from .auth import WORKSPACE_DEFAULT, AuthContext, DisplayIdentity
from .credentials import REDACTED, InvitationSecret, refuse_to_serialize
from .invitation_email import validate_mailbox
from .org_source import org_source_is_single
from .orgs import ROLE_MEMBER, ROLE_OWNER
from .service_access_state import OutstandingGrant, ServiceAccessStateStore, claim_pending

invitations_logger = logging.getLogger("frames_server.invitations")

INVITATION_TTL = timedelta(hours=48)
"""Default invitation lifetime (server plan §4; shortened from 7 days by
issue #131). 48 hours is the compensating control for the operator page
rendering the redemption link (#91): delivery is human-paced, the issuer
knows when the link went out and can reissue in seconds, so a longer window
buys nothing and is only more time for a forwarded mail or shared screen to
be a live invitation. Not configurable in this beta: a per-request TTL is an
issuance-policy decision nobody has made, and a longer-lived link is the one
knob that makes a leaked link worse."""

INVITATION_SECRET_BYTES = 32
"""256 bits of ``secrets.token_urlsafe`` entropy. Far past online guessing,
and past offline guessing too — which is what makes the unsalted, unstretched
hash below correct rather than lazy."""

STATUS_PENDING = "pending"
STATUS_ACCEPTED = "accepted"
STATUS_REVOKED = "revoked"
STATUS_EXPIRED = "expired"
"""Derived only — never written to ``collab_invitations.status``."""

INVITATION_STORED_STATUSES = frozenset({STATUS_PENDING, STATUS_ACCEPTED, STATUS_REVOKED})
"""Mirrors the CHECK constraint in migration v3; pinned together by a test."""


# --- Terminal states --------------------------------------------------------
#
# One class per outcome the wire distinguishes. They are separate classes
# rather than one carrying a code because the router maps each to its own
# status and registered error code, and because a reader of a handler should
# not have to look up which string means which HTTP status.
#
# None of their messages interpolates caller-supplied text. In particular no
# message may ever contain the token: these strings reach the response body,
# and from there any proxy that logs bodies.


class InvitationError(Exception):
    """Base class for the explicit invitation terminal states."""


class InvitationNotFoundError(InvitationError):
    """No invitation matches this token or id.

    Also the answer for an invitation that exists but is out of the caller's
    scope: existence is not leaked to a caller who may not act on it.
    """


class InvitationExpiredError(InvitationError):
    """The invitation lapsed before it was accepted (derived expired state)."""


class InvitationRevokedError(InvitationError):
    """The invitation was revoked by its issuer or an operator."""


class InvitationAlreadyUsedError(InvitationError):
    """Single-use replay: this token has already been redeemed by someone else.

    Also raised by revoke on an already-accepted invitation — undoing an
    acceptance is member removal, which is a different action with different
    authority, not a late revoke.
    """


class EmailNotVerifiedError(InvitationError):
    """The caller's token carries no usable *verified* email claim.

    One state for every way that can be true: ``email_verified`` absent,
    false, or not a boolean, and ``email`` absent, empty, or not a string.
    Folding them together is deliberate — the distinctions are properties of
    the login's IdP configuration, not something the invitee can act on
    differently, and enumerating them would describe another account's claims
    to whoever holds the link.

    **Two conditions share this one state, and only one of them is always
    reachable.** With ``frames.invitations.require_verified_email`` on -- the
    default -- it is raised for an unverified or non-boolean claim *and* for a
    missing address. With it off, only the missing address can raise it, and
    the message says so, because ``routers/invitations.py`` returns
    ``str(exc)`` verbatim to every non-browser client.

    They share the exception and the wire code deliberately: widening either is
    a contract change for the desktop app and the acceptance page, and the
    remedy a caller has is the same in both cases -- sign in with an account
    that carries the invited address.
    """


class InvitationEmailMismatchError(InvitationError):
    """The caller's verified email is not the invited address.

    Distinct from :class:`EmailNotVerifiedError`: the claim is verified and
    well-formed, it simply belongs to a different mailbox. Does **not**
    consume the token — the invited mailbox's owner can still accept.
    """


class AlreadyInOrganizationError(InvitationError):
    """This login already has a home-organization row.

    Any row blocks: active or removed, in any organization, including the
    invitation's own target. One home organization per login is the
    ``collab_org_members`` primary key, and a *removed* row still binds the
    login to its organization — restoring it is an explicit owner action, not
    something a new invitation may do behind the owner's back.
    """


class OrganizationAlreadyNamedError(Exception):
    """The organization already carries a real name, so naming it is refused.

    :meth:`PostgresInvitationService.name_organization` is the *first* naming
    only — the step #92 specified so an owner names their organization before
    inviting anyone into it. Changing a name that was already chosen
    is a different action with a different audience (an operator, or a future
    owner settings page) and is not reachable through this one.

    Deliberately **not** an :class:`InvitationError`: that hierarchy is the set
    of acceptance outcomes the wire distinguishes, and the acceptance page's
    test enumerates it to prove every member has a page outcome. Naming is
    not an acceptance state and never reaches that page; the owner page
    catches this class by name.
    """


class OrgNotFoundError(InvitationError):
    """The invitation names an organization that does not exist."""


class OrganizationCreationRefusedError(InvitationError):
    """An org-creating invitation on a deployment that declares a single organization.

    ``frames.auth.orgSource=single`` (issue #91) is a declaration that this hub
    hosts exactly one organization, and an invitation with no ``org_id`` mints
    a new one on acceptance — the one write that would make the declaration
    false. Refused at **both** ends: at issuance, so the operator hears it
    while they can still act on it, and at acceptance, because an invitation
    issued before the deployment flipped to ``single`` is still live and
    acceptance is where the organization row is actually created. The
    acceptance-side refusal consumes nothing: the token stays live, exactly
    like the other fixable refusals, because the state is the deployment's
    configuration and not the invitation's.

    Invitations *into* an existing organization are untouched — they are how a
    single-organization hub grants the ``owner`` role, which auto-admission
    never does.
    """


_ORGANIZATION_CREATION_REFUSED_MESSAGE = (
    "This deployment declares a single organization (orgSource=single), so an invitation that"
    " would create another organization is refused. Invite into the declared organization, or"
    " move the deployment to orgSource=membership first."
)


def _refuse_org_creation_under_single_org(org_id: str | None) -> None:
    """The single-organization guard, shared by issuance and acceptance."""

    if org_id is None and org_source_is_single():
        raise OrganizationCreationRefusedError(_ORGANIZATION_CREATION_REFUSED_MESSAGE)


class InvitationsUnavailableError(RuntimeError):
    """No Postgres backend, so the invitation service does not exist here."""


# --- The row and the outcomes ----------------------------------------------


@dataclass(frozen=True)
class Invitation:
    """One ``collab_invitations`` row.

    ``token_hash`` is deliberately absent: nothing above the store needs it,
    and a field that is not carried cannot be logged, echoed, or compared
    against by accident. ``email`` is the invited address exactly as issued
    (Gate B exact match — there is no canonical form).
    """

    id: str
    org_id: str | None
    email: str
    status: str
    created_at: datetime
    created_by: str
    expires_at: datetime
    accepted_at: datetime | None = None
    accepted_by: str | None = None
    accepted_org_id: str | None = None
    revoked_at: datetime | None = None
    revoked_by: str | None = None

    @property
    def creates_organization(self) -> bool:
        return self.org_id is None

    @property
    def granted_role(self) -> str:
        """The role acceptance grants: owner of a new org, else member."""

        return ROLE_OWNER if self.creates_organization else ROLE_MEMBER


@dataclass(frozen=True)
class InvitationPage:
    """One bounded page of a listing.

    ``has_more`` rather than a total count: the count would be a second query
    against a different snapshot, so it could contradict the page it came
    with, and "is there another page" is the only question a pager actually
    needs answered.
    """

    invitations: list[Invitation]
    has_more: bool


@dataclass(frozen=True, repr=False, slots=True)
class IssuedInvitation:
    """A freshly created invitation and the one copy of its raw secret.

    The secret is returned rather than delivered inside the service on
    purpose: email delivery is an **unrecoverable side effect**, and #87's
    contract requires those to be sequenced *after* the audited transaction
    has committed, so a rollback never has to un-send a message. The caller
    holds this object for exactly as long as it takes to hand the secret to
    the email adapter.

    ``repr=False`` is not decoration, and it is the same decision the shipped
    SES adapter made for :class:`~.invitation_email.InvitationEmailMessage`:
    a generated dataclass ``__repr__`` prints every field, so any traceback
    that renders locals, any ``logger.warning("... %s", issued)``, and any
    debugger or error-reporter dump would print an access-granting credential
    in full. The replacement below names the invitation instead — an id is
    opaque, is what every error message and audit row already quotes, and is
    the thing anyone reading such a line actually needs.

    Suppressing the ``repr`` is only the rendering half. The field holds an
    :class:`InvitationSecret` rather than a ``str``, which is what closes
    every *value*-reading route — ``asdict``, ``astuple``, ``vars``, the
    FastAPI response encoder — structurally rather than one at a time.
    """

    invitation: Invitation
    raw_secret: InvitationSecret

    def __repr__(self) -> str:
        return f"IssuedInvitation(invitation_id={self.invitation.id!r}, raw_secret={REDACTED})"

    __getstate__ = refuse_to_serialize("IssuedInvitation")


@dataclass(frozen=True)
class LiveInvitationExists:
    """Nothing was issued: this address already holds a live invitation.

    The answer :meth:`PostgresInvitationService.create_unless_live` gives
    instead of minting a second token. It carries the existing invitation so a
    caller can name it — its id is what a revoke takes — and it deliberately
    carries **no secret**: the existing invitation's secret was handed to its
    issuer once, at issuance, and this service has only the digest. A caller
    who needs a fresh link revokes and issues again.

    A returned value rather than a raised exception, on purpose. Every
    exception this module defines is a terminal state of *redemption*, and the
    acceptance page enumerates ``InvitationError.__subclasses__()`` to prove it
    has copy for each; a sibling that only an issuer can ever see would make
    that enumeration describe a state the invitee's page cannot reach.
    """

    existing: Invitation


@dataclass(frozen=True)
class InvitationAcceptance:
    """The outcome of a successful redemption, replays included."""

    invitation_id: str
    org_id: str
    role: str
    org_created: bool
    replay: bool = False
    """True when this login had already redeemed this same token: the same
    success is returned, nothing was created, and no new audit row was
    written. A reloaded acceptance page must not strand its invitee."""


SERVICE_ACCESS_GRANTED = "granted"
SERVICE_ACCESS_FAILED = "failed"
"""The two outcomes a ``service_access.grant`` row records, in
``detail.outcome``.

Named constants because the reconciliation query matches on them as strings
and a typo on either side would silently return "nothing outstanding" — the
answer that looks exactly like the healthy one. Not a CHECK constraint: they
live inside a jsonb document, and #180's contract is the pair of queries, not
a third vocabulary to migrate."""


# --- Secrets ----------------------------------------------------------------


def hash_invitation_secret(raw_secret: str) -> str:
    """SHA-256 hex of a raw secret — the only form that is ever persisted.

    Unsalted and unstretched, deliberately, and this is the one place where
    that is the right call rather than a password-storage mistake: the input
    is 256 uniformly random bits, so there is no dictionary to run and no
    rainbow table to build, and a deterministic digest is what lets the
    column carry a unique index and the lookup be an index probe.

    Defined for every ordinary ``str``, lone surrogates included (hence
    ``surrogatepass``): a minted secret is URL-safe ASCII, and any other
    string must hash to a value that simply does not match — "not found" —
    rather than raising, because an encode error would turn a garbage request
    into a 500 that distinguishes garbage from a near-miss.

    Not total over *arbitrary* Python values, and the earlier wording of this
    docstring overstated that: a non-``str`` argument raises ``AttributeError``,
    and a hostile ``str`` subclass overriding ``encode`` can raise anything it
    likes. Neither is reachable from the accept route, whose input pydantic
    has already validated as a plain ``str`` against an ASCII pattern. Callers
    outside that route must not assume more than "total over ``str``".
    """

    return hashlib.sha256(raw_secret.encode("utf-8", "surrogatepass")).hexdigest()


@dataclass(frozen=True, repr=False, slots=True)
class MintedSecret:
    """A newly minted secret and its digest, in a box that cannot print itself.

    A bare ``(raw, hash)`` tuple would be the obvious return type and is the
    wrong one: a tuple prints its contents, so the local variable holding a
    freshly minted credential would be rendered in full by anything that
    walks frame locals — ``traceback.TracebackException(capture_locals=True)``,
    an error reporter, a debugger. Boxing it means the *only* name bound to
    the raw secret between minting and delivery belongs to an object whose
    ``repr`` is a constant and whose pickle is a refusal.
    """

    raw: InvitationSecret
    token_hash: str

    def __repr__(self) -> str:
        return f"MintedSecret({REDACTED})"

    __getstate__ = refuse_to_serialize("MintedSecret")


def mint_invitation_secret() -> MintedSecret:
    """Mint one invitation secret and its at-rest digest."""

    raw_secret = secrets.token_urlsafe(INVITATION_SECRET_BYTES)
    # Wrapped immediately: the plain string exists only as the argument of the
    # hash call below, in a frame that cannot fail (see hash_invitation_secret).
    return MintedSecret(raw=InvitationSecret(raw_secret), token_hash=hash_invitation_secret(raw_secret))


INVITATION_EMAIL_LOCK_CLASS = 0x494E
"""First half of the advisory-lock key used by :meth:`create_unless_live`.

PostgreSQL's advisory locks live in one deployment-wide namespace, so a
two-integer key is how unrelated features avoid colliding with each other. This
constant names *this* feature; the second integer names the address.
"""


def invitation_email_lock_key(email: str) -> int:
    """The advisory-lock object id for one invited address.

    A signed 32-bit integer derived from the address, because that is the shape
    the two-integer ``pg_advisory_xact_lock`` takes. Derived in Python from
    SHA-256 rather than from ``hashtext()`` so the key does not depend on a
    PostgreSQL internal whose value is explicitly not guaranteed stable across
    versions — a key that changed under a rolling upgrade would leave two
    replicas serializing on different locks for the same address.

    Collisions are harmless by construction: two addresses sharing a key
    serialize against each other unnecessarily and nothing else. The lock
    orders issuances; it does not decide them.
    """

    digest = hashlib.sha256(email.encode("utf-8", "surrogatepass")).digest()
    return int.from_bytes(digest[:4], "big", signed=True)


ASCII_CASE_FOLD = bytes.maketrans(
    bytes(range(ord("A"), ord("Z") + 1)),
    bytes(range(ord("a"), ord("z") + 1)),
)
"""``A``-``Z`` to ``a``-``z``, and not one byte more."""


def ascii_folded_bytes(value: str) -> bytes:
    """UTF-8 bytes with ASCII letters lowered and everything else untouched.

    The single definition of how this service normalizes an address, used by
    both issuance and comparison so the two cannot disagree (#157).

    **Folding on bytes, in the ASCII range only, is the safety property.**
    ``str.casefold()`` would make ``ss`` equal ``ss`` -- and ``s`` equal
    ``ss`` -- widening address equality; ``str.lower()`` maps U+212A KELVIN
    SIGN onto ASCII ``k`` and Turkish ``I`` onto ``i`` plus a combining dot,
    so a non-ASCII claim could lower onto an ASCII invited address. A byte
    translation of 0x41-0x5A cannot: every byte of a multi-byte UTF-8
    sequence is >= 0x80 and is copied through unchanged. So this introduces
    exactly one equivalence -- ASCII case -- which is the one Keycloak has
    already imposed by lowercasing every account's email.
    """

    return value.encode("utf-8", "surrogatepass").translate(ASCII_CASE_FOLD)


def validate_invited_email(value: str) -> str:
    """Validate an address at issuance, returning it ASCII-lowercased.

    **Lowercased, per the Gate B amendment on #157.** The original rule stored
    what the issuer typed, reasoning that a "helpful" lowercase would make the
    stored address something they never entered. That weighed the wrong cost:
    Keycloak lowercases the email on every account it holds, so an address
    stored with a capital is one the ``email`` claim can never equal, and the
    invitee is told their invitation was sent to a different address while
    looking at their own. Observed live on the public hub, blocking a real
    invitee.

    Normalizing here rather than only at comparison also makes the stored row,
    the operator's listing, and the address the onboarding email tells the
    invitee to use all agree with what the IdP will assert -- and makes the
    live-invitation check see ``Alice@`` and ``alice@`` as one address.

    The validation itself is :func:`.invitation_email.validate_mailbox`, the
    same one the shipped SES adapter applies to a recipient. Sharing it means
    an invitation that is accepted here is one the delivery path can also
    send: an address that only fails at the provider boundary would leave a
    stored invitation nobody can ever receive. It also guarantees ASCII, so
    the fold below is total rather than partial.
    """

    validated = validate_mailbox(value)
    return ascii_folded_bytes(validated).decode("ascii")


MAX_ORGANIZATION_NAME_LENGTH = 120
"""Bound on an organization's display name, in characters.

Long enough for any real organization, short enough that the name fits the
audit row's ``target_label`` (bounded at :data:`~.audit.AUDIT_LABEL_MAX_CHARS`)
and the page's intro sentence without wrapping into a paragraph. The page's
``maxlength`` restates it as a hint; the bound is enforced here.
"""


def is_placeholder_organization_name(name: str | None) -> bool:
    """Whether *name* is the neutral placeholder every organization starts with.

    ``NULL``, blank, and the placeholder string itself (compared without
    regard to ASCII case) all count: the column is nullable and defaults to
    the placeholder, and :meth:`PostgresInvitationService.organization_name`
    already words all three the same way. This is the predicate the owner page
    branches on — an organization for which it holds has never been named by
    anyone, and an owner inviting into it has never seen it identified as
    anything but the placeholder.

    The comparison is :func:`ascii_folded_bytes`, this module's one fold: the
    placeholder is ASCII, so an ASCII fold catches every capitalization of it,
    and no wider equivalence is introduced here any more than for addresses.
    """

    from .collab_schema import NEUTRAL_ORG_NAME

    if name is None:
        return True
    stripped = name.strip()
    return not stripped or ascii_folded_bytes(stripped) == ascii_folded_bytes(NEUTRAL_ORG_NAME)


def validate_organization_name(value: str) -> str:
    """Validate a display name an owner typed, returning it normalized.

    Display-only text, so the rule is deliberately thin: one line, between 1
    and :data:`MAX_ORGANIZATION_NAME_LENGTH` characters once whitespace is
    normalized, containing at least one letter or digit, and not the
    placeholder itself in any capitalization — "Unnamed organization" typed
    by hand is not a name, and accepting it would satisfy the page's check
    while leaving the owner exactly as uninformed as before.

    Whitespace is normalized rather than merely stripped: every Unicode space
    separator (``Zs`` — NBSP, ideographic space, and the rest) becomes an
    ASCII space and runs collapse to one, so ``Unnamed\u00a0organization`` is
    the placeholder, and a name cannot differ from another only in invisible
    spacing. "At least one letter or digit" (categories ``L*``/``N*``) is what
    rules out the visually blank: a Braille blank (U+2800), a string of
    combining marks, or punctuation alone renders as nothing an owner could
    recognize their organization by, and naming is one shot.

    "One line" is decided by Unicode category, not by the ASCII range: every
    control character (``Cc`` — C0, DEL, and the C1 block including NEL), the
    line and paragraph separators (``Zl``/``Zp``, U+2028/U+2029), and the
    format characters (``Cf`` — among them the bidirectional overrides that
    make text render as something other than what it is) are refused. The
    name becomes an audit ``target_label`` and a heading on the owner page,
    and a value that can forge a line break or reorder its own rendering in
    either is not a name. The audit primitive's own check
    covers only the ASCII controls; this rule is deliberately wider than
    that, so nothing this function accepts is later refused there. No other
    normalization — the owner's spelling is stored.

    Raises :class:`ValueError`; the page words that as its own fixed notice.
    """

    if not isinstance(value, str):
        raise ValueError("organization name must be a string")
    if any(unicodedata.category(ch) in _NOT_A_NAME_CATEGORIES for ch in value):
        raise ValueError("organization name contains control, separator, or format characters")
    spaced = "".join(" " if unicodedata.category(ch) == "Zs" else ch for ch in value)
    name = " ".join(spaced.split())
    if not name:
        raise ValueError("organization name is empty")
    if len(name) > MAX_ORGANIZATION_NAME_LENGTH:
        raise ValueError(f"organization name exceeds {MAX_ORGANIZATION_NAME_LENGTH} characters")
    if not any(unicodedata.category(ch)[0] in "LN" for ch in name):
        raise ValueError("organization name has no letter or digit")
    if is_placeholder_organization_name(name):
        raise ValueError("organization name is the placeholder")
    return name


_NOT_A_NAME_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})
"""Unicode general categories :func:`validate_organization_name` refuses.

``Cc`` is every control character (ASCII C0 and DEL, and the C1 block —
U+0085 NEL is a line break to many renderers); ``Zl``/``Zp`` are the line and
paragraph separators; ``Cf`` the format characters, which include the bidi
overrides (U+202E and kin) and the zero-width joiners a display name has no
honest use for. Everything else — letters, marks, digits, punctuation, symbols,
ordinary spaces — is a name's business.
"""


def verified_claim_email(email: object, email_verified: object, *, require_verified: bool = True) -> str:
    """The caller's usable email, or raise :class:`EmailNotVerifiedError`.

    ``email_verified`` must be the boolean ``True``. The string ``"true"``
    is rejected: some IdPs render claims as strings, and accepting the string
    would mean accepting ``"false"``-shaped truthiness from any IdP that ever
    emits a non-empty value here. This deployment's IdP contract (see
    ``docs/frames-operations.md``) is a boolean claim.

    **``require_verified=False`` drops the verification requirement and nothing
    else.** The address must still be a non-empty string, and the caller still
    compares it to the invited address with :func:`emails_match` — dropping the
    flag must never be mistaken for dropping the match, which is the check that
    makes an invitation an invitation. See
    :class:`~...config.FramesInvitationsConfig` for what the deployment is
    trading: the token is a 256-bit secret delivered only to the invited
    address, so holding it is itself proof of mailbox control, and the cost is
    that a forwarded invitation becomes usable by whoever received it.

    The default is ``True`` here as well as in configuration, so a caller that
    forgets to thread the setting fails closed.
    """

    if require_verified and email_verified is not True:
        raise EmailNotVerifiedError("Accepting an invitation requires a verified email address on your account.")
    if not isinstance(email, str) or not email:
        # A DIFFERENT condition, and the only one reachable when verification is
        # not required: the signed-in account carried no usable address. It shares
        # the exception and the wire code -- widening either is a contract
        # change -- but not the sentence, because `routers/invitations.py`
        # returns `str(exc)` verbatim and this is the one string every
        # non-browser client sees. Telling an API caller on a relaxed
        # deployment to "verify an address" points them at mail that deployment
        # never sends.
        raise EmailNotVerifiedError(
            "Accepting an invitation requires an email address on your account, and the"
            " account you signed in with did not provide one."
        )
    return email


def emails_match(invited: str, claimed: str) -> bool:
    """Gate B's comparison: equal but for ASCII case, in constant time.

    Case-insensitive per the amendment on #157, and **only** case: no
    plus-tag stripping, no dot-folding, no provider rules. Gate B rejected
    that ruleset and this does not reintroduce it. What it accepts is the one
    normalization the identity provider has already performed — Keycloak
    lowercases every account's email — so preserving case here never made the
    check stricter, it only made mixed-case invitations impossible to redeem.

    Folding still happens on bytes and still only over ``A``-``Z``; see
    :func:`ascii_folded_bytes` for why that bound is the safety property
    rather than a detail.

    Constant time is not the security boundary here — the invited address is
    not a secret — but the comparison sits immediately after a token lookup
    on the same request, and a length-dependent early exit is free to avoid.

    Compared as UTF-8 bytes, because ``compare_digest`` refuses non-ASCII
    ``str`` arguments with a ``TypeError`` and a claim is whatever the IdP
    put in the token. ``surrogatepass`` keeps a lone surrogate — which
    ``json`` will happily decode out of a ``\\ud800`` escape — a comparison
    that returns False rather than an exception that becomes a 500.
    """

    return hmac.compare_digest(ascii_folded_bytes(invited), ascii_folded_bytes(claimed))


def effective_status(invitation: Invitation, now: datetime) -> str:
    """The status to present, deriving ``expired`` from ``pending`` + expiry.

    Stored terminal states win: an invitation revoked or accepted before its
    expiry stays ``revoked``/``accepted`` forever, because what happened to
    it is more informative than the clock passing afterwards.
    """

    if invitation.status == STATUS_PENDING and invitation.expires_at <= now:
        return STATUS_EXPIRED
    return invitation.status


# --- Internal control-flow signals ------------------------------------------
#
# Raised *inside* an audited() body to abandon it without writing an event
# row, and caught immediately outside. Both cases are "the state changed
# under us and the right answer is a no-op success", which cannot be
# expressed by returning from the body: audited() writes its row whenever the
# body completes, and a no-op must not be recorded as an action.


class _NoAuditedChange(Exception):
    """Abandon the audited transaction; the outcome is already decided."""

    def __init__(self, outcome: object) -> None:
        super().__init__("no audited change")
        self.outcome = outcome


TRANSIENT_CONFLICT_ATTEMPTS = 3
"""How many times an audited mutation is re-run after a transient conflict."""


def _is_transient_conflict(exc: BaseException) -> bool:
    """Whether *exc* is a conflict the database expects the client to retry.

    Exactly the two SQLSTATE classes PostgreSQL documents as retryable —
    ``40001`` serialization failure and ``40P01`` deadlock detected. Both mean
    the transaction was aborted whole, so nothing it did survives and re-running
    it is not a partial repeat of anything.
    """

    import psycopg

    return isinstance(exc, (psycopg.errors.SerializationFailure, psycopg.errors.DeadlockDetected))


def _retrying(operation):
    """Run one audited mutation, re-running it on a transient conflict.

    This service is designed against **READ COMMITTED**, PostgreSQL's default,
    where the ``FOR UPDATE`` lock and the membership primary key resolve every
    race by blocking and re-reading. A deployment can set
    ``default_transaction_isolation`` to REPEATABLE READ or SERIALIZABLE on the
    database or the role, at any time, with no signal to this application —
    and under those levels the same races abort with ``40001`` instead of
    resolving. Correctness holds either way (an aborted transaction leaves no
    membership, no organization, and no audit row), but the *contract* does
    not: an invitee would receive a 503 where the wire contract promises
    ``invitation_already_used`` or an idempotent replay.

    Retrying is chosen over asserting READ COMMITTED at startup for two
    reasons. The setting is not a property of this build — it can change
    underneath a running pod, so a startup assertion would guard a fact that
    can quietly stop being true, which is the kind of guard this codebase
    treats as worse than none. And refusing to boot would turn a change that
    does not break correctness into an outage.

    Bounded, and without backoff: a serialization failure means the conflicting
    transaction has already committed or aborted, so the retry contends with
    nothing and a sleep would only add latency to a request someone is waiting
    on. Three attempts, then the error propagates as the 503 it would have
    been anyway.

    Only whole audited mutations are passed here. Each attempt re-runs its own
    pre-read, so a retry re-decides every terminal state against current
    committed state rather than replaying a stale conclusion.
    """

    for attempt in range(1, TRANSIENT_CONFLICT_ATTEMPTS + 1):
        try:
            return operation()
        except Exception as exc:
            if attempt == TRANSIENT_CONFLICT_ATTEMPTS or not _is_transient_conflict(exc):
                raise
            invitations_logger.warning(
                "invitation_transaction_retry",
                extra={"attempt": attempt, "reason": type(exc).__name__},
            )
    raise AssertionError("unreachable: the loop returns or raises")  # pragma: no cover


class UnavailableInvitationService:
    """The service on a deployment with no Postgres behind it.

    Every method raises. There is no permissive or in-memory path — see the
    module docstring: the API without the transaction is not the feature.
    """

    available = False

    def _unavailable(self) -> InvitationsUnavailableError:
        return InvitationsUnavailableError(
            "Invitations require the shared frames Postgres database, which is not configured."
        )

    def create(self, *args, **kwargs) -> IssuedInvitation:
        raise self._unavailable()

    def create_unless_live(self, *args, **kwargs) -> IssuedInvitation | LiveInvitationExists:
        raise self._unavailable()

    def list_for_org(self, *args, **kwargs) -> InvitationPage:
        raise self._unavailable()

    def list_all(self, *args, **kwargs) -> InvitationPage:
        raise self._unavailable()

    def get(self, *args, **kwargs) -> Invitation | None:
        raise self._unavailable()

    def revoke(self, *args, **kwargs) -> Invitation:
        raise self._unavailable()

    def accept(self, *args, **kwargs) -> InvitationAcceptance:
        raise self._unavailable()

    def organization_name(self, *args, **kwargs) -> str | None:
        raise self._unavailable()

    def name_organization(self, *args, **kwargs) -> str:
        raise self._unavailable()

    def record_service_access_grant(self, *args, **kwargs) -> None:
        raise self._unavailable()

    def settle_service_access_grant(self, *args, **kwargs) -> None:
        raise self._unavailable()

    def outstanding_service_access_grants(self, *args, **kwargs) -> list[OutstandingGrant]:
        raise self._unavailable()

    def server_now(self) -> datetime:
        raise self._unavailable()


_PAGE_ORDER = "ORDER BY created_at DESC, id"
"""Total order for listings, and the reason paging is safe.

``created_at`` alone is not a total order — two invitations issued in the same
transaction share it — and rows tied on the sort key can be returned in any
order between queries, so a ``LIMIT``/``OFFSET`` walk over them could show one
twice and another never. Breaking the tie on the primary key makes the order
total, so consecutive pages neither overlap nor skip."""


_COLUMNS = (
    "id, org_id, email, status, created_at, created_by, expires_at, "
    "accepted_at, accepted_by, accepted_org_id, revoked_at, revoked_by"
)
"""Named once so a SELECT and a RETURNING can never drift apart."""


class PostgresInvitationService:
    """The invitation lifecycle over the shared frames Postgres pool.

    **Every mutation runs inside :func:`.audit.audited`, on the guarded
    connection that primitive yields.** That is #87's contract and it is not
    mechanically enforced: a second ``db.connection()`` checkout inside one of
    these bodies would commit independently and silently leave the atomicity
    guarantee, with nothing to catch it. The rule this class follows, without
    exception, is that inside an ``audited()`` block the only database handle
    that exists is ``event.conn``, and ``self._db`` is not touched.

    Reads (list, get, and the fail-fast pre-read of an acceptance) are
    ordinary pooled checkouts, because a read has nothing to be atomic with.
    """

    available = True

    def __init__(self, db, *, require_verified_email: bool = True):
        self._db = db
        # A deployment property, so it is read once here rather than per
        # request: nothing about a single acceptance should be able to change
        # what the deployment requires of an identity. Defaults to the strict
        # value so a construction that forgets it fails closed.
        self._require_verified_email = require_verified_email

    # --- Reads --------------------------------------------------------------

    def server_now(self) -> datetime:
        """The database's clock, which is the only clock this service reads.

        Expiry is a comparison between a stored ``timestamptz`` and "now",
        and app replicas do not share a clock with each other or with
        Postgres. Deriving ``expired`` from the database's own clock makes the
        answer identical on every replica; asking Python would make a link's
        validity depend on which pod answered.

        ``clock_timestamp()`` rather than ``now()`` throughout this module's
        expiry comparisons: ``now()`` is the *transaction* timestamp, fixed
        when the transaction opened. That is the right thing for recording
        when something happened and the wrong thing for asking whether a
        deadline has passed, because a transaction that waits — on a row
        lock, on a conflicting insert — keeps answering with the time it
        started. See the consume statement in :meth:`_redeem`.
        """

        with self._db.connection() as conn:
            return conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]

    def organization_name(self, org_id: str) -> str:
        """The display name of an existing organization, for the email's wording.

        Falls back to the neutral placeholder rather than ``None`` when the
        column is null or the organization is unknown: the email template
        words a null name as "accepting creates an organization with you as
        its owner", which would be a lie in an invitation that joins an
        existing one. A vague name in that sentence is a much smaller wrong
        than a false description of what the link does.
        """

        from .collab_schema import NEUTRAL_ORG_NAME

        with self._db.connection() as conn:
            row = conn.execute("SELECT name FROM collab_orgs WHERE id = %s", (org_id,)).fetchone()
        if row is None or not row["name"]:
            return NEUTRAL_ORG_NAME
        return row["name"]

    # --- Naming -------------------------------------------------------------

    def name_organization(self, ctx: AuthContext, *, org_id: str, name: str) -> str:
        """Give a placeholder-named organization its name, recorded as ``org.rename``.

        The first-invite naming flow (#92 criterion 4, observed missing live
        on #44): every organization starts as ``NEUTRAL_ORG_NAME`` —
        acceptance of an org-creating invitation never supplies a name — and
        the owner page refuses to issue while that placeholder stands. This
        is the step that clears it.

        Same authorization contract as every other mutation here: the caller
        has already been authorized by :func:`~.authorization.requires_org_role`
        pinned to *org_id*, and the row is written inside :func:`~.audit.audited`
        with the owner as actor, the organization as target, and the new name
        as ``target_label`` — so the log answers "who named it, and what" from
        one row.

        **One shot.** The organization's current name is read ``FOR UPDATE``
        inside the transaction, and if it is not the placeholder the call
        raises :class:`OrganizationAlreadyNamedError` and writes nothing: two
        owners naming at once resolve to one ``org.rename`` row and one
        refusal, and a stale page cannot silently overwrite a name that was
        already chosen. Renaming a named organization is not this method's
        job (see the error's docstring).

        *name* goes through :func:`validate_organization_name`; the page has
        already applied it and worded a failure, so a :class:`ValueError`
        here is a programming error rather than the owner's typo.
        """

        return _retrying(lambda: self._name_organization_once(ctx, org_id=org_id, name=name))

    def _name_organization_once(self, ctx: AuthContext, *, org_id: str, name: str) -> str:
        new_name = validate_organization_name(name)
        with audited(
            self._db,
            ctx,
            AUDIT_ACTION_ORG_RENAME,
            target_type="org",
            target_id=org_id,
            target_label=new_name,
            org_id=org_id,
            detail={"replaced_placeholder": True},
        ) as event:
            row = event.conn.execute("SELECT name FROM collab_orgs WHERE id = %s FOR UPDATE", (org_id,)).fetchone()
            if row is None:
                raise OrgNotFoundError("Organization not found")
            if not is_placeholder_organization_name(row["name"]):
                raise OrganizationAlreadyNamedError("Organization already has a name")
            event.conn.execute("UPDATE collab_orgs SET name = %s WHERE id = %s", (new_name, org_id))
        return new_name

    def get(self, invitation_id: str) -> Invitation | None:
        with self._db.connection() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM collab_invitations WHERE id = %s",
                (invitation_id,),
            ).fetchone()
        return _invitation(row) if row is not None else None

    def list_for_org(self, org_id: str, *, limit: int, offset: int = 0) -> InvitationPage:
        """One page of an organization's invitations, newest first.

        Org-creating invitations (``org_id IS NULL``) are not in any
        organization's list: they are hub artifacts, and an owner has no
        authority over them.
        """

        return self._page(
            f"SELECT {_COLUMNS} FROM collab_invitations WHERE org_id = %s {_PAGE_ORDER}",
            (org_id,),
            limit=limit,
            offset=offset,
        )

    def list_all(self, *, limit: int, offset: int = 0) -> InvitationPage:
        """One page of every invitation, newest first — the operator's view."""

        return self._page(
            f"SELECT {_COLUMNS} FROM collab_invitations {_PAGE_ORDER}",
            (),
            limit=limit,
            offset=offset,
        )

    def _page(self, sql: str, params: tuple, *, limit: int, offset: int) -> InvitationPage:
        """Run a bounded, deterministically ordered listing query.

        One row beyond the page is fetched to answer "is there more?" without
        a second ``count(*)`` over a table that has no bound on its size — the
        count would also be a different snapshot from the page, so it could
        disagree with what the caller just received.
        """

        if limit < 1 or offset < 0:
            raise ValueError("limit must be positive and offset must not be negative")
        with self._db.connection() as conn:
            rows = conn.execute(f"{sql} LIMIT %s OFFSET %s", (*params, limit + 1, offset)).fetchall()
        return InvitationPage(
            invitations=[_invitation(row) for row in rows[:limit]],
            has_more=len(rows) > limit,
        )

    # --- Issue --------------------------------------------------------------

    def create(self, ctx: AuthContext, *, email: str, org_id: str | None) -> IssuedInvitation:
        """Issue one invitation, recorded as ``invitation.send``.

        The caller has **already been authorized** by one of the two wrappers
        in :mod:`.authorization`; this method is deliberately unaware of which
        one, which is exactly why the operator-issued and owner-issued rows
        come out identical apart from ``actor``/``actor_label``.

        Returns the raw secret to the caller instead of sending it: delivery
        is unrecoverable and must happen after this transaction commits.
        """

        _refuse_org_creation_under_single_org(org_id)
        return _retrying(lambda: self._create_once(ctx, email=email, org_id=org_id))

    def _create_once(self, ctx: AuthContext, *, email: str, org_id: str | None) -> IssuedInvitation:
        invited = validate_invited_email(email)
        invitation_id = uuid4().hex
        # One name, one redacted repr: see MintedSecret. A retry mints again,
        # which is correct — the previous attempt committed nothing, so its
        # secret corresponds to no row and is unusable by construction.
        minted = mint_invitation_secret()
        with audited(
            self._db,
            ctx,
            AUDIT_ACTION_INVITATION_SEND,
            target_type="invitation",
            target_id=invitation_id,
            target_label=invited,
            org_id=org_id,
            # No secret, no hash: `detail` is a redacted summary that a person
            # reads out of psql, and a token hash in an audit row is a
            # redemption oracle for anyone with log access. `ttl_hours` since
            # #131: a sub-day TTL truncated to `.days` would record 0, and no
            # reader consumes the old `ttl_days` key — rows written before the
            # rename keep it, which is correct, because detail describes the
            # issuance as it happened.
            detail={"creates_organization": org_id is None, "ttl_hours": INVITATION_TTL // timedelta(hours=1)},
        ) as event:
            try:
                row = event.conn.execute(
                    f"""
                    -- `now()` here, not `clock_timestamp()`: this is the one
                    -- timestamp comparison in the module that *should* use the
                    -- transaction clock. `created_at` defaults to `now()`, so
                    -- taking `expires_at` from the same value is what makes
                    -- `expires_at - created_at` exactly the TTL rather than the
                    -- TTL plus however long the insert took.
                    INSERT INTO collab_invitations (id, org_id, email, token_hash, created_by, expires_at)
                    VALUES (%s, %s, %s, %s, %s, now() + %s)
                    RETURNING {_COLUMNS}
                    """,
                    (invitation_id, org_id, invited, minted.token_hash, ctx.user, INVITATION_TTL),
                ).fetchone()
            except Exception as exc:
                if _is_foreign_key_violation(exc):
                    # A named organization that does not exist. Same answer as
                    # "not yours" on the owner path, so probing the operator
                    # surface does not enumerate organization ids either.
                    raise OrgNotFoundError("Organization not found") from None
                raise
            invitation = _invitation(row)
        return IssuedInvitation(invitation=invitation, raw_secret=minted.raw)

    def create_unless_live(
        self, ctx: AuthContext, *, email: str, org_id: str | None
    ) -> IssuedInvitation | LiveInvitationExists:
        """Issue one invitation **unless** this address already holds a live one.

        The variant issue #91's operator page calls. Same authorization
        contract as :meth:`create` (the caller has already been authorized by
        one of the two wrappers in :mod:`.authorization`), same audited
        ``invitation.send`` row when it issues — and **no row at all** when it
        does not, because a refusal is not an action.

        "Live" means ``pending`` and not yet expired, evaluated against the
        database's own clock. A revoked, accepted, or lapsed invitation does
        not block: those are exactly the states an issuer retires a link into
        before issuing a fresh one.

        **The guarantee is "this call mints no second live token", not "the
        deployment holds at most one".** :meth:`create` takes no lock and
        applies no such rule, so a concurrent API issuance to the same address
        still succeeds; the advisory lock orders this method against itself.
        See the module docstring for why that is the scope for this milestone.

        See the module docstring for why this is a separate method, why the
        address comparison is exact, and why the advisory lock is load-bearing
        rather than belt-and-braces.
        """

        _refuse_org_creation_under_single_org(org_id)
        return _retrying(lambda: self._create_unless_live_once(ctx, email=email, org_id=org_id))

    def _create_unless_live_once(
        self, ctx: AuthContext, *, email: str, org_id: str | None
    ) -> IssuedInvitation | LiveInvitationExists:
        invited = validate_invited_email(email)
        invitation_id = uuid4().hex
        minted = mint_invitation_secret()
        try:
            with audited(
                self._db,
                ctx,
                AUDIT_ACTION_INVITATION_SEND,
                target_type="invitation",
                target_id=invitation_id,
                target_label=invited,
                org_id=org_id,
                detail={"creates_organization": org_id is None, "ttl_hours": INVITATION_TTL // timedelta(hours=1)},
            ) as event:
                # First statement in the transaction, before anything reads the
                # table: a concurrent issuance for the same address blocks here
                # and does its own read only after this one has committed or
                # rolled back. `_xact_` so the lock is released by the
                # transaction ending, on either path — there is no unlock call
                # to forget and none to leak a lock past a raised body.
                event.conn.execute(
                    "SELECT pg_advisory_xact_lock(%s, %s)",
                    (INVITATION_EMAIL_LOCK_CLASS, invitation_email_lock_key(invited)),
                )
                live = event.conn.execute(
                    f"""
                    SELECT {_COLUMNS} FROM collab_invitations
                    WHERE email = %s AND status = %s AND expires_at > clock_timestamp()
                    {_PAGE_ORDER}
                    LIMIT 1
                    """,
                    (invited, STATUS_PENDING),
                ).fetchone()
                if live is not None:
                    # Abandon the transaction: nothing was created, so nothing
                    # is recorded. Minting happened above and is simply
                    # discarded — the secret corresponds to no row and is
                    # unusable by construction, exactly as a retried attempt's
                    # is.
                    raise _NoAuditedChange(LiveInvitationExists(existing=_invitation(live)))
                try:
                    row = event.conn.execute(
                        f"""
                        INSERT INTO collab_invitations
                            (id, org_id, email, token_hash, created_by, expires_at)
                        VALUES (%s, %s, %s, %s, %s, now() + %s)
                        RETURNING {_COLUMNS}
                        """,
                        (invitation_id, org_id, invited, minted.token_hash, ctx.user, INVITATION_TTL),
                    ).fetchone()
                except Exception as exc:
                    if _is_foreign_key_violation(exc):
                        raise OrgNotFoundError("Organization not found") from None
                    raise
                invitation = _invitation(row)
        except _NoAuditedChange as signal:
            return signal.outcome
        return IssuedInvitation(invitation=invitation, raw_secret=minted.raw)

    # --- Revoke -------------------------------------------------------------

    def revoke(self, ctx: AuthContext, invitation_id: str, *, expect_org_id: str | None = None) -> Invitation:
        """Revoke a pending (or lapsed) invitation, recorded as ``invitation.revoke``.

        ``expect_org_id``, when given, re-asserts inside the transaction the
        organization the caller was authorized against. The column is
        immutable — enforced by a trigger in migration v3, not merely by
        convention — so this cannot differ; checking anyway pins the
        authorization decision and the mutation to the same value at the same
        moment, which is what makes the pairing readable rather than implied.

        Revoking an already-revoked invitation is a no-op success that writes
        **no** second audit row — a revoke that did not change anything is
        not an action, and recording it would make the log describe events
        that did not happen.

        Unaffected by the transaction-clock problem that governs acceptance:
        revoke compares no timestamps at all. It branches on the *stored*
        status and its ``UPDATE`` guards on ``status = 'pending'``, which is
        deliberate — a lapsed invitation is still revocable, and revoking one
        is exactly how an issuer retires a link whose expiry they do not want
        to wait out. There is no deadline here to be evaluated stale.
        """

        return _retrying(lambda: self._revoke_once(ctx, invitation_id, expect_org_id=expect_org_id))

    def _revoke_once(self, ctx: AuthContext, invitation_id: str, *, expect_org_id: str | None) -> Invitation:
        existing = self.get(invitation_id)
        if existing is None or not _in_scope(existing, expect_org_id):
            raise InvitationNotFoundError("Invitation not found")
        if existing.status == STATUS_REVOKED:
            return existing
        try:
            with audited(
                self._db,
                ctx,
                AUDIT_ACTION_INVITATION_REVOKE,
                target_type="invitation",
                target_id=existing.id,
                target_label=existing.email,
                org_id=existing.org_id,
                detail={"creates_organization": existing.org_id is None},
            ) as event:
                locked = event.conn.execute(
                    f"SELECT {_COLUMNS} FROM collab_invitations WHERE id = %s FOR UPDATE",
                    (invitation_id,),
                ).fetchone()
                if locked is None or not _in_scope(_invitation(locked), expect_org_id):
                    raise InvitationNotFoundError("Invitation not found")
                current = _invitation(locked)
                if current.status == STATUS_ACCEPTED:
                    raise InvitationAlreadyUsedError(
                        "The invitation has already been accepted and can no longer be revoked."
                    )
                if current.status == STATUS_REVOKED:
                    # Lost the race to a concurrent revoke. Idempotent, and
                    # emphatically not a second event row.
                    raise _NoAuditedChange(current)
                row = event.conn.execute(
                    f"""
                    UPDATE collab_invitations
                    SET status = 'revoked', revoked_at = now(), revoked_by = %s
                    WHERE id = %s AND status = 'pending'
                    RETURNING {_COLUMNS}
                    """,
                    (ctx.user, invitation_id),
                ).fetchone()
                if row is None:  # pragma: no cover - the FOR UPDATE lock rules this out
                    raise InvitationNotFoundError("Invitation not found")
                revoked = _invitation(row)
        except _NoAuditedChange as signal:
            return signal.outcome
        return revoked

    # --- Accept -------------------------------------------------------------

    def accept(
        self,
        *,
        user_id: str,
        display: DisplayIdentity,
        token_hash: str,
        claim_email: object,
        email_verified: object,
        service_groups: Sequence[str] = (),
    ) -> InvitationAcceptance:
        """Redeem a token: create the membership (and maybe the organization).

        ``service_groups`` are the identity-provider groups this acceptance
        *owes* the accepter (#180). They are recorded here, as ``pending``, and
        granted by the caller after this transaction commits — which is the
        only ordering that leaves no instant where somebody has accepted and
        nothing knows a grant is due. Empty means owes nothing, which is the
        default and what a deployment without membership authority passes.

        Takes the **digest**, never the raw secret. The caller hashes at the
        edge, so no frame in this service ever binds a live credential to a
        name; the digest is not one — it grants nothing and is already what
        the column stores. On the accept route the hashing call cannot raise
        (its argument is a pydantic-validated ASCII ``str``), so it does not
        appear in a traceback either; that is a property of the route's
        validation, not of the hash function being total over all inputs.

        The accepter authenticates at identity level — by definition they
        belong to no organization yet, so the membership choke point would
        refuse them before this code ran. Their authority here is not a role;
        it is holding the secret *and* controlling the invited mailbox, and
        both are checked below.

        Structure, and why it is two phases:

        1. **Pre-read, outside any transaction.** Every terminal state that
           does not require a mutation is answered here, so the common
           failures (expired link, wrong mailbox, replayed token) never open
           a transaction. This phase also decides the audit action and scope,
           which :func:`~.audit.audited` requires *before* the body runs and
           will not let the body rewrite.
        2. **The audited transaction.** Re-reads the row ``FOR UPDATE`` and
           re-runs every check against that locked row, which is the only
           authoritative evaluation. The pre-read's conclusions are advisory;
           nothing is decided by them that the locked read does not confirm.

        The scope declared up front cannot go stale, and that is a property
        of the schema rather than of timing: ``org_id`` is immutable for the
        row's whole life, so whether this acceptance creates an organization
        is fixed at issuance. That immutability is **enforced** — a trigger
        in migration v3 refuses any UPDATE that changes it, including one run
        by hand in psql — because this argument is load-bearing and a
        convention nobody can violate by accident is not the same as one
        nothing can violate. The only thing that can change between the two
        phases is ``status``, and every changed status raises.

        Concurrency, stated as the mechanisms rather than as a hope:

        - **Two accepts of one token.** They serialize on the ``FOR UPDATE``
          row lock. The loser re-reads the committed row, sees ``accepted``,
          and raises — so at most one membership, and for an org-creating
          invitation the loser's organization insert rolls back with it.
        - **One login accepting two different tokens.** They lock different
          rows and never meet, so the barrier is
          ``collab_org_members``'s primary key on ``user_id``: the second
          insert conflicts, and the whole transaction — organization insert
          included — rolls back. Exactly one membership and exactly one
          organization.
        - **Failure never consumes a token.** The invitation UPDATE is the
          last statement in the body, and any raise above it rolls back
          everything, audit row included.
        - **Stricter isolation levels.** Under REPEATABLE READ or
          SERIALIZABLE the same races abort with a serialization failure
          instead of resolving; :func:`_retrying` turns that back into the
          promised terminal state rather than a 503. See its docstring.
        """

        return _retrying(
            lambda: self._accept_once(
                user_id=user_id,
                display=display,
                token_hash=token_hash,
                claim_email=claim_email,
                email_verified=email_verified,
                service_groups=service_groups,
            )
        )

    def _accept_once(
        self,
        *,
        user_id: str,
        display: DisplayIdentity,
        token_hash: str,
        claim_email: object,
        email_verified: object,
        service_groups: Sequence[str] = (),
    ) -> InvitationAcceptance:
        with self._db.connection() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS}, clock_timestamp() AS server_now FROM collab_invitations WHERE token_hash = %s",
                (token_hash,),
            ).fetchone()
        if row is None:
            raise InvitationNotFoundError("Invitation not found")
        invitation = _invitation(row)
        replay = _evaluate_acceptance(
            invitation,
            row["server_now"],
            user_id,
            claim_email,
            email_verified,
            require_verified=self._require_verified_email,
        )
        if replay is not None:
            return replay
        # Cannot raise: the evaluation above returned, so the claim is usable
        # and matches. Re-derived rather than threaded through, so there is one
        # function that decides what a usable claim is -- and it is asked the
        # same question, with the same setting, both times.
        #
        # `matched_email`, not `verified_email`. On a relaxed deployment this
        # value is not verified by anyone, and it is *persisted* -- it becomes
        # `collab_org_members.email`. A name asserting a property the code no
        # longer establishes is the exact reading this change has to avoid,
        # since a reader who trusts it might treat the column as evidence of
        # verification. What is always true of it is that it matched the
        # invited address.
        matched_email = verified_claim_email(claim_email, email_verified, require_verified=self._require_verified_email)

        # Minted before the transaction so the audit scope can be declared;
        # discarded with the transaction if the body does not commit. Org ids
        # are opaque and server-generated, so an unused one costs nothing and
        # leaks nothing.
        new_org_id = uuid4().hex if invitation.creates_organization else None
        ctx = _accepter_context(user_id, display)
        if invitation.creates_organization:
            # An acceptance that creates an organization is recorded as
            # `org.create` with the accepter as actor, per the ratified
            # decision — the creation is the consequential thing here, and
            # `detail.invitation_id` keeps the token that caused it on the
            # same row. `target_label` is left unset: the target is an
            # organization whose only name is the neutral placeholder, and
            # labelling it with a person would be the derived-from-login name
            # the Gate B revision removed, reappearing in the audit log.
            action, target_type, target_id, target_label = (
                AUDIT_ACTION_ORG_CREATE,
                "org",
                new_org_id,
                None,
            )
            scope_org_id = new_org_id
        else:
            action, target_type, target_id, target_label = (
                AUDIT_ACTION_INVITATION_REDEEM,
                "invitation",
                invitation.id,
                invitation.email,
            )
            scope_org_id = invitation.org_id

        try:
            with audited(
                self._db,
                ctx,
                action,
                target_type=target_type,
                target_id=target_id,
                target_label=target_label,
                org_id=scope_org_id,
                detail={
                    "invitation_id": invitation.id,
                    "role": invitation.granted_role,
                    "org_created": invitation.creates_organization,
                    # Whether this acceptance required a verified address (#190).
                    # The docs describe the relaxed trade honestly -- "usable by
                    # anyone who obtains the link" -- but without this there is
                    # no way to answer "which memberships were granted without a
                    # verified address" during an incident, and the membership
                    # row cannot say: it stores the matched address either way.
                    # One boolean makes the documented trade reviewable after
                    # the fact.
                    "verified_email_required": self._require_verified_email,
                },
            ) as event:
                acceptance = self._redeem(
                    event.conn,
                    token_hash=token_hash,
                    user_id=user_id,
                    display=display,
                    matched_email=matched_email,
                    expect_invitation_id=invitation.id,
                    new_org_id=new_org_id,
                    service_groups=service_groups,
                )
        except _NoAuditedChange as signal:
            return signal.outcome
        return acceptance

    def _redeem(
        self,
        conn,
        *,
        token_hash: str,
        user_id: str,
        display: DisplayIdentity,
        matched_email: str,
        expect_invitation_id: str,
        new_org_id: str | None,
        service_groups: Sequence[str] = (),
    ) -> InvitationAcceptance:
        """The authoritative half of :meth:`accept`, on the guarded connection.

        ``conn`` is ``audited()``'s guarded handle and is the **only**
        database handle this method may use: a checkout from the pool here
        would commit on its own and take the membership out from under the
        audit row.
        """

        locked = conn.execute(
            f"SELECT {_COLUMNS}, clock_timestamp() AS server_now FROM collab_invitations "
            f"WHERE token_hash = %s FOR UPDATE",
            (token_hash,),
        ).fetchone()
        if locked is None:  # pragma: no cover - nothing deletes invitations
            raise InvitationNotFoundError("Invitation not found")
        invitation = _invitation(locked)
        if invitation.id != expect_invitation_id:  # pragma: no cover - the hash is unique
            raise InvitationNotFoundError("Invitation not found")
        # `True` and no `require_verified`, deliberately: this is the re-check
        # against the LOCKED row, and what it re-decides is the invitation's
        # state, not the identity. `matched_email` is already the string the
        # outer evaluation produced under whatever the deployment requires, so
        # asking the question again here would either be a no-op or would let a
        # setting change mid-acceptance. The address match still runs, against
        # the locked row's email.
        replay = _evaluate_acceptance(
            invitation, locked["server_now"], user_id, matched_email, True, require_verified=True
        )
        if replay is not None:
            # Raced by this same login's own duplicate submit. The right
            # answer is the same success, and no second audit row.
            raise _NoAuditedChange(replay)

        # Redundant with the ON CONFLICT below, and kept deliberately: it
        # answers the common case (an already-affiliated login clicking an
        # invitation) without speculatively inserting an organization that
        # then rolls back, and it keeps the check visible next to the other
        # acceptance rules rather than hidden in an insert's failure mode.
        # The barrier that actually holds under concurrency is the primary
        # key, because this read cannot see an uncommitted row.
        if conn.execute(
            "SELECT 1 AS present FROM collab_org_members WHERE user_id = %s",
            (user_id,),
        ).fetchone():
            raise AlreadyInOrganizationError(_ALREADY_IN_ORG_MESSAGE)

        org_id = invitation.org_id
        if org_id is None:
            # The guard runs at issuance too, but acceptance is where the
            # organization row is actually created — and an org-creating
            # invitation issued before the deployment flipped to
            # orgSource=single is still live. Raising here rolls the whole
            # transaction back: nothing is created, nothing is consumed.
            _refuse_org_creation_under_single_org(org_id)
            org_id = new_org_id
            # `name` is deliberately not supplied: the column's schema default
            # is the neutral placeholder, and a name derived from the
            # accepter's login was ratified OUT by the dated Gate B revision
            # of 2026-08-04. Omitting the column keeps one source of truth for
            # the placeholder and makes "derived from the accepter" a thing
            # this code cannot do by accident.
            conn.execute(
                "INSERT INTO collab_orgs (id, created_by) VALUES (%s, %s)",
                (org_id, user_id),
            )

        # ON CONFLICT rather than catching the violation: a raised
        # UniqueViolation aborts the transaction before this method can
        # decide what it means, and the answer here is a specific terminal
        # state, not a database error.
        member = conn.execute(
            """
            INSERT INTO collab_org_members (user_id, org_id, role, email, display_name)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (user_id) DO NOTHING
            RETURNING user_id
            """,
            (user_id, org_id, invitation.granted_role, matched_email, display.name),
        ).fetchone()
        if member is None:
            # Lost the cross-token race on the membership primary key. The
            # whole transaction rolls back — including the organization
            # inserted moments ago, which is what makes "exactly one
            # organization" true rather than "usually one".
            raise AlreadyInOrganizationError(_ALREADY_IN_ORG_MESSAGE)

        # The consume, guarded on **both** the state and the clock.
        #
        # `AND status = 'pending'` is the second barrier behind the row lock:
        # if the lock were ever removed, this is what would still keep a token
        # single-use.
        #
        # `AND expires_at > clock_timestamp()` is the barrier that matters for
        # expiry, and it has to be here rather than only in the read above.
        # Every check before this point was made at some earlier instant and
        # then *trusted* across however long the lock and the membership
        # insert took to acquire; only a predicate evaluated inside the
        # statement that performs the transition is atomic with it.
        # `clock_timestamp()` and not `now()`, because `now()` is frozen at
        # transaction start — and `audited()` opens the transaction before any
        # of this runs, so `now()` here is the time the *request* began, not
        # the time the row is being consumed.
        consumed = conn.execute(
            """
            UPDATE collab_invitations
            SET status = 'accepted', accepted_at = now(), accepted_by = %s, accepted_org_id = %s
            WHERE id = %s AND status = 'pending' AND expires_at > clock_timestamp()
            RETURNING id
            """,
            (user_id, org_id, invitation.id),
        ).fetchone()
        if consumed is None:
            # The row is locked, so its stored status cannot have moved under
            # us: the only predicate that can have flipped since the read is
            # the clock. Re-read it rather than guessing, so the caller is
            # told what is actually true.
            raise _terminal_state_of(conn, invitation.id)
        # What this acceptance owes, recorded on the same connection and
        # therefore in the same transaction (#180). Placed after the consume so
        # it is written only for an acceptance that actually took the token: a
        # replay raises above and never reaches here, which is what keeps a
        # reloaded acceptance page from re-owing anything.
        claim_pending(
            conn,
            user_id=user_id,
            invitation_id=invitation.id,
            group_paths=service_groups,
        )
        return InvitationAcceptance(
            invitation_id=invitation.id,
            org_id=org_id,
            role=invitation.granted_role,
            org_created=invitation.creates_organization,
        )

    # --- Service access granted by an acceptance (#180) ---------------------

    def record_service_access_grant(
        self,
        user_id: str,
        display: DisplayIdentity,
        *,
        invitation_id: str,
        org_id: str | None,
        group_path: str,
        granted: bool,
    ) -> None:
        """Record one attempt to grant a service group, in either outcome.

        **Why a row and not only a log line.** A log line is found only by
        somebody who already suspects; a row is found by a query. This is the
        *history* half of #180 — what was attempted and when — and it sits
        beside the state half in ``collab_service_access_grants``, which is what
        makes a failure reconcilable. Two records because they answer different
        questions and fail differently: this one is best-effort, and the state
        row is not.

        **Both outcomes, on purpose.** Recording only failures would make the
        table say nothing about the grants that worked, and the trail is read to
        answer "what happened to this person" as often as "what went wrong".
        Reconciliation does not read this row at all — that is
        :meth:`outstanding_service_access_grants`, over stored state — so
        losing this write loses a line of history rather than the fact that a
        grant is owed.

        **This is not the audited-mutation pattern, and says so.** Every other
        writer in this class runs its mutation *inside* the audited
        transaction, because the row and the change must land together. Here
        the change is an HTTP call to the identity provider that has already
        happened and cannot be rolled back, so the body writes nothing: the
        row records an outcome rather than guarding one. Using :func:`audited`
        anyway is deliberate — it is the only writer of this table, and a
        second insert path would be exactly the drift its "one writer" rule
        exists to prevent.

        ``target_type`` is ``user`` and ``target_id`` the accepter's ``sub``:
        the grant's target is the person, and the group is in ``detail``.

        **The accepter's address is on this row**, in ``actor_label``, because
        :func:`audited` snapshots ``ctx.display.email`` for every row it writes
        — the accepter is the actor here, and a row read months later showing
        only a UUID is the thing that column exists to prevent. Not suppressed
        for this action: it would make the grant row the one row in the table
        a person cannot read, and the table's contract already puts it in scope
        for any future deletion-request handling. What is kept off the row is a
        *second* copy — ``target_label`` is unset, and ``detail`` carries the
        invitation id rather than the address.
        """

        ctx = _accepter_context(user_id, display)
        with audited(
            self._db,
            ctx,
            AUDIT_ACTION_SERVICE_ACCESS_GRANT,
            target_type="user",
            target_id=user_id,
            target_label=None,
            org_id=org_id,
            detail={
                "invitation_id": invitation_id,
                "group_path": group_path,
                "outcome": SERVICE_ACCESS_GRANTED if granted else SERVICE_ACCESS_FAILED,
            },
        ):
            # Nothing. See the docstring: the recorded change happened at the
            # identity provider, and there is no database mutation to bind the
            # row to. `audited` still owns the transaction, so the row commits
            # exactly once or not at all.
            pass

    def settle_service_access_grant(self, *, user_id: str, group_path: str, granted: bool) -> None:
        """Move the durable row for one pair to its outcome.

        The counterpart to the ``pending`` row :meth:`accept` wrote inside its
        own transaction. Called after the identity-provider attempt, so it is
        the *second* write of a two-step lifecycle rather than the only record
        of it — which is what makes losing this write survivable: the row stays
        ``pending`` and is retried, instead of the attempt vanishing.
        """

        ServiceAccessStateStore(self._db).settle(user_id=user_id, group_path=group_path, granted=granted)

    def outstanding_service_access_grants(self) -> list[OutstandingGrant]:
        """Grants this deployment owes and has not delivered.

        Defined once here so the runbook, a future sweep, and #176's operator
        surface cannot each invent their own version of "outstanding".

        A plain read of stored state, which is the whole point of the state
        table existing alongside the audit log. The first version of this asked
        the audit log instead — "the latest ``service_access.grant`` row for
        this pair says failed" — and that had two defects the state row does
        not: it could not see an attempt whose audit write never happened, and
        it ordered history by a ``bigserial`` that is allocated at ``INSERT``
        rather than at commit.

        ``pending`` and ``failed`` are both returned. They differ in what is
        known — nobody saw an answer, versus the provider said no — and a
        reader wants that, while a reconciler treats them the same.
        """

        return ServiceAccessStateStore(self._db).outstanding()


InvitationService = PostgresInvitationService | UnavailableInvitationService
"""Either backend, as one annotation. Callers never branch on which they hold:
the unavailable one raises :class:`InvitationsUnavailableError` from every
method, which the router maps to a 503."""


_ALREADY_IN_ORG_MESSAGE = (
    "This login already belongs to an organization. Sign in with a different account to accept "
    "this invitation, or ask an owner of your organization for access."
)


def _accepter_context(user_id: str, display: DisplayIdentity) -> AuthContext:
    """The accepter's own context, for the audit row's actor columns.

    Built here rather than taken from the auth choke point because the choke
    point cannot produce it: acceptance runs for a login with no membership
    and no platform role, which membership-mode resolution answers with
    ``no_organization``. ``home_org_id=None`` is not a placeholder — at the
    moment this row is written the accepter genuinely has no organization;
    the one this transaction creates or joins is the audit row's ``org_id``,
    which :func:`~.audit.audited` takes separately and the body cannot
    rewrite. Nothing here is an authorization input: ``audited()`` reads only
    ``user`` and the display label.
    """

    return AuthContext(
        user=user_id,
        home_org_id=None,
        workspace_id=WORKSPACE_DEFAULT,
        display=display,
    )


def _evaluate_acceptance(
    invitation: Invitation,
    now: datetime,
    user_id: str,
    claim_email: object,
    email_verified: object,
    *,
    require_verified: bool,
) -> InvitationAcceptance | None:
    """Run the accept-time checks; return a replay outcome or ``None``.

    ``require_verified`` has **no default**, deliberately. It is an internal
    hop, and a default here is the hazard rather than the safety property: a
    caller that dropped the keyword would get working code that quietly ignored
    the deployment's choice, which is exactly the defect review found in the
    first version of this change (deleting both threads left the suite green).
    Required, it is a ``TypeError`` at the first call instead.

    Raises the terminal state for every failure. The order is fixed and
    load-bearing: the token's own state is decided before anything about the
    caller, so a revoked, expired, or already-used link answers the same way
    to everyone holding it — including to the person it was addressed to,
    who should be told the link is dead rather than that their email is
    wrong. Only a link that is otherwise usable reaches the two claim checks.

    The membership check is **not** here: it needs the database, and it must
    run inside the acceptance transaction where its answer cannot go stale.
    """

    status = effective_status(invitation, now)
    if status == STATUS_REVOKED:
        raise InvitationRevokedError("This invitation was revoked.")
    if status == STATUS_ACCEPTED:
        if invitation.accepted_by == user_id and invitation.accepted_org_id:
            # Replay short-circuits before the claim checks on purpose: this
            # login already holds the membership this token granted, so the
            # checks have nothing left to protect, and failing a page reload
            # because an IdP flipped a claim afterwards would strand someone
            # who is already a member.
            return InvitationAcceptance(
                invitation_id=invitation.id,
                org_id=invitation.accepted_org_id,
                role=invitation.granted_role,
                org_created=invitation.creates_organization,
                replay=True,
            )
        raise InvitationAlreadyUsedError("This invitation has already been used.")
    if status == STATUS_EXPIRED:
        raise InvitationExpiredError("This invitation has expired. Ask for a new one.")
    # The match is NOT conditional on `require_verified`, and it is worth being
    # exact about why, because the reason differs by mode.
    #
    # Strict: the match is an access control. The accepter has proved control of
    # a verified address, and this is what ties that proof to *this* invitation.
    #
    # Relaxed: the claim is self-asserted, so the match is not a barrier -- a
    # link-holder can type the invited address. What it still buys is that the
    # address recorded on the membership row is the invited one, which is worth
    # keeping unconditional: an invitation that recorded any address would make
    # the member list unreliable as well as the access. What it does not buy is
    # protection against whoever holds the link.
    matched_email = verified_claim_email(claim_email, email_verified, require_verified=require_verified)
    if not emails_match(invitation.email, matched_email):
        raise InvitationEmailMismatchError(
            "This invitation was sent to a different email address. Sign in with the account "
            "for the address it was sent to."
        )
    return None


def _terminal_state_of(conn, invitation_id: str) -> InvitationError:
    """Why the guarded consume matched nothing, read from the locked row.

    Reached only when the ``UPDATE`` that consumes an invitation affected no
    rows. The row is held under ``FOR UPDATE``, so its stored status cannot
    have moved; in practice that leaves the clock, which advanced past
    ``expires_at`` while this transaction waited for the lock or for a
    conflicting membership insert. Re-reading costs one indexed lookup inside
    a transaction that is about to roll back anyway, and buys the invitee an
    accurate answer — "this expired" rather than a guess.
    """

    row = conn.execute(
        f"SELECT {_COLUMNS}, clock_timestamp() AS server_now FROM collab_invitations WHERE id = %s",
        (invitation_id,),
    ).fetchone()
    if row is None:  # pragma: no cover - nothing deletes invitations
        return InvitationNotFoundError("Invitation not found")
    status = effective_status(_invitation(row), row["server_now"])
    if status == STATUS_EXPIRED:
        return InvitationExpiredError("This invitation has expired. Ask for a new one.")
    if status == STATUS_REVOKED:  # pragma: no cover - the row is locked above
        return InvitationRevokedError("This invitation was revoked.")
    return InvitationAlreadyUsedError("This invitation has already been used.")


def _in_scope(invitation: Invitation, expect_org_id: str | None) -> bool:
    """Whether an invitation is inside the organization scope the caller holds.

    ``None`` means hub scope (an operator), which sees everything. An
    organization scope sees that organization's invitations and never the
    org-creating ones, which belong to no organization.
    """

    return expect_org_id is None or invitation.org_id == expect_org_id


def _is_foreign_key_violation(exc: Exception) -> bool:
    import psycopg

    return isinstance(exc, psycopg.errors.ForeignKeyViolation)


def _invitation(row) -> Invitation:
    return Invitation(
        id=row["id"],
        org_id=row["org_id"],
        email=row["email"],
        status=row["status"],
        created_at=row["created_at"],
        created_by=row["created_by"],
        expires_at=row["expires_at"],
        accepted_at=row["accepted_at"],
        accepted_by=row["accepted_by"],
        accepted_org_id=row["accepted_org_id"],
        revoked_at=row["revoked_at"],
        revoked_by=row["revoked_by"],
    )
