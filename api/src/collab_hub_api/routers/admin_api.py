"""The admin panel's JSON endpoints.

Everything the panel reads and writes goes through here, on the browser axis:
the session cookie authenticates, the operator role authorizes, and mutations
carry the ``X-CSRF-Token`` header that :func:`~..web.authz.require_csrf`
already knows how to read.

Why this is not under ``/v1``
-----------------------------
See :data:`~..web.surface.ADMIN_API_PREFIX`. The short version is that the rule
this codebase keeps is not "JSON belongs to the API" but "browser credentials
do not work on the machine API, and machine credentials do not sign a browser
in". A panel running in a browser is a browser client.

The practical gain is the guard. ``/admin`` is a guarded prefix, and
:class:`~..web.guard.WebSessionGuardMiddleware` validates the session cookie by
path, before routing, consulting no route and no dependency -- so an endpoint
added here without its dependency is still authenticated. ``/v1`` has no such
middleware and relies on each route carrying its own, which is the arrangement
the guard module was rewritten to stop depending on.

The router still carries ``require_operator``, for the reason the operator
invitation page carries it: the guard decides *authentication*, and authority
is a separate question that must be asked on every route of this surface.
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, BaseModel, Field

from ..dependencies import (
    get_audit_log,
    get_invitation_email_delivery,
    get_invitation_service,
    get_usage_store,
    get_user_directory_client,
)
from ..frames.audit_log import AuditLog, AuditLogUnavailableError
from ..frames.connector_state import CONNECTOR_KEYS
from ..frames.connector_status import connector_statuses
from ..frames.group_membership import GroupMembershipError
from ..frames.invitation_email import DELIVERY_PROVIDER_ACCEPTED, DELIVERY_UNKNOWN
from ..frames.invitations import (
    InvitationService,
    InvitationsUnavailableError,
    LiveInvitationExists,
    effective_status,
)
from ..frames.model_catalog import ModelCatalogError
from ..frames.platform_role_admin import PlatformRoleChangeRefused
from ..frames.usage import UsageStore, UsageUnavailableError
from ..user_directory import UserDirectoryClient, UserDirectoryUnavailableError
from ..web.authz import require_csrf, require_operator, resolve_platform_role
from ..web.operator import operator_context
from ..web.session import WebSession
from ..web.surface import ADMIN_API_PREFIX
from .admin import issue_invitation, revoke_invitation

logger = logging.getLogger("frames_server.web")

DEFAULT_PAGE_SIZE = 50
INVITATION_LISTING_LIMIT = 100


def _active_role_rows(request: Request, user_ids: list[str]) -> dict[str, dict]:
    """The *active* role rows for a page of people, read in one round trip.

    Read through the org store, the same table the authorization path reads,
    so the panel and the request path agree about who is an operator. A
    revoked row is dropped here: it grants nothing, so it shows as no role.
    """

    store = request.app.state.org_store
    rows = store.get_platform_role_rows(user_ids)
    return {user_id: row for user_id, row in rows.items() if row["status"] == "active"}


def _running_version() -> str:
    """The version of the package actually running, or a truthful placeholder.

    Read from installed metadata rather than a constant in the source: a
    hand-maintained version string drifts from the build, and this one gets
    quoted in incident reports. ``PackageNotFoundError`` happens when the app
    is run from a source tree that was never installed, and "unknown" is a
    better answer there than a number that was right once.
    """

    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("collab-hub-api")
    except PackageNotFoundError:
        return "unknown"


class InvitationRequest(BaseModel):
    """One address to invite.

    Bounded here as well as by the form on the server-rendered page: this
    endpoint is reachable without that page, so its own limits have to hold.
    """

    email: str = Field(min_length=3, max_length=320)


class ConnectorSwitch(BaseModel):
    """Turn one connector on or off."""

    enabled: bool


class RoleChange(BaseModel):
    """Grant or take away the operator role for one person."""

    action: Literal["grant", "revoke"]
    user_label: str | None = Field(default=None, max_length=256)


class ModelAccessChange(BaseModel):
    """One membership change, as the panel asks for it.

    ``action`` is a closed set rather than a boolean: a future third outcome
    should have to be added here, where the audit vocabulary can be extended to
    match, instead of being smuggled in as a flag.
    """

    user_id: str = Field(min_length=1, max_length=256)
    group_path: str = Field(min_length=1, max_length=512)
    action: Literal["grant", "revoke"]
    user_label: str | None = Field(default=None, max_length=256)


__all__ = ["make_router"]


def make_router() -> APIRouter:
    router = APIRouter(
        prefix=ADMIN_API_PREFIX,
        include_in_schema=False,
        dependencies=[Depends(require_operator)],
    )

    @router.get("/session")
    def admin_session(request: Request, session: WebSession = Depends(require_operator)) -> dict:
        """Who the panel is talking to, and the token its writes must carry.

        The CSRF secret lives inside the signed, HttpOnly session cookie, which
        JavaScript cannot read by design -- so something has to hand it over,
        and this is that something. It is not a second credential: presenting
        the token without the cookie proves nothing, and the cookie is what
        authenticates.

        The role is **resolved**, not asserted. ``require_operator`` has
        already refused anyone else, so in practice this is always
        ``operator`` -- but returning the resolution's own answer keeps this a
        report of a fact rather than a restatement of an assumption, which is
        the same rule :func:`~..web.operator.operator_context` keeps.
        """

        return {
            "user": session.user,
            "name": session.name,
            "email": session.email,
            "email_verified": session.email_verified,
            "role": resolve_platform_role(request, session.user),
            "csrf_token": session.csrf,
            "version": _running_version(),
            "features": request.app.state.features.enabled_names,
        }

    @router.get("/invitations")
    def admin_invitations(
        service: Annotated[InvitationService, Depends(get_invitation_service)],
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        """One page of every invitation on this deployment, newest first.

        The same listing the server-rendered page shows, read through the same
        service call, so the panel and that page cannot disagree about what
        exists. Issuing and revoking are the two endpoints below.

        ``next_offset`` is where the following page starts, or ``None`` on the
        last page. It is an offset because that is what the service pages by:
        an invitation issued while someone is paging shifts later pages by one.

        No secret appears here, and none can: the service returns invitation
        rows, and the raw token exists only as the return value of creating
        one.
        """

        try:
            page = service.list_all(limit=INVITATION_LISTING_LIMIT, offset=offset)
            now = service.server_now()
        except InvitationsUnavailableError:
            return JSONResponse({"error": "invitations_unavailable"}, status_code=503)

        return {
            "invitations": [
                {
                    "id": row.id,
                    "email": row.email,
                    "status": effective_status(row, now),
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                    "expires_at": row.expires_at.isoformat() if row.expires_at else None,
                }
                for row in page.invitations
            ],
            "has_more": page.has_more,
            "next_offset": offset + len(page.invitations) if page.has_more else None,
        }

    @router.post("/invitations", status_code=201)
    def admin_issue_invitation(
        request: Request,
        body: InvitationRequest,
        actor: Annotated[object, Depends(operator_context)],
        service: Annotated[InvitationService, Depends(get_invitation_service)],
        delivery: Annotated[object, Depends(get_invitation_email_delivery)],
        _csrf: Annotated[object, Depends(require_csrf)],
    ):
        """Invite one address, and send them the link.

        The same in-process call the server-rendered page makes, so the two
        cannot drift: one live invitation per address, enforced inside the
        audited transaction under an advisory lock, and the organization
        created on acceptance with the accepter as its owner.

        **The secret never appears in the response.** It is read once, handed
        to the mail adapter, and dropped -- exactly as on the page. A token
        that travelled two routes would double its exposure and make "was it
        sent?" ambiguous.

        Sending is sequenced after the transaction commits, because a send
        cannot be rolled back. The residual risk runs the other way -- a
        committed invitation whose email failed -- which is why the outcome is
        reported rather than flattened into success.
        """

        address = body.email.strip()
        if "@" not in address or address.startswith("@") or address.endswith("@"):
            return JSONResponse({"outcome": "invalid_email"}, status_code=400)

        try:
            outcome = issue_invitation(actor, service, email=address)
        except InvitationsUnavailableError:
            return JSONResponse({"outcome": "unavailable"}, status_code=503)

        if isinstance(outcome, LiveInvitationExists):
            return JSONResponse(
                {"outcome": "already_live", "email": outcome.existing.email},
                status_code=409,
            )

        invitation = outcome.invitation
        delivered = delivery.deliver(
            invitation_id=invitation.id,
            recipient=invitation.email,
            invitation_secret=outcome.raw_secret.reveal(),
            organization_name=None,
            expires_at=invitation.expires_at,
        )
        if delivered.status == DELIVERY_PROVIDER_ACCEPTED:
            result = "sent"
        elif delivered.status == DELIVERY_UNKNOWN:
            result = "send_unknown"
        else:
            result = "send_failed"

        logger.info(
            "admin_api_invitation_issued",
            extra={"invitation_id": invitation.id, "delivery_status": delivered.status},
        )
        return JSONResponse({"outcome": result, "email": invitation.email}, status_code=201)

    @router.post("/invitations/{invitation_id}/revoke")
    def admin_revoke_invitation(
        invitation_id: str,
        actor: Annotated[object, Depends(operator_context)],
        service: Annotated[InvitationService, Depends(get_invitation_service)],
        _csrf: Annotated[object, Depends(require_csrf)],
    ):
        """Revoke one invitation. Revoking twice is a no-op success."""

        try:
            revoke_invitation(actor, service, invitation_id=invitation_id)
        except InvitationsUnavailableError:
            return JSONResponse({"outcome": "unavailable"}, status_code=503)
        return {"outcome": "revoked"}

    @router.get("/models")
    def admin_models(request: Request):
        """The models this hub serves, and which group gates each.

        The catalogue is the serving layer's and is never copied here. When it
        cannot be read the section degrades: ``models`` is empty and
        ``catalog_error`` says why, so the rest of the panel keeps working and
        an operator is told the difference between "no models" and "could not
        ask".
        """

        catalog = getattr(request.app.state, "model_catalog", None)
        groups: dict = getattr(request.app.state, "model_groups", {})
        if catalog is None or not catalog.configured:
            return {"models": [], "catalog_error": "not_configured", "manageable": False}
        try:
            models = catalog.list_models()
        except ModelCatalogError:
            return {"models": [], "catalog_error": "unavailable", "manageable": False}

        access = getattr(request.app.state, "model_access", None)
        return {
            "models": [
                {"id": model.id, "owned_by": model.owned_by, "group_path": groups.get(model.id)} for model in models
            ],
            "catalog_error": None,
            "manageable": access is not None,
        }

    @router.get("/model-access")
    def admin_model_access(
        request: Request,
        group_path: Annotated[str, Query(max_length=512)],
    ):
        """Who is currently in one model's access group."""

        access = getattr(request.app.state, "model_access", None)
        if access is None:
            return JSONResponse({"error": "model_access_unavailable"}, status_code=503)
        try:
            members = access.list_members(group_path)
        except GroupMembershipError as exc:
            return JSONResponse({"error": "group_unavailable", "detail": str(exc)}, status_code=502)
        return {
            "group_path": group_path,
            "members": [{"id": member.id, "username": member.username, "email": member.email} for member in members],
        }

    @router.post("/model-access")
    def admin_change_model_access(
        request: Request,
        body: ModelAccessChange,
        actor: Annotated[object, Depends(operator_context)],
        _csrf: Annotated[object, Depends(require_csrf)],
    ):
        """Add or remove one person from one model's access group.

        The change lands in Keycloak, which is what the serving gateway reads,
        and is recorded here. Nothing about enforcement lives in this process.
        """

        access = getattr(request.app.state, "model_access", None)
        if access is None:
            return JSONResponse({"error": "model_access_unavailable"}, status_code=503)
        action = access.grant if body.action == "grant" else access.revoke
        try:
            action(
                actor,
                user_id=body.user_id,
                group_path=body.group_path,
                user_label=body.user_label,
            )
        except GroupMembershipError as exc:
            return JSONResponse({"error": "group_unavailable", "detail": str(exc)}, status_code=502)
        return {"ok": True}

    @router.get("/users")
    def admin_users(
        request: Request,
        directory: Annotated[UserDirectoryClient, Depends(get_user_directory_client)],
        query: Annotated[str | None, Query(max_length=256)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = DEFAULT_PAGE_SIZE,
        first: Annotated[int, Query(ge=0)] = 0,
    ):
        """People this hub knows, with the authority this hub holds for them.

        One page at a time: ``next_first`` is where the following page starts,
        or ``None`` on the last. One extra person is asked for to tell the two
        apart, so a page that happens to end exactly at the last person does
        not offer an empty "next".

        Two sources, joined here and nowhere else: the directory knows who
        exists, and only this deployment knows who is an operator. Neither can
        answer the panel's question alone.

        ``role_source`` travels with the role because the two behave
        differently: a ``manual`` grant outlives the admin group, while an
        ``idp`` one is restored at the holder's next sign-in if the group still
        lists them. An administrator deciding whether to revoke somebody needs
        to know which they are looking at.
        """

        try:
            people = directory.search_users(query, limit=limit + 1, first=first)
        except UserDirectoryUnavailableError:
            return JSONResponse({"error": "user_directory_unavailable"}, status_code=503)

        more = len(people) > limit
        people = people[:limit]
        roles = _active_role_rows(request, [person.id for person in people])
        return {
            "users": [
                {
                    "id": person.id,
                    "username": person.username,
                    "email": person.email,
                    "role": roles.get(person.id, {}).get("role"),
                    "role_source": roles.get(person.id, {}).get("source"),
                }
                for person in people
            ],
            "next_first": first + limit if more else None,
            "manageable": getattr(request.app.state, "platform_role_admin", None) is not None,
        }

    @router.post("/users/{user_id}/role")
    def admin_change_role(
        request: Request,
        user_id: str,
        body: RoleChange,
        actor: Annotated[object, Depends(operator_context)],
        _csrf: Annotated[object, Depends(require_csrf)],
    ):
        """Grant or revoke the operator role.

        A grant is recorded as hand-administered, so the identity-provider sync
        will not remove it later; a revoke reaches any role row whatever its
        source. Both are explained in :mod:`..frames.platform_role_admin`, and
        the panel repeats the consequence where the button is.
        """

        admin = getattr(request.app.state, "platform_role_admin", None)
        if admin is None:
            return JSONResponse({"error": "role_management_unavailable"}, status_code=503)

        change = admin.grant if body.action == "grant" else admin.revoke
        try:
            change(actor, user_id=user_id, user_label=body.user_label)
        except PlatformRoleChangeRefused as exc:
            return JSONResponse({"error": exc.reason}, status_code=409)
        return {"ok": True, "action": body.action}

    @router.get("/connectors")
    def admin_connectors(request: Request):
        """Which connectors this deployment has, and how they authenticate.

        Read-only on purpose; see :mod:`..frames.connector_status`. No secret
        value is rendered, and the response carries no field that could hold
        one.
        """

        # The unfiltered configuration, deliberately: this screen must show a
        # connector that is configured but switched off as exactly that. Asking
        # the request-path dependency would show it as unconfigured, which is
        # what every *other* caller should see and the one thing this caller
        # must not.
        connectors = getattr(request.app.state, "connectors_config", None)
        store = getattr(request.app.state, "connector_store", None)
        disabled: set[str] = set()
        if store is not None:
            try:
                disabled = store.disabled()
            except Exception:
                disabled = set()

        return {
            "connectors": [
                {
                    "key": status.key,
                    "label": status.label,
                    "configured": status.configured,
                    "credential": status.credential,
                    "probeable": status.probeable,
                    "enabled": status.key not in disabled,
                }
                for status in connector_statuses(connectors)
            ],
            "switchable": store is not None,
        }

    @router.post("/connectors/{connector}")
    def admin_switch_connector(
        request: Request,
        connector: str,
        body: ConnectorSwitch,
        actor: Annotated[object, Depends(operator_context)],
        _csrf: Annotated[object, Depends(require_csrf)],
    ):
        """Switch one connector on or off.

        This writes one bit. It cannot supply a credential, so it can never
        turn on a connector this deployment was not configured for -- turning
        an unconfigured connector "on" leaves it exactly as unusable as it was.

        Switching off takes effect on the next request, because the enforcing
        dependency reads this table rather than caching it.
        """

        if connector not in CONNECTOR_KEYS:
            return JSONResponse({"error": "unknown_connector"}, status_code=404)

        store = getattr(request.app.state, "connector_store", None)
        if store is None:
            return JSONResponse({"error": "connector_switch_unavailable"}, status_code=503)

        store.set_enabled(actor, connector=connector, enabled=body.enabled)
        return {"ok": True, "connector": connector, "enabled": body.enabled}

    @router.get("/usage")
    def admin_usage(
        usage_store: Annotated[UsageStore, Depends(get_usage_store)],
        since: Annotated[AwareDatetime | None, Query()] = None,
        until: Annotated[AwareDatetime | None, Query()] = None,
    ):
        """Activity across every organization on this deployment.

        The workspace-scoped endpoints under ``/v1/usage`` are untouched and
        still answer for the caller's own tenant. This one answers the question
        only an operator has, and it is the only place the two differ: there is
        no tenant to pass, because the whole hub is the scope.

        The window bounds events. The people count is current state, so asking
        about last week does not make the hub smaller.
        """

        try:
            summary = usage_store.hub_summary(since=since, until=until)
        except UsageUnavailableError:
            return JSONResponse({"error": "usage_unavailable"}, status_code=503)

        return {
            "users_total": summary.users_total,
            "events_total": summary.events_total,
            "events": [{"event": event, "count": count} for event, count in summary.events],
            "organizations": [
                {"org_id": org.org_id, "users": org.users, "events": org.events} for org in summary.organizations
            ],
        }

    @router.get("/audit")
    def admin_audit(
        audit_log: Annotated[AuditLog, Depends(get_audit_log)],
        limit: Annotated[int, Query(ge=1, le=200)] = DEFAULT_PAGE_SIZE,
        before_id: Annotated[int | None, Query(ge=1)] = None,
        actor: Annotated[str | None, Query(max_length=256)] = None,
        action: Annotated[str | None, Query(max_length=256)] = None,
    ):
        """One page of recorded actions, newest first.

        ``before_id`` is the cursor the previous page handed back, not a page
        number: the log is appended to while it is being read, and a page
        number would silently repeat or skip entries as rows land above it.

        A deployment with no database answers 503 rather than an empty page.
        Somebody reading this after an incident must never be told "nothing was
        recorded" when the truth is "the record cannot be reached".
        """

        try:
            page = audit_log.list_events(limit=limit, before_id=before_id, actor=actor, action=action)
        except AuditLogUnavailableError:
            return JSONResponse({"error": "audit_log_unavailable"}, status_code=503)

        return {
            "entries": [
                {
                    "id": entry.id,
                    "at": entry.at.isoformat(),
                    "actor": entry.actor,
                    "actor_label": entry.actor_label,
                    "action": entry.action,
                    "target_type": entry.target_type,
                    "target_id": entry.target_id,
                    "target_label": entry.target_label,
                    "org_id": entry.org_id,
                    "detail": entry.detail,
                }
                for entry in page.entries
            ],
            "next_before_id": page.next_before_id,
        }

    return router
