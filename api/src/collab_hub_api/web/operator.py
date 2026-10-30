"""The operator-action pattern: how a browser page performs a privileged act.

Issue #91 is the first operator action on this surface, and Gate E's decision
was that it exists as much to set the template as to send invitations. This
module is the template's reusable half. **A future operator page should need
nothing from here but :func:`operator_context`, and should add nothing to it.**

The shape, in four parts
------------------------
1. **The page router is gated by the role, not by the handler.**
   ``session_gated_router(dependencies=[Depends(require_operator)])`` — or the
   same dependency on the router #91 builds — so every route on it, GET and
   POST alike, refuses a signed-in non-operator with the surface's own 403
   page. A refusal is a real answer to a real person: not a 404, not a blank
   page, not an API error envelope.

2. **The action is authorized again, by #87's wrapper, on the function that
   performs it.** The handler calls a plain function decorated with
   ``@requires_platform_role(PLATFORM_ROLE_OPERATOR)``. That is not
   belt-and-braces theatre: the router dependency protects the *route*, and
   the decorator protects the *action*, which outlives any particular route
   and can be called from a future CLI or MCP tool. If the router dependency
   were ever dropped, the action still refuses.

3. **The authority the decorator reads is resolved, never asserted.**
   :func:`operator_context` builds the :class:`~..frames.auth.AuthContext` with
   whatever :func:`~.authz.resolve_platform_role` actually answered — it does
   **not** stamp ``platform_role="operator"`` because the request got this far.
   Fabricating the value would make #87's check a comparison of a constant
   against itself, which is worse than no check because it reads as one. The
   distinction is the single most important line in this module.

4. **The event row comes from the shared primitive, through the service.**
   No page code writes ``collab_audit_events``; ``audited()`` is its only
   writer, composed inside the invitation lifecycle service. A page that had
   to write its own row would mean the foundation was wrong, and the fix would
   be the foundation.

What this module deliberately is not
------------------------------------
There is no ``@operator_action`` decorator that authorizes *and* records. #87
separated those two halves on purpose: an organization owner and a platform
operator perform some of the same recorded actions, so fusing them would
either lock owners out of the audit primitive or make the authorization half
meaningless. This module keeps them separate and just makes the operator half
convenient.

There is also no cross-org browsing, member management, or org deletion here,
and none should be added. Gate E scoped the operator surface for this beta to
invitation issuance exactly, and a helper module is precisely where extra
capability arrives without being noticed.
"""

from __future__ import annotations

from fastapi import Depends, Request

from ..frames.auth import WORKSPACE_DEFAULT, AuthContext, DisplayIdentity
from .authz import require_operator, resolve_platform_role
from .session import WebSession

__all__ = ["operator_context"]


def operator_context(request: Request, session: WebSession = Depends(require_operator)) -> AuthContext:
    """The caller's :class:`~..frames.auth.AuthContext`, for an operator action.

    The bridge between the two authentication axes: the browser surface
    authenticates people with a session cookie, while every privileged action
    in this system — and both of #87's authorization wrappers, and the audited
    primitive's actor columns — speak ``AuthContext``. Building one here is
    what lets an operator page call the same service methods the API routes
    call, with the same authorization and the same audit rows, differing only
    in how the caller was authenticated.

    Three fields and why each is what it is:

    ``platform_role``
        Whatever :func:`~.authz.resolve_platform_role` answered on **this**
        request — canonically ``OrgStore.resolve_principal``, i.e. #87's
        ``collab_platform_roles`` table, with a revoked grant already collapsed
        to ``None``. Resolved, never asserted; see part 3 of the module
        docstring. ``require_operator`` has already refused a non-operator with
        the 403 page, so in practice this is ``"operator"`` — but it is the
        resolution's answer, so #87's guard on the action is deciding a fact
        rather than restating an assumption.

    ``home_org_id=None``
        Hub scope. An operator invitation belongs to no organization (the
        organization is created on acceptance), and the audit row's scope is
        passed to ``audited()`` explicitly rather than defaulted from the
        caller. ``None`` also fails **closed**: ``AuthContext.org_id`` raises
        :class:`~..frames.auth.NoOrganizationError`, so any future code that
        reads this context as though it were org-scoped raises instead of
        silently acting inside whichever organization the operator happens to
        belong to.

    ``display``
        The session's name, address, and verified flag, which become the audit
        row's ``actor_label``. Display only — the ACL principal is ``user``,
        and nothing here changes that.

    ``resolve_platform_role`` raises :class:`~.authz.WebAuthorizationUnavailable`
    when the deployment has no platform-role source at all. That propagates: a
    deployment that cannot decide operator authority must say so loudly (503
    page, error log) rather than answer a plain 403 that is indistinguishable
    from a correct refusal.
    """

    return AuthContext(
        user=session.user,
        home_org_id=None,
        workspace_id=WORKSPACE_DEFAULT,
        display=DisplayIdentity(
            name=session.name,
            email=session.email,
            email_verified=session.email_verified,
        ),
        org_role=None,
        platform_role=resolve_platform_role(request, session.user),
    )
