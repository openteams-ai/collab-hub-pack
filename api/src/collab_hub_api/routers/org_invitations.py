"""The owner invitation routes (issue #142) — the first owner action.

Four routes on the browser surface, all under ``/web/org``, all requiring a
web session *and* an active ``role = 'owner'`` membership:

``GET  /web/org/invitations``           the page and the listing
``POST /web/org/invitations``           issue one invitation into the caller's org
``POST /web/org/invitations/revoke``    revoke one of the caller's org's
``POST /web/org/invitations/name``      give a placeholder-named org its name (#44)

The naming route is the first-invite naming flow #92 specified and #142 did
not ship: every organization starts with the neutral placeholder name, so an
owner reaching this page for the first time owns "Unnamed organization", and
the page identifies the destination of every invitation they issue only as
that (observed live, #44). The invitation email is organization-neutral by
decision and unaffected; this is the owner's side — knowing what they are
inviting people into. While the placeholder stands the page renders the
naming form instead of the issue form, and ``POST /web/org/invitations``
refuses to issue: a submission that passes the page's ordinary request checks
(bounded body, CSRF, a valid address) is answered **409** before the issue
action and before delivery, and a submission that fails those checks is
answered as it always was — so no body shape reaches issuance. The refusal
is the server's; the hidden form is just its honest rendering. Naming is
one-shot and audited as ``org.rename``; see
:meth:`~..frames.invitations.PostgresInvitationService.name_organization`.

The pattern is issue #91's, instantiated for the org axis — see
:mod:`..web.owner` for what carries over and what the axis changes.
Concretely, in this file:

* the router carries ``Depends(require_org_owner)``, so **every** route on it
  refuses a signed-in non-owner with the surface's own 403 page rather than a
  404, a blank page, or an API error envelope;
* the three things this page can *do* are plain functions carrying #87's
  ``@requires_org_role(ROLE_OWNER, org_arg="org_id")`` guard, so the
  authority check lives on the action, is pinned to the organization the call
  names, and holds wherever the function is called from;
* the organization those guards are pinned to is resolved per request by
  :func:`~..web.owner.owner_context` from the caller's own membership row —
  never a form field, never a path segment an owner could vary. The wrapper
  compares it against the context's ``home_org_id``, which came from the same
  resolution, and ``expect_org_id`` re-asserts it inside the revoke
  transaction;
* neither this module nor the page writes an audit row. ``audited()`` is the
  only writer of ``collab_audit_events``, composed inside #89's lifecycle
  service, and an owner-issued ``invitation.send`` row is identical to an
  operator-issued one apart from ``actor``/``actor_label`` and ``org_id``.

The secret
----------
Issuing calls the invitation service **in this process** — no HTTP client, no
self-request, no dependence on the ``/v1`` router being mounted — and the raw
secret then travels exactly one route: it is handed to
``InvitationEmailDelivery.deliver`` (the adapter #89's API route uses) and
dropped. The page renders the sanitized outcome and never the token.

This page once had a second mode that rendered the link instead, on a
deployment with no provider, through ``web/invite_link.py``. That module's end
condition is met and it is deleted; a deployment with no provider now gets a
visible failed send, which is the correct answer once mail is the delivery
channel. ``configured`` survives as the page's *warning* — the owner is told
before issuing, not after.

What reaches a log: the subject, the invitation **id**, and a fixed reason
word — never the token, never the submitted address. Request bodies are
bounded and parsed by :mod:`..web.forms`, shared with the operator page,
which keeps every property that module's docstring argues for (counting
rather than trusting ``Content-Length``, no ``request.form()`` anywhere, and
``Connection: close`` on a refusal issued before the body was read).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, Response, status

from ..dependencies import get_invitation_email_delivery, get_invitation_service
from ..frames.auth import AuthContext
from ..frames.authorization import requires_org_role
from ..frames.invitation_email import (
    DELIVERY_PROVIDER_ACCEPTED,
    DELIVERY_UNKNOWN,
    InvitationEmailDelivery,
)
from ..frames.invitations import (
    MAX_ORGANIZATION_NAME_LENGTH,
    Invitation,
    InvitationAlreadyUsedError,
    InvitationNotFoundError,
    InvitationService,
    InvitationsUnavailableError,
    IssuedInvitation,
    LiveInvitationExists,
    OrganizationAlreadyNamedError,
    is_placeholder_organization_name,
    validate_invited_email,
    validate_organization_name,
)
from ..frames.orgs import ROLE_OWNER
from ..web.admin import (
    EMAIL_FIELD,
    INVITATION_ID_FIELD,
    MAX_EMAIL_LENGTH,
    MAX_INVITATION_ID_LENGTH,
)
from ..web.authz import require_org_owner
from ..web.forms import (
    MAX_FORM_BYTES,
    FormRefused,
    csrf_ok,
    form_field,
    form_fields,
)
from ..web.org_invitations import (
    NOTICE_ALREADY_LIVE,
    NOTICE_ALREADY_NAMED,
    NOTICE_INVALID_EMAIL,
    NOTICE_INVALID_ORGANIZATION_NAME,
    NOTICE_ISSUED_SEND_FAILED,
    NOTICE_ISSUED_SEND_UNKNOWN,
    NOTICE_ISSUED_SENT,
    NOTICE_NAMED,
    NOTICE_NOT_FOUND,
    NOTICE_ORGANIZATION_UNNAMED,
    NOTICE_REVOKE_REFUSED,
    NOTICE_REVOKED,
    NOTICE_UNAVAILABLE,
    ORGANIZATION_NAME_FIELD,
    Notice,
    invitations_page,
    request_refused_page,
)
from ..web.owner import owner_context
from ..web.pages import forbidden_page, page_response
from ..web.request_limits import connection_close_headers
from ..web.session import WebSession
from ..web.surface import (
    ORG_INVITATIONS_NAME_PATH,
    ORG_INVITATIONS_PATH,
    ORG_INVITATIONS_REVOKE_PATH,
)

logger = logging.getLogger("frames_server.web")

LISTING_LIMIT = 100
"""How many invitations the page shows, newest first.

Bounded because the listing has none of its own. An organization's view is
narrower than the operator's whole-deployment one, but it still grows for as
long as the organization lives; no paging controls, deliberately — the owner
API pages properly for anyone who needs more.
"""


# --- The actions -------------------------------------------------------------
#
# Plain functions, each carrying #87's org-role guard pinned to the target
# organization — the same wrapper, with the same arguments, that guards the
# /v1/orgs/{org_id}/invitations routes, so an owner-issued row from this page
# and one from the API are indistinguishable apart from how the caller was
# authenticated.


@requires_org_role(ROLE_OWNER, org_arg="org_id")
def issue_invitation(
    auth: AuthContext, service: InvitationService, *, org_id: str, email: str
) -> IssuedInvitation | LiveInvitationExists:
    """Issue one invitation into *org_id*, recorded as ``invitation.send``.

    ``org_id`` is always the caller's own organization — resolved by
    :func:`~..web.owner.owner_context`, compared by the wrapper against the
    same resolution's ``home_org_id`` — and there is deliberately no spelling
    on this surface for "no organization": org-creating invitations are the
    operator page's, by construction.

    ``create_unless_live`` rather than ``create``, exactly as on the operator
    page and for the same reason: a human may retry after an ambiguous
    outcome, and issuing twice for one address must not mint a second live
    token. The rule is enforced inside the audited transaction under an
    advisory lock. The ``/v1`` owner route still calls ``create``, which
    mints unconditionally — unifying them is #93's re-send policy, not this
    page's.
    """

    return service.create_unless_live(auth, email=email, org_id=org_id)


@requires_org_role(ROLE_OWNER, org_arg="org_id")
def revoke_invitation(auth: AuthContext, service: InvitationService, *, org_id: str, invitation_id: str) -> Invitation:
    """Revoke one of *org_id*'s invitations, recorded as ``invitation.revoke``.

    ``expect_org_id`` carries the authorized scope down into the transaction,
    so an invitation belonging to another organization — or to none — is a
    plain not-found decided against the same locked row the update runs on,
    and probing this page enumerates nothing.
    """

    return service.revoke(auth, invitation_id, expect_org_id=org_id)


@requires_org_role(ROLE_OWNER, org_arg="org_id")
def name_organization(auth: AuthContext, service: InvitationService, *, org_id: str, name: str) -> str:
    """Give *org_id* its first name, recorded as ``org.rename``.

    Same pin as the other two actions: ``org_id`` is the caller's own
    organization from :func:`~..web.owner.owner_context`, compared by the
    wrapper against the same resolution's ``home_org_id``. The service reads
    the current name ``FOR UPDATE`` and refuses with
    :class:`~..frames.invitations.OrganizationAlreadyNamedError` unless it is
    still the placeholder, so this is the first naming and only that.
    """

    return service.name_organization(auth, org_id=org_id, name=name)


# --- Request helpers ---------------------------------------------------------


def _root_path(request: Request) -> str:
    return (request.scope.get("root_path") or "").rstrip("/")


def _email_configured(delivery: InvitationEmailDelivery) -> bool:
    """Whether a real provider stands behind the delivery seam.

    Read from the adapter's own ``configured`` attribute, **defaulting to
    True**: an adapter that does not declare itself is assumed to work, so the
    page stays quiet and a real failure shows up as the "could not be sent"
    notice. This decides the intro's warning only — never whether the secret is
    sent, which it always is.
    """

    return getattr(delivery, "configured", True) is not False


def _forbidden(request: Request) -> Response:
    # The body was read before this decision, so the connection survives.
    return page_response(
        forbidden_page(root_path=_root_path(request)),
        status_code=status.HTTP_403_FORBIDDEN,
        path=ORG_INVITATIONS_PATH,
    )


def _refused(request: Request, refusal: FormRefused) -> Response:
    """Answer a body this page refused to read, and let the connection go.

    No listing and no session-dependent content, so a caller hammering the
    cap cannot turn each refusal into database work. ``Connection: close``
    because the body was **not** consumed — see :mod:`..web.request_limits`.
    """

    response = page_response(
        request_refused_page(root_path=_root_path(request), status_code=refusal.status_code),
        status_code=refusal.status_code,
        path=ORG_INVITATIONS_PATH,
    )
    response.headers.update(connection_close_headers(body_consumed=False))
    return response


# --- Routes ------------------------------------------------------------------


def make_router() -> APIRouter:
    """Build the owner page's router.

    ``require_org_owner`` as a **router-level** dependency, so the role check
    is a property of the router rather than of each handler's parameter list.
    It reaches ``require_web_session`` one level down, which is what the
    surface's route lint walks for, and ``WebSessionGuardMiddleware``
    authenticates the ``/web`` prefix before routing regardless.
    """

    router = APIRouter(include_in_schema=False, dependencies=[Depends(require_org_owner)])

    def _render(
        request: Request,
        session: WebSession,
        auth: AuthContext,
        service: InvitationService,
        delivery: InvitationEmailDelivery,
        *,
        notice: Notice | None = None,
        status_code: int = status.HTTP_200_OK,
    ) -> Response:
        """The page, with the listing read fresh on every response.

        Read *after* the mutation, so the row an owner just created or
        revoked is on the page they are handed, and every row's state derives
        from one clock reading. ``auth.org_id`` is safe here:
        ``owner_context`` refused any caller whose membership did not resolve.
        """

        org_id = auth.org_id
        try:
            organization_name = service.organization_name(org_id)
            organization_named = not is_placeholder_organization_name(organization_name)
            page = service.list_for_org(org_id, limit=LISTING_LIMIT, offset=0)
            now = service.server_now()
        except InvitationsUnavailableError:
            # No listing to show. `now` is unused with no rows; a real value
            # rather than a placeholder so nothing downstream has to special
            # case it. `organization_named` is True: with no store to answer,
            # the page has no verdict to render, and every form on it fails
            # into the same unavailable notice anyway.
            organization_name, page, now = "your organization", None, datetime.now(tz=timezone.utc)
            organization_named = True
            notice = notice if notice is not None else Notice(NOTICE_UNAVAILABLE)
            if status_code == status.HTTP_200_OK:
                status_code = status.HTTP_503_SERVICE_UNAVAILABLE

        return page_response(
            invitations_page(
                root_path=_root_path(request),
                session=session,
                organization_name=organization_name,
                organization_named=organization_named,
                email_configured=_email_configured(delivery),
                invitations=page.invitations if page is not None else (),
                has_more=page.has_more if page is not None else False,
                now=now,
                notice=notice,
            ),
            status_code=status_code,
            path=ORG_INVITATIONS_PATH,
        )

    @router.get(ORG_INVITATIONS_PATH)
    async def invitations(
        request: Request,
        session: WebSession = Depends(require_org_owner),
        auth: AuthContext = Depends(owner_context),
        service: InvitationService = Depends(get_invitation_service),
        delivery: InvitationEmailDelivery = Depends(get_invitation_email_delivery),
    ) -> Response:
        """The page: the issue form, and the caller's organization's invitations."""

        return _render(request, session, auth, service, delivery)

    @router.post(ORG_INVITATIONS_PATH)
    async def create_invitation(
        request: Request,
        session: WebSession = Depends(require_org_owner),
        auth: AuthContext = Depends(owner_context),
        service: InvitationService = Depends(get_invitation_service),
        delivery: InvitationEmailDelivery = Depends(get_invitation_email_delivery),
    ) -> Response:
        """Issue one invitation; email it, or render its link once.

        The whole outcome vocabulary is :data:`~..web.org_invitations.NOTICES`
        — fixed sentences whose only dynamic part is the address. Nothing
        derived from an exception's or a provider's message reaches the page.
        """

        # The body is bounded and parsed before anything else looks at it, so
        # no path here reaches an unbounded read.
        try:
            fields = await form_fields(request, max_bytes=MAX_FORM_BYTES)
        except FormRefused as refusal:
            return _refused(request, refusal)
        if not csrf_ok(request, fields, session, page="/web/org"):
            return _forbidden(request)

        # Validated here rather than by catching ValueError around the action:
        # the audited primitive raises ValueError subclasses of its own, and
        # laundering one of those into "that is not an email address" would
        # describe an internal failure as the owner's typo.
        submitted = form_field(fields, EMAIL_FIELD, max_length=MAX_EMAIL_LENGTH)
        try:
            address = validate_invited_email(submitted)
        except ValueError:
            return _render(
                request,
                session,
                auth,
                service,
                delivery,
                notice=Notice(NOTICE_INVALID_EMAIL),
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        # Before the audited transaction, same as the API route: it is only
        # used to word the email, and it must not run between commit and send.
        # It is also the first-invite gate (#44): an organization still
        # carrying the placeholder name issues nothing from this page. The
        # checks above (body bound, CSRF, address) answer a malformed
        # submission first, as they always did; anything that passes them is
        # refused here, before the audited action and before delivery — the
        # form the page hides is a rendering of this refusal, not the refusal
        # itself. A rename racing this read can only make the name handed to
        # the delivery adapter better, never worse (and the shipped renderer
        # is organization-neutral and discards it), so the gate is read here
        # rather than locked into the send.
        try:
            organization_name = service.organization_name(auth.org_id)
            if is_placeholder_organization_name(organization_name):
                return _render(
                    request,
                    session,
                    auth,
                    service,
                    delivery,
                    notice=Notice(NOTICE_ORGANIZATION_UNNAMED, address),
                    status_code=status.HTTP_409_CONFLICT,
                )
            outcome = issue_invitation(auth, service, org_id=auth.org_id, email=address)
        except InvitationsUnavailableError:
            return _render(
                request,
                session,
                auth,
                service,
                delivery,
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
                auth,
                service,
                delivery,
                notice=Notice(NOTICE_ALREADY_LIVE, outcome.existing.email),
                status_code=status.HTTP_409_CONFLICT,
            )

        # The one place a live secret is read on this page, and it is handed
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
            organization_name=organization_name,
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
            auth,
            service,
            delivery,
            notice=Notice(kind, invitation.email),
            status_code=status.HTTP_201_CREATED,
        )

    @router.post(ORG_INVITATIONS_REVOKE_PATH)
    async def revoke(
        request: Request,
        session: WebSession = Depends(require_org_owner),
        auth: AuthContext = Depends(owner_context),
        service: InvitationService = Depends(get_invitation_service),
        delivery: InvitationEmailDelivery = Depends(get_invitation_email_delivery),
    ) -> Response:
        """Revoke one invitation, named by the id the listing rendered."""

        try:
            fields = await form_fields(request, max_bytes=MAX_FORM_BYTES)
        except FormRefused as refusal:
            return _refused(request, refusal)
        if not csrf_ok(request, fields, session, page="/web/org"):
            return _forbidden(request)
        invitation_id = form_field(fields, INVITATION_ID_FIELD, max_length=MAX_INVITATION_ID_LENGTH)
        try:
            invitation = revoke_invitation(auth, service, org_id=auth.org_id, invitation_id=invitation_id)
        except InvitationNotFoundError:
            return _render(
                request,
                session,
                auth,
                service,
                delivery,
                notice=Notice(NOTICE_NOT_FOUND),
                status_code=status.HTTP_404_NOT_FOUND,
            )
        except InvitationAlreadyUsedError:
            return _render(
                request,
                session,
                auth,
                service,
                delivery,
                notice=Notice(NOTICE_REVOKE_REFUSED),
                status_code=status.HTTP_409_CONFLICT,
            )
        except InvitationsUnavailableError:
            return _render(
                request,
                session,
                auth,
                service,
                delivery,
                notice=Notice(NOTICE_UNAVAILABLE),
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        logger.info(
            "web_invitation_revoked",
            extra={"user": session.user, "invitation_id": invitation.id},
        )
        return _render(request, session, auth, service, delivery, notice=Notice(NOTICE_REVOKED, invitation.email))

    @router.post(ORG_INVITATIONS_NAME_PATH)
    async def name(
        request: Request,
        session: WebSession = Depends(require_org_owner),
        auth: AuthContext = Depends(owner_context),
        service: InvitationService = Depends(get_invitation_service),
        delivery: InvitationEmailDelivery = Depends(get_invitation_email_delivery),
    ) -> Response:
        """Name the caller's organization, once, so invitations can be issued."""

        try:
            fields = await form_fields(request, max_bytes=MAX_FORM_BYTES)
        except FormRefused as refusal:
            return _refused(request, refusal)
        if not csrf_ok(request, fields, session, page="/web/org"):
            return _forbidden(request)

        # Same shape as the address: validated here, before the action, so an
        # internal ValueError is never worded as the owner's typo. An
        # over-long field comes back from form_field as "" and fails the same
        # way a blank one does.
        submitted = form_field(fields, ORGANIZATION_NAME_FIELD, max_length=MAX_ORGANIZATION_NAME_LENGTH)
        try:
            chosen = validate_organization_name(submitted)
        except ValueError:
            return _render(
                request,
                session,
                auth,
                service,
                delivery,
                notice=Notice(NOTICE_INVALID_ORGANIZATION_NAME),
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        try:
            named = name_organization(auth, service, org_id=auth.org_id, name=chosen)
        except OrganizationAlreadyNamedError:
            return _render(
                request,
                session,
                auth,
                service,
                delivery,
                notice=Notice(NOTICE_ALREADY_NAMED),
                status_code=status.HTTP_409_CONFLICT,
            )
        except InvitationsUnavailableError:
            return _render(
                request,
                session,
                auth,
                service,
                delivery,
                notice=Notice(NOTICE_UNAVAILABLE),
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        # The organization id and the actor, not the name: the name is on the
        # audit row, and a log line is not where display text belongs.
        logger.info("web_organization_named", extra={"user": session.user, "org_id": auth.org_id})
        return _render(request, session, auth, service, delivery, notice=Notice(NOTICE_NAMED, named))

    return router


__all__ = [
    "LISTING_LIMIT",
    "issue_invitation",
    "make_router",
    "name_organization",
    "revoke_invitation",
]
