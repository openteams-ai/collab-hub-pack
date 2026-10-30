"""The operator invitation routes (issue #91) — the first operator action.

Three routes on the browser surface, all under ``/admin``, all requiring a web
session *and* ``platform_role = 'operator'``:

``GET  /admin/invitations``           the page and the listing
``POST /admin/invitations``           issue one invitation
``POST /admin/invitations/revoke``    revoke one

The pattern this sets, which future operator actions copy
---------------------------------------------------------
See :mod:`..web.operator` for the reasoning. Concretely, in this file:

* the router carries ``Depends(require_operator)``, so **every** route on it —
  the page included — refuses a signed-in non-operator with the surface's own
  403 page rather than a 404, a blank page, or an API error envelope;
* the two things this page can *do* are plain functions carrying #87's
  ``@requires_platform_role`` guard, so the authority check lives on the action
  and holds wherever the function is called from. Its refusal is #87's plain
  ``HTTPException`` and therefore API-shaped rather than page-shaped, which is
  correct: it is the **backstop**, unreachable while the router dependency
  above it stands, and if it ever does fire the honest answer is a refusal that
  does not look like a designed part of the flow;
* the authority those guards read is resolved per request by
  :func:`~..web.operator.operator_context`, never asserted by this module. The
  role is therefore resolved twice on a mutating request — once by the router's
  ``require_operator``, once building the context — and that is the intended
  cost of the guard deciding a fact rather than restating an assumption;
* neither this module nor the page writes an audit row. ``audited()`` is the
  only writer of ``collab_audit_events``, composed inside #89's lifecycle
  service, and an operator-issued ``invitation.send`` row is identical to an
  owner-issued one apart from ``actor``/``actor_label``. A page that had to
  write its own row would mean the foundation was wrong, and the fix would be
  the foundation.

The token, and why it never leaves this process except to the mail adapter
-------------------------------------------------------------------------
:func:`issue_invitation` calls the invitation service **in this process**, the
same call #89's API route makes, and receives the raw secret as its return
value. There is no HTTP client here, no self-request, and no dependence on the
``/v1`` router being mounted — the page issues invitations on a deployment
where that router does not exist, which is what makes "the token did not
arrive over HTTP" a property rather than a claim.

The secret is then handed to ``InvitationEmailDelivery.deliver`` — the adapter
#89's API route uses — and dropped. The page renders the sanitized outcome
(sent / could not be sent / unconfirmed) and never the token.

Until invitation mail was deliverable, this path did the opposite: it rendered
the redemption link through ``web/invite_link.py`` for an operator to send by
hand, and suppressed the send, because one single-use secret travelling two
routes doubles its exposure and makes "was it sent?" ambiguous. That was the
relaxation the 2026-08-07 amendment to #91 granted, on its own end condition
(an internal issue). The condition is met, the module is
deleted, and the send is restored in the same edit — one secret, one route.

What reaches a log
------------------
The subject, the invitation **id**, and a fixed reason word. Never the token,
never a message from an exception that was raised near one, and never the
submitted address: an id is opaque, is what the audit row already quotes, and
is what anyone reading such a line actually needs.

Request-size limits
-------------------
Both ``POST`` bodies are refused above :data:`MAX_FORM_BYTES` **by counting what
arrives**, not by trusting ``Content-Length``. The header is a fast path and
nothing more: a chunked request carries none, so a header-only check was no
limit at all against exactly the caller it needed to bound — 2,000,000 bytes
went through a 4,096-byte cap. The bound now lives in
:mod:`..web.request_limits`, shared with the acceptance page (#90), which had
already fixed the same defect; two implementations of one limit is how the
second one gets it wrong. The parsing-and-checking layer above it moved to
:mod:`..web.forms` when the owner page (#142) became its second caller, for
the same reason, and the properties below are now that module's to keep.

Neither route calls ``request.form()``. The body is read under the cap and the
urlencoded fields are parsed from those bytes, so Starlette's unbounded form
parse is not on either path — which also means #119's fix to ``require_csrf``
has nothing to reach here.

A refusal issued **before** the body was read closes the connection. Answering
without reading to end-of-message otherwise leaves the server stalled on that
connection until something times out, which is the same exhaustion moved from
memory to sockets. See :func:`~..web.request_limits.connection_close_headers`.

That is the cheap abuse control milestone decision #116 keeps on the 12 August
bar: it protects the server rather than the user population, which is the part
strict invitation does not already cover.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, Response, status

from ..dependencies import get_invitation_email_delivery, get_invitation_service
from ..frames.auth import AuthContext
from ..frames.authorization import requires_platform_role
from ..frames.invitation_email import (
    DELIVERY_PROVIDER_ACCEPTED,
    DELIVERY_UNKNOWN,
    InvitationEmailDelivery,
)
from ..frames.invitations import (
    Invitation,
    InvitationAlreadyUsedError,
    InvitationNotFoundError,
    InvitationService,
    InvitationsUnavailableError,
    IssuedInvitation,
    LiveInvitationExists,
    validate_invited_email,
)
from ..frames.orgs import PLATFORM_ROLE_OPERATOR
from ..web.admin import (
    EMAIL_FIELD,
    INVITATION_ID_FIELD,
    MAX_EMAIL_LENGTH,
    MAX_INVITATION_ID_LENGTH,
    NOTICE_ALREADY_LIVE,
    NOTICE_INVALID_EMAIL,
    NOTICE_ISSUED_SEND_FAILED,
    NOTICE_ISSUED_SEND_UNKNOWN,
    NOTICE_ISSUED_SENT,
    NOTICE_NOT_FOUND,
    NOTICE_REVOKE_REFUSED,
    NOTICE_REVOKED,
    NOTICE_UNAVAILABLE,
    Notice,
    invitations_page,
    request_refused_page,
)
from ..web.authz import require_operator
from ..web.forms import (
    FORM_CONTENT_TYPE,
    MAX_FORM_BYTES,
    REQUEST_TOO_LARGE,
    UNSUPPORTED_MEDIA_TYPE,
    FormRefused,
    csrf_ok,
    form_field,
    form_fields,
)
from ..web.operator import operator_context
from ..web.pages import forbidden_page, page_response
from ..web.request_limits import connection_close_headers
from ..web.session import WebSession
from ..web.surface import (
    ADMIN_INVITATIONS_PATH,
    ADMIN_INVITATIONS_REVOKE_PATH,
)

logger = logging.getLogger("frames_server.web")

# REQUEST_TOO_LARGE, UNSUPPORTED_MEDIA_TYPE, MAX_FORM_BYTES, FORM_CONTENT_TYPE
# and FormRefused are re-exported above from ..web.forms, where they moved when
# the owner page (#142) became the second page with this POST shape. The names
# stay importable from here — this module was their first home and its tests
# and __all__ still speak them.

LISTING_LIMIT = 100
"""How many invitations the page shows, newest first.

Bounded because the listing has none of its own: the operator's view is the
whole deployment's, and it grows for as long as the deployment lives. No paging
controls, deliberately — the milestone issues invitations in small batches, and
the operator API pages properly for anyone who needs more.
"""


# --- The actions -------------------------------------------------------------
#
# Plain functions, each carrying #87's platform-role guard. This is the shape a
# future operator action copies: the guard sits on the function that performs
# the act, so it holds wherever the function is called from, and the
# AuthContext it reads was resolved for this request rather than assumed.


@requires_platform_role(PLATFORM_ROLE_OPERATOR)
def issue_invitation(
    auth: AuthContext, service: InvitationService, *, email: str
) -> IssuedInvitation | LiveInvitationExists:
    """Issue one org-creating invitation, recorded as ``invitation.send``.

    ``org_id=None`` is not a parameter and never will be on this surface.
    Every invitation issued from the operator page creates its organization on
    acceptance, with the accepter as owner (Gate B, revised 2026-08-04); a page
    that could name an existing organization would be the cross-org capability
    Gate E scoped out, and pre-creating one here would leave an orphan behind
    every invitation that is revoked, expires, or is never accepted.

    ``create_unless_live`` rather than ``create``: issuing twice for one
    address must not mint a second live token, which matters more now that a
    human may retry after an ambiguous send. The rule is enforced inside the
    audited transaction under an advisory lock, so even a double-submitted form
    produces one invitation and one event row — see the invitation service.

    **Scoped to this page**, and stated that way wherever it is stated. #89's
    ``/v1`` operator and owner routes still call ``create``, which mints
    unconditionally, so an address can hold two live invitations if one came
    from the API. Unifying the two would change the semantics of a merged,
    shipped endpoint the desktop is built against; #93 owns re-send policy.
    """

    return service.create_unless_live(auth, email=email, org_id=None)


@requires_platform_role(PLATFORM_ROLE_OPERATOR)
def revoke_invitation(auth: AuthContext, service: InvitationService, *, invitation_id: str) -> Invitation:
    """Revoke any invitation on the deployment, recorded as ``invitation.revoke``.

    ``expect_org_id`` is left unset, which is hub scope: an operator's revoke
    is not pinned to an organization, and the org-creating invitations this
    page issues belong to none. A revoke that changes nothing writes no second
    audit row (#89) — it is not an action.
    """

    return service.revoke(auth, invitation_id)


# --- Request helpers ---------------------------------------------------------


def _root_path(request: Request) -> str:
    return (request.scope.get("root_path") or "").rstrip("/")


def _csrf_ok(request: Request, fields: dict[str, str], session: WebSession) -> bool:
    """This page's spelling of :func:`~..web.forms.csrf_ok`.

    The predicate itself — the constant-time comparison over already-parsed,
    already-bounded fields, and the reasoning for running it in-route rather
    than as a dependency — lives in :mod:`..web.forms` now that two pages use
    it. That is the claim :data:`~..web.surface.CSRF_ENFORCED_IN_ROUTE`
    records for these two paths.
    """

    return csrf_ok(request, fields, session, page="/admin")


def _forbidden(request: Request) -> Response:
    # The body was read before this decision, so the connection survives.
    return page_response(
        forbidden_page(root_path=_root_path(request)),
        status_code=status.HTTP_403_FORBIDDEN,
        path=ADMIN_INVITATIONS_PATH,
    )


def _refused(request: Request, refusal: FormRefused) -> Response:
    """Answer a body this page refused to read, and let the connection go.

    No listing and no session-dependent content: this response is issued
    without touching the invitation service, so a caller hammering the cap
    cannot turn each refusal into database work. ``Connection: close`` because
    the body was **not** consumed — see
    :mod:`~..web.request_limits` for why answering without it strands the
    connection instead.
    """

    response = page_response(
        request_refused_page(root_path=_root_path(request), status_code=refusal.status_code),
        status_code=refusal.status_code,
        path=ADMIN_INVITATIONS_PATH,
    )
    response.headers.update(connection_close_headers(body_consumed=False))
    return response


# --- Routes ------------------------------------------------------------------


def make_router() -> APIRouter:
    """Build the operator page's router.

    ``require_operator`` as a **router-level** dependency, so the role check is
    a property of the router rather than of each handler's parameter list. It
    reaches ``require_web_session`` one level down, which is what the surface's
    route lint walks for, and ``WebSessionGuardMiddleware`` authenticates the
    ``/admin`` prefix before routing regardless.
    """

    router = APIRouter(include_in_schema=False, dependencies=[Depends(require_operator)])

    def _render(
        request: Request,
        session: WebSession,
        service: InvitationService,
        *,
        notice: Notice | None = None,
        status_code: int = status.HTTP_200_OK,
    ) -> Response:
        """The page, with the listing read fresh on every response.

        Read *after* the mutation rather than before, so the row an operator
        just created or revoked is on the page they are handed. One clock
        reading derives every row's state, so a listing cannot show two rows
        decided against different instants.
        """

        try:
            page = service.list_all(limit=LISTING_LIMIT, offset=0)
            now = service.server_now()
        except InvitationsUnavailableError:
            # No listing to show. `now` is unused with no rows; a real value
            # rather than a placeholder so nothing downstream has to special
            # case it.
            page, now = None, datetime.now(tz=timezone.utc)
            notice = notice if notice is not None else Notice(NOTICE_UNAVAILABLE)
            if status_code == status.HTTP_200_OK:
                status_code = status.HTTP_503_SERVICE_UNAVAILABLE

        return page_response(
            invitations_page(
                root_path=_root_path(request),
                session=session,
                invitations=page.invitations if page is not None else (),
                has_more=page.has_more if page is not None else False,
                now=now,
                notice=notice,
            ),
            status_code=status_code,
            path=ADMIN_INVITATIONS_PATH,
        )

    @router.get(ADMIN_INVITATIONS_PATH)
    async def invitations(
        request: Request,
        session: WebSession = Depends(require_operator),
        service: InvitationService = Depends(get_invitation_service),
    ) -> Response:
        """The page: the issue form, and the invitations on this deployment."""

        return _render(request, session, service)

    @router.post(ADMIN_INVITATIONS_PATH)
    async def create_invitation(
        request: Request,
        session: WebSession = Depends(require_operator),
        auth: AuthContext = Depends(operator_context),
        service: InvitationService = Depends(get_invitation_service),
        delivery: InvitationEmailDelivery = Depends(get_invitation_email_delivery),
    ) -> Response:
        """Issue one invitation and send it to the invited address.

        The whole outcome vocabulary is :data:`~..web.admin.NOTICES` — fixed
        sentences whose only dynamic part is the address. Nothing derived from
        an exception's message reaches the page.
        """

        # The body is bounded and parsed before anything else looks at it, so
        # no path here reaches an unbounded read.
        try:
            fields = await form_fields(request)
        except FormRefused as refusal:
            return _refused(request, refusal)
        if not _csrf_ok(request, fields, session):
            return _forbidden(request)

        # Validated here rather than by catching ValueError around the action:
        # the audited primitive raises ValueError subclasses of its own, and
        # laundering one of those into "that is not an email address" would
        # describe an internal failure as the operator's typo.
        submitted = form_field(fields, EMAIL_FIELD, max_length=MAX_EMAIL_LENGTH)
        try:
            address = validate_invited_email(submitted)
        except ValueError:
            return _render(
                request,
                session,
                service,
                notice=Notice(NOTICE_INVALID_EMAIL),
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        try:
            outcome = issue_invitation(auth, service, email=address)
        except InvitationsUnavailableError:
            return _render(
                request,
                session,
                service,
                notice=Notice(NOTICE_UNAVAILABLE, address),
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        if isinstance(outcome, LiveInvitationExists):
            logger.info(
                "web_invitation_issue_refused",
                extra={
                    "user": session.user,
                    "reason": NOTICE_ALREADY_LIVE,
                    "invitation_id": outcome.existing.id,
                },
            )
            return _render(
                request,
                session,
                service,
                notice=Notice(NOTICE_ALREADY_LIVE, outcome.existing.email),
                status_code=status.HTTP_409_CONFLICT,
            )

        # The one place a live secret is read on this surface, and it is handed
        # straight to the delivery adapter. `.reveal()` is the supported escape
        # and greps as one; everywhere else the wrapper redacts itself.
        #
        # Sequenced after the audited transaction committed, per #87's contract:
        # a send is unrecoverable and a rollback cannot un-send it. The residual
        # risk runs the other way — a committed invitation whose email failed —
        # which is why the outcome is worded on the page instead of dropped.
        invitation = outcome.invitation
        delivered = delivery.deliver(
            invitation_id=invitation.id,
            recipient=invitation.email,
            invitation_secret=outcome.raw_secret.reveal(),
            organization_name=None,
            expires_at=invitation.expires_at,
        )
        if delivered.status == DELIVERY_PROVIDER_ACCEPTED:
            kind = NOTICE_ISSUED_SENT
        elif delivered.status == DELIVERY_UNKNOWN:
            kind = NOTICE_ISSUED_SEND_UNKNOWN
        else:
            kind = NOTICE_ISSUED_SEND_FAILED
        logger.info(
            "web_invitation_issued",
            extra={
                "user": session.user,
                "invitation_id": invitation.id,
                "delivery_status": delivered.status,
            },
        )
        return _render(
            request,
            session,
            service,
            notice=Notice(kind, invitation.email),
            status_code=status.HTTP_201_CREATED,
        )

    @router.post(ADMIN_INVITATIONS_REVOKE_PATH)
    async def revoke(
        request: Request,
        session: WebSession = Depends(require_operator),
        auth: AuthContext = Depends(operator_context),
        service: InvitationService = Depends(get_invitation_service),
    ) -> Response:
        """Revoke one invitation, named by the id the listing rendered."""

        try:
            fields = await form_fields(request)
        except FormRefused as refusal:
            return _refused(request, refusal)
        if not _csrf_ok(request, fields, session):
            return _forbidden(request)
        invitation_id = form_field(fields, INVITATION_ID_FIELD, max_length=MAX_INVITATION_ID_LENGTH)
        try:
            invitation = revoke_invitation(auth, service, invitation_id=invitation_id)
        except InvitationNotFoundError:
            return _render(
                request,
                session,
                service,
                notice=Notice(NOTICE_NOT_FOUND),
                status_code=status.HTTP_404_NOT_FOUND,
            )
        except InvitationAlreadyUsedError:
            return _render(
                request,
                session,
                service,
                notice=Notice(NOTICE_REVOKE_REFUSED),
                status_code=status.HTTP_409_CONFLICT,
            )
        except InvitationsUnavailableError:
            return _render(
                request,
                session,
                service,
                notice=Notice(NOTICE_UNAVAILABLE),
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        logger.info(
            "web_invitation_revoked",
            extra={"user": session.user, "invitation_id": invitation.id},
        )
        return _render(request, session, service, notice=Notice(NOTICE_REVOKED, invitation.email))

    return router


__all__ = [
    "FORM_CONTENT_TYPE",
    "LISTING_LIMIT",
    "MAX_FORM_BYTES",
    "REQUEST_TOO_LARGE",
    "UNSUPPORTED_MEDIA_TYPE",
    "FormRefused",
    "issue_invitation",
    "make_router",
    "revoke_invitation",
]
