"""Sign-in reconciles the operator role against the ID token's groups claim.

The unit-level decisions live in ``test_platform_role_sync``. What is asserted
here is the wiring: that a real sign-in through the real callback actually
reaches the sync, and that a deployment which named no admin group is left
exactly as it was.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

pytest.importorskip("jwt")

from test_web_surface import (  # noqa: E402
    _StubIdp,
    make_web_app,
    sign_in,
    web_client,
)

from collab_hub_api.frames.identity import IDENTITY_CLAIM_ENV  # noqa: E402
from collab_hub_api.frames.org_source import ORG_SOURCE_ENV  # noqa: E402
from collab_hub_api.frames.orgs import PLATFORM_ROLE_OPERATOR  # noqa: E402


@pytest.fixture(autouse=True)
def membership_env(monkeypatch):
    """Membership mode: the only mode in which an operator role exists at all.

    Under claims-sourced auth the ``collab_`` tables are never read, so the
    platform-role axis is structurally ``None`` -- which is why ``make_app``
    mounts neither the operator page nor the admin API there.

    Identity is pinned to ``sub`` because roles are keyed on the principal the
    session carries, and the sync writes under that same principal. That is the
    configuration the runbook documents.
    """

    monkeypatch.delenv("FRAMES_BEARER_ISSUER", raising=False)
    monkeypatch.setenv(IDENTITY_CLAIM_ENV, "sub")
    monkeypatch.setenv(ORG_SOURCE_ENV, "membership")


ADMIN_GROUP = "/hub-admins"
PUBLIC_BASE_URL = "https://web.test"


@pytest.fixture
def idp(monkeypatch):
    from collab_hub_api.frames import auth

    endpoint = _StubIdp()
    monkeypatch.setitem(auth.__dict__, "_jwks_clients", {})
    try:
        yield endpoint
    finally:
        endpoint.close()


def _org_store(app):
    return app.state.org_store


async def _sign_in_with_groups(app, idp, groups):
    idp.claims_override = {"groups": groups}
    async with web_client(app) as client:
        response = await sign_in(client, idp, next_path="/web")
    assert response.status_code == 303
    return response


@pytest.mark.asyncio
async def test_signing_in_from_the_admin_group_grants_the_operator_role(tmp_path, idp: _StubIdp):
    app = make_web_app(tmp_path, idp, web={"admin_group": ADMIN_GROUP, "public_base_url": PUBLIC_BASE_URL})

    async with app.router.lifespan_context(app):
        await _sign_in_with_groups(app, idp, [ADMIN_GROUP, "/everyone"])
        principal = _org_store(app).resolve_principal(idp.sub)

    assert principal.platform_role == PLATFORM_ROLE_OPERATOR


@pytest.mark.asyncio
async def test_signing_in_without_the_group_revokes_a_previously_synced_role(tmp_path, idp: _StubIdp):
    app = make_web_app(tmp_path, idp, web={"admin_group": ADMIN_GROUP, "public_base_url": PUBLIC_BASE_URL})

    async with app.router.lifespan_context(app):
        # Another operator, so this is not the last one: sync keeps the last.
        _org_store(app).set_platform_role("u-bootstrap")
        await _sign_in_with_groups(app, idp, [ADMIN_GROUP])
        assert _org_store(app).resolve_principal(idp.sub).platform_role == PLATFORM_ROLE_OPERATOR

        await _sign_in_with_groups(app, idp, ["/everyone"])
        principal = _org_store(app).resolve_principal(idp.sub)

    assert principal.platform_role is None


@pytest.mark.asyncio
async def test_a_deployment_that_named_no_admin_group_is_unchanged_by_sign_in(tmp_path, idp: _StubIdp):
    """The claim is present and carries the group; nothing reads it."""

    app = make_web_app(tmp_path, idp, web={"public_base_url": PUBLIC_BASE_URL})

    async with app.router.lifespan_context(app):
        await _sign_in_with_groups(app, idp, [ADMIN_GROUP])
        principal = _org_store(app).resolve_principal(idp.sub)

    assert principal.platform_role is None


@pytest.mark.asyncio
async def test_a_hand_granted_operator_survives_a_sign_in_carrying_no_groups(tmp_path, idp: _StubIdp):
    """The bootstrap operator must not be locked out by their own sign-in."""

    app = make_web_app(tmp_path, idp, web={"admin_group": ADMIN_GROUP, "public_base_url": PUBLIC_BASE_URL})

    async with app.router.lifespan_context(app):
        _org_store(app).set_platform_role(idp.sub)
        await _sign_in_with_groups(app, idp, [])
        principal = _org_store(app).resolve_principal(idp.sub)

    assert principal.platform_role == PLATFORM_ROLE_OPERATOR


@pytest.mark.asyncio
async def test_the_sync_runs_off_the_event_loop(tmp_path, idp: _StubIdp):
    """The Postgres sync makes blocking database calls. Run on the loop, every
    sign-in would stall every other request this process is serving."""

    import threading

    app = make_web_app(tmp_path, idp, web={"admin_group": ADMIN_GROUP, "public_base_url": PUBLIC_BASE_URL})

    async with app.router.lifespan_context(app):
        sync = app.state.platform_role_sync
        reconcile = sync.reconcile
        threads: list[int] = []

        def recording(**kwargs):
            threads.append(threading.get_ident())
            return reconcile(**kwargs)

        sync.reconcile = recording
        await _sign_in_with_groups(app, idp, [ADMIN_GROUP])
        principal = _org_store(app).resolve_principal(idp.sub)

    assert threads and threads[0] != threading.get_ident()
    assert principal.platform_role == PLATFORM_ROLE_OPERATOR
