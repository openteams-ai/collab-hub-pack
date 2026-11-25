"""The invitation HTTP surface: issue, list, revoke, and body-based accept.

Issue #89. Seven routes, in three groups, and the grouping *is* the
authorization design rather than a URL-tidiness preference:

- ``/v1/operator/invitations…`` — guarded by
  :func:`~..frames.authorization.requires_platform_role`. An operator may
  invite into any organization or into none at all.
- ``/v1/orgs/{org_id}/invitations…`` — guarded by
  :func:`~..frames.authorization.requires_org_role` with ``org_arg="org_id"``,
  so the organization is a path parameter the guard pins to the caller's own.
  An owner naming somebody else's organization is refused by the wrapper
  before the handler exists, and there is no request shape in which an owner
  can issue an org-creating invitation: that surface has no "no organization"
  spelling at all.
- ``/v1/invitations/accept`` — no role guard, because the caller has no role
  yet. Authority is holding the one-time secret *and* controlling the
  verified mailbox it was sent to, both checked inside the acceptance
  transaction.

The two management groups call the *same* service methods with the same
arguments, so an operator-issued and an owner-issued ``invitation.send`` row
differ only in ``actor``/``actor_label``. Neither this module nor the service
writes an audit row or makes an authorization decision of its own — #87 owns
both, and this issue composes them.

**Decorator order is load-bearing.** Every guard sits *below* its
``@router.…`` decorator, nearest the function. Decorators apply bottom-up, so
a guard placed above the route decorator wraps the object the route already
holds and enforces nothing; ``verify_protected_routes(app)``, called from
``make_app``, turns that mistake into a failed startup instead of an open
route.

Token safety (R3)
-----------------
- The raw secret is minted in the service, returned to the create handler,
  handed to the email adapter, and dropped. It is in **no** response body:
  the desktop settings UI is built against these responses and must never be
  able to display or forward a live invitation.
- Acceptance takes the secret in a **POST body** — never a path segment,
  query parameter, or header — so no request line, access log, `Referer`, or
  proxy URL log can carry it. The access log records the matched route
  template, which is a constant.
- A 422 on the accept route has its ``details`` dropped, because pydantic
  echoes the rejected input value back and an almost-valid token in a 422
  body is a token in whatever logs response bodies. That redaction lives in
  ``core.make_app``; :func:`redact_validation_details` is the predicate.
- Nothing here logs the token, and no error message interpolates it. Errors
  name the invitation *id*, which is opaque and safe to quote.

Email delivery is sequenced **after** the audited transaction commits, per
#87's contract: an unrecoverable side effect must never run inside a block
that can roll back, because a rollback cannot un-send a message. The cost is
the other direction — a committed invitation whose delivery failed — which is
recoverable and is reported in the create response instead of hidden.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, field_serializer

from ..dependencies import (
    get_granted_service_groups,
    get_invitation_email_delivery,
    get_invitation_service,
    get_service_access_granter,
)
from ..frames import error_codes
from ..frames.account_provisioning import ServiceAccessError, ServiceAccessGranter
from ..frames.auth import (
    AuthContext,
    CallerIdentity,
    DisplayIdentity,
    get_auth_context,
    get_caller_identity,
)
from ..frames.authorization import requires_org_role, requires_platform_role
from ..frames.credentials import REDACTED, InvitationSecret
from ..frames.invitation_email import InvitationEmailDelivery
from ..frames.invitations import (
    AlreadyInOrganizationError,
    EmailNotVerifiedError,
    Invitation,
    InvitationAcceptance,
    InvitationAlreadyUsedError,
    InvitationEmailMismatchError,
    InvitationExpiredError,
    InvitationNotFoundError,
    InvitationRevokedError,
    InvitationService,
    InvitationsUnavailableError,
    OrganizationCreationRefusedError,
    OrgNotFoundError,
    effective_status,
    hash_invitation_secret,
    validate_invited_email,
)
from ..frames.orgs import PLATFORM_ROLE_OPERATOR, ROLE_OWNER

logger = logging.getLogger("frames_server.invitations")

router = APIRouter(tags=["invitations"])

AuthDep = Annotated[AuthContext, Depends(get_auth_context)]
IdentityDep = Annotated[CallerIdentity, Depends(get_caller_identity)]
ServiceDep = Annotated[InvitationService, Depends(get_invitation_service)]
DeliveryDep = Annotated[InvitationEmailDelivery, Depends(get_invitation_email_delivery)]

ACCEPT_PATH = "/v1/invitations/accept"
"""The one route that authenticates at identity level rather than through the
membership choke point. Named here and consumed by ``core.make_app``, which
must apply the same rule in the path-protection middleware — that middleware
authenticates *before* routing, so a hardened deployment would otherwise
refuse every invitee with ``no_organization`` before this router was reached.
"""

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
"""Bounds on a listing page.

Unbounded listings were a real defect rather than a style point: both list
endpoints returned every matching invitation — email addresses included — in
one response, so response size grew without limit with the number of
invitations an organization had ever issued, and an operator's view grew with
the whole deployment's. The maximum is what makes the endpoint's cost
predictable; the default is what makes a caller who passes nothing safe.
"""

PageLimit = Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)]
PageOffset = Annotated[int, Query(ge=0)]

ACCEPT_TOKEN_MAX_LENGTH = 512
"""Bound on the accept body's token, generously above the ~43-character
minted secret. Oversized junk is refused by validation (with its details
redacted) instead of being hashed and looked up."""


def identity_only_path(path: str) -> bool:
    """Whether *path* authenticates at identity level, not membership level."""

    return path == ACCEPT_PATH


TOKEN_BEARING_PATH_SUFFIXES = ("/invitations/accept", "/invite/accept/redeem")
"""Every path whose request body can contain a live invitation secret.

Two, and they are two different surfaces of the same redemption: the API
route above, and the browser acceptance page's own POST endpoint (#90). The
browser endpoint parses its body by hand precisely so pydantic's validation
machinery never sees the token — but it is listed here anyway, because the
cost is one tuple entry and the failure mode of forgetting is an
almost-correct one-time secret echoed in a 422 body.
"""


def redact_validation_details(path: str) -> bool:
    """Whether a 422 on *path* must drop its details (R3).

    Suffix match, so a deployment served under a root path — where the
    middleware's stripped path and FastAPI's raw ``request.url.path`` differ —
    cannot end up echoing a near-miss token because the prefix did not match.
    """

    return path.endswith(TOKEN_BEARING_PATH_SUFFIXES)


# --- Wire models -------------------------------------------------------------


class InvitationCreateRequest(BaseModel):
    """Create body for the owner surface: just the address.

    The organization is never in the body. On the owner surface it is the
    path parameter the authorization guard is pinned to; letting a body field
    name it too would create a second, unguarded spelling of the target.
    """

    email: str


class OperatorInvitationCreateRequest(InvitationCreateRequest):
    """Create body for the operator surface: an address, and optionally an org.

    ``org_id`` omitted or ``null`` means an **org-creating** invitation: the
    accepter becomes the owner of a brand-new organization under a neutral
    placeholder name. That is the bootstrap invitation, and it is
    operator-only by construction — the owner surface has no field for it.
    """

    org_id: str | None = None


class InvitationResource(BaseModel):
    """One invitation as presented on the management surface.

    Deliberately absent: the token in any form, and the ``created_by`` /
    ``accepted_by`` / ``revoked_by`` subjects. ``status`` is always the
    *derived* status, so a lapsed pending row presents as ``expired``
    everywhere without a sweeper having ever touched it.
    """

    id: str
    org_id: str | None
    email: str
    status: str
    created_at: datetime
    expires_at: datetime


class InvitationCreateResponse(InvitationResource):
    """A created invitation, plus what became of its email.

    ``delivery_status`` is the adapter's sanitized outcome
    (``provider_accepted`` / ``failed`` / ``unknown``). It is reported rather
    than swallowed because the invitation is already committed by the time
    delivery runs — the issuer needs to know whether anyone will ever receive
    the link, and the only remedy is to revoke and reissue.
    """

    delivery_status: str
    delivery_error_code: str | None = None


class InvitationListResponse(BaseModel):
    """One bounded page of invitations.

    ``has_more`` rather than a total: a count would come from a different
    snapshot than the page and could contradict it, and the only question a
    pager needs answered is whether to ask again.
    """

    invitations: list[InvitationResource]
    limit: int
    offset: int
    has_more: bool


def _as_secret(value: object) -> InvitationSecret:
    """Box a token value, whatever construction path it arrived by.

    Used by every validation-skipping constructor below. Idempotent, so a
    caller who already holds a wrapper is not double-boxed.
    """

    return value if isinstance(value, InvitationSecret) else InvitationSecret(value)


def _boxed_update(update):
    """A copy of an ``update`` mapping with the token boxed, if it carries one.

    Shared by :meth:`InvitationAcceptRequest.model_copy` and its deprecated
    ``copy`` twin, because the two reach ``__dict__`` by different internal
    routes and fixing only the one you happen to think of is how the
    deprecated path stayed open through a whole review round.
    """

    if update and "token" in update:
        return {**update, "token": _as_secret(update["token"])}
    return update


def _unwrap_secret(value: object) -> object:
    """Undo the boxing so validation can run again on an already-boxed value.

    Needed because the model revalidates instances (see
    :class:`InvitationAcceptRequest`): re-validating a model whose ``token``
    is already an :class:`~..frames.credentials.InvitationSecret` would
    otherwise fail the ``str`` schema, turning a safety setting into a broken
    ``model_validate``. Unwrapping first makes the whole chain idempotent —
    ``str`` in, ``str`` in, wrapper out, every time.
    """

    return value.reveal() if isinstance(value, InvitationSecret) else value


AcceptToken = Annotated[
    str,
    Field(
        min_length=1,
        max_length=ACCEPT_TOKEN_MAX_LENGTH,
        repr=False,
        # Minted secrets are token_urlsafe output. Anything outside that
        # alphabet cannot match, so it is refused as validation (with redacted
        # details) rather than hashed and looked up. This is also what
        # narrows the accept path's input domain to plain ASCII `str`.
        pattern=r"^[A-Za-z0-9_-]+$",
    ),
    BeforeValidator(_unwrap_secret),
    AfterValidator(InvitationSecret),
]
"""The accept body's token: validated as a string, held as a credential.

The ordering inside ``Annotated`` is load-bearing, in both directions. The
constraints bind to the ``str`` schema, so length and alphabet are enforced on
the value as it arrives off the wire, and only then does the after-validator
box it. Declaring the field as
:class:`~..frames.credentials.InvitationSecret` directly does **not** work —
pydantic tries to apply ``min_length`` to the wrapper and raises — and that
failure is worth recording, because the tempting fix is to drop the
constraints rather than to reorder. The before-validator sits between them so
that a value which is *already* boxed is unwrapped before the constraints see
it, which is what makes revalidation idempotent.

The consequence for introspection: ``model_fields["token"].annotation`` is
``str``, so a static "does this field hold the wrapper?" check cannot see it.
The tests therefore assert this one on the validated instance instead.
"""


class InvitationAcceptRequest(BaseModel):
    """Accept body: the raw one-time secret, and nothing else.

    This is the one model in the system that holds a **live** credential, and
    what protects it is the *type of the field*, not a list of patched exits.
    ``token`` is an :class:`~..frames.credentials.InvitationSecret`, so
    anything that walks the model generically — ``vars()``, ``__dict__``,
    ``json.dumps(..., default=vars)``, a ``repr``, FastAPI's response
    encoder, a traceback that captures locals — finds an object that redacts
    itself and refuses to be copied, pickled, or mutated.

    Model-level protections sit on top, because pydantic reads and writes
    fields through its own machinery rather than through the object:

    - the field serializer covers ``model_dump`` and ``model_dump_json``,
      which ignore ``repr`` settings entirely, and dumping a request model is
      the single most routine thing anyone does when adding request logging;
    - ``__getstate__`` refuses pickling of the model itself, so the failure
      names the model instead of surfacing from somewhere inside pydantic;
    - ``frozen=True`` closes assignment, which does not validate by default —
      ``payload.token = raw`` would otherwise put a bare string back in;
    - :meth:`model_construct` and :meth:`model_copy` box explicitly, because
      **both skip validation by design** and would otherwise leave a raw
      ``str`` in the model's ``__dict__``, where a locals-capturing traceback
      would print it. ``model_construct`` is not exotic — it is exactly what
      someone reaches for to skip validation on a hot path — so the field's
      after-validator cannot be the only thing that boxes.

    The value stays usable in-process as ``payload.token.reveal()``, which is
    the only *supported* way to read it. Stated at the same width as
    :class:`~..frames.credentials.InvitationSecret`'s own guarantee, and no
    wider: what is closed is **accidental** escape through machinery that
    walks the model generically. Code that deliberately reaches past the
    model — ``object.__setattr__``, a direct ``__dict__`` write — is not
    defended against and does not need to be, since it could call
    ``.reveal()`` instead.

    What is *proven*, rather than asserted universally: every public entry
    point on ``BaseModel`` that builds or mutates an instance either boxes
    the token or refuses. The list is enumerated from the installed class and
    exercised one by one — ``__init__``, ``model_validate``,
    ``model_validate_json``, ``model_validate_strings``, ``model_construct``,
    ``model_copy``, ``__replace__``, ``__copy__``, ``__deepcopy__``,
    ``__setattr__``, ``__setstate__``, and the deprecated-but-live
    ``construct``, ``copy``, ``parse_obj``, ``parse_raw``, ``parse_file``,
    ``validate``, and ``from_orm``. A test compares that list against the
    installed ``BaseModel``, so a pydantic upgrade adding a public method
    fails rather than slipping past unaudited, and a second test pins the
    entry points that cannot honestly be reclassified as read-only, so the
    audit cannot be defined into passing.

    Covered alongside them: a **subtype** redeclaring ``token: str``, whose
    instance ``model_validate`` and ``TypeAdapter`` would otherwise return
    untouched. ``revalidate_instances="always"`` is what closes that, and it
    is a different kind of hole from the rest — not an entry point that
    forgot to box, but a value arriving already inside a model.

    Both checks exist because of what earlier audits missed. The first
    reasoned *backwards* from the ``__dict__`` write sites it could find and
    concluded the field was safe, while the deprecated ``copy(update=...)``
    reached ``__dict__`` by a third route
    (``pydantic/deprecated/copy_internals.py``, which does not go through
    :meth:`model_copy`). The second enumerated entry points forwards and was
    right about all of them, and still missed the subtype — because no entry
    point was at fault.
    """

    model_config = ConfigDict(frozen=True, revalidate_instances="always")

    token: AcceptToken

    @classmethod
    def model_construct(cls, _fields_set=None, **values):
        """Construct without validation — but never without boxing.

        Skipping validation is the documented point of this API, and callers
        who use it are entitled to that. What they are not entitled to is a
        model whose ``token`` is a bare string, because every protection on
        this class assumes the field holds the wrapper.
        """

        if "token" in values:
            values["token"] = _as_secret(values["token"])
        return super().model_construct(_fields_set, **values)

    def model_copy(self, *, update=None, deep=False):
        """Copy, boxing anything the ``update`` mapping tries to smuggle in.

        ``model_copy(update=...)`` does not validate either, so an update
        carrying a raw string would land unboxed in the copy.

        ``deep=True`` raises, because deep-copying the model deep-copies the
        wrapper and the wrapper refuses to be duplicated. That is a refusal,
        not a leak, and it is the same answer ``copy.deepcopy`` gives.
        """

        return super().model_copy(update=_boxed_update(update), deep=deep)

    def copy(self, **kwargs):
        """The deprecated ``.copy()`` — boxed, not refused.

        Deprecated is not the same as unreachable: ``.copy()`` is a documented
        public method, a ``DeprecationWarning`` does not stop it working, and
        in pydantic 2.13.4 it does **not** route through
        :meth:`model_copy`. It goes to
        ``pydantic/deprecated/copy_internals.py``, which installs the update
        mapping straight into ``__dict__`` — so an unboxed string landed in a
        model through an ordinary call, with no deliberate manipulation
        anywhere.

        Boxing rather than raising, deliberately: a caller using a deprecated
        API is doing something discouraged, not something wrong, and turning
        their working call into a runtime failure would be this class
        punishing them for a problem that is ours to solve.
        """

        if "update" in kwargs:
            kwargs["update"] = _boxed_update(kwargs["update"])
        return super().copy(**kwargs)

    def __setstate__(self, state):
        """Restore from state, boxing the field on the way in.

        Unreachable through pickle — ``__getstate__`` refuses, so no state
        for this model can be produced in the first place — and calling this
        by hand with a hand-built dict is deliberate manipulation, which is
        explicitly out of scope. It is boxed anyway because it is a
        *construction* path and costs four lines: leaving the one entry in
        the audit table that yields a bare string, purely on the argument
        that nobody should call it, invites exactly the re-litigation this
        docstring is trying to end.
        """

        values = state.get("__dict__")
        if isinstance(values, dict) and "token" in values:
            state = {**state, "__dict__": {**values, "token": _as_secret(values["token"])}}
        super().__setstate__(state)

    @field_serializer("token", when_used="always")
    def _redact_token(self, _value: InvitationSecret) -> str:
        """Emit a redaction from every dump, in both serialization modes.

        ``when_used="always"`` on purpose: a serializer registered for one
        mode leaves the other open, and ``model_dump_json`` does not route
        through ``model_dump``. Both are asserted separately by the tests.

        The redaction is not a valid token under the field's own pattern, so
        a dumped body fed back in is rejected loudly rather than being
        mistaken for a real accept request.
        """

        return REDACTED

    def __getstate__(self):
        raise TypeError(
            "InvitationAcceptRequest holds a live invitation secret and must not be serialized: "
            "pickling would write an access-granting credential into whatever queue, cache, or "
            "crash dump the object was travelling to."
        )


class InvitationAcceptResponse(BaseModel):
    """What the acceptance page renders on success."""

    org_id: str
    role: str
    org_created: bool


def _resource(invitation: Invitation, now: datetime) -> InvitationResource:
    return InvitationResource(
        id=invitation.id,
        org_id=invitation.org_id,
        email=invitation.email,
        status=effective_status(invitation, now),
        created_at=invitation.created_at,
        expires_at=invitation.expires_at,
    )


def _page_response(page, service: InvitationService, limit: int, offset: int) -> InvitationListResponse:
    """Render one page, deriving every row's status from one clock reading."""

    now = service.server_now()
    return InvitationListResponse(
        invitations=[_resource(invitation, now) for invitation in page.invitations],
        limit=limit,
        offset=offset,
        has_more=page.has_more,
    )


def _issue(
    auth: AuthContext,
    service: InvitationService,
    delivery: InvitationEmailDelivery,
    *,
    email: str,
    org_id: str | None,
) -> InvitationCreateResponse:
    """Issue one invitation and then, separately, try to email it.

    Shared verbatim by both authority axes: the caller has already been
    authorized by whichever wrapper guards the route, and everything from
    here down is identical, which is what makes the two ``invitation.send``
    rows differ only in who acted.

    The ordering is #87's contract, not a preference. ``service.create``
    opens the audited transaction, writes the invitation and its event row,
    and commits. Only then is the secret handed to the email adapter — an SES
    send is unrecoverable, and a rollback cannot un-send it. The residual
    risk runs the other way (a committed invitation whose email never went
    out), which is why the outcome is returned rather than dropped.
    """

    try:
        invited = validate_invited_email(email)
    except ValueError as exc:
        # The shared mailbox validator's messages never quote their input, so
        # returning one cannot echo a malformed address back through a log.
        # Validated here rather than caught around service.create, so the
        # audited primitive's own ValueError subclasses cannot be laundered
        # into a 422 about the email address.
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from None

    # Read before the transaction: it is only used to word the email, and a
    # non-existent organization is caught by the foreign key inside.
    organization_name = service.organization_name(org_id) if org_id is not None else None
    issued = service.create(auth, email=invited, org_id=org_id)

    invitation = issued.invitation
    outcome = delivery.deliver(
        invitation_id=invitation.id,
        recipient=invitation.email,
        invitation_secret=issued.raw_secret.reveal(),
        organization_name=organization_name,
        expires_at=invitation.expires_at,
    )
    # `created_at` stands in for "now": the row was written moments ago and
    # its expiry is a week out, so the derived status is `pending` on any
    # clock, and the alternative is a round trip to learn that.
    resource = _resource(invitation, invitation.created_at)
    return InvitationCreateResponse(
        **resource.model_dump(),
        delivery_status=outcome.status,
        delivery_error_code=outcome.error_code,
    )


# --- Operator surface: any organization, or none -----------------------------


@router.post("/operator/invitations", status_code=status.HTTP_201_CREATED)
@requires_platform_role(PLATFORM_ROLE_OPERATOR)
def operator_create_invitation(
    payload: OperatorInvitationCreateRequest,
    auth: AuthDep,
    service: ServiceDep,
    delivery: DeliveryDep,
) -> InvitationCreateResponse:
    """Issue an invitation for any organization, or for none.

    Signup grants nothing (R11): this writes an invitation row and an audit
    row and touches no membership. The organization and the membership come
    into existence only when the invitation is accepted.
    """

    return _issue(auth, service, delivery, email=payload.email, org_id=payload.org_id)


@router.get("/operator/invitations")
@requires_platform_role(PLATFORM_ROLE_OPERATOR)
def operator_list_invitations(
    auth: AuthDep,
    service: ServiceDep,
    limit: PageLimit = DEFAULT_PAGE_SIZE,
    offset: PageOffset = 0,
) -> InvitationListResponse:
    """One page of every invitation on the deployment, newest first.

    No *filter* parameters: the visible set follows from the caller's
    standing, so there is no parameter combination whose scope needs
    checking. ``limit``/``offset`` bound the page, they do not widen it.
    """

    del auth
    return _page_response(service.list_all(limit=limit, offset=offset), service, limit, offset)


@router.post("/operator/invitations/{invitation_id}/revoke")
@requires_platform_role(PLATFORM_ROLE_OPERATOR)
def operator_revoke_invitation(
    invitation_id: str,
    auth: AuthDep,
    service: ServiceDep,
) -> InvitationResource:
    """Revoke any invitation. The row is retained, never deleted."""

    return _resource(service.revoke(auth, invitation_id), service.server_now())


# --- Owner surface: exactly one organization, the caller's own ---------------


@router.post("/orgs/{org_id}/invitations", status_code=status.HTTP_201_CREATED)
@requires_org_role(ROLE_OWNER, org_arg="org_id")
def create_org_invitation(
    org_id: str,
    payload: InvitationCreateRequest,
    auth: AuthDep,
    service: ServiceDep,
    delivery: DeliveryDep,
) -> InvitationCreateResponse:
    """Issue an invitation into the caller's own organization.

    "An owner may only invite into an organization they own" is enforced by
    the guard, against the path parameter, before this function runs — not by
    a comparison in the body that a later edit could drop.
    """

    return _issue(auth, service, delivery, email=payload.email, org_id=org_id)


@router.get("/orgs/{org_id}/invitations")
@requires_org_role(ROLE_OWNER, org_arg="org_id")
def list_org_invitations(
    org_id: str,
    auth: AuthDep,
    service: ServiceDep,
    limit: PageLimit = DEFAULT_PAGE_SIZE,
    offset: PageOffset = 0,
) -> InvitationListResponse:
    """One page of the caller's organization's invitations, newest first.

    Org-creating invitations belong to no organization and appear in no
    organization's list; they are visible on the operator surface only.
    """

    del auth
    return _page_response(service.list_for_org(org_id, limit=limit, offset=offset), service, limit, offset)


@router.post("/orgs/{org_id}/invitations/{invitation_id}/revoke")
@requires_org_role(ROLE_OWNER, org_arg="org_id")
def revoke_org_invitation(
    org_id: str,
    invitation_id: str,
    auth: AuthDep,
    service: ServiceDep,
) -> InvitationResource:
    """Revoke one of the caller's organization's invitations.

    ``expect_org_id`` carries the authorized scope down into the transaction,
    so an invitation belonging to another organization — or to none — is a
    plain 404 decided against the same locked row the update runs on.
    """

    invitation = service.revoke(auth, invitation_id, expect_org_id=org_id)
    return _resource(invitation, service.server_now())


# --- Acceptance: identity only ------------------------------------------------


def redeem(
    service: InvitationService,
    payload: InvitationAcceptRequest,
    *,
    user_id: str,
    display: DisplayIdentity,
    service_access: ServiceAccessGranter | None = None,
    granted_groups: Sequence[str] = (),
    frames_store=None,
):
    """Redeem a validated accept body. **The one call both surfaces make.**

    Extracted for the browser acceptance page (#90), which authenticates with
    a web session cookie instead of an API credential and therefore cannot
    call the route above — but must not grow a second opinion about how
    redemption works. Everything that decides an outcome stays in the
    lifecycle service; this function only unboxes the secret, hashes it,
    passes the digest on, and — since #180 — grants the service groups an
    acceptance carries.

    The raw string exists in exactly one expression, as the argument to
    :func:`~..frames.invitations.hash_invitation_secret`, which cannot raise
    for a body the accept model validated (ASCII, bounded, ``token_urlsafe``
    alphabet) — so it is never bound to a local that a traceback could print.

    **The grant is sequenced after the acceptance, deliberately (#180).** It is
    not inside the audited transaction and must never be: a group write cannot
    be rolled back, and an identity-provider outage must not cost somebody a
    membership they hold a valid invitation and a verified address for. So the
    acceptance commits first and the grant is attempted against it — the same
    ordering, and the same reasoning, as invitation delivery (#87/#89).

    A failed grant therefore leaves an accepted invitation with no service
    access. That is the right trade and the wrong silence, so **the acceptance
    transaction records what it owes** before the external call happens: a
    ``pending`` row per group, committed with the acceptance itself. The
    attempt then settles that row to ``granted`` or ``failed``, and a failure
    is additionally logged at ``error`` with the invitation id.

    Writing the intent first is what makes this reconcilable rather than merely
    reported. Every point at which the process can stop — before the call,
    after it, or during the settle — leaves the row ``pending``, which
    :meth:`~..frames.invitations.PostgresInvitationService.outstanding_service_access_grants`
    returns; retrying is safe because a group add is idempotent. The invitee is
    told "you are in", which is true, and the grant they are still owed is a
    row rather than a support message.
    """

    # Decided before the transaction, because the transaction records what it
    # owes: a deployment that will not attempt a grant must not write rows
    # saying one is due. Same four conditions the grant itself skips on, in one
    # place so the two cannot disagree about what was promised.
    owed = groups_to_grant(service_access, granted_groups)
    acceptance = service.accept(
        user_id=user_id,
        display=display,
        token_hash=hash_invitation_secret(payload.token.reveal()),
        claim_email=display.email,
        email_verified=display.email_verified,
        service_groups=owed,
    )
    grant_service_access(
        service_access,
        owed,
        user_id=user_id,
        acceptance=acceptance,
        record=service,
        display=display,
    )
    if acceptance.moved_from_org_id is not None and frames_store is not None:
        # The person left a self-service organization (issue #365); its frames
        # are owed a move, recorded in the acceptance's transaction. Attempted
        # now so they are where the person is on their next request; a failure
        # is left for the periodic sweep and never fails the acceptance.
        try:
            service.complete_frame_moves(frames_store, from_org_id=acceptance.moved_from_org_id)
        except Exception:
            logger.exception("frame_move_after_acceptance_failed", extra={"invitation_id": acceptance.invitation_id})
    return acceptance


def groups_to_grant(granter: ServiceAccessGranter | None, groups: Sequence[str]) -> tuple[str, ...]:
    """The groups an acceptance will actually try to grant.

    Three of the four reasons to grant nothing are properties of the
    *deployment* rather than of the acceptance, and they have to be known
    before the acceptance transaction opens, because that transaction writes
    the durable record of what is owed. Promising a grant a deployment cannot
    attempt would put a permanent entry on the outstanding list for every
    invitee — the reconciliation list's own version of a stuck queue.

    * no groups configured — the default, and the only safe one. The behaviour
      this replaced granted at *account creation* and so reached anyone who
      self-registered (an internal issue);
    * no granter wired in at all;
    * the granter reports itself unconfigured, which is what a deployment
      without membership authority looks like.

    The fourth — a replayed acceptance — is a property of the acceptance and is
    handled where it is known, inside :func:`grant_service_access`. It needs no
    equivalent here: a replay writes nothing at all, so it cannot owe anything.
    """

    if not groups or granter is None or not getattr(granter, "configured", False):
        return ()
    return tuple(groups)


def grant_service_access(
    granter: ServiceAccessGranter | None,
    groups: Sequence[str],
    *,
    user_id: str,
    acceptance: InvitationAcceptance,
    record: InvitationService,
    display: DisplayIdentity,
) -> None:
    """Grant the configured service groups to somebody who has just accepted.

    Four reasons this does nothing, and none of them is an error:

    * no groups are configured — the default, and the only safe one. The
      behaviour this replaced granted at *account creation* and so reached
      anyone who self-registered (an internal issue);
    * no granter is wired in at all;
    * the granter reports itself unconfigured, which is what a deployment
      without membership authority looks like;
    * the acceptance was a **replay** — a reloaded acceptance page. Nothing was
      created, so nothing is granted, and re-granting would turn a page refresh
      into repeated writes against the identity provider.

    Note what is *not* checked here: whether the accepter deserves it. That was
    decided by the acceptance itself, which required the one-time secret and an
    identity-provider-verified address matching the invited one. This function
    is reached only for somebody who cleared both — and it is the **only**
    caller of ``granter.grant``, which is the structural reason a
    self-registration that never redeemed a token can never be granted
    anything (an internal issue, the behaviour this
    replaced).

    None of the four skip paths writes an audit row. Nothing was attempted, and
    a row per non-event would bury the attempts that matter in noise
    proportional to page reloads.
    """

    if not groups or granter is None or not getattr(granter, "configured", False):
        return
    if acceptance.replay:
        return
    for group_path in groups:
        granted = True
        try:
            granter.grant(user_id=user_id, group_path=group_path)
        except ServiceAccessError:
            # Deliberately not re-raised: the acceptance has committed and is
            # correct. Logged with the invitation id — never the address, which
            # belongs in the audit row and not in a log line.
            granted = False
            logger.error(
                "service_access_grant_failed",
                extra={
                    "invitation_id": acceptance.invitation_id,
                    "group_path": group_path,
                    "org_id": acceptance.org_id,
                },
            )
        _record_grant_attempt(
            record,
            display,
            user_id=user_id,
            acceptance=acceptance,
            group_path=group_path,
            granted=granted,
        )


def _record_grant_attempt(
    record: InvitationService,
    display: DisplayIdentity,
    *,
    user_id: str,
    acceptance: InvitationAcceptance,
    group_path: str,
    granted: bool,
) -> None:
    """Settle the durable row for one attempt, and write its audit trail.

    Two writes with two different jobs, and the order matters. The **state**
    row — written ``pending`` inside the acceptance transaction — is settled
    first, because it is what a reconciler reads; the **audit** row is the
    history of the attempt, which a person reads. Neither is allowed to change
    the outcome of an acceptance that has already committed.

    **Losing either write is survivable, and that is the design rather than a
    hope.** A settle that fails leaves the row ``pending``, which is
    indistinguishable to a reconciler from a process that died before getting
    here — both are retried, and adding somebody to a group is idempotent. So
    the failure mode of this function is a redundant future attempt, not a
    person who silently has no access. That is exactly what the earlier
    audit-only version could not promise: there, a lost write lost the only
    record that anything had been attempted.

    ``BaseException`` is not caught: a cancellation or a shutdown signal
    arriving mid-write is not this function's to absorb — and does not need to
    be, because the row it would interrupt is already ``pending``.

    ``record`` and ``display`` are required rather than defaulted, so a future
    caller of :func:`grant_service_access` cannot drop the trail by omission.
    """

    try:
        record.settle_service_access_grant(user_id=user_id, group_path=group_path, granted=granted)
    except Exception:
        # Deliberately not re-raised, and deliberately not fatal to the trail
        # below: the row stays `pending` and will be retried, which is a
        # weaker claim than "settled" and a much stronger one than "lost".
        logger.exception(
            "service_access_grant_unsettled",
            extra={
                "invitation_id": acceptance.invitation_id,
                "group_path": group_path,
                "granted": granted,
            },
        )

    try:
        record.record_service_access_grant(
            user_id,
            display,
            invitation_id=acceptance.invitation_id,
            org_id=acceptance.org_id,
            group_path=group_path,
            granted=granted,
        )
    except Exception:
        logger.exception(
            "service_access_grant_unrecorded",
            extra={
                "invitation_id": acceptance.invitation_id,
                "group_path": group_path,
                "granted": granted,
            },
        )


@router.post("/invitations/accept")
def accept_invitation(
    request: Request,
    payload: InvitationAcceptRequest,
    identity: IdentityDep,
    service: ServiceDep,
    service_access: ServiceAccessGranter = Depends(get_service_access_granter),
    granted_groups: Sequence[str] = Depends(get_granted_service_groups),
) -> InvitationAcceptResponse:
    """Redeem an invitation token.

    No authorization decorator, and that is the point rather than an
    omission: the accepter holds no role in any organization — that is the
    state the invitation exists to end. What stands in for a role is checked
    inside the transaction: the secret must resolve to a live invitation, and
    their ``email`` claim must equal the invited address but for ASCII case.
    On a deployment with ``frames.invitations.require_verified_email`` on --
    the default -- the caller's ``email_verified`` claim must also be boolean
    true; where it is off, the token itself is the proof of mailbox control and
    those two are the only checks. This docstring is what a reader consults to
    learn what stands in for a role here, so it states both shapes rather than
    the stricter one.

    The secret arrives in the body and is hashed **here**, at the edge, so
    that the only name bound to it anywhere is ``payload`` — which refuses to
    render or serialize it — and the only frame that ever holds the raw
    string is :func:`~..frames.invitations.hash_invitation_secret`. By the
    time that call is made, pydantic has validated ``token`` as a plain
    ``str`` matching an ASCII pattern, which is the domain the hash is
    defined over, so it cannot raise here and therefore cannot appear in a
    traceback. The lifecycle service is handed the digest, which grants
    nothing.
    """

    acceptance = redeem(
        service,
        payload,
        user_id=identity.user,
        display=identity.display,
        service_access=service_access,
        granted_groups=granted_groups,
        frames_store=getattr(request.app.state, "frames_store", None),
    )
    return InvitationAcceptResponse(
        org_id=acceptance.org_id,
        role=acceptance.role,
        org_created=acceptance.org_created,
    )


# --- Terminal states → wire codes --------------------------------------------


def register_exception_handlers(app) -> None:
    """Map each invitation terminal state to its registered code and status.

    Registered unconditionally; inert on a deployment where the router is not
    mounted. Every message these carry is a fixed string written for a
    person — none interpolates caller input, and in particular none can
    contain the token.
    """

    from .frames import error_response

    def handler(status_code: int, code: str):
        async def handle(_request: Request, exc: Exception):
            return error_response(status_code, code, str(exc))

        return handle

    for exception, status_code, code in (
        (InvitationNotFoundError, status.HTTP_404_NOT_FOUND, error_codes.INVITATION_NOT_FOUND),
        (OrgNotFoundError, status.HTTP_404_NOT_FOUND, error_codes.NOT_FOUND),
        (InvitationExpiredError, status.HTTP_410_GONE, error_codes.INVITATION_EXPIRED),
        (InvitationRevokedError, status.HTTP_410_GONE, error_codes.INVITATION_REVOKED),
        (InvitationAlreadyUsedError, status.HTTP_410_GONE, error_codes.INVITATION_ALREADY_USED),
        (EmailNotVerifiedError, status.HTTP_403_FORBIDDEN, error_codes.EMAIL_NOT_VERIFIED),
        (InvitationEmailMismatchError, status.HTTP_403_FORBIDDEN, error_codes.INVITATION_EMAIL_MISMATCH),
        (AlreadyInOrganizationError, status.HTTP_409_CONFLICT, error_codes.ALREADY_IN_ORGANIZATION),
        (
            OrganizationCreationRefusedError,
            status.HTTP_409_CONFLICT,
            error_codes.ORGANIZATION_CREATION_REFUSED,
        ),
        (
            InvitationsUnavailableError,
            status.HTTP_503_SERVICE_UNAVAILABLE,
            error_codes.INVITATIONS_UNAVAILABLE,
        ),
    ):
        app.add_exception_handler(exception, handler(status_code, code))
