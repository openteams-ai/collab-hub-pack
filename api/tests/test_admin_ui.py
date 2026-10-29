"""Serving the admin panel's built assets.

The panel is a single-page app, so the document it boots from is served by this
API and sits behind the same gate as everything else under ``/admin``. These
tests cover placement and gating only; what the app then renders is the
front-end suite's business.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import urljoin

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

PUBLIC_BASE_URL = "https://web.test"
SHELL = (
    "<!doctype html><title>Collab Hub admin</title>"
    # Exactly the shape Vite emits: document-relative, so how the document is
    # addressed decides where the browser looks for these.
    '<script type="module" crossorigin src="./assets/index-abc123.js"></script>'
    '<link rel="stylesheet" crossorigin href="./assets/index-abc123.css">'
    "<div id=root></div>"
)


@pytest.fixture(autouse=True)
def membership_env(monkeypatch):
    monkeypatch.delenv("FRAMES_BEARER_ISSUER", raising=False)
    monkeypatch.setenv(IDENTITY_CLAIM_ENV, "sub")
    monkeypatch.setenv(ORG_SOURCE_ENV, "membership")


@pytest.fixture
def idp(monkeypatch):
    from collab_hub_api.frames import auth

    endpoint = _StubIdp()
    monkeypatch.setitem(auth.__dict__, "_jwks_clients", {})
    try:
        yield endpoint
    finally:
        endpoint.close()


def built_dist(tmp_path: Path) -> Path:
    dist = tmp_path / "admin-ui-dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(SHELL)
    (dist / "assets" / "index-abc123.js").write_text("console.log('panel')")
    (dist / "assets" / "index-abc123.css").write_text("body{margin:0}")
    return dist


def build_app(tmp_path, idp, *, dist: Path | None = None):
    web = {"public_base_url": PUBLIC_BASE_URL}
    if dist is not None:
        web["admin_ui_dist"] = str(dist)
    return make_web_app(tmp_path, idp, web=web)


@pytest.mark.asyncio
async def test_an_operator_is_served_the_panel_shell(tmp_path, idp: _StubIdp):
    """``/admin`` redirects to ``/admin/``, which serves the document.

    The slash is not cosmetic; see the asset-resolution test below and the note
    in ``routers.admin_ui``.
    """

    app = build_app(tmp_path, idp, dist=built_dist(tmp_path))

    async with app.router.lifespan_context(app), web_client(app) as client:
        app.state.org_store.set_platform_role(idp.sub)
        await sign_in(client, idp, next_path="/web")
        bounce = await client.get("/admin")
        response = await client.get("/admin", follow_redirects=True)

    assert bounce.status_code == 308
    assert bounce.headers["location"].endswith("/admin/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "id=root" in response.text


@pytest.mark.asyncio
async def test_a_signed_in_non_operator_cannot_reach_the_panel(tmp_path, idp: _StubIdp):
    app = build_app(tmp_path, idp, dist=built_dist(tmp_path))

    async with app.router.lifespan_context(app), web_client(app) as client:
        await sign_in(client, idp, next_path="/web")
        response = await client.get("/admin")

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_the_unauthenticated_are_sent_to_sign_in(tmp_path, idp: _StubIdp):
    app = build_app(tmp_path, idp, dist=built_dist(tmp_path))

    async with app.router.lifespan_context(app), web_client(app) as client:
        response = await client.get("/admin")

    assert response.status_code == 303
    assert "/web/signin" in response.headers["location"]


@pytest.mark.asyncio
async def test_a_deployment_with_no_built_panel_serves_none(tmp_path, idp: _StubIdp):
    """Absent assets mount no routes, rather than a route that 500s on open.

    ``/admin`` matches no route on such a deployment, so a signed-in operator
    gets the router's plain 404. (While the MCP app was mounted at ``/`` it
    answered here instead, with its own credential refusal; #67 removed that
    catch-all, so an unrouted path now answers as unrouted.)

    What matters for this spec is the part below: no shell reaches the browser.
    """

    app = build_app(tmp_path, idp)

    async with app.router.lifespan_context(app), web_client(app) as client:
        app.state.org_store.set_platform_role(idp.sub)
        await sign_in(client, idp, next_path="/web")
        response = await client.get("/admin")

    assert response.status_code == 404
    assert "id=root" not in response.text


@pytest.mark.asyncio
async def test_the_panel_document_may_run_its_own_bundle_and_nothing_else(tmp_path, idp: _StubIdp):
    """The surface's default CSP names no script source; the panel needs one.

    Widened for the panel's own paths only, and only to ``'self'``: the bundle
    and its fonts are served from this origin, so no external host, no inline
    script, and no ``unsafe-eval`` is required to run it.
    """

    app = build_app(tmp_path, idp, dist=built_dist(tmp_path))

    async with app.router.lifespan_context(app), web_client(app) as client:
        app.state.org_store.set_platform_role(idp.sub)
        await sign_in(client, idp, next_path="/web")
        panel = await client.get("/admin")
        page = await client.get("/admin/invitations")

    csp = panel.headers["content-security-policy"]
    assert "script-src 'self'" in csp
    assert "connect-src 'self'" in csp
    assert "font-src 'self'" in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert "frame-ancestors 'none'" in csp

    # The pages that never needed script keep the policy they had.
    assert "script-src" not in page.headers["content-security-policy"]


def asset_urls(document: str) -> list[str]:
    """Every asset the served document tells the browser to fetch."""

    return re.findall(r'(?:src|href)="([^"]+)"', document)


@pytest.mark.asyncio
async def test_every_asset_the_document_references_actually_resolves(tmp_path, idp: _StubIdp):
    """The bug this pins: a blank panel whose document served perfectly well.

    Vite emits root-relative-to-the-document URLs (``./assets/…``). Served at
    ``/admin`` with no trailing slash, the browser resolves those against
    ``/`` and asks for ``/assets/…`` -- outside the panel, refused by the
    protection map, so nothing loads and the page renders empty. The document
    itself is a perfect 200 throughout, which is why asserting on its markup
    caught nothing.

    Resolving each reference the way a browser would, and fetching it, is the
    assertion that fails when that breaks.
    """

    app = build_app(tmp_path, idp, dist=built_dist(tmp_path))

    async with app.router.lifespan_context(app), web_client(app) as client:
        app.state.org_store.set_platform_role(idp.sub)
        await sign_in(client, idp, next_path="/web")
        document = await client.get("/admin", follow_redirects=True)
        base = str(document.url)
        fetched = {
            url: (await client.get(urljoin(base, url), follow_redirects=True)).status_code
            for url in asset_urls(document.text)
        }

    assert document.status_code == 200
    assert fetched, "the built document must reference at least one asset"
    assert all(status == 200 for status in fetched.values()), fetched


@pytest.mark.asyncio
async def test_the_panel_api_resolves_from_the_document_the_browser_was_given(tmp_path, idp: _StubIdp):
    """The panel calls ``api/session`` relative to its own document."""

    app = build_app(tmp_path, idp, dist=built_dist(tmp_path))

    async with app.router.lifespan_context(app), web_client(app) as client:
        app.state.org_store.set_platform_role(idp.sub)
        await sign_in(client, idp, next_path="/web")
        document = await client.get("/admin", follow_redirects=True)
        session = await client.get(urljoin(str(document.url), "api/session"))

    assert session.status_code == 200
    assert session.json()["role"] == "operator"
