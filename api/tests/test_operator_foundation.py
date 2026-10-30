"""Operator foundation (issue #87): the two authority axes and the audited
transaction-bound execution primitive.

Coverage maps onto the R9 acceptance list:

1. **The axes are independent and each fails closed.** A non-operator cannot
   pass a platform-operator check and a non-owner cannot pass an org-owner
   check — tested separately, plus the cross cases (an operator is not
   thereby an owner, an owner is not thereby an operator).
2. **The mutation and its event row commit or roll back together** — proven
   on a live Postgres by forcing a failure on each side of the transaction
   and confirming neither write is left behind, not by inspection. The body
   cannot complete the transaction itself: the guarded connection refuses
   commit/rollback/close/autocommit, and the whole scope runs in an explicit
   transaction block only ``audited()`` can complete.
3. **Owner- and operator-initiated performances of the same action produce
   identical event rows** apart from the actor columns.
4. **No code path updates or deletes ``collab_audit_events``** — asserted
   against the source tree (the ACL cannot enforce it: the application role
   owns the table).
5. **Rows survive a restart** (read back through a brand-new pool) and are
   readable by the documented psql procedure.
6. **The documented bootstrap procedure works**: the runbook's insert grants
   an operator that the auth choke point resolves, recorded as
   ``operator.manual`` — and that operator can act **without belonging to
   any organization** (the hub-scoped context issue #89's first invite
   depends on), while every org-scoped surface fails closed for them.

Live-Postgres tests opt in with ``COLLAB_HUB_TEST_POSTGRES_URL`` exactly like
``test_collab_schema.py``; everything else runs everywhere.
"""

from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

psycopg = pytest.importorskip("psycopg")

from fake_audit_db import FakeAuditDatabase  # noqa: E402
from psycopg import pq, sql  # noqa: E402

from collab_hub_api.config import Config  # noqa: E402
from collab_hub_api.core import make_app  # noqa: E402
from collab_hub_api.frames import audit as audit_module  # noqa: E402
from collab_hub_api.frames import error_codes  # noqa: E402
from collab_hub_api.frames.audit import (  # noqa: E402
    AUDIT_ACTIONS,
    AUDIT_DETAIL_MAX_BYTES,
    AUDIT_ID_MAX_CHARS,
    AUDIT_LABEL_MAX_CHARS,
    AUDIT_TARGET_TYPES,
    AuditTransactionBrokenError,
    AuditTransactionViolation,
    InvalidAuditFieldError,
    UnknownAuditActionError,
    UnknownAuditTargetTypeError,
    audited,
)
from collab_hub_api.frames.auth import (  # noqa: E402
    WORKSPACE_DEFAULT,
    AuthContext,
    DisplayIdentity,
    NoOrganizationError,
    auth_context_from_claims,
    auth_context_from_membership,
)
from collab_hub_api.frames.authorization import (  # noqa: E402
    requires_org_role,
    requires_platform_role,
    verify_protected_routes,
)
from collab_hub_api.frames.collab_schema import (  # noqa: E402
    COLLAB_SCHEMA_MIGRATIONS,
    run_collab_schema_migrations,
)
from collab_hub_api.frames.identity import IDENTITY_CLAIM_ENV  # noqa: E402
from collab_hub_api.frames.org_source import (  # noqa: E402
    DEFAULT_ORG_ENV,
    DEFAULT_WORKSPACE_ENV,
    ORG_SOURCE_ENV,
)
from collab_hub_api.frames.orgs import (  # noqa: E402
    PLATFORM_ROLE_OPERATOR,
    ROLE_MEMBER,
    ROLE_OWNER,
    InMemoryOrgStore,
    OrgsUnavailableError,
    PostgresOrgStore,
    UnavailableOrgStore,
)
from collab_hub_api.frames.service_access_state import claim_pending  # noqa: E402

OPERATOR = "0p3r4t0r-sub-1111-4111-8111-abcdefabcdef"
OWNER = "owner-sub-2222-4222-8222-abcdefabcdef"
MEMBER = "member-sub-3333-4333-8333-abcdefabcdef"

ORG = "org-aaaa"
OTHER_ORG = "org-bbbb"


def ctx(
    user: str = MEMBER,
    *,
    home_org_id: str | None = ORG,
    org_role: str | None = ROLE_MEMBER,
    platform_role: str | None = None,
    email: str | None = None,
) -> AuthContext:
    return AuthContext(
        user=user,
        home_org_id=home_org_id,
        workspace_id=WORKSPACE_DEFAULT,
        display=DisplayIdentity(email=email),
        org_role=org_role,
        platform_role=platform_role,
    )


OPERATOR_CTX = ctx(OPERATOR, org_role=ROLE_MEMBER, platform_role=PLATFORM_ROLE_OPERATOR)
OWNER_CTX = ctx(OWNER, org_role=ROLE_OWNER)
MEMBER_CTX = ctx(MEMBER, org_role=ROLE_MEMBER)
CLAIMS_CTX = ctx("claims-sub", org_role=None, platform_role=None)
# The bootstrap shape (issue #89's first invite): an operator with no home
# organization at all.
HUB_OPERATOR_CTX = ctx(OPERATOR, home_org_id=None, org_role=None, platform_role=PLATFORM_ROLE_OPERATOR)


# ---------------------------------------------------------------------------
# Layer 2: the authorization wrappers, one per axis
# ---------------------------------------------------------------------------


def test_platform_role_wrapper_admits_exactly_the_operator():
    calls: list[str] = []

    @requires_platform_role("operator")
    def action(auth: AuthContext) -> str:
        calls.append(auth.user)
        return "done"

    assert action(OPERATOR_CTX) == "done"
    assert action(HUB_OPERATOR_CTX) == "done"
    assert calls == [OPERATOR, OPERATOR]

    for denied in (OWNER_CTX, MEMBER_CTX, CLAIMS_CTX):
        with pytest.raises(HTTPException) as excinfo:
            action(denied)
        assert excinfo.value.status_code == 403
    # The wrapped function never ran for a denied caller.
    assert calls == [OPERATOR, OPERATOR]


def test_org_role_wrapper_admits_the_owner_of_the_target_org_only():
    calls: list[str] = []

    @requires_org_role("owner", org_arg="org_id")
    def action(auth: AuthContext, org_id: str) -> str:
        calls.append(auth.user)
        return "done"

    assert action(OWNER_CTX, org_id=ORG) == "done"

    # A member of the right org is not an owner.
    with pytest.raises(HTTPException) as excinfo:
        action(MEMBER_CTX, org_id=ORG)
    assert excinfo.value.status_code == 403

    # An owner of org A is nobody in org B: same plain 403, not a 404 and not
    # an error that reveals the org exists.
    with pytest.raises(HTTPException) as excinfo:
        action(OWNER_CTX, org_id=OTHER_ORG)
    assert excinfo.value.status_code == 403

    # Claims-sourced auth carries no org role at all.
    with pytest.raises(HTTPException):
        action(CLAIMS_CTX, org_id=ORG)

    assert calls == [OWNER]


def test_the_two_axes_are_independent():
    """An operator is not thereby an owner, and an owner is not an operator."""

    @requires_platform_role("operator")
    def platform_action(auth: AuthContext) -> str:
        return "platform"

    @requires_org_role("owner", org_arg="org_id")
    def org_action(auth: AuthContext, org_id: str) -> str:
        return "org"

    # The operator holds no owner role, in this or any org — including the
    # hub-scoped operator, whose org_role is None and who must get a plain
    # 403 (never a NoOrganizationError leaking from inside the check).
    for operator in (OPERATOR_CTX, HUB_OPERATOR_CTX):
        for org in (ORG, OTHER_ORG):
            with pytest.raises(HTTPException) as excinfo:
                org_action(operator, org_id=org)
            assert excinfo.value.status_code == 403
            assert not isinstance(excinfo.value, NoOrganizationError)

    # The owner holds no platform role.
    with pytest.raises(HTTPException):
        platform_action(OWNER_CTX)

    assert platform_action(OPERATOR_CTX) == "platform"
    assert org_action(OWNER_CTX, org_id=ORG) == "org"


def test_org_role_wrapper_without_org_arg_scopes_to_the_callers_own_role():
    @requires_org_role("owner")
    def action(auth: AuthContext) -> str:
        return "done"

    assert action(OWNER_CTX) == "done"
    with pytest.raises(HTTPException):
        action(MEMBER_CTX)
    with pytest.raises(HTTPException):
        action(HUB_OPERATOR_CTX)


async def test_wrappers_support_async_callables():
    @requires_platform_role("operator")
    async def platform_action(auth: AuthContext) -> str:
        return "platform"

    @requires_org_role("owner", org_arg="org_id")
    async def org_action(auth: AuthContext, org_id: str) -> str:
        return "org"

    assert await platform_action(OPERATOR_CTX) == "platform"
    assert await org_action(OWNER_CTX, org_id=ORG) == "org"
    with pytest.raises(HTTPException):
        await platform_action(MEMBER_CTX)
    with pytest.raises(HTTPException):
        await org_action(OWNER_CTX, org_id=OTHER_ORG)


def test_wrappers_find_the_context_positionally_and_by_keyword():
    @requires_platform_role("operator")
    def action(prefix: str, auth: AuthContext) -> str:
        return prefix

    assert action("by-position", OPERATOR_CTX) == "by-position"
    assert action(prefix="by-keyword", auth=OPERATOR_CTX) == "by-keyword"
    with pytest.raises(HTTPException):
        action("denied", MEMBER_CTX)


def test_wrappers_fail_closed_without_exactly_one_auth_context():
    @requires_platform_role("operator")
    def no_context(name: str) -> str:
        return name

    with pytest.raises(RuntimeError, match="exactly one is required"):
        no_context("anonymous")

    @requires_platform_role("operator")
    def two_contexts(a: AuthContext, b: AuthContext) -> str:
        return "ambiguous"

    with pytest.raises(RuntimeError, match="exactly one is required"):
        two_contexts(OPERATOR_CTX, MEMBER_CTX)

    # A default AuthContext in the signature is a fixture nobody
    # authenticated, not a caller: it must not satisfy the check.
    @requires_platform_role("operator")
    def defaulted(auth: AuthContext = OPERATOR_CTX) -> str:
        return "defaulted"

    with pytest.raises(RuntimeError, match="exactly one is required"):
        defaulted()


def test_org_role_wrapper_fails_closed_on_a_missing_or_empty_target_org():
    @requires_org_role("owner", org_arg="org_id")
    def action(auth: AuthContext, org_id: str | None = None) -> str:
        return "done"

    # "No org supplied" must never widen into "any org allowed".
    with pytest.raises(RuntimeError, match="fails closed"):
        action(OWNER_CTX)
    with pytest.raises(RuntimeError, match="fails closed"):
        action(OWNER_CTX, org_id="")


def test_wrapper_misuse_fails_at_decoration_time_not_at_request_time():
    # A typo'd role would deny everyone forever while looking strict.
    with pytest.raises(ValueError, match="Unknown platform role"):
        requires_platform_role("admin")
    with pytest.raises(ValueError, match="Unknown org role"):
        requires_org_role("superuser")

    # A typo'd org_arg would fail closed on every call in production.
    with pytest.raises(ValueError, match="no parameter"):

        @requires_org_role("owner", org_arg="organization")
        def action(auth: AuthContext, org_id: str) -> str:
            return "done"


# ---------------------------------------------------------------------------
# Decorator order: misuse is detectable, not silently unguarded
# ---------------------------------------------------------------------------


def test_a_misordered_guard_leaves_the_route_unguarded_and_is_detected():
    """The codex probe: @requires_... ABOVE @router.post registers the raw fn.

    Decorators apply bottom-up, so the route decorator runs first and
    registers the original; the guard then wraps an object nobody calls.
    verify_protected_routes must catch exactly this.
    """

    router = APIRouter()

    @requires_platform_role("operator")
    @router.post("/misordered")
    def misordered(auth: AuthContext) -> str:
        return "unguarded"

    app = FastAPI()
    app.include_router(router)
    route = next(r for r in app.routes if getattr(r, "path", "") == "/misordered")
    # The registered endpoint is NOT the guarded callable...
    assert route.endpoint is not misordered
    # ...and the registered endpoint enforces nothing.
    assert route.endpoint(MEMBER_CTX) == "unguarded"

    with pytest.raises(RuntimeError, match="ORPHANED.*misordered.*requires_platform_role"):
        verify_protected_routes(app)


def test_a_partially_misordered_stack_is_detected():
    """Codex's round-2 reproduction: inner guard registered, outer orphaned.

    The org guard sits correctly below @router.post, so the route holds a
    genuine guard wrapper — which is why an "is the endpoint a guard?" rule
    passes it. But the platform guard sits ABOVE the route decorator, wraps
    the already-registered object, and enforces nothing: an owner with no
    platform role sails through. The verifier must flag exactly this.
    """

    router = APIRouter()

    @requires_platform_role("operator")
    @router.post("/partial")
    @requires_org_role("owner", org_arg="org_id")
    def partial(auth: AuthContext, org_id: str) -> str:
        return "reached"

    app = FastAPI()
    app.include_router(router)
    route = next(r for r in app.routes if getattr(r, "path", "") == "/partial")

    # The registered endpoint IS a guard wrapper: the org check enforces...
    with pytest.raises(HTTPException):
        route.endpoint(MEMBER_CTX, org_id=ORG)
    # ...but the platform check does not — an owner with no platform role
    # reaches the action. This is the hole.
    assert route.endpoint(OWNER_CTX, org_id=ORG) == "reached"

    with pytest.raises(RuntimeError, match=r"ORPHANED.*partial.*requires_platform_role\('operator'\)"):
        verify_protected_routes(app)


def test_a_fully_ordered_mixed_stack_passes_and_both_guards_enforce():
    """The correct form of the stack above: both guards below @router.post."""

    router = APIRouter()

    @router.post("/both")
    @requires_platform_role("operator")
    @requires_org_role("owner", org_arg="org_id")
    def both(auth: AuthContext, org_id: str) -> str:
        return "reached"

    app = FastAPI()
    app.include_router(router)
    verify_protected_routes(app)  # nothing orphaned

    route = next(r for r in app.routes if getattr(r, "path", "") == "/both")
    assert route.endpoint is both
    # Stacked guards mean BOTH axes must hold, and both are live through the
    # registered endpoint.
    operator_owner = ctx(OPERATOR, org_role=ROLE_OWNER, platform_role=PLATFORM_ROLE_OPERATOR)
    assert route.endpoint(operator_owner, org_id=ORG) == "reached"
    with pytest.raises(HTTPException):
        route.endpoint(OWNER_CTX, org_id=ORG)  # owner, but no platform role
    with pytest.raises(HTTPException):
        route.endpoint(OPERATOR_CTX, org_id=ORG)  # operator, but org member only


def test_a_correctly_ordered_guard_passes_route_verification():
    router = APIRouter()

    @router.post("/guarded")
    @requires_platform_role("operator")
    def guarded(auth: AuthContext) -> str:
        return "guarded"

    app = FastAPI()
    app.include_router(router)
    route = next(r for r in app.routes if getattr(r, "path", "") == "/guarded")
    assert route.endpoint is guarded
    with pytest.raises(HTTPException):
        route.endpoint(MEMBER_CTX)

    verify_protected_routes(app)  # does not raise


def test_route_verification_ignores_unrelated_routes():
    app = FastAPI()

    @app.get("/plain")
    def plain() -> str:
        return "ok"

    verify_protected_routes(app)  # nothing guarded, nothing to flag


# ---------------------------------------------------------------------------
# The auth choke point: one read, both axes, four caller shapes
# ---------------------------------------------------------------------------


def membership_store() -> InMemoryOrgStore:
    store = InMemoryOrgStore()
    store.set_membership(OPERATOR, ORG, role=ROLE_MEMBER)
    store.set_membership(OWNER, ORG, role=ROLE_OWNER)
    return store


def test_an_active_member_with_a_grant_resolves_both_axes():
    store = membership_store()
    store.set_platform_role(OPERATOR)

    resolved = auth_context_from_membership({"sub": OPERATOR}, store)
    assert resolved is not None
    assert resolved.platform_role == PLATFORM_ROLE_OPERATOR
    # The org axis is untouched by the grant.
    assert resolved.org_role == ROLE_MEMBER
    assert resolved.org_id == ORG and resolved.home_org_id == ORG


def test_an_active_member_without_a_grant_or_with_a_revoked_one_has_no_platform_role():
    store = membership_store()
    resolved = auth_context_from_membership({"sub": OWNER}, store)
    assert resolved is not None and resolved.platform_role is None

    store.set_platform_role(OPERATOR, status="revoked")
    resolved = auth_context_from_membership({"sub": OPERATOR}, store)
    assert resolved is not None and resolved.platform_role is None


def test_an_operator_without_membership_gets_a_hub_scoped_context():
    """Issue #89's bootstrap: the first invite happens before any org exists."""

    store = InMemoryOrgStore()
    store.set_platform_role(OPERATOR)

    resolved = auth_context_from_membership({"sub": OPERATOR}, store)
    assert resolved is not None
    assert resolved.platform_role == PLATFORM_ROLE_OPERATOR
    # No organization, and no manufactured org authority.
    assert resolved.home_org_id is None and resolved.org_role is None
    assert resolved.workspace_id == WORKSPACE_DEFAULT
    # Every org-scoped consumer fails closed at the point of use.
    with pytest.raises(NoOrganizationError):
        _ = resolved.org_id

    # A removed membership grants no org context either: the platform axis
    # stands alone, and revoking an operator means revoking the grant.
    store.set_membership(OPERATOR, ORG, role=ROLE_MEMBER, status="removed")
    resolved = auth_context_from_membership({"sub": OPERATOR}, store)
    assert resolved is not None
    assert resolved.home_org_id is None and resolved.platform_role == PLATFORM_ROLE_OPERATOR


def test_a_caller_with_neither_membership_nor_grant_keeps_no_organization():
    store = InMemoryOrgStore()
    with pytest.raises(NoOrganizationError):
        auth_context_from_membership({"sub": MEMBER}, store)

    store.set_membership(MEMBER, ORG, role=ROLE_MEMBER, status="removed")
    with pytest.raises(NoOrganizationError):
        auth_context_from_membership({"sub": MEMBER}, store)

    # A revoked grant is not a grant.
    store.set_platform_role(MEMBER, status="revoked")
    with pytest.raises(NoOrganizationError):
        auth_context_from_membership({"sub": MEMBER}, store)


def test_a_failing_principal_lookup_fails_the_whole_resolution():
    """No partial context: the two axes answer together or not at all."""

    class FailingStore(InMemoryOrgStore):
        def resolve_principal(self, user_id: str):
            raise psycopg.OperationalError("connection refused")

    store = FailingStore()
    store.set_membership(OPERATOR, ORG, role=ROLE_MEMBER)
    with pytest.raises(psycopg.OperationalError):
        auth_context_from_membership({"sub": OPERATOR}, store)


def test_the_unavailable_store_fails_the_principal_read_closed():
    with pytest.raises(OrgsUnavailableError):
        UnavailableOrgStore().resolve_principal(OPERATOR)


def test_claims_sourced_auth_never_carries_a_platform_role(monkeypatch):
    monkeypatch.setenv(DEFAULT_ORG_ENV, "default-org")
    monkeypatch.setenv(DEFAULT_WORKSPACE_ENV, "default")
    resolved = auth_context_from_claims({"sub": OPERATOR, "preferred_username": "op"})
    assert resolved is not None
    assert resolved.platform_role is None and resolved.org_role is None


# ---------------------------------------------------------------------------
# The hub-scoped context on the wire: org surfaces fail closed end to end
# ---------------------------------------------------------------------------


def _jwt(payload: dict) -> str:
    def encode(part: dict) -> str:
        raw = json.dumps(part, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode(payload)}."


def cookies_for(sub: str) -> dict[str, str]:
    return {"IdToken-test": _jwt({"sub": sub, "email": f"{sub[:4]}@example.test"})}


async def test_a_hub_scoped_operator_cannot_reach_org_scoped_endpoints(tmp_path, monkeypatch):
    """End to end: the org_id property's fail-closed answer reaches the wire.

    The hub-scoped operator authenticates (no 401, no no_organization at the
    choke point), but an org-scoped endpoint answers exactly the
    no_organization envelope the moment it reads auth.org_id — proving no
    org-scoped surface can be *used* without an organization, which is the
    guarantee that makes handing out the hub-scoped shape safe at all.
    """

    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_IDTOKEN_ALLOW_UNSIGNED", "true")
    monkeypatch.setenv(IDENTITY_CLAIM_ENV, "sub")
    monkeypatch.setenv(ORG_SOURCE_ENV, "membership")
    monkeypatch.delenv(DEFAULT_ORG_ENV, raising=False)
    monkeypatch.delenv(DEFAULT_WORKSPACE_ENV, raising=False)
    config = Config.parse(
        {
            "storage": {"frames_path": str(tmp_path / "frames")},
            "frames": {
                "active_state": {"backend": "memory"},
                "history": {"backend": "memory"},
                "groups": {"backend": "memory"},
                "usage": {"backend": "memory"},
                "orgs": {"backend": "memory"},
                "mcp_session_manager_enabled": False,
            },
            "tasks": {"backend": "memory"},
        }
    )
    app = make_app(config)
    async with app.router.lifespan_context(app):
        store = app.state.org_store
        store.set_platform_role(OPERATOR)
        store.set_membership(MEMBER, ORG, role=ROLE_MEMBER)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            for path in ("/v1/frames", "/v1/tasks", "/v1/frame-groups"):
                response = await client.get(path, cookies=cookies_for(OPERATOR))
                assert response.status_code == 403, (path, response.text)
                assert response.json()["error"]["code"] == error_codes.NO_ORGANIZATION

            # The same deployment serves an ordinary member normally, so the
            # failure above is the context's shape, not a broken app.
            response = await client.get("/v1/frames", cookies=cookies_for(MEMBER))
            assert response.status_code == 200, response.text

            # The complement, checked rather than implied: the deployment's
            # two authenticated surfaces with *no* org scoping stay REACHABLE
            # to the hub-scoped operator — intentionally so. Neither reads
            # auth.org_id (the directory serves unfiltered deployment-wide
            # reads; every connector endpoint scopes on the caller's own
            # brokered token), so access there is decided by authentication
            # alone, and an operator is the deployment's highest-privilege
            # principal. Reachable means past authentication: not a 401, and
            # never the no_organization refusal.
            class StaticDirectory:
                def search_users(self, query=None, *, limit=50):
                    return []

                def search_groups(self, query=None, *, limit=50):
                    return []

            app.state.user_directory_client = StaticDirectory()
            for path in ("/v1/user-directory/users", "/v1/connectors"):
                response = await client.get(path, cookies=cookies_for(OPERATOR))
                assert response.status_code == 200, (path, response.text)


def test_an_org_read_inside_an_mcp_tool_body_fails_closed_as_a_tool_error(tmp_path, monkeypatch):
    """The one layer that does NOT answer the ``no_organization`` envelope.

    ``McpAuthMiddleware`` maps :class:`NoOrganizationError` only when
    authentication itself raises it. A hub-scoped operator *authenticates*,
    so the ``auth.org_id`` read happens later, inside the mounted tool body —
    after the transport has already answered — and FastMCP renders it as a
    JSON-RPC tool error instead: HTTP 200, ``isError`` true, this refusal's
    own message in the content. Pinned here so the property docstring's
    claim about each layer is a checked fact: the machine-readable code does
    not reach this path, but the refusal is total (no frame data) and the
    person-readable reason survives verbatim.
    """

    from fastapi.testclient import TestClient

    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_IDTOKEN_ALLOW_UNSIGNED", "true")
    monkeypatch.setenv(IDENTITY_CLAIM_ENV, "sub")
    monkeypatch.setenv(ORG_SOURCE_ENV, "membership")
    monkeypatch.delenv(DEFAULT_ORG_ENV, raising=False)
    monkeypatch.delenv(DEFAULT_WORKSPACE_ENV, raising=False)
    config = Config.parse(
        {
            "storage": {"frames_path": str(tmp_path / "frames")},
            "frames": {
                "active_state": {"backend": "memory"},
                "history": {"backend": "memory"},
                "groups": {"backend": "memory"},
                "usage": {"backend": "memory"},
                "orgs": {"backend": "memory"},
            },
            "tasks": {"backend": "memory"},
        }
    )
    app = make_app(config)

    def mcp_post(client, headers, body):
        return client.post("/mcp", headers=headers, json=body)

    def last_event(response) -> dict:
        (data_line,) = [line for line in response.text.splitlines() if line.startswith("data:")]
        return json.loads(data_line[len("data:") :])

    with TestClient(app) as client:
        app.state.org_store.set_platform_role(OPERATOR)
        headers = {
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
            "Cookie": f"IdToken-test={cookies_for(OPERATOR)['IdToken-test']}",
        }
        response = mcp_post(
            client,
            headers,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "0"},
                },
            },
        )
        # Authentication succeeds: the hub-scoped operator is a valid MCP
        # principal (public cross-tenant frame reads never touch org_id).
        assert response.status_code == 200, response.text
        headers["mcp-session-id"] = response.headers["mcp-session-id"]
        assert mcp_post(client, headers, {"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202

        response = mcp_post(
            client,
            headers,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "list_frames", "arguments": {}}},
        )
        assert response.status_code == 200, response.text
        result = last_event(response)["result"]
        assert result["isError"] is True
        (content,) = result["content"]
        assert "not part of an organization" in content["text"]
        assert "frames" not in json.dumps(result.get("structuredContent"))

        # The session survives the refusal: the same session answers a
        # subsequent, org-free request normally.
        response = mcp_post(client, headers, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        assert response.status_code == 200, response.text
        assert "list_frames" in [tool["name"] for tool in last_event(response)["result"]["tools"]]


# ---------------------------------------------------------------------------
# Layer 1: the audited-execution primitive (always-on fake coverage)
# ---------------------------------------------------------------------------


# The connection double lives in ``fake_audit_db`` now that the
# platform-role sync suite drives the same contract: two hand-copied
# doubles would be one psycopg change away from disagreeing about what the
# pooled context manager does, which is the only thing the double is for.


def test_audited_runs_the_mutation_and_the_event_insert_on_one_connection():
    db = FakeAuditDatabase()

    with audited(
        db,
        OPERATOR_CTX,
        "org.rename",
        target_type="org",
        target_id=ORG,
        org_id=ORG,
    ) as event:
        event.conn.execute("UPDATE collab_orgs SET name = %s WHERE id = %s", ("Acme", ORG))
        event.detail = {"renamed_to": "Acme"}
        event.target_label = "Acme"

    (conn,) = db.connections
    assert conn.outcome == "commit"
    mutation, insert = conn.statements
    assert mutation[0].startswith("UPDATE collab_orgs")
    assert "INSERT INTO collab_audit_events" in insert[0]
    # Post-yield mutations of the pending event made it into the row.
    actor, actor_label, action, target_type, target_id, target_label, org_id, detail = insert[1]
    assert (actor, action, target_type, target_id, org_id) == (OPERATOR, "org.rename", "org", ORG, ORG)
    assert target_label == "Acme" and detail.obj == {"renamed_to": "Acme"}
    assert event.event_id is not None


def test_audited_accepts_a_hub_scoped_operator_with_no_org():
    """org_id=None is a legitimate scope: hub-level actions belong to no org."""

    db = FakeAuditDatabase()
    with audited(db, HUB_OPERATOR_CTX, "invitation.send", target_type="invitation", target_id="inv-1") as event:
        event.detail = {"email_domain": "example.test"}

    (conn,) = db.connections
    assert conn.outcome == "commit"
    (insert,) = conn.statements
    assert insert[1][0] == OPERATOR and insert[1][6] is None  # actor, org_id


def test_audited_rolls_back_when_the_body_raises_and_writes_no_row():
    db = FakeAuditDatabase()

    with pytest.raises(RuntimeError, match="mutation failed"):
        with audited(db, OPERATOR_CTX, "org.create", target_type="org", target_id=ORG) as event:
            event.conn.execute("INSERT INTO collab_orgs (id, created_by) VALUES (%s, %s)", (ORG, OPERATOR))
            raise RuntimeError("mutation failed")

    (conn,) = db.connections
    assert conn.outcome == "rollback"
    # No event insert was even issued: the row cannot outlive its mutation.
    assert not any("collab_audit_events" in sql for sql, _ in conn.statements)


def test_audited_refuses_unvocabularied_actions_and_targets_before_connecting():
    db = FakeAuditDatabase()

    with pytest.raises(UnknownAuditActionError):
        with audited(db, OPERATOR_CTX, "org.delete"):
            pass
    with pytest.raises(UnknownAuditTargetTypeError):
        with audited(db, OPERATOR_CTX, "org.create", target_type="frame"):
            pass
    # Neither invalid call ever checked out a connection.
    assert db.connections == []

    # A body that rewrites the vocabulary fields cannot smuggle a row out:
    # the pin against the declared values raises before vocabulary is even
    # consulted, which also rolls back the mutation.
    with pytest.raises(InvalidAuditFieldError, match="action was rewritten"):
        with audited(db, OPERATOR_CTX, "org.create") as event:
            event.action = "org.obliterate"
    assert db.connections[-1].outcome == "rollback"


@pytest.mark.parametrize(
    "field_name, rewritten_to",
    [
        ("actor", "someone-else"),
        ("actor_label", "someone-else"),
        ("org_id", "someone-else"),
        # A *valid* vocabulary value: passing the closed-set check is not
        # enough — the row must record the action that was declared, not one
        # the body substituted after the fact.
        ("action", "operator.manual"),
        ("target_type", "invitation"),
    ],
)
def test_audited_refuses_a_rewritten_declared_field(field_name, rewritten_to):
    """Who did what, to what kind of thing, in which scope: stamped at entry,
    immutable after — even when the substitute value is itself in vocabulary."""

    db = FakeAuditDatabase()
    caller = ctx(OPERATOR, platform_role=PLATFORM_ROLE_OPERATOR, email="op@example.test")

    with pytest.raises(InvalidAuditFieldError, match=f"{field_name} was rewritten"):
        with audited(db, caller, "org.create", target_type="org", org_id=ORG) as event:
            setattr(event, field_name, rewritten_to)

    (conn,) = db.connections
    assert conn.outcome == "rollback"
    assert not any("collab_audit_events" in sql for sql, _ in conn.statements)


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda ev: setattr(ev, "target_label", "x" * (AUDIT_LABEL_MAX_CHARS + 1)), "exceeds"),
        (lambda ev: setattr(ev, "target_id", "x" * (AUDIT_ID_MAX_CHARS + 1)), "exceeds"),
        (lambda ev: setattr(ev, "target_label", "line\nbreak"), "control characters"),
        (lambda ev: setattr(ev, "target_id", "nul\x00byte"), "control characters"),
        (lambda ev: setattr(ev, "detail", {"blob": "x" * AUDIT_DETAIL_MAX_BYTES}), "exceeds"),
        (lambda ev: setattr(ev, "detail", {"nul": "a\x00b"}), "NUL"),
        (lambda ev: setattr(ev, "detail", ["not", "a", "dict"]), "must be a dict"),
        (lambda ev: setattr(ev, "target_id", 42), "must be a string"),
    ],
)
def test_audited_bounds_every_row_field(mutate, message):
    """Oversized, control-character, or mistyped fields abort the action."""

    db = FakeAuditDatabase()

    with pytest.raises(InvalidAuditFieldError, match=message):
        with audited(db, OPERATOR_CTX, "org.create", target_type="org") as event:
            event.conn.execute("INSERT INTO collab_orgs (id, created_by) VALUES (%s, %s)", (ORG, OPERATOR))
            mutate(event)

    (conn,) = db.connections
    assert conn.outcome == "rollback"
    assert not any("collab_audit_events" in sql for sql, _ in conn.statements)


def test_audited_stamps_the_actor_label_from_the_display_identity():
    db = FakeAuditDatabase()
    labeled = ctx(OPERATOR, platform_role=PLATFORM_ROLE_OPERATOR, email="op@example.test")

    with audited(db, labeled, "operator.manual"):
        pass

    (conn,) = db.connections
    (insert,) = conn.statements
    assert insert[1][0] == OPERATOR and insert[1][1] == "op@example.test"


# ---------------------------------------------------------------------------
# The guarded connection: the body cannot complete the transaction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "violate",
    [
        lambda conn: conn.commit(),
        lambda conn: conn.rollback(),
        lambda conn: conn.close(),
        lambda conn: conn.cancel(),
        lambda conn: conn.cancel_safe(),
        lambda conn: conn.set_autocommit(True),
        lambda conn: setattr(conn, "autocommit", True),
    ],
    ids=["commit", "rollback", "close", "cancel", "cancel_safe", "set_autocommit", "autocommit-assign"],
)
def test_the_body_cannot_complete_or_abandon_the_audited_transaction(violate):
    db = FakeAuditDatabase()

    with pytest.raises(AuditTransactionViolation):
        with audited(db, OPERATOR_CTX, "org.create", target_type="org", target_id=ORG) as event:
            event.conn.execute("INSERT INTO collab_orgs (id, created_by) VALUES (%s, %s)", (ORG, OPERATOR))
            violate(event.conn)

    # The violation aborted everything: rollback, and no event row was issued.
    (conn,) = db.connections
    assert conn.outcome == "rollback"
    assert not any("collab_audit_events" in sql for sql, _ in conn.statements)


def test_the_guard_exposes_no_unvetted_connection_surface():
    db = FakeAuditDatabase()

    with audited(db, OPERATOR_CTX, "org.create") as event:
        # Reads stay honest without widening the surface.
        assert event.conn.autocommit is False
        with pytest.raises(AttributeError, match="does not expose"):
            event.conn.pgconn
        with pytest.raises(AttributeError, match="does not expose"):
            event.conn.prepare_threshold


def test_the_guard_still_allows_nested_savepoints():
    """conn.transaction() on the guard is a savepoint, not an escape hatch."""

    db = FakeAuditDatabase()
    with audited(db, OPERATOR_CTX, "org.create") as event:
        with event.conn.transaction():
            event.conn.execute("INSERT INTO collab_orgs (id, created_by) VALUES (%s, %s)", (ORG, OPERATOR))

    (conn,) = db.connections
    assert conn.outcome == "commit"


@pytest.mark.parametrize(
    "statement",
    [
        "COMMIT",
        "commit",
        " Commit ",
        "COMMIT AND CHAIN",
        "COMMIT PREPARED 'gid'",
        "END",
        "END TRANSACTION",
        "BEGIN",
        "START TRANSACTION",
        "ABORT",
        "ROLLBACK",
        "ROLLBACK AND CHAIN",
        "ROLLBACK PREPARED 'gid'",
        "PREPARE TRANSACTION 'gid'",
        "UPDATE collab_orgs SET name = 'x'; COMMIT",
        "INSERT INTO collab_orgs (id) VALUES ('o');COMMIT;",
        "UPDATE collab_orgs SET name = 'x' /* sneak */; commit -- done",
        # Quoting-regime payloads. Each is a *real* escape on a live server
        # under one of PostgreSQL's two readings of a backslash inside a
        # plain literal, which is why the screen lexes the text under both:
        # with standard_conforming_strings on (the default) the first
        # literal ends at the second quote...
        r"SELECT '\'; COMMIT; SELECT 'x'",
        # ...and with it off, this mirrored payload is the dangerous one.
        r"SELECT '\', 'x; COMMIT; --'",
        # `name'abc\'` is the identifier `name` followed by an ordinary
        # literal — a valid name-typed constant — not an E-string, so the
        # backslash does not escape the quote and the COMMIT is a statement.
        r"SELECT name'abc\'; COMMIT; SELECT name'x'",
        # Block comments nest: this one ends at the *second* `*/`, leaving a
        # real COMMIT behind. (Reading it as a single non-nesting comment
        # leaves a stray quote that then swallows the COMMIT into a literal.)
        "/* /* */ ' */ COMMIT; SELECT '",
        # A `$` opens a dollar-quote only where it does not continue an
        # identifier: `x$tag$` is the identifier `x$tag$`, so this is an
        # aliased select, a statement end, and a COMMIT the server runs —
        # while reading the first `$tag$` as an opening delimiter would blank
        # the COMMIT away inside a phantom body.
        "SELECT 1 AS x$tag$; COMMIT; SELECT $tag$foo$tag$;",
        "SELECT 1 AS x$$; COMMIT; SELECT $$foo$$;",
        "SELECT 1 AS _$t$; COMMIT; SELECT $t$foo$t$;",
        # ...and PostgreSQL's identifier characters are its own, not Python's:
        # scan.l admits EVERY high-bit byte, so each of these separators is
        # identifier continuation to the server (no dollar-quote opens and the
        # COMMIT is a statement) while Python calls most of them non-alnum.
        "SELECT 1 AS a\u0301$tag$; COMMIT; SELECT $tag$foo$tag$;",  # combining acute
        "SELECT 1 AS a\U0001f600$tag$; COMMIT; SELECT $tag$foo$tag$;",  # emoji
        "SELECT 1 AS a\u200d$tag$; COMMIT; SELECT $tag$foo$tag$;",  # zero-width joiner
        "SELECT 1 AS a\u00a0$tag$; COMMIT; SELECT $tag$foo$tag$;",  # no-break space
        "SELECT 1 AS a\u3000$tag$; COMMIT; SELECT $tag$foo$tag$;",  # ideographic space
        "SELECT 1 AS a\u0660$tag$; COMMIT; SELECT $tag$foo$tag$;",  # Arabic-Indic digit
        "SELECT 1 AS a\u4e2d$tag$; COMMIT; SELECT $tag$foo$tag$;",  # CJK
        # A lone CR ends a line comment (scan.l non_newline is [^\n\r]), so
        # scanning only to the next \n blanks a real statement away.
        "SELECT 1; -- x\rCOMMIT; SELECT 2",
        "SELECT 1 -- x\r; COMMIT",
        # bytes are a query form of their own, screened like text.
        b"COMMIT",
    ],
)
def test_transaction_control_sql_text_is_refused(statement):
    """execute("COMMIT") must not escape: psycopg only guards the METHODS.

    The whole execute call is refused before any of it reaches the
    connection, so a multi-statement smuggle cannot land its mutation half.
    """

    db = FakeAuditDatabase()

    with pytest.raises(AuditTransactionViolation, match="transaction-control SQL"):
        with audited(db, OPERATOR_CTX, "org.create", target_type="org", target_id=ORG) as event:
            event.conn.execute(statement)

    (conn,) = db.connections
    assert conn.outcome == "rollback"
    # Nothing of the refused call reached the connection, and no event row
    # was issued.
    assert conn.statements == []


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO notes (body) VALUES ('please COMMIT this')",
        "INSERT INTO notes (body) VALUES (E'escaped \\'; COMMIT; still a literal')",
        "UPDATE stats SET commit_count = commit_count + 1",
        'UPDATE "commit" SET x = 1',
        "-- COMMIT\nUPDATE notes SET x = 1",
        "/* COMMIT; ROLLBACK */ UPDATE notes SET x = 1",
        "/* outer /* COMMIT; */ ROLLBACK */ UPDATE notes SET x = 1",
        "SELECT $body$ COMMIT; ROLLBACK $body$",
        "SELECT $$ COMMIT; ROLLBACK $$",
        "SELECT $a$ x $b$ COMMIT; $b$ y $a$",
        # Two dollar-quoted strings, the second opening on the `$`
        # immediately after the first one closes: the identifier rule is
        # about the preceding *token*, and a closed delimiter ends one.
        "SELECT $a$x$a$$b$y; COMMIT; $b$",
        "SELECT $a$abc$ax$a$",
        # Legitimate non-ASCII SQL is untouched: identifiers, literals,
        # dollar-quote tags and comments all still carry their keywords.
        'UPDATE "café" SET x = 1',
        "INSERT INTO notes (body) VALUES ('naïve; COMMIT; --')",
        "SELECT $café$ COMMIT; ROLLBACK $café$",
        "-- á COMMIT\nUPDATE notes SET x = 1",
        "SELECT 1 AS \u00e1, $tag$ COMMIT; $tag$",
        "SELECT $1 FROM notes WHERE body = 'a''; COMMIT; b'",
        r"UPDATE notes SET body = E'ends with a backslash\\' WHERE id = 1",
        "SAVEPOINT sp",
        "ROLLBACK TO SAVEPOINT sp",
        "ROLLBACK TO sp",
        "RELEASE SAVEPOINT sp",
        "PREPARE plan AS SELECT 1",
        "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE",
    ],
)
def test_sql_screening_does_not_false_positive(statement):
    """Keywords inside literals, identifiers, and comments are not commands,
    and savepoint control (which cannot complete the outer transaction) stays
    available to bodies."""

    db = FakeAuditDatabase()
    with audited(db, OPERATOR_CTX, "org.create") as event:
        event.conn.execute(statement)

    (conn,) = db.connections
    assert conn.outcome == "commit"
    # The statement executed and the event insert followed it.
    assert len(conn.statements) == 2


def test_flipping_standard_conforming_strings_buys_the_body_nothing():
    """The GUC that decides how the server reads `\\'` is not a way in.

    A body may legitimately `SET` it (and it may already be off in the
    server's configuration or the connection string), so the screen never
    trusts one reading: every statement is lexed under both regimes, and the
    payload that only executes under the legacy reading is refused all the
    same.
    """

    db = FakeAuditDatabase()

    with pytest.raises(AuditTransactionViolation, match="transaction-control SQL"):
        with audited(db, OPERATOR_CTX, "org.create", target_type="org", target_id=ORG) as event:
            # Not transaction control, so it lands...
            event.conn.execute("SET standard_conforming_strings = off")
            # ...and the legacy-regime smuggle behind it is still refused.
            event.conn.execute(r"SELECT '\', 'x; COMMIT; --'")

    (conn,) = db.connections
    assert conn.outcome == "rollback"
    assert [statement for statement, _ in conn.statements] == ["SET standard_conforming_strings = off"]


class DivergentComposable(sql.Composable):
    """A composable whose text and bytes disagree.

    ``as_string`` is innocent; ``as_bytes`` — the rendering psycopg actually
    sends (``PostgresQuery._ensure_bytes``) — is a COMMIT. Screening the
    former while executing the latter screens an artifact that never runs.
    """

    def __init__(self):
        super().__init__("SELECT 1")

    def as_string(self, context=None):
        return "SELECT 1"

    def as_bytes(self, context=None):
        return b"COMMIT"


def test_a_composable_is_screened_by_the_bytes_that_will_execute(monkeypatch):
    """The round-3 BLOCKER: as_string is not what the server receives.

    The type allowlist is switched off here on purpose, so that what refuses
    this query is the byte-level screen alone — the fix itself, not the
    defense in depth stacked on top of it.
    """

    monkeypatch.setattr(audit_module, "_refuse_foreign_composable", lambda query: None)
    db = FakeAuditDatabase()

    with pytest.raises(AuditTransactionViolation, match="transaction-control SQL"):
        with audited(db, OPERATOR_CTX, "org.create", target_type="org", target_id=ORG) as event:
            event.conn.execute(DivergentComposable())

    (conn,) = db.connections
    assert conn.outcome == "rollback"
    assert conn.statements == []


def test_only_psycopgs_own_composables_are_rendered_for_screening():
    """Defense in depth: rendering runs the object's code, so foreign
    composable types are refused before ``as_bytes`` is ever called — nested
    ones too, since a Composed renders its members."""

    db = FakeAuditDatabase()

    for query in (
        DivergentComposable(),
        sql.Composed([sql.SQL("SELECT "), DivergentComposable()]),
    ):
        with pytest.raises(AuditTransactionViolation, match="psycopg.sql's own composables"):
            with audited(db, OPERATOR_CTX, "org.create") as event:
                event.conn.execute(query)
        assert db.connections[-1].outcome == "rollback"
        assert db.connections[-1].statements == []


def test_legitimate_composables_execute_as_the_bytes_that_were_screened():
    db = FakeAuditDatabase()
    query = sql.SQL("INSERT INTO {} (id, name) VALUES ({}, {}) RETURNING id").format(
        sql.Identifier("collab_orgs"),
        sql.Placeholder(),
        sql.Literal("please COMMIT to quality"),
    )

    with audited(db, OPERATOR_CTX, "org.create", target_type="org", target_id=ORG) as event:
        event.conn.execute(query, (ORG,))

    (conn,) = db.connections
    assert conn.outcome == "commit"
    mutation, insert = conn.statements
    # What executed is the rendered bytes — the exact text that was screened.
    assert mutation == (
        "INSERT INTO \"collab_orgs\" (id, name) VALUES (%s, 'please COMMIT to quality') RETURNING id",
        (ORG,),
    )
    assert "INSERT INTO collab_audit_events" in insert[0]


class EncodingContext:
    """A psycopg-shaped adaptation context pinned to one client encoding."""

    def __init__(self, encoding):
        self.info = type("Info", (), {"encoding": encoding})()

    @property
    def connection(self):
        return self


def test_str_queries_execute_as_the_bytes_that_were_screened():
    """The screened text is encoded here, not left for psycopg to encode.

    Returning the ``str`` would leave one encoding step downstream of the
    screen — what executes would be bytes this module never saw. The encoding
    is the one psycopg itself would use (the connection's client_encoding),
    and it must round-trip, so the bytes mean exactly the screened text.
    """

    assert audit_module._screened_query("SELECT 1", None) == b"SELECT 1"
    latin1 = EncodingContext("iso8859-1")
    assert audit_module._screened_query("SELECT 'café'", latin1) == "SELECT 'café'".encode("iso8859-1")
    assert audit_module._screened_query("SELECT 'café'", EncodingContext("utf-8")) == "SELECT 'café'".encode()

    with pytest.raises(AuditTransactionViolation, match="cannot be encoded"):
        audit_module._screened_query("SELECT '€'", latin1)


def test_queries_that_cannot_be_read_as_text_are_refused():
    """A query the screen cannot decode is not a query the guard will pass."""

    db = FakeAuditDatabase()
    with pytest.raises(AuditTransactionViolation, match="not valid UTF-8"):
        with audited(db, OPERATOR_CTX, "org.create") as event:
            event.conn.execute(b"SELECT '\xff\xfe'")
    assert db.connections[-1].outcome == "rollback"


def test_cursors_do_not_hand_back_the_raw_connection():
    """cursor().connection is the classic road around a connection guard."""

    db = FakeAuditDatabase()
    with audited(db, OPERATOR_CTX, "org.create") as event:
        cursor = event.conn.cursor()
        with pytest.raises(AuditTransactionViolation, match="raw connection stays with audited"):
            cursor.connection
        # The cursor execute()/executemany() surface is screened like the
        # connection's...
        with pytest.raises(AuditTransactionViolation, match="transaction-control SQL"):
            cursor.execute("COMMIT")
        with pytest.raises(AuditTransactionViolation, match="transaction-control SQL"):
            cursor.executemany("ROLLBACK", [()])
        # ...and the cursor returned by conn.execute() is guarded the same way.
        result = event.conn.execute("SELECT 1")
        with pytest.raises(AuditTransactionViolation, match="raw connection stays with audited"):
            result.connection
        with pytest.raises(AttributeError, match="does not expose"):
            result.copy
        # The nested-savepoint handle closes the same road.
        with pytest.raises(AuditTransactionViolation, match="raw connection stays with audited"):
            event.conn.transaction().connection


def test_unrenderable_query_objects_are_refused_not_guessed():
    class Opaque:
        pass

    db = FakeAuditDatabase()
    with pytest.raises(AuditTransactionViolation, match="cannot screen"):
        with audited(db, OPERATOR_CTX, "org.create") as event:
            event.conn.execute(Opaque())
    assert db.connections[-1].outcome == "rollback"


def test_the_backstop_turns_an_unforeseen_escape_into_a_named_failure():
    """Layer 3: if the body broke the invariant anyway, the insert refuses.

    Simulated by reaching past the guard (white box) and flipping the
    connection's reported transaction status, standing in for any escape the
    guard and the SQL screen did not foresee. The distinct error names the
    broken invariant instead of letting the event row be written into
    whatever transaction (or none) the connection is now in.
    """

    db = FakeAuditDatabase()

    with pytest.raises(AuditTransactionBrokenError, match="no longer active"):
        with audited(db, OPERATOR_CTX, "org.create", target_type="org", target_id=ORG) as event:
            event.conn.execute("INSERT INTO collab_orgs (id, created_by) VALUES (%s, %s)", (ORG, OPERATOR))
            event.conn._conn.info.transaction_status = pq.TransactionStatus.IDLE

    (conn,) = db.connections
    assert conn.outcome == "rollback"
    assert not any("collab_audit_events" in sql for sql, _ in conn.statements)


# ---------------------------------------------------------------------------
# Vocabularies: code and schema pinned together
# ---------------------------------------------------------------------------


def _audit_ddl_statements() -> list[str]:
    """Every migration statement that touches the audit table, in order."""

    return [
        " ".join(statement.split())
        for _, statements in COLLAB_SCHEMA_MIGRATIONS
        for statement in statements
        if "collab_audit_events" in " ".join(statement.split())
    ]


def _effective_check(column: str) -> set[str]:
    """The vocabulary a migrated database actually enforces for ``column``.

    Not the CREATE TABLE's list: a released version's SQL is frozen text, so
    widening the set means appending a version that *replaces* the constraint.
    Reading only the create statement would therefore keep passing while the
    code and the live schema drifted apart in exactly the way this file exists
    to catch. So every statement carrying a ``CHECK (<column> IN (...))`` is
    applied in migration order and the last one wins -- which is what Postgres
    does with a DROP CONSTRAINT / ADD CONSTRAINT pair.
    """

    pattern = re.compile(rf"CHECK \({column} IN \(([^)]*)\)\)")
    effective: set[str] | None = None
    for statement in _audit_ddl_statements():
        found = pattern.search(statement)
        if found:
            effective = set(re.findall(r"'([^']+)'", found.group(1)))
    assert effective is not None, f"no CHECK on {column} in any migration"
    return effective


def test_the_action_vocabulary_is_exactly_the_ratified_beta_set():
    """The closed set, spelled out so widening it is a deliberate edit here.

    ``service_access.grant`` was appended by #180, and the two
    ``platform_role.*`` actions by the sync that lets an identity provider
    decide who is an operator. The target vocabulary has not moved: every one
    of these aims at a person, which is already ``user``.
    """

    assert AUDIT_ACTIONS == {
        "invitation.send",
        "invitation.redeem",
        "invitation.revoke",
        "membership.create",
        "org.create",
        "org.rename",
        "operator.manual",
        "service_access.grant",
        "platform_role.grant",
        "platform_role.revoke",
        "service_access.revoke",
        "connector.enable",
        "connector.disable",
    }
    assert AUDIT_TARGET_TYPES == {"org", "user", "invitation", "connector"}


def test_the_check_constraints_match_the_code_vocabulary_exactly():
    """AUDIT_ACTIONS/AUDIT_TARGET_TYPES and the migrated CHECKs are one list.

    A value accepted by the code but refused by the schema would roll back
    real actions at insert time; a value accepted by the schema but missing
    from the code would let psql rows drift off the documented vocabulary.
    Either drift fails here, at unit speed, on every change.
    """

    assert _effective_check("action") == AUDIT_ACTIONS
    assert _effective_check("target_type") == AUDIT_TARGET_TYPES


# ---------------------------------------------------------------------------
# The append-only convention, asserted against the code
# ---------------------------------------------------------------------------


def test_no_code_path_updates_or_deletes_audit_rows():
    """The enforceable form of "append-only": the application never does it.

    The ACL cannot make this a boundary — the application role owns the table
    (auto-migration creates it over the runtime pool), so a REVOKE would prove
    today's grants and nothing more. What the codebase can promise is that no
    application code path issues UPDATE or DELETE against the table; this scan
    is that promise's regression test. TRUNCATE and DROP are included for the
    same reason, and the migration list is exempt only for CREATE.
    """

    source_root = Path(__file__).resolve().parents[1] / "src"
    forbidden = re.compile(
        r"\b(UPDATE\s+collab_audit_events|DELETE\s+FROM\s+collab_audit_events"
        r"|TRUNCATE\s+(TABLE\s+)?collab_audit_events|DROP\s+TABLE\s+(IF\s+EXISTS\s+)?collab_audit_events)\b",
        re.IGNORECASE,
    )
    offenders = [
        str(path) for path in sorted(source_root.rglob("*.py")) if forbidden.search(" ".join(path.read_text().split()))
    ]
    assert offenders == [], f"application code performs non-INSERT DML on collab_audit_events: {offenders}"


def test_the_only_schema_changes_to_the_audit_table_preserve_its_history():
    """``ALTER TABLE``, allowed for constraints only, and only in migrations.

    This assertion used to be "no ALTER TABLE anywhere", which was the right
    shape until the action vocabulary had to grow: #180 widens a CHECK, and a
    released version's SQL is frozen, so the constraint is replaced by an
    appended migration. A blanket ban would have forced the alternative of
    editing version 2 in place -- forking old databases from new ones, which
    is the failure the freeze rule exists to prevent.

    So the ban is narrowed to what it was actually protecting. Constraint
    changes cannot lose a recorded row; ``DROP COLUMN``, ``RENAME``, and
    ``ALTER COLUMN ... TYPE`` can destroy or rewrite recorded history, and stay
    forbidden even inside the migration list. And an ALTER anywhere outside
    that list is still an application code path changing the audit table's
    shape at runtime, which nothing has any business doing.
    """

    source_root = Path(__file__).resolve().parents[1] / "src"
    altering_files = sorted(
        path.relative_to(source_root).as_posix()
        for path in source_root.rglob("*.py")
        if re.search(r"ALTER\s+TABLE\s+collab_audit_events", " ".join(path.read_text().split()), re.IGNORECASE)
    )
    assert altering_files == ["collab_hub_api/frames/collab_schema.py"], altering_files

    destructive = re.compile(r"\b(DROP\s+COLUMN|RENAME|ALTER\s+COLUMN)\b", re.IGNORECASE)
    # A CHECK constraint, by this schema's naming convention. `_pkey` and any
    # foreign-key name fail to match, which is the point: "constraint change"
    # is too broad a licence -- dropping `collab_audit_events_pkey` is a
    # constraint change too, and it would let duplicate rows in. Only the
    # vocabulary CHECKs may be replaced.
    check_constraint = re.compile(r"CONSTRAINT\s+(?:IF\s+EXISTS\s+)?(collab_audit_events_\w+_check)\b", re.IGNORECASE)
    dropped: dict[int, set[str]] = {}
    added: dict[int, set[str]] = {}

    for version, statements in COLLAB_SCHEMA_MIGRATIONS:
        for raw in statements:
            statement = " ".join(raw.split())
            if not re.search(r"ALTER\s+TABLE\s+collab_audit_events", statement, re.IGNORECASE):
                continue
            # Quoted values are blanked before the scan: a vocabulary
            # containing `'org.rename'` is not a RENAME, and the first version
            # of this test said it was. The same reasoning (and the same trap)
            # as `_blank_noise` in the audit module's statement screening.
            tail = re.sub(r"'[^']*'", "''", statement.split("collab_audit_events", 1)[1])
            assert not destructive.search(tail), f"a migration rewrites recorded history: {statement}"

            named = check_constraint.search(tail)
            assert named, (
                "the only permitted ALTER on the audit table replaces one of its "
                f"vocabulary CHECK constraints, named collab_audit_events_*_check: {statement}"
            )
            operation = re.search(r"\b(DROP|ADD)\s+CONSTRAINT\b", tail, re.IGNORECASE)
            assert operation, f"neither a DROP nor an ADD: {statement}"
            bucket = dropped if operation.group(1).upper() == "DROP" else added
            bucket.setdefault(version, set()).add(named.group(1).lower())

    # A drop must be a *replacement*, in the same version. Dropping a CHECK and
    # never re-adding it would widen the vocabulary to "anything", silently.
    for version, names in dropped.items():
        assert names <= added.get(version, set()), (
            f"migration v{version} drops a CHECK it does not re-add: {sorted(names - added.get(version, set()))}"
        )


def test_the_audit_module_is_the_only_writer_of_audit_rows():
    source_root = Path(__file__).resolve().parents[1] / "src"
    writers = [
        path.relative_to(source_root).as_posix()
        for path in sorted(source_root.rglob("*.py"))
        if "INSERT INTO collab_audit_events" in " ".join(path.read_text().split())
    ]
    assert writers == ["collab_hub_api/frames/audit.py"], writers


# ---------------------------------------------------------------------------
# Live-Postgres coverage (opt in with COLLAB_HUB_TEST_POSTGRES_URL)
# ---------------------------------------------------------------------------

POSTGRES_URL = os.environ.get("COLLAB_HUB_TEST_POSTGRES_URL", "")

live_postgres = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="set COLLAB_HUB_TEST_POSTGRES_URL to a disposable database to run the live operator-foundation tests",
)

COLLAB_TABLES = (
    "collab_service_access_grants",
    "collab_provisioned_accounts",
    "collab_invitations",
    "collab_orgs",
    "collab_org_members",
    "collab_platform_roles",
    "collab_audit_events",
    "collab_schema_migrations",
)


def _database():
    from collab_hub_api.frames.db import PostgresDatabase

    return PostgresDatabase(POSTGRES_URL, min_size=0, max_size=10, timeout_seconds=10.0)


@pytest.fixture
def migrated_database():
    """A live database migrated from scratch, dropped clean before and after."""

    def drop_all() -> None:
        with database.connection() as conn:
            for table in COLLAB_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

    database = _database()
    try:
        drop_all()
        run_collab_schema_migrations(database)
        yield database
        drop_all()
    finally:
        database.close()


def _audit_rows(database) -> list[dict]:
    # The documented read procedure, minus the generated columns.
    with database.connection() as conn:
        return conn.execute(
            "SELECT actor, actor_label, action, target_type, target_id, target_label, org_id, detail"
            " FROM collab_audit_events ORDER BY at, id"
        ).fetchall()


def _org_count(database, org_id: str) -> int:
    with database.connection() as conn:
        return conn.execute("SELECT count(*) AS n FROM collab_orgs WHERE id = %s", (org_id,)).fetchone()["n"]


@live_postgres
def test_live_audited_commits_the_mutation_and_the_event_together(migrated_database):
    with audited(
        migrated_database,
        OPERATOR_CTX,
        "org.create",
        target_type="org",
        target_id=ORG,
        org_id=ORG,
    ) as event:
        event.conn.execute(
            "INSERT INTO collab_orgs (id, name, created_by) VALUES (%s, %s, %s)",
            (ORG, "Acme", OPERATOR),
        )
        event.detail = {"name": "Acme"}
        event.target_label = "Acme"

    assert event.event_id is not None
    assert _org_count(migrated_database, ORG) == 1
    (row,) = _audit_rows(migrated_database)
    assert row == {
        "actor": OPERATOR,
        "actor_label": None,
        "action": "org.create",
        "target_type": "org",
        "target_id": ORG,
        "target_label": "Acme",
        "org_id": ORG,
        "detail": {"name": "Acme"},
    }

    # Restart survival: a brand-new pool (fresh connections, no shared state)
    # reads the same committed row.
    fresh = _database()
    try:
        assert len(_audit_rows(fresh)) == 1
    finally:
        fresh.close()


@live_postgres
def test_live_audited_rolls_back_the_event_when_the_mutation_fails(migrated_database):
    """Forced failure on the mutation side: no org row, no event row."""

    with pytest.raises(psycopg.errors.UniqueViolation):
        with audited(migrated_database, OPERATOR_CTX, "org.create", target_type="org", target_id=ORG) as event:
            event.conn.execute("INSERT INTO collab_orgs (id, created_by) VALUES (%s, %s)", (ORG, OPERATOR))
            # The mutation's second statement violates the primary key.
            event.conn.execute("INSERT INTO collab_orgs (id, created_by) VALUES (%s, %s)", (ORG, OPERATOR))

    assert _org_count(migrated_database, ORG) == 0
    assert _audit_rows(migrated_database) == []


@live_postgres
def test_live_audited_rolls_back_the_mutation_when_the_event_write_fails(migrated_database):
    """Forced failure on the event side: the mutation must not outlive it.

    ``detail`` is grown past the serialized-size bound *after* the mutation
    ran, so the mutation succeeds and the event write itself is what dies. A
    primitive that wrote the mutation first and the event second *outside*
    one transaction would leave the org row behind here — the silently
    diverging log the issue calls worse than no log.
    """

    with pytest.raises(InvalidAuditFieldError):
        with audited(migrated_database, OPERATOR_CTX, "org.create", target_type="org", target_id=ORG) as event:
            event.conn.execute("INSERT INTO collab_orgs (id, created_by) VALUES (%s, %s)", (ORG, OPERATOR))
            event.detail = {"blob": "x" * AUDIT_DETAIL_MAX_BYTES}

    assert _org_count(migrated_database, ORG) == 0
    assert _audit_rows(migrated_database) == []


@live_postgres
def test_live_the_body_cannot_commit_the_mutation_without_the_event(migrated_database):
    """The BLOCKER case: a body that commits mid-flight and then fails.

    Without the guard, conn.commit() would persist the mutation and the
    subsequent failure would skip the event insert — a recorded-nowhere
    privileged action. The guard refuses the commit, the refusal aborts the
    action, and nothing is left behind.
    """

    with pytest.raises(AuditTransactionViolation):
        with audited(migrated_database, OPERATOR_CTX, "org.create", target_type="org", target_id=ORG) as event:
            event.conn.execute("INSERT INTO collab_orgs (id, created_by) VALUES (%s, %s)", (ORG, OPERATOR))
            event.conn.commit()  # the escape attempt
            raise RuntimeError("never reached")

    assert _org_count(migrated_database, ORG) == 0
    assert _audit_rows(migrated_database) == []


@live_postgres
def test_live_transaction_control_sql_cannot_detach_the_mutation(migrated_database):
    """The round-2 BLOCKER, on a real server: execute("COMMIT") then fail.

    Without the SQL screen the COMMIT would persist the org row and the
    subsequent exception would skip the event insert — a privileged action
    recorded nowhere. The screen refuses the statement, everything rolls
    back, and the multi-statement smuggle behaves identically.
    """

    for escape in ("COMMIT", "UPDATE collab_orgs SET name = 'x'; COMMIT"):
        with pytest.raises(AuditTransactionViolation):
            with audited(migrated_database, OPERATOR_CTX, "org.create", target_type="org", target_id=ORG) as event:
                event.conn.execute("INSERT INTO collab_orgs (id, created_by) VALUES (%s, %s)", (ORG, OPERATOR))
                event.conn.execute(escape)
                raise RuntimeError("never reached")

        assert _org_count(migrated_database, ORG) == 0
        assert _audit_rows(migrated_database) == []


@live_postgres
def test_live_lexer_disagreement_payloads_really_escape_and_are_really_refused(migrated_database):
    """Every payload the screen once mis-lexed, on a real server, both ways.

    First that these are not strawmen: run unguarded, each payload's phantom
    literal really does end where the *server* says it ends, and the COMMIT
    it smuggles persists a mutation that a later rollback can no longer
    undo. Then that the screen refuses exactly those payloads inside
    ``audited()``, leaving neither an org row nor an event row.
    """

    payloads = (
        # standard_conforming_strings is on: '\' is a complete literal.
        r"SELECT '\'; COMMIT; SELECT 'x'",
        # `name` is an identifier, so `'abc\'` is an ordinary literal too.
        r"SELECT name'abc\'; COMMIT; SELECT name'x'",
        # `x$tag$` is one identifier, so no dollar-quote opens here and the
        # COMMIT is a statement of its own.
        "SELECT 1 AS x$tag$; COMMIT; SELECT $tag$foo$tag$;",
        # Same rule, with a separator only PostgreSQL's character classes
        # call an identifier character (an `a` and a combining acute).
        "SELECT 1 AS a\u0301$tag$; COMMIT; SELECT $tag$foo$tag$;",
        # A lone CR ends the line comment, so the COMMIT is not commented out.
        "SELECT 1; -- x\rCOMMIT; SELECT 2",
    )

    for payload in payloads:
        with migrated_database.connection() as conn:
            conn.execute("INSERT INTO collab_orgs (id, created_by) VALUES (%s, %s)", (ORG, OPERATOR))
            conn.execute(payload)
            conn.rollback()  # too late: the smuggled COMMIT already landed it
        assert _org_count(migrated_database, ORG) == 1, f"{payload!r} is not an escape after all"
        with migrated_database.connection() as conn:
            conn.execute("DELETE FROM collab_orgs WHERE id = %s", (ORG,))

    for payload in payloads:
        with pytest.raises(AuditTransactionViolation, match="transaction-control SQL"):
            with audited(migrated_database, OPERATOR_CTX, "org.create", target_type="org", target_id=ORG) as event:
                event.conn.execute("INSERT INTO collab_orgs (id, created_by) VALUES (%s, %s)", (ORG, OPERATOR))
                event.conn.execute(payload)
                raise RuntimeError("never reached")

        assert _org_count(migrated_database, ORG) == 0
        assert _audit_rows(migrated_database) == []


@live_postgres
def test_live_a_do_block_cannot_terminate_the_audited_transaction(migrated_database):
    """The documented residual, pinned: the screen cannot see inside a DO
    body (it is a dollar-quoted literal), so the claim that carries the
    weight is PostgreSQL's own — transaction termination is refused inside a
    DO block running in an explicit transaction block, which this always is.
    The action dies with the server's error and nothing is left behind."""

    with pytest.raises(psycopg.errors.InvalidTransactionTermination):
        with audited(migrated_database, OPERATOR_CTX, "org.create", target_type="org", target_id=ORG) as event:
            event.conn.execute("INSERT INTO collab_orgs (id, created_by) VALUES (%s, %s)", (ORG, OPERATOR))
            event.conn.execute("DO $$ BEGIN COMMIT; END $$")

    assert _org_count(migrated_database, ORG) == 0
    assert _audit_rows(migrated_database) == []


@live_postgres
def test_live_composable_queries_run_as_the_bytes_that_were_screened(migrated_database):
    """psycopg.sql composition keeps working, screened at the byte level."""

    with audited(
        migrated_database,
        OPERATOR_CTX,
        "org.create",
        target_type="org",
        target_id=ORG,
        org_id=ORG,
    ) as event:
        cursor = event.conn.execute(
            sql.SQL("INSERT INTO {} (id, name, created_by) VALUES ({}, {}, {}) RETURNING id").format(
                sql.Identifier("collab_orgs"),
                sql.Placeholder(),
                sql.Literal("please COMMIT to quality"),
                sql.Placeholder(),
            ),
            (ORG, OPERATOR),
        )
        assert cursor.fetchone()["id"] == ORG

    assert _org_count(migrated_database, ORG) == 1
    assert len(_audit_rows(migrated_database)) == 1


@live_postgres
def test_live_savepoints_and_literals_survive_the_sql_screen(migrated_database):
    """Legitimate bodies are untouched: savepoint control and keyword-bearing
    literals execute normally and the whole action commits with its row."""

    with audited(migrated_database, OPERATOR_CTX, "org.create", target_type="org", target_id=ORG) as event:
        with event.conn.transaction():  # SAVEPOINT under the hood
            event.conn.execute(
                "INSERT INTO collab_orgs (id, name, created_by) VALUES (%s, %s, %s)",
                (ORG, "please COMMIT to quality", OPERATOR),
            )
        cursor = event.conn.execute("SELECT name FROM collab_orgs WHERE id = %s", (ORG,))
        assert cursor.fetchone()["name"] == "please COMMIT to quality"

    assert _org_count(migrated_database, ORG) == 1
    assert len(_audit_rows(migrated_database)) == 1


@live_postgres
def test_live_the_backstop_names_an_escape_the_guard_did_not_catch(migrated_database):
    """Layer 3 on a real server: a contrived white-box escape commits behind
    the guard's back; the backstop refuses to write the event row into the
    wrong transaction and names the broken invariant.

    The mutation itself is committed and cannot be undone — that is exactly
    the residual the backstop documents — but the action fails loudly with
    the distinct error instead of producing a silently unrecorded action.
    """

    with pytest.raises(AuditTransactionBrokenError, match="no longer active"):
        with audited(migrated_database, OPERATOR_CTX, "org.create", target_type="org", target_id=ORG) as event:
            raw = event.conn._conn  # deliberate white-box escape
            raw.execute("INSERT INTO collab_orgs (id, created_by) VALUES (%s, %s)", (ORG, OPERATOR))
            raw.execute("COMMIT")

    # The escape committed the mutation (the residual the backstop cannot
    # undo) — but no audit row was written into a detached transaction, and
    # the failure above was loud and named.
    assert _org_count(migrated_database, ORG) == 1
    assert _audit_rows(migrated_database) == []


@live_postgres
def test_live_even_the_raw_connection_cannot_commit_inside_audited(migrated_database):
    """Reaching past the guard still cannot complete the transaction.

    The body runs inside an explicit conn.transaction() block, and psycopg
    itself refuses commit()/rollback() within one — so even code that digs
    out the raw connection (here via the pool, same object) has no way to
    commit the mutation without the event row.
    """

    with pytest.raises(psycopg.ProgrammingError):
        with audited(migrated_database, OPERATOR_CTX, "org.create", target_type="org", target_id=ORG) as event:
            event.conn.execute("INSERT INTO collab_orgs (id, created_by) VALUES (%s, %s)", (ORG, OPERATOR))
            event.conn._conn.commit()  # deliberate white-box escape attempt

    assert _org_count(migrated_database, ORG) == 0
    assert _audit_rows(migrated_database) == []


@live_postgres
def test_live_owner_and_operator_initiated_actions_write_identical_rows(migrated_database):
    """R9: identical event rows apart from the actor columns."""

    owner = ctx(OWNER, org_role=ROLE_OWNER, email="owner@example.test")
    operator = ctx(OPERATOR, org_role=ROLE_MEMBER, platform_role=PLATFORM_ROLE_OPERATOR, email="op@example.test")

    for caller in (owner, operator):
        with audited(
            migrated_database,
            caller,
            "invitation.send",
            target_type="invitation",
            target_id="inv-123",
            org_id=ORG,
        ) as event:
            event.detail = {"email_domain": "example.test"}

    owner_row, operator_row = _audit_rows(migrated_database)
    assert owner_row.pop("actor") == OWNER and owner_row.pop("actor_label") == "owner@example.test"
    assert operator_row.pop("actor") == OPERATOR and operator_row.pop("actor_label") == "op@example.test"
    assert owner_row == operator_row


@live_postgres
def test_live_bootstrap_runbook_grants_an_operator_the_choke_point_resolves(migrated_database):
    """The documented bootstrap procedure, executed verbatim.

    One psql transaction: the grant plus its ``operator.manual`` record. The
    grant must resolve through the same store the auth choke point uses —
    including with **no membership row at all**, which is exactly the fresh
    deployment the bootstrap exists for — and revocation must take effect on
    the next lookup.
    """

    with migrated_database.connection() as conn:
        conn.execute(
            "INSERT INTO collab_platform_roles (user_id, role, granted_by) VALUES (%s, 'operator', NULL)",
            (OPERATOR,),
        )
        conn.execute(
            "INSERT INTO collab_audit_events (actor, actor_label, action, target_type, target_id, detail)"
            " VALUES (%s, %s, 'operator.manual', 'user', %s, %s)",
            (OPERATOR, "op@example.test", OPERATOR, '{"summary": "bootstrap operator grant"}'),
        )

    store = PostgresOrgStore(migrated_database)
    principal = store.resolve_principal(OPERATOR)
    assert principal.platform_role == PLATFORM_ROLE_OPERATOR and principal.membership is None
    assert store.resolve_principal(OWNER).platform_role is None

    # The freshly bootstrapped operator resolves to the hub-scoped context —
    # authenticated, platform authority, no organization.
    resolved = auth_context_from_membership({"sub": OPERATOR}, store)
    assert resolved is not None
    assert resolved.platform_role == PLATFORM_ROLE_OPERATOR and resolved.home_org_id is None

    (row,) = _audit_rows(migrated_database)
    assert row["action"] == "operator.manual" and row["actor"] == OPERATOR
    assert row["detail"] == {"summary": "bootstrap operator grant"}

    # Runbook revocation: effective on the very next lookup, and with no
    # membership either the caller is back to no_organization.
    with migrated_database.connection() as conn:
        conn.execute("UPDATE collab_platform_roles SET status = 'revoked' WHERE user_id = %s", (OPERATOR,))
    assert store.resolve_principal(OPERATOR).platform_role is None
    with pytest.raises(NoOrganizationError):
        auth_context_from_membership({"sub": OPERATOR}, store)


@live_postgres
def test_live_resolve_principal_answers_both_axes_in_one_read(migrated_database):
    with migrated_database.connection() as conn:
        conn.execute("INSERT INTO collab_orgs (id, created_by) VALUES (%s, %s)", (ORG, OPERATOR))
        conn.execute(
            "INSERT INTO collab_org_members (user_id, org_id, role) VALUES (%s, %s, 'member')",
            (OPERATOR, ORG),
        )
        conn.execute("INSERT INTO collab_platform_roles (user_id, role) VALUES (%s, 'operator')", (OPERATOR,))
        conn.execute(
            "INSERT INTO collab_org_members (user_id, org_id, role) VALUES (%s, %s, 'owner')",
            (OWNER, ORG),
        )

    store = PostgresOrgStore(migrated_database)

    both = store.resolve_principal(OPERATOR)
    assert both.membership is not None and both.membership.org_id == ORG and both.membership.role == ROLE_MEMBER
    assert both.platform_role == PLATFORM_ROLE_OPERATOR

    member_only = store.resolve_principal(OWNER)
    assert member_only.membership is not None and member_only.membership.role == ROLE_OWNER
    assert member_only.platform_role is None

    neither = store.resolve_principal(MEMBER)
    assert neither.membership is None and neither.platform_role is None

    resolved = auth_context_from_membership({"sub": OPERATOR}, store)
    assert resolved is not None
    assert resolved.platform_role == PLATFORM_ROLE_OPERATOR
    assert resolved.org_role == ROLE_MEMBER and resolved.org_id == ORG


# ---------------------------------------------------------------------------
# The widened vocabulary, against a real migrated database (#180)
# ---------------------------------------------------------------------------


@live_postgres
def test_live_the_migrated_constraint_accepts_exactly_the_code_vocabulary(migrated_database):
    """The replacement constraint took effect, proven where it matters.

    Migration v5 drops version 2's CHECK by the name PostgreSQL generated for
    an unnamed column constraint. If that generated name were ever different,
    the ``DROP ... IF EXISTS`` would silently match nothing, the ``ADD`` would
    install a second constraint beside the surviving first one, and the old one
    would keep refusing ``service_access.grant`` -- while every text-reading
    unit test in this file kept passing. Only a real migrated database can
    close that gap, so this inserts one row per action in the code vocabulary
    and expects all of them to land.
    """

    for action in sorted(AUDIT_ACTIONS):
        with audited(migrated_database, OPERATOR_CTX, action, target_type="user", target_id=OPERATOR):
            pass

    assert {row["action"] for row in _audit_rows(migrated_database)} == AUDIT_ACTIONS


@live_postgres
def test_live_the_database_still_refuses_an_action_outside_the_vocabulary(migrated_database):
    """The other half: widening the set did not remove the constraint.

    Written as a raw insert rather than through ``audited``, because the Python
    layer refuses an unratified action before any SQL is sent -- so going
    through it would prove the guard and say nothing about the schema. The
    runbook's hand-written inserts do not pass through that guard at all, which
    is exactly whose typo this constraint catches.
    """

    import psycopg

    with pytest.raises(psycopg.errors.CheckViolation):
        with migrated_database.connection() as conn:
            conn.execute(
                "INSERT INTO collab_audit_events (actor, action) VALUES (%s, %s)",
                (OPERATOR, "service_access.granted"),  # a plausible typo of the new action
            )

    # Exactly one constraint governs the column: a leftover from a mis-named
    # DROP would be a second one, and would refuse the value the first accepts.
    with migrated_database.connection() as conn:
        checks = conn.execute(
            """
            SELECT conname FROM pg_constraint
            WHERE conrelid = 'collab_audit_events'::regclass
              AND contype = 'c'
              AND pg_get_constraintdef(oid) LIKE '%%action%%'
            """
        ).fetchall()
    assert [row["conname"] for row in checks] == ["collab_audit_events_action_check"], checks


# ---------------------------------------------------------------------------
# Recording and reconciling service-access grants (#180)
# ---------------------------------------------------------------------------


def _service(database):
    from collab_hub_api.frames.invitations import PostgresInvitationService

    return PostgresInvitationService(database)


ACCEPTER = "acc3pt3r-sub-2222-4222-8222-abcdefabcdef"
ACCEPTER_DISPLAY = DisplayIdentity(email="accepter@example.test", name="Accepter", email_verified=True)


INVITATION = "inv-180"


def _record(service, *, group_path: str, granted: bool, invitation_id: str = INVITATION) -> None:
    service.record_service_access_grant(
        ACCEPTER,
        ACCEPTER_DISPLAY,
        invitation_id=invitation_id,
        org_id=ORG,
        group_path=group_path,
        granted=granted,
    )


def _store(database):
    from collab_hub_api.frames.service_access_state import ServiceAccessStateStore

    return ServiceAccessStateStore(database)


def _issue_invitation(database, invitation_id: str) -> str:
    """One invitation row, for the foreign key the owed-grant row carries."""

    with database.connection() as conn:
        conn.execute(
            """
            INSERT INTO collab_orgs (id, name, created_by) VALUES (%s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (ORG, "Acme", OPERATOR),
        )
        conn.execute(
            """
            INSERT INTO collab_invitations (id, org_id, email, token_hash, created_by, expires_at)
            VALUES (%s, %s, %s, %s, %s, now() + interval '7 days')
            """,
            (invitation_id, ORG, "accepter@example.test", f"hash-{invitation_id}", OPERATOR),
        )
    return invitation_id


def _accept_owing(database, group_paths):
    """What an acceptance leaves behind: the invitation, and what it owes.

    Written through ``claim_pending`` on a plain connection rather than through
    the whole acceptance path, because what these tests are about is the state
    the row is left in. That the write really happens inside the audited
    acceptance transaction is asserted in ``test_invitations.py``, against the
    acceptance itself -- the two halves are deliberately covered in the places
    that own them.
    """

    _issue_invitation(database, INVITATION)
    with database.connection() as conn:
        claim_pending(conn, user_id=ACCEPTER, invitation_id=INVITATION, group_paths=group_paths)


@live_postgres
def test_live_a_grant_attempt_is_recorded_as_a_whole_row(migrated_database):
    """Every column pinned, including the ones carrying an address.

    Asserted as a whole row rather than field by field: the first version of
    this test checked ``target_label`` and not ``actor_label``, which made it
    read as "no address on this row" while the address was sitting in the
    column it did not look at. A whole-row comparison cannot be reassuring
    about a field it forgot.
    """

    service = _service(migrated_database)
    _record(service, group_path="/llm", granted=True)

    (row,) = _audit_rows(migrated_database)
    assert row == {
        "actor": ACCEPTER,
        # The accepter is the actor of their own grant, and `audited` snapshots
        # the address for every row it writes. Present here on purpose.
        "actor_label": "accepter@example.test",
        "action": "service_access.grant",
        "target_type": "user",
        "target_id": ACCEPTER,
        # No *second* label: the row identifies its target by the opaque sub.
        "target_label": None,
        "org_id": ORG,
        "detail": {"invitation_id": "inv-180", "group_path": "/llm", "outcome": "granted"},
    }
    # The address is in `actor_label` and nowhere else -- `detail` is a redacted
    # summary, and an address in there would be a third copy no reader asked for.
    assert "@" not in json.dumps(row["detail"])


@live_postgres
def test_live_an_owed_grant_is_pending_the_moment_the_acceptance_commits(migrated_database):
    """The window the audit-only version left open, closed.

    The `pending` row is written inside the acceptance's own transaction, so
    there is no instant at which somebody has accepted and nothing knows a
    grant is due. Asserted directly here by claiming without settling -- which
    is precisely the state a process killed between the acceptance and the
    identity-provider call leaves behind.
    """

    _accept_owing(migrated_database, ["/llm"])

    (owed,) = _store(migrated_database).outstanding()
    assert (owed.user_id, owed.group_path, owed.state) == (ACCEPTER, "/llm", "pending")
    assert owed.never_attempted, "nobody has seen an answer for this one yet"
    assert owed.invitation_id == INVITATION


@live_postgres
def test_live_every_stopping_point_leaves_something_to_retry(migrated_database):
    """Enumerated rather than argued: each way the process can die, and what
    the reconciler is left holding.

    This is the property the review asked for and the audit-only version could
    not provide -- there, a fault after the group call and before the insert
    lost the fact that anything had been attempted.
    """

    store = _store(migrated_database)

    # 1. died before the call, 2. died after the call, 3. the settle itself
    # failed -- all three are indistinguishable from here, and all three are
    # `pending`, which is the point: the reconciler does not need to tell them
    # apart to do the right thing.
    _accept_owing(migrated_database, ["/llm"])
    assert [g.state for g in store.outstanding()] == ["pending"]

    # 4. the provider refused, and that was recorded.
    store.settle(user_id=ACCEPTER, group_path="/llm", granted=False)
    assert [g.state for g in store.outstanding()] == ["failed"]

    # 5. nothing failed.
    store.settle(user_id=ACCEPTER, group_path="/llm", granted=True)
    assert store.outstanding() == []


@live_postgres
def test_live_a_failed_reattempt_does_not_reopen_access_somebody_holds(migrated_database):
    """`granted` is terminal, because a failed retry removes no membership.

    Treating a later failure as a regression would put somebody who has their
    access back on the outstanding list and keep them there -- the list's own
    version of a false alarm that never clears.
    """

    store = _store(migrated_database)
    _accept_owing(migrated_database, ["/llm"])
    store.settle(user_id=ACCEPTER, group_path="/llm", granted=True)

    store.settle(user_id=ACCEPTER, group_path="/llm", granted=False)
    assert store.outstanding() == []

    with migrated_database.connection() as conn:
        state = conn.execute(
            "SELECT state FROM collab_service_access_grants WHERE user_id = %s AND group_path = %s",
            (ACCEPTER, "/llm"),
        ).fetchone()["state"]
    assert state == "granted"


@live_postgres
def test_live_a_second_invitation_does_not_owe_a_second_grant(migrated_database):
    """One row per person and group, whatever the invitation count.

    The primary key is the idempotence contract. Re-accepting must not reset a
    settled row to `pending` either: that would report somebody who holds their
    access and provoke a pointless re-grant.
    """

    store = _store(migrated_database)
    _accept_owing(migrated_database, ["/llm"])
    store.settle(user_id=ACCEPTER, group_path="/llm", granted=True)

    second = _issue_invitation(migrated_database, "second-inv-180")
    with migrated_database.connection() as conn:
        claim_pending(conn, user_id=ACCEPTER, invitation_id=second, group_paths=["/llm"])

    assert store.outstanding() == []
    with migrated_database.connection() as conn:
        rows = conn.execute(
            "SELECT state, invitation FROM collab_service_access_grants WHERE user_id = %s", (ACCEPTER,)
        ).fetchall()
    assert [(r["state"], r["invitation"]) for r in rows] == [("granted", INVITATION)], (
        "the first invitation stays as the recorded context, and the state is untouched"
    )


@live_postgres
def test_live_each_owed_group_is_settled_independently(migrated_database):
    """A two-group acceptance where one call fails owes exactly the failed one."""

    store = _store(migrated_database)
    _accept_owing(migrated_database, ["/llm", "/services/next"])
    store.settle(user_id=ACCEPTER, group_path="/llm", granted=True)
    store.settle(user_id=ACCEPTER, group_path="/services/next", granted=False)

    assert [(g.group_path, g.state) for g in store.outstanding()] == [("/services/next", "failed")]


@live_postgres
def test_live_owing_nothing_writes_no_rows(migrated_database):
    """The default. A deployment that grants nothing accumulates no queue."""

    _accept_owing(migrated_database, [])
    assert _store(migrated_database).outstanding() == []
    with migrated_database.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM collab_service_access_grants").fetchone()["n"] == 0


@live_postgres
def test_live_an_owed_grant_cannot_name_an_invitation_that_does_not_exist(migrated_database):
    """The foreign key, asserted rather than assumed.

    A dangling invitation id would make the outstanding list unjoinable to the
    person it is about, which is the one thing a reader needs it for.
    """

    import psycopg

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with migrated_database.connection() as conn:
            claim_pending(conn, user_id=ACCEPTER, invitation_id="no-such-invitation", group_paths=["/llm"])


@live_postgres
def test_live_the_state_vocabulary_is_check_constrained(migrated_database):
    """A typo'd state would be a row no reconciliation query ever finds."""

    import psycopg

    invitation = _issue_invitation(migrated_database, "state-check-inv")
    with pytest.raises(psycopg.errors.CheckViolation):
        with migrated_database.connection() as conn:
            conn.execute(
                """
                INSERT INTO collab_service_access_grants (user_id, group_path, state, invitation)
                VALUES (%s, %s, %s, %s)
                """,
                (ACCEPTER, "/llm", "granting", invitation),
            )


@live_postgres
def test_live_the_documented_manual_repair_actually_clears_the_item(migrated_database):
    """The runbook's repair must touch the thing the query reads.

    An earlier version told an operator to add the group in Keycloak and record
    an `operator.manual` audit row. That row is invisible to reconciliation, so
    following the runbook left the candidate outstanding forever and the list
    would have grown one immortal entry per repair. Both halves are asserted:
    the audit note alone does nothing, and the documented update clears it.
    """

    store = _store(migrated_database)
    _accept_owing(migrated_database, ["/llm"])
    store.settle(user_id=ACCEPTER, group_path="/llm", granted=False)
    assert len(store.outstanding()) == 1

    with migrated_database.connection() as conn:
        conn.execute(
            """
            INSERT INTO collab_audit_events (actor, actor_label, action, detail)
            VALUES (%s, %s, 'operator.manual', %s)
            """,
            ("op-sub", "op@example.test", json.dumps({"summary": "added to /llm by hand"})),
        )
    assert len(store.outstanding()) == 1, "an operator.manual row is the note that work happened, not the state change"

    # What the runbook now documents.
    with migrated_database.connection() as conn:
        conn.execute(
            """
            UPDATE collab_service_access_grants
            SET state = 'granted', updated_at = now()
            WHERE user_id = %s AND group_path = %s
            """,
            (ACCEPTER, "/llm"),
        )
    assert store.outstanding() == []
