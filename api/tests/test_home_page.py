"""The Collab Hub home page (``/web/home``): what a person sees at the hub's address."""

from __future__ import annotations

import html
import os
import re
import sys

import pytest
from pydantic import ValidationError

sys.path.insert(0, os.path.dirname(__file__))

from test_web_surface import _StubIdp, make_web_app, sign_in, web_client  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_bearer_issuer(monkeypatch):
    monkeypatch.delenv("FRAMES_BEARER_ISSUER", raising=False)


@pytest.fixture
def idp(monkeypatch):
    from collab_hub_api.frames import auth

    endpoint = _StubIdp()
    monkeypatch.setitem(auth.__dict__, "_jwks_clients", {})
    try:
        yield endpoint
    finally:
        endpoint.close()


HOME = "/web/home"
ACCOUNT = "/web/account"
SIGN_IN_HREF = html.escape("/web/signin?next=%2Fweb%2Fhome")
SIGN_UP_HREF = html.escape("/web/signin?register=1&next=%2Fweb%2Fhome")


async def test_a_signed_out_visitor_is_offered_sign_in_and_sign_up(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        page = await client.get(HOME)
    assert page.status_code == 200
    assert "<title>Collab Hub</title>" in page.text
    assert f'href="{SIGN_IN_HREF}"' in page.text
    assert f'href="{SIGN_UP_HREF}"' in page.text
    assert "Sign out" not in page.text


async def test_signing_in_from_the_page_returns_to_it_and_shows_who_is_signed_in(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        callback = await sign_in(client, idp, next_path=HOME)
        assert callback.headers["location"] == HOME
        page = await client.get(HOME)
    assert page.status_code == 200
    assert "Alice Example" in page.text
    assert 'action="/web/signout"' in page.text
    assert 'name="csrf_token"' in page.text
    assert f'href="{SIGN_IN_HREF}"' not in page.text
    assert f'href="{SIGN_UP_HREF}"' not in page.text


def link(url: str) -> str:
    return f'href="{html.escape(url)}"'


async def test_every_link_is_built_from_the_deployment_domain(tmp_path, idp):
    app = make_web_app(tmp_path, idp, web={"hub_domain": "hub.example.org"})
    async with web_client(app) as client:
        await sign_in(client, idp, next_path=HOME)
        page = await client.get(HOME)
    assert link("https://collab.hub.example.org/") in page.text
    assert link("https://collab.hub.example.org/frames") in page.text
    assert link("https://frames.hub.example.org/docs") in page.text
    # The account link stays on the hub, in the hub's own look.
    assert link(ACCOUNT) in page.text


async def test_the_domain_defaults_to_the_issuer_host_without_keycloak(tmp_path, idp):
    app = make_web_app(tmp_path, idp, web={"issuer_url": "https://keycloak.dev.example.org/realms/nebari"})
    async with web_client(app) as client:
        page = await client.get(HOME)
    assert link("https://collab.dev.example.org/") in page.text
    assert link("https://collab.dev.example.org/frames") in page.text


DOWNLOAD_URL = "https://downloads.example.com/collab"
DOCS_URL = "https://docs.example.com/collab"


async def test_desktop_download_and_docs_link_where_the_deployment_names_them(tmp_path, idp):
    app = make_web_app(tmp_path, idp, web={"desktop_download_url": DOWNLOAD_URL, "docs_url": DOCS_URL})
    async with web_client(app) as client:
        await sign_in(client, idp, next_path=HOME)
        page = await client.get(HOME)
    assert link(DOWNLOAD_URL) in page.text
    assert link(DOCS_URL) in page.text


async def test_unnamed_download_and_docs_render_no_link(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        page = await client.get(HOME)
    assert "Download" not in page.text
    assert "Docs" not in page.text


@pytest.mark.parametrize("field", ["desktop_download_url", "docs_url"])
def test_a_link_that_is_not_an_https_url_fails_the_rollout(tmp_path, idp, field):
    with pytest.raises(ValidationError, match=field):
        make_web_app(tmp_path, idp, web={field: "javascript:alert(1)"})


@pytest.mark.parametrize("domain", ["https://hub.example.org", "hub.example.org/x", "a@hub.example.org", "hub example"])
def test_a_hub_domain_that_is_not_a_bare_host_fails_the_rollout(tmp_path, idp, domain):
    with pytest.raises(ValidationError, match="hub_domain"):
        make_web_app(tmp_path, idp, web={"hub_domain": domain})


async def test_the_page_carries_collab_branding_served_without_a_session(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        page = await client.get(HOME)
        assert '<link rel="stylesheet" href="/web/home.css">' in page.text
        assert '<link rel="icon" href="/web/collab-mark.svg" type="image/svg+xml">' in page.text
        stylesheet = await client.get("/web/home.css")
        mark = await client.get("/web/collab-mark.svg")
    assert stylesheet.status_code == 200
    assert stylesheet.headers["content-type"].startswith("text/css")
    assert ':root[data-theme="dark"]' in stylesheet.text
    assert mark.status_code == 200
    assert mark.headers["content-type"] == "image/svg+xml"
    for foreign in ("Nebari", "Launchpad"):
        assert foreign not in page.text


FONT_PATHS = [f"/web/fonts/ibm-plex-sans-{w}.woff2" for w in (400, 500, 600, 700)] + [
    "/web/fonts/cascadia-code-400.woff2",
    "/web/fonts/ibm-plex-mono-400.woff2",
]


async def test_the_page_brings_its_own_fonts_from_this_origin(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        page = await client.get(HOME)
        css = await client.get("/web/home.css")
        fonts = [await client.get(path) for path in FONT_PATHS]
    policy = page.headers["content-security-policy"]
    assert "font-src 'self'" in policy
    for path, font in zip(FONT_PATHS, fonts, strict=True):
        assert font.status_code == 200, path
        assert font.headers["content-type"] == "font/woff2", path
        assert font.content[:4] == b"wOF2", path
        assert f"url({path})" in css.text


SHOT_VIEWS = ("frames", "hub", "hub-usage")


async def test_the_page_shows_real_collab_screens_in_both_themes(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        page = await client.get(HOME)
        for view in SHOT_VIEWS:
            for scheme in ("dark", "light"):
                path = f"/web/shots/{view}-{scheme}.jpg"
                assert path in page.text, path
                shot = await client.get(path)
                assert shot.status_code == 200, path
                assert shot.headers["content-type"] == "image/jpeg", path
                assert shot.content[:3] == b"\xff\xd8\xff", path


async def test_account_and_sign_out_live_in_the_avatar_menu(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        await sign_in(client, idp, next_path=HOME)
        page = await client.get(HOME)
    menu = page.text[page.text.index('<details class="menu">') : page.text.index("</details>")]
    assert "Alice Example" in menu
    assert link(ACCOUNT) in menu
    assert 'action="/web/signout"' in menu
    # No account card in the page body; the footer lists it with the other hub links.
    assert f'class="door" {link(ACCOUNT)}' not in page.text


async def test_the_demo_runs_one_script_from_this_origin_and_nothing_inline(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        page = await client.get(HOME)
        script = await client.get("/web/home.js")
        other = await client.get("/web/data-statement")
    policy = page.headers["content-security-policy"]
    assert "script-src 'self';" in policy
    assert "unsafe-inline" not in policy and "unsafe-eval" not in policy
    assert '<script src="/web/home.js" defer></script>' in page.text
    # Only the two scripts this origin serves, and nothing inline.
    assert re.findall(r"<script[^>]*>", page.text) == [
        '<script src="/web/theme.js">',
        '<script src="/web/home.js" defer>',
    ]
    assert script.status_code == 200
    assert script.headers["content-type"].startswith("text/javascript")
    # Every other page of the surface still runs no script at all.
    assert "script-src" not in other.headers["content-security-policy"]


async def test_the_model_picker_shows_real_model_logos(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        page = await client.get(HOME)
        for vendor in (
            "anthropic",
            "openai",
            "gemini",
            "meta",
            "mistral",
            "deepseek",
            "qwen",
            "xai",
            "moonshot",
            "zai",
        ):
            path = f"/web/models/{vendor}.svg"
            assert path in page.text, path
            logo = await client.get(path)
            assert logo.status_code == 200, path
            assert logo.headers["content-type"] == "image/svg+xml", path
    assert "Thinking Machines" in page.text


async def test_every_connector_named_on_the_page_shows_its_logo(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        page = await client.get(HOME)
        for name in ("gmail", "google-drive", "google-calendar", "slack", "github", "notion", "sharepoint"):
            path = f"/web/connectors/{name}.svg"
            assert path in page.text, path
            logo = await client.get(path)
            assert logo.status_code == 200, path
            assert logo.headers["content-type"] == "image/svg+xml", path


async def test_the_account_page_needs_a_session(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        response = await client.get(ACCOUNT)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/web/signin")


async def test_the_account_page_shows_who_is_signed_in_in_the_home_page_look(tmp_path, idp):
    app = make_web_app(tmp_path, idp, web={"hub_domain": "hub.example.org"})
    async with web_client(app) as client:
        await sign_in(client, idp, next_path=ACCOUNT)
        page = await client.get(ACCOUNT)
    assert page.status_code == 200
    assert "Alice Example" in page.text
    assert "alice@example.com" in page.text
    assert "hub.example.org" in page.text
    assert '<link rel="stylesheet" href="/web/home.css">' in page.text
    assert 'action="/web/signout"' in page.text
    assert "font-src 'self'" in page.headers["content-security-policy"]
    # The marketing footer belongs to the landing page only.
    assert "<footer" not in page.text


async def test_account_links_go_to_the_hub_account_page_and_never_to_the_realm(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        await sign_in(client, idp, next_path=HOME)
        page = await client.get(HOME)
    assert link(ACCOUNT) in page.text
    assert f"{idp.issuer}/account" not in page.text


async def test_the_theme_is_applied_before_the_page_draws(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        page = await client.get(HOME)
        script = await client.get("/web/theme.js")
    head = page.text[: page.text.index("</head>")]
    assert '<script src="/web/theme.js"></script>' in head
    assert script.status_code == 200
    assert "data-theme" in script.text
    assert "data-theme-toggle" in page.text


async def test_the_hub_root_opens_the_home_page_where_the_web_pages_run(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        response = await client.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == HOME


BILLING = "/web/account/billing"


async def test_the_billing_page_needs_a_session(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        response = await client.get(BILLING)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/web/signin")


@pytest.mark.parametrize("path", [ACCOUNT, BILLING])
async def test_the_account_dashboard_has_a_sidebar_with_account_and_billing(tmp_path, idp, path):
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        await sign_in(client, idp, next_path=path)
        page = await client.get(path)
    assert page.status_code == 200
    start = page.text.index('<nav class="dash-nav"')
    sidebar = page.text[start : page.text.index("</nav>", start)]
    assert link(ACCOUNT) in sidebar
    assert link(BILLING) in sidebar
    # The page being shown is the one marked current.
    assert f'{link(path)} aria-current="page"' in sidebar
    # The product links live in the sidebar too, and the landing page's
    # framed column is not used here.
    assert link("https://collab.127.0.0.1/") in sidebar
    assert link("https://collab.127.0.0.1/frames") in sidebar
    assert 'class="frame"' not in page.text
    assert "<footer" not in page.text
    assert "font-src 'self'" in page.headers["content-security-policy"]


async def test_the_dashboard_sidebar_can_be_collapsed(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        await sign_in(client, idp, next_path=ACCOUNT)
        page = await client.get(ACCOUNT)
    # A button folds the sidebar to its icons; each link keeps its name as a
    # tooltip, so a folded sidebar still says where each icon goes.
    assert (
        '<button type="button" class="dash-fold" data-sidebar-toggle aria-controls="dash-nav" '
        'aria-expanded="true" aria-label="Collapse sidebar">'
    ) in page.text
    assert f'{link(BILLING)} title="Billing"' in page.text


async def test_the_billing_page_is_empty_for_now(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        await sign_in(client, idp, next_path=BILLING)
        page = await client.get(BILLING)
    assert "<h1>Billing</h1>" in page.text


async def test_a_logged_in_visitor_is_not_offered_the_download_in_the_header(tmp_path, idp):
    app = make_web_app(tmp_path, idp, web={"desktop_download_url": DOWNLOAD_URL})
    async with web_client(app) as client:
        anonymous = await client.get(HOME)
        await sign_in(client, idp, next_path=HOME)
        signed_in = await client.get(HOME)

    def header_nav(text: str) -> str:
        start = text.index('<nav class="nav">')
        return text[start : text.index("</nav>", start)]

    assert link(DOWNLOAD_URL) in header_nav(anonymous.text)
    assert link(DOWNLOAD_URL) not in header_nav(signed_in.text)
