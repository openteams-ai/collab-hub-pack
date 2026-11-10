"""The owner-action pattern: how a browser page acts inside one organization.

Issue #142's owner invitation page is the first owner action on this surface,
and it follows the template issue #91 set for operator actions — see
:mod:`.operator`, whose four-part shape this module instantiates for the org
axis. **A future owner page should need nothing from here but
:func:`owner_context`, and should add nothing to it.**

What is the same, and what is different
---------------------------------------
Same: the page router is gated by the role
(``Depends(require_org_owner)``), the action is authorized again by #87's
wrapper on the function that performs it (``@requires_org_role``), the
authority the wrapper reads is **resolved from the org store on this
request** and never asserted, and no page code writes an audit row —
``audited()`` remains the only writer, composed inside the invitation
lifecycle service.

Different, and worth stating because the operator module says its context
should not grow: the org axis needs one more resolved fact than the platform
axis — *which* organization the caller owns. ``requires_org_role(...,
org_arg=...)`` pins every action to an explicit org id compared against the
context's ``home_org_id``, and the page's ``GET`` needs the same id to scope
its listing. So :func:`owner_context` refuses — with the surface's own 403
page, via :class:`~.authz.WebForbidden` — when the resolution does not come
back an active owner membership, where :func:`~.operator.operator_context`
carries whatever role it found and lets the action's guard answer. The
operator page's ``GET`` reads nothing role-scoped, so a torn read there costs
nothing; here it would either crash the listing (no org to name) or scope it
by a membership that just stopped being true. Refusing on the resolved fact
is not asserting the fact: ``org_role`` and ``home_org_id`` in the returned
context are still exactly what the store answered, and the action's guard
still compares them itself.

``platform_role`` is ``None``, deliberately unresolved rather than resolved.
No owner action reads it, resolving it would couple every owner page to the
platform-role source's availability (:class:`~.authz.WebAuthorizationUnavailable`
on deployments that cannot answer it), and ``None`` fails closed on any
operator guard a future edit might route this context into.
"""

from __future__ import annotations

from fastapi import Depends, Request

from ..frames.auth import WORKSPACE_DEFAULT, AuthContext, DisplayIdentity
from ..frames.orgs import ROLE_OWNER, OrgMembership, OrgsUnavailableError
from .authz import WebForbidden, require_org_owner
from .session import WebSession

__all__ = ["owner_context"]


def _resolved_membership(request: Request, user: str) -> OrgMembership | None:
    """The caller's membership row, from the server's own store, or ``None``.

    A store outage propagates as :class:`~..frames.orgs.OrgsUnavailableError`
    (a 503 through the app's handlers) — "cannot answer" must never quietly
    become "not an owner".
    """

    store = getattr(request.app.state, "org_store", None)
    if store is None:
        raise OrgsUnavailableError("Organization storage is not available on this app")
    return store.get_membership(user)


def owner_context(request: Request, session: WebSession = Depends(require_org_owner)) -> AuthContext:
    """The caller's :class:`~..frames.auth.AuthContext`, for an owner action.

    The same bridge :func:`~.operator.operator_context` is: the browser
    surface authenticates people with a session cookie, while every
    privileged action — both of #87's wrappers, and the audited primitive's
    actor columns — speaks ``AuthContext``. Building one here lets an owner
    page call the same service methods the ``/v1/orgs/{org_id}/…`` routes
    call, with the same authorization and the same audit rows, differing only
    in how the caller was authenticated.

    ``home_org_id`` and ``org_role`` are whatever the org store answered on
    **this** request — resolved, never stamped. ``require_org_owner`` has
    already refused a non-owner with the 403 page, so in practice the role is
    ``owner`` — but it is the resolution's answer, so #87's guard on the
    action is deciding a fact rather than restating an assumption. The
    membership is read again here, after the router dependency's read, and
    that repetition is the same intended cost the operator page documents.

    The one refusal this function adds — :class:`~.authz.WebForbidden` when
    the second read does not come back an active owner row — covers the torn
    read between the two resolutions; the module docstring holds the argument
    for why the owner axis needs it when the operator axis does not.
    """

    membership = _resolved_membership(request, session.user)
    if membership is None or not membership.is_active or membership.role != ROLE_OWNER:
        raise WebForbidden(f"user {session.user!r} is no longer an active organization owner")
    return AuthContext(
        user=session.user,
        home_org_id=membership.org_id,
        workspace_id=WORKSPACE_DEFAULT,
        display=DisplayIdentity(
            name=session.name,
            email=session.email,
            email_verified=session.email_verified,
        ),
        org_role=membership.role,
        platform_role=None,
    )
