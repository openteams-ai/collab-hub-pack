"""Serving the registration pages' built bundle.

The invitation-acceptance page is a small React app, built beside the admin
panel and served on its own public paths under ``/invite``. These tests cover
placement, gating and headers; what the app renders, and how it handles the
invitation code, is the front-end suite's business.
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from urllib.parse import urljoin

import pytest

sys.path.insert(0, str(Path(__file__).parent))

pytest.importorskip("jwt")

from registration_bundle import built_dist  # noqa: E402
from test_web_surface import (  # noqa: E402
    _StubIdp,
    make_web_app,
    sign_in,
    web_client,
)

from collab_hub_api.frames.identity import IDENTITY_CLAIM_ENV  # noqa: E402
from collab_hub_api.frames.org_source import ORG_SOURCE_ENV  # noqa: E402
from collab_hub_api.web.data_statement import DATA_STATEMENT_TEXT  # noqa: E402
from collab_hub_api.web.surface import ACCEPT_PAGE_PATH, ACCEPT_SESSION_PATH  # noqa: E402

PUBLIC_BASE_URL = "https://web.test"


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


def build_app(tmp_path, idp, *, dist: Path | None = None):
    web = {"public_base_url": PUBLIC_BASE_URL}
    if dist is not None:
        web["admin_ui_dist"] = str(dist)
    return make_web_app(tmp_path, idp, web=web)


@pytest.mark.asyncio
async def test_an_invitee_with_no_account_is_served_the_registration_app(tmp_path, idp: _StubIdp):
    app = build_app(tmp_path, idp, dist=built_dist(tmp_path))

    async with web_client(app) as client:
        response = await client.get(ACCEPT_PAGE_PATH)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "id=root" in response.text


def asset_urls(document: str) -> list[str]:
    """Every asset the served document tells the browser to fetch."""

    return re.findall(r'(?:src|href)="([^"]+)"', document)


@pytest.mark.asyncio
async def test_every_asset_the_document_references_resolves_without_a_session(tmp_path, idp: _StubIdp):
    """The invitee has no account yet, so the bundle has to load anonymously.

    Resolved the way a browser would, from the address the document was served
    at: Vite emits document-relative URLs, so this is the assertion that fails
    when the document and its files stop being siblings.
    """

    app = build_app(tmp_path, idp, dist=built_dist(tmp_path))

    async with web_client(app) as client:
        document = await client.get(ACCEPT_PAGE_PATH)
        base = str(document.url)
        fetched = {url: (await client.get(urljoin(base, url))).status_code for url in asset_urls(document.text)}

    assert fetched, "the built document must reference at least one asset"
    assert all(status == 200 for status in fetched.values()), fetched


@pytest.mark.asyncio
async def test_only_a_file_directly_inside_the_asset_directory_is_public(tmp_path, idp: _StubIdp):
    """The public grant is one flat directory of files, and nothing around it."""

    app = build_app(tmp_path, idp, dist=built_dist(tmp_path))

    async with web_client(app) as client:
        missing = await client.get("/invite/assets/not-in-the-build.js")
        directory = await client.get("/invite/assets/")
        nested = await client.get("/invite/assets/deeper/index-abc123.js")
        document_by_name = await client.get("/invite/assets/index.html")

    assert missing.status_code == 404
    assert document_by_name.status_code == 404
    for guarded in (directory, nested):
        assert guarded.status_code == 303
        assert guarded.headers["location"].startswith("/web/signin?")


@pytest.mark.asyncio
async def test_the_registration_app_may_run_its_own_bundle_and_nothing_else(tmp_path, idp: _StubIdp):
    """Script is allowed from this origin only, on the document and its files.

    The app is a built bundle, so every script and stylesheet is a file served
    here: no inline script, no ``unsafe-eval`` and no external host is needed
    to run it. Every other header of the surface is unchanged.
    """

    app = build_app(tmp_path, idp, dist=built_dist(tmp_path))

    async with web_client(app) as client:
        document = await client.get(ACCEPT_PAGE_PATH)
        asset = await client.get("/invite/assets/index-abc123.js")
        redeem = await client.post("/invite/accept/redeem")
        elsewhere = await client.get("/invite/something-else")

    for response in (document, asset):
        csp = response.headers["content-security-policy"]
        assert "default-src 'none'" in csp
        assert "script-src 'self'" in csp
        assert "connect-src 'self'" in csp
        assert "font-src 'self'" in csp
        assert "form-action 'none'" in csp
        assert "frame-ancestors 'none'" in csp
        assert "base-uri 'none'" in csp
        assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "no-store" in response.headers["cache-control"]
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"

    # The paths beside it never needed script and keep the policy they had.
    for response in (redeem, elsewhere):
        assert "script-src" not in response.headers["content-security-policy"]


@pytest.mark.asyncio
async def test_the_app_is_told_an_anonymous_browser_has_no_session(tmp_path, idp: _StubIdp):
    """What the app asks first, answered for someone with no account yet.

    No CSRF token: there is no session to bind one to, and the app sends this
    person to create an account instead of posting anything.
    """

    app = build_app(tmp_path, idp, dist=built_dist(tmp_path))

    async with web_client(app) as client:
        response = await client.get(ACCEPT_SESSION_PATH)

    assert response.status_code == 200
    assert response.json() == {
        "signed_in": False,
        "claims_current": False,
        "csrf_token": None,
        "identity": None,
        "require_verified_email": True,
        # The statement shown beside the accept button is the same constant the
        # canonical page serves, so the two can never be different texts.
        "data_statement": DATA_STATEMENT_TEXT,
    }
    assert "no-store" in response.headers["cache-control"]


@pytest.mark.asyncio
async def test_the_app_is_told_who_is_signed_in_and_handed_their_csrf_token(tmp_path, idp: _StubIdp):
    app = build_app(tmp_path, idp, dist=built_dist(tmp_path))

    async with web_client(app) as client:
        idp.claims_override = {"name": "Alice Example", "email": "alice@example.com", "email_verified": True}
        await sign_in(client, idp, next_path=ACCEPT_PAGE_PATH)
        answer = (await client.get(ACCEPT_SESSION_PATH)).json()
        # The token is the session's own: the sign-out route, which checks it
        # through the same comparison every POST here does, accepts it.
        signed_out = await client.post("/web/signout", headers={"X-CSRF-Token": answer["csrf_token"]})

    assert answer["signed_in"] is True
    assert answer["claims_current"] is True
    assert answer["identity"] == "Alice Example"
    assert signed_out.status_code == 303


@pytest.mark.asyncio
async def test_the_session_answer_does_not_depend_on_a_built_bundle(tmp_path, idp: _StubIdp):
    app = build_app(tmp_path, idp)

    async with web_client(app) as client:
        response = await client.get(ACCEPT_SESSION_PATH)

    assert response.status_code == 200
    assert response.json()["signed_in"] is False


@pytest.mark.asyncio
async def test_a_deployment_with_no_built_bundle_says_so_on_the_invitation_page(tmp_path, idp: _StubIdp):
    """No build means no app to show, and the invitee is told that plainly.

    Unlike the panel, this path stays mounted without its bundle: the link in
    an invitation email has to answer with something a person can act on, and
    an unrouted path under ``/invite`` would answer with the API's credential
    refusal instead.
    """

    app = build_app(tmp_path, idp)

    async with web_client(app) as client:
        page = await client.get(ACCEPT_PAGE_PATH)
        asset = await client.get("/invite/assets/index-abc123.js")

    assert page.status_code == 503
    assert "id=root" not in page.text
    assert "<script" not in page.text
    assert "Your invitation has not been used" in page.text
    assert asset.status_code != 200


@pytest.mark.parametrize("dist", ["unset", "empty"])
def test_a_hub_without_the_registration_build_says_so_at_error_level(tmp_path, idp, caplog, dist):
    """Every invitation link would answer 503, so starting quietly is not enough.

    Logged at error level whether the bundle directory is not configured at all
    or is configured and holds no registration build: either way the image was
    built without it, and an operator should see that before an invitee does.
    """

    web = {"public_base_url": "https://web.test"}
    if dist == "empty":
        (tmp_path / "empty-dist").mkdir()
        web["admin_ui_dist"] = str(tmp_path / "empty-dist")
    with caplog.at_level(logging.WARNING):
        make_web_app(tmp_path, idp, web=web)

    refusals = [r for r in caplog.records if r.getMessage() == "registration_ui_missing"]
    assert len(refusals) == 1
    assert refusals[0].levelno == logging.ERROR
