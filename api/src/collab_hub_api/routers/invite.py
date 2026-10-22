"""The browser invitation-acceptance routes (issue #90).

Two routes, and the split between them is the security design rather than URL
tidiness:

``GET /invite/accept``
    The page. **Anonymous by design** — its audience is people with no
    account on this deployment yet — and therefore listed in
    :data:`~..web.surface.PUBLIC_WEB_PATHS`, which is the surface's one
    reviewed way to be public. It is safe to serve anonymously because it is
    a static document: it reads nothing from the request, holds no invitation
    state, and the one-time secret the whole flow is about does not reach the
    server on this request at all — it is in the URL fragment, which browsers
    never transmit.

``POST /invite/accept/redeem``
    The act. Requires a web session (the guard enforces it by path, and
    :func:`~..web.authz.require_csrf` again in-route), takes the secret in a
    **JSON body**, and answers a fixed outcome word the page turns into copy.

Freshness of the verified address
---------------------------------
Holding a session is not enough to redeem. The session's ``email`` and
``email_verified`` are one half of #89's authority pair, and unlike the
subject they are facts about the account *at the IdP* that the IdP can
withdraw.

Where ``frames.invitations.require_verified_email`` is off (#190) the pair is
the ``email`` claim alone, and the freshness rule below still applies to it for
the surviving half of this reasoning: an address can be reassigned at the IdP
just as a verification can be revoked, and the match against it is not
configurable. A session lasts eight hours; a membership lasts forever (one
organization per login, no change afterwards). Acting on an assertion up to a
whole session old could therefore bind a permanent membership on a
verification that had already been revoked.

So the redemption endpoint requires the assertion to be recent
(:data:`~..web.session.VERIFIED_CLAIM_MAX_AGE_SECONDS`) and otherwise answers
``reauthentication_required``, which sends the person back through the
authorization-code flow so the IdP mints a **new** ID token from the current
user record — usually invisibly, off their existing SSO session. This surface
cannot simply re-read the IdP: it keeps no access or refresh token, and
acquiring one to close this would be a larger regression than the gap.

What that buys, stated no wider than it is: the assertion a redemption acts on
was minted by the IdP within
:data:`~..web.session.VERIFIED_CLAIM_WORST_CASE_AGE_SECONDS` (six minutes —
five nominal, plus the sixty seconds of replica clock skew the session codec
allows), not up to eight hours earlier. It is a bound, not "as of this
instant": nothing short of reading the IdP inside the redemption transaction
gives that, and no OIDC relying party has it.

They are two paths rather than two methods on one path because the guard's
public exemption is keyed on the path and knows nothing about methods:
sharing a path would have made the redemption endpoint anonymous as a side
effect of making the page anonymous.

Token handling (R3), stated as what is actually proven here
-----------------------------------------------------------
* The secret arrives in a **POST body**. It is in no request line, no path,
  no query string, no header, and no ``Referer`` — the page's own script
  never builds a URL from it, and the surface sends ``Referrer-Policy:
  no-referrer`` on every response anyway.
* The body is parsed by hand in :func:`_accept_request`, not declared as a
  FastAPI body model. A pydantic ``ValidationError`` carries the **rejected
  input** in its ``errors()``, which is how an almost-valid one-time secret
  ends up in a 422 body and from there in anything that logs bodies. Hand
  parsing means that machinery never sees the value.
* Every exception inside that function is caught inside it, so no traceback
  whose frame holds the bare string ever propagates. Above it, the token
  exists only as an :class:`~..frames.credentials.InvitationSecret` inside
  :class:`~.invitations.InvitationAcceptRequest`, which redacts itself from
  every repr, dump, and serializer.
* Nothing logged here interpolates the body. The log line records the
  outcome word and the subject, both of which are safe to quote.

What this deliberately does **not** claim: that a gateway configured to log
request bodies will not capture the token. It will, and no application-level
test can show otherwise. That half belongs to the deployment and is tracked
as an internal issue.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from ..dependencies import (
    get_granted_service_groups,
    get_invitation_service,
    get_service_access_granter,
)
from ..frames.account_provisioning import ServiceAccessGranter
from ..frames.auth import DisplayIdentity
from ..frames.invitations import (
    AlreadyInOrganizationError,
    EmailNotVerifiedError,
    InvitationAlreadyUsedError,
    InvitationEmailMismatchError,
    InvitationExpiredError,
    InvitationNotFoundError,
    InvitationRevokedError,
    InvitationService,
    InvitationsUnavailableError,
    OrganizationCreationRefusedError,
    OrgNotFoundError,
)
from ..web.acceptance import (
    ACCEPT_PAGE_PATH,
    ACCEPT_REDEEM_PATH,
    OUTCOME_ACCEPTED,
    OUTCOME_ALREADY_IN_ORGANIZATION,
    OUTCOME_ALREADY_USED,
    OUTCOME_EMAIL_MISMATCH,
    OUTCOME_EMAIL_NOT_VERIFIED,
    OUTCOME_ERROR,
    OUTCOME_EXPIRED,
    OUTCOME_NOT_FOUND,
    OUTCOME_ORGANIZATION_CREATION_REFUSED,
    OUTCOME_ORGANIZATION_MISSING,
    OUTCOME_REAUTHENTICATION_REQUIRED,
    OUTCOME_REVOKED,
    OUTCOME_UNAVAILABLE,
    acceptance_page,
)
from ..web.authz import WebForbidden, get_web_session, require_csrf, require_web_session
from ..web.forms import media_type
from ..web.pages import page_response
from ..web.request_limits import bounded_body, connection_close_headers, declares_oversize
from ..web.session import WebSession, verified_claims_are_current
from .invitations import InvitationAcceptRequest, redeem

logger = logging.getLogger("frames_server.web")

REQUEST_TOO_LARGE = 413
UNSUPPORTED_MEDIA_TYPE = 415
"""Spelled as numbers because ``starlette.status`` renamed 413 and the old
spelling now emits a ``DeprecationWarning`` on every use."""

MAX_REDEEM_BODY_BYTES = 2048
"""The cap on what this endpoint will read, enforced by counting bytes.

The only legitimate body is a JSON object with one bounded token in it — under
600 bytes at the accept model's own maximum. This is the request-size limit
the 12 August bar asks for, on the one endpoint an invitee can reach.

Enforced in :func:`_bounded_body` rather than from ``Content-Length``. The
header is a fast path and nothing more: a chunked request carries none, so a
header-only check was no limit at all against exactly the caller it needed to
bound.
"""

TERMINAL_OUTCOMES: tuple[tuple[type[Exception], str, int], ...] = (
    (InvitationNotFoundError, OUTCOME_NOT_FOUND, status.HTTP_404_NOT_FOUND),
    (InvitationExpiredError, OUTCOME_EXPIRED, status.HTTP_410_GONE),
    (InvitationRevokedError, OUTCOME_REVOKED, status.HTTP_410_GONE),
    (InvitationAlreadyUsedError, OUTCOME_ALREADY_USED, status.HTTP_410_GONE),
    (EmailNotVerifiedError, OUTCOME_EMAIL_NOT_VERIFIED, status.HTTP_403_FORBIDDEN),
    (InvitationEmailMismatchError, OUTCOME_EMAIL_MISMATCH, status.HTTP_403_FORBIDDEN),
    (AlreadyInOrganizationError, OUTCOME_ALREADY_IN_ORGANIZATION, status.HTTP_409_CONFLICT),
    (
        OrganizationCreationRefusedError,
        OUTCOME_ORGANIZATION_CREATION_REFUSED,
        status.HTTP_409_CONFLICT,
    ),
    (OrgNotFoundError, OUTCOME_ORGANIZATION_MISSING, status.HTTP_404_NOT_FOUND),
    (InvitationsUnavailableError, OUTCOME_UNAVAILABLE, status.HTTP_503_SERVICE_UNAVAILABLE),
)
"""Each terminal state the lifecycle service can raise → the word the page
renders, and the status this endpoint answers with.

Presentation only: the states, and which of them consume the token, are
#89's and are not restated here. The status codes deliberately match the ones
#89's API handlers use, so the two surfaces cannot drift into disagreeing
about what a state *is* — and a test asserts that every ``InvitationError``
subclass appears in this table, so a new terminal state fails the suite
rather than quietly rendering the generic error page.
"""


def outcome_for(exc: Exception) -> tuple[str, int] | None:
    """The page word and status for *exc*, or ``None`` if it is not terminal.

    ``isinstance`` against the table in order. The listed classes are all
    direct siblings under ``InvitationError`` (plus the unavailable
    ``RuntimeError``), so no entry shadows another; if that ever stops being
    true, the ordering here is the tie-break and the table's order is the
    place to fix it.
    """

    for exception_type, outcome, status_code in TERMINAL_OUTCOMES:
        if isinstance(exc, exception_type):
            return outcome, status_code
    return None


def _outcome_response(outcome: str, status_code: int, *, body_consumed: bool) -> JSONResponse:
    """The endpoint's whole response vocabulary: one word, no free text.

    A fixed word rather than a message, because every message this endpoint
    could compose would be composed next to a variable holding a live
    credential, and the page already owns the human copy. Nothing derived
    from the request can appear in a response body.

    ``body_consumed`` is **required**, with no default, and it decides whether
    the connection survives the response. Answering an HTTP/1.1 request whose
    body has not been read to end-of-message leaves the server unable to start
    the next cycle on that connection: it buffers what the client is still
    sending up to its high-water mark and then stalls, holding the connection
    until something times out. On the oversize path that is the exact failure
    being defended against, moved from memory to connections — a caller who
    keeps sending ties up a connection per request, and the size cap that
    stopped the memory problem is what creates it.

    So a refusal issued before the body was read closes the connection, which
    is what a 413 conventionally does anyway. Draining instead would mean
    reading past the cap, which is the original problem again; draining to a
    second bound and *then* closing is a strictly more complicated way to
    reach the same place.

    No default, because the safe value depends on where the call sits and a
    new refusal path added above the read would silently inherit the wrong
    one. Being made to answer the question is the point.
    """

    headers = {
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        **connection_close_headers(body_consumed=body_consumed),
    }
    return JSONResponse(status_code=status_code, content={"outcome": outcome}, headers=headers)


def _accept_request(raw: bytes) -> InvitationAcceptRequest | None:
    """Parse the redeem body into the boxed accept model, or ``None``.

    **Every failure is swallowed inside this function on purpose.** This is
    the only frame in the server that binds the raw secret to a local name;
    letting any exception escape it would hand a traceback the value in its
    locals, and tracebacks are exactly what error reporters serialize.
    ``ValidationError`` in particular carries the rejected input verbatim, so
    it is caught and discarded rather than inspected, chained, or logged —
    note there is no ``from exc``, no ``exc_info``, and no reference to the
    exception at all.

    ``None`` means "not a usable token", uniformly, for a body that is not
    JSON, is not an object, has no ``token``, has one that is not a string,
    or has one outside the accept model's length and alphabet. Uniform
    because the invitee can do nothing different about any of them, and
    distinguishing them would describe the secret's shape back to whoever is
    guessing at it.
    """

    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    token = payload.get("token")
    if not isinstance(token, str):
        return None
    try:
        return InvitationAcceptRequest(token=token)
    except ValidationError:
        return None


def _declares_oversize(request: Request) -> bool:
    """This route's cap, on the shared fast path. See :mod:`..web.request_limits`."""

    return declares_oversize(request, max_bytes=MAX_REDEEM_BODY_BYTES)


async def _bounded_body(request: Request) -> bytes | None:
    """This route's cap, on the shared counting read.

    The implementation moved to :mod:`..web.request_limits` when #91 was found
    to have the same ``Content-Length``-only defect this route had already
    fixed. Two copies of one bound is how the second one gets it wrong; these
    thin wrappers exist only to bind this route's own maximum.
    """

    return await bounded_body(request, max_bytes=MAX_REDEEM_BODY_BYTES)


JSON_CONTENT_TYPE = "application/json"


def _sends_json(request: Request) -> bool:
    """Whether the request declares a JSON body.

    Gating on this keeps :func:`~..web.authz.require_csrf` off the body
    entirely: that dependency falls back to reading a **form** when no
    ``X-CSRF-Token`` header is present, and refusing anything that is not
    JSON before the CSRF check runs means the branch is unreachable here.
    This endpoint's contract is JSON in and JSON out on every path, and the
    gate is what makes that provable.

    (When this was written the fallback was ``request.form()`` — an
    *unbounded* read — and this gate was the only thing standing between the
    surface's POST routes and it; that was recorded as the follow-up that
    became #119. That fix bounded the fallback inside ``require_csrf``
    itself, so the gate is no longer load-bearing for size. It still decides
    the content type, which is this route's own business.)
    """

    return media_type(request) == JSON_CONTENT_TYPE


def make_routers(
    *, memberships_enabled: bool, require_verified_email: bool
) -> tuple[APIRouter, APIRouter]:
    """Build the acceptance page's ``(public_router, session_gated_router)``.

    ``memberships_enabled`` is ``org_source_resolves_membership()``, resolved once
    at startup. On a claims-sourced deployment redemption would write
    ``collab_org_members`` rows that the authentication choke point never
    reads — reporting success while granting nothing — which is the exact
    reason #89 mounts its own router conditionally. Here the *page* still
    mounts (it costs nothing and explains itself), and the redemption
    endpoint answers ``invitations_unavailable`` instead of pretending.
    """

    public_router = APIRouter(include_in_schema=False)
    gated_router = APIRouter(include_in_schema=False, dependencies=[Depends(require_web_session)])

    @public_router.get(ACCEPT_PAGE_PATH)
    async def accept_page(request: Request) -> Response:
        """Serve the acceptance page to anyone, signed in or not.

        ``get_web_session`` rather than ``require_web_session``: an invitee
        arriving for the first time has no session, and bouncing them to
        Keycloak before telling them what they are being asked to sign in for
        is how an invitation link looks like a phishing attempt. The page
        renders the sign-in prompt itself, after its script has banked the
        fragment.
        """

        root_path = (request.scope.get("root_path") or "").rstrip("/")
        session = get_web_session(request)
        return page_response(
            acceptance_page(
                root_path=root_path,
                session=session,
                claims_current=session is not None and verified_claims_are_current(session),
                # The page's copy for `email_not_verified` describes a
                # verification email, which a deployment that does not require
                # verification never sends. Same value the acceptance check and
                # the invitation email read, fixed for the process's life. The
                # sections it selects are still built per request, for the
                # reason `acceptance.py` records at the join.
                require_verified_email=require_verified_email,
            ),
            path=ACCEPT_PAGE_PATH,
        )

    @gated_router.post(ACCEPT_REDEEM_PATH)
    async def redeem_invitation(
        request: Request,
        service: InvitationService = Depends(get_invitation_service),
        service_access: ServiceAccessGranter = Depends(get_service_access_granter),
        granted_groups: Sequence[str] = Depends(get_granted_service_groups),
        session: WebSession = Depends(require_web_session),
    ) -> Response:
        """Redeem the token the page is holding, for this browser's session.

        Authority is the pair #89 defined and nothing else: holding the
        secret, and controlling the mailbox it was issued to. The session
        supplies the second half — ``email`` and ``email_verified`` as the IdP
        asserted them, recently (see the module note on freshness) — and the
        lifecycle service checks it inside its transaction.

        **How much of that second half is checked is a deployment setting.**
        With ``frames.invitations.require_verified_email`` on, the default, the
        service requires both the verified flag and the address match. With it
        off it requires the match alone, on the argument that the one-time
        secret is itself evidence of mailbox control (#190). Written out because
        this is the docstring a reader consults to learn what stands in for a
        role on this route.

        ``require_csrf`` is called here instead of being declared as a
        dependency, and the reason has changed once. Originally the ordering
        was a security property: the dependency's form fallback was an
        unbounded ``request.form()``, and the content-type gate had to
        provably precede it — relying on FastAPI's dependency-solving order
        for that would be the same mistake #88's guard was rebuilt to stop
        making. #119 bounded the fallback inside ``require_csrf`` itself, so
        the ordering is no longer what keeps a read bounded. The call stays
        in-route for the contract: a ``Depends`` refusal would render the
        surface's HTML 403 page, and this endpoint answers JSON on every
        path (see the ``except WebForbidden`` below).
        """

        if not memberships_enabled:
            return _outcome_response(
                OUTCOME_UNAVAILABLE, status.HTTP_503_SERVICE_UNAVAILABLE, body_consumed=False
            )
        if not verified_claims_are_current(session):
            # The session asserts a verified address, but that assertion is a
            # fact about the account at the IdP and the IdP can withdraw it.
            # Acting on a stale one would bind a **permanent** membership (one
            # organization per login) on a verification that may already have
            # been revoked. This surface holds no token it could re-ask the
            # IdP with, so the person is sent back through the flow to have
            # the claims re-minted — usually invisibly, via their SSO session.
            #
            # Checked here rather than only at page render because the window
            # can lapse between the render and the click, and because the page
            # is a convenience while this is the control.
            logger.info(
                "web_invitation_accept",
                extra={"outcome": OUTCOME_REAUTHENTICATION_REQUIRED, "user": session.user},
            )
            return _outcome_response(
                OUTCOME_REAUTHENTICATION_REQUIRED,
                status.HTTP_401_UNAUTHORIZED,
                body_consumed=False,
            )
        if not _sends_json(request):
            # Before the CSRF check, so require_csrf's form fallback stays
            # unreachable here and this endpoint's refusals are all
            # JSON-shaped. (The fallback itself has been bounded since #119;
            # the ordering is about the contract now, not the read.)
            return _outcome_response(OUTCOME_ERROR, UNSUPPORTED_MEDIA_TYPE, body_consumed=False)
        try:
            await require_csrf(request, session)
        except WebForbidden as refusal:
            # Answered here rather than left to the surface's HTML 403 page,
            # so this endpoint's contract is JSON on every path and the page's
            # script never has to guess at a document it cannot parse.
            #
            # Both events, deliberately. Catching the exception took this route
            # out of `register_exception_handlers`' reach, and with it the
            # shared `web_forbidden` line every other page of the surface
            # emits — so an operator's existing query would have stopped
            # counting CSRF refusals here without anything appearing to break.
            # The shared event is re-emitted with the same `reason` field it
            # groups by (a fixed string naming the subject, never the token),
            # and the specific event is kept for anyone who wants this route
            # alone.
            logger.info("web_forbidden", extra={"reason": str(refusal)})
            logger.warning(
                "web_invitation_accept_csrf_refused",
                extra={"user": session.user, "reason": str(refusal)},
            )
            return _outcome_response(
                OUTCOME_ERROR, status.HTTP_403_FORBIDDEN, body_consumed=False
            )
        if _declares_oversize(request):
            return _outcome_response(OUTCOME_ERROR, REQUEST_TOO_LARGE, body_consumed=False)
        raw = await _bounded_body(request)
        if raw is None:
            # Stopped mid-body on purpose, so the rest of it is still coming:
            # this response must close the connection or the client's unsent
            # remainder stalls it.
            return _outcome_response(OUTCOME_ERROR, REQUEST_TOO_LARGE, body_consumed=False)
        payload = _accept_request(raw)
        if payload is None:
            # Same answer as a token that resolves to nothing, because to the
            # person holding a mangled link it is the same situation and the
            # copy already tells them what to do about it.
            return _outcome_response(
                OUTCOME_NOT_FOUND, status.HTTP_404_NOT_FOUND, body_consumed=True
            )

        display = DisplayIdentity(
            name=session.name,
            email=session.email,
            email_verified=session.email_verified,
        )
        try:
            redeem(
                service,
                payload,
                user_id=session.user,
                display=display,
                service_access=service_access,
                granted_groups=granted_groups,
            )
        except Exception as exc:
            resolved = outcome_for(exc)
            if resolved is None:
                raise
            outcome, status_code = resolved
            # The outcome word and the subject, never the exception's message
            # and never the body: #89's messages are fixed strings today, and
            # this line must stay true if one ever stops being.
            logger.info(
                "web_invitation_accept",
                extra={"outcome": outcome, "user": session.user},
            )
            return _outcome_response(outcome, status_code, body_consumed=True)

        logger.info(
            "web_invitation_accept",
            extra={"outcome": OUTCOME_ACCEPTED, "user": session.user},
        )
        # The organization and role are deliberately not in this response.
        # The page renders fixed copy, so returning them would put membership
        # facts into a body for no reader — and every field that exists is a
        # field some future logger will record.
        return _outcome_response(OUTCOME_ACCEPTED, status.HTTP_200_OK, body_consumed=True)

    return public_router, gated_router


__all__ = ["TERMINAL_OUTCOMES", "make_routers", "outcome_for"]
