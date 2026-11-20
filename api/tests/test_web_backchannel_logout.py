"""Back-channel logout: when the realm session ends, the Hub's web session ends.

openteams-ai/subscription-manager-pack#79, the Hub's half of "log out anywhere = logged out
everywhere". The session cookie carries the realm session's ``sid``; Keycloak's
back-channel logout (``POST /web/oidc/backchannel-logout``) names that ``sid``
when the realm session ends, and from then on the cookie is no session — on a
guarded page, and for every page that reads the session through
``get_web_session``.

These run against the same live stub realm as ``test_web_surface``: logout
tokens are really RS256-signed by the realm key and really verified through the
realm's JWKS.
"""

from __future__ import annotations

import logging
import pathlib
import re
import sys
import time
import uuid

import jwt
import pytest
import pytest_asyncio
from fastapi import APIRouter, Depends, Request

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from test_web_surface import (  # noqa: E402
    REALM_KEY_PEM,
    ROGUE_KEY_PEM,
    SESSION_SECRET,
    WEB_CLIENT_ID,
    _StubIdp,
    hardened_map,
    make_web_app,
    rendered_csrf,
    sign_in,
    web_client,
    web_values,
)

from collab_hub_api.config import Config  # noqa: E402
from collab_hub_api.core import make_app  # noqa: E402
from collab_hub_api.web import oidc  # noqa: E402
from collab_hub_api.web.authz import get_web_session, require_web_session  # noqa: E402
from collab_hub_api.web.ended_sessions import (  # noqa: E402
    ENDED_SESSIONS_TABLE,
    InMemoryEndedWebSessions,
    PostgresEndedWebSessions,
)
from collab_hub_api.web.session import SESSION_COOKIE, SessionCodec  # noqa: E402
from collab_hub_api.web.surface import BACKCHANNEL_LOGOUT_PATH, PUBLIC_WEB_PATHS  # noqa: E402

BACKCHANNEL_EVENT = "http://schemas.openid.net/event/backchannel-logout"
FORM = {"Content-Type": "application/x-www-form-urlencoded"}


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


def logout_token(idp: _StubIdp, *, drop: tuple[str, ...] = (), key: bytes = REALM_KEY_PEM, **overrides) -> str:
    """A Keycloak-shaped logout token for ``idp.sid``, signed by the realm key unless told otherwise."""

    now = int(time.time())
    claims = {
        "iss": idp.issuer,
        "aud": WEB_CLIENT_ID,
        "iat": now,
        "exp": now + 300,
        "jti": str(uuid.uuid4()),
        "sub": idp.sub,
        "sid": idp.sid,
        "typ": "Logout",
        "events": {BACKCHANNEL_EVENT: {}},
    }
    claims.update(overrides)
    for name in drop:
        claims.pop(name, None)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "web-key-1"})


async def backchannel(client, token: str):
    return await client.post(BACKCHANNEL_LOGOUT_PATH, content=f"logout_token={token}", headers=FORM)


async def signed_in_client(app, idp):
    client = web_client(app)
    await client.__aenter__()
    response = await sign_in(client, idp)
    assert response.status_code == 303
    assert (await client.get("/web")).status_code == 200
    return client


def assert_signed_out_on_a_guarded_page(response) -> None:
    assert response.status_code == 303
    assert response.headers["location"].startswith("/web/signin?")


def assert_home_page_signed_out(response) -> None:
    """The home page reads the session through ``get_web_session``: signed out, it offers sign-in."""

    assert response.status_code == 200
    assert "Alice Example" not in response.text
    assert 'action="/web/signout"' not in response.text


def assert_home_page_signed_in(response) -> None:
    assert response.status_code == 200
    assert "Alice Example" in response.text
    assert 'action="/web/signout"' in response.text


# --- the session carries the realm session ----------------------------------


async def test_the_session_cookie_carries_the_realm_sessions_sid(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    idp.sid = "realm-session-abc"
    async with web_client(app) as client:
        await sign_in(client, idp)
        session = SessionCodec(SESSION_SECRET).decode_session(client.cookies[SESSION_COOKIE])
    assert session is not None and session.sid == "realm-session-abc"


@pytest.mark.parametrize("sid", [None, "", 7])
async def test_an_id_token_without_a_sid_signs_nobody_in(tmp_path, idp, caplog, sid):
    app = make_web_app(tmp_path, idp)
    if sid is None:
        idp.drop_claims = {"sid"}
    else:
        idp.claims_override = {"sid": sid}
    caplog.set_level(logging.WARNING, logger="frames_server.web")
    async with web_client(app) as client:
        response = await sign_in(client, idp)
        assert response.status_code == 400
        assert SESSION_COOKIE not in client.cookies
        assert_signed_out_on_a_guarded_page(await client.get("/web"))
    assert any(getattr(r, "reason", None) == "id_token carries no sid" for r in caplog.records)


async def test_a_v1_cookie_no_longer_verifies(tmp_path, idp):
    """The purpose is v2: a v1 cookie names no realm session a logout could end."""

    codec = SessionCodec(SESSION_SECRET)
    now = int(time.time())
    payload = {"sub": "alice", "sid": "realm-session-1", "csrf": "c", "iat": now, "exp": now + 600}
    v1 = codec.encode("nexus-web-session-v1", payload)
    assert codec.decode_session(v1) is None
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        client.cookies.set(SESSION_COOKIE, v1, domain="web.test", path="/")
        assert_signed_out_on_a_guarded_page(await client.get("/web"))


def test_a_cookie_without_a_sid_does_not_decode():
    codec = SessionCodec(SESSION_SECRET)
    now = int(time.time())
    from collab_hub_api.web.session import SESSION_PURPOSE

    for sid in (None, "", 3):
        payload = {"sub": "alice", "csrf": "c", "iat": now, "exp": now + 600}
        if sid is not None:
            payload["sid"] = sid
        assert codec.decode_session(codec.encode(SESSION_PURPOSE, payload)) is None


# --- a back-channel logout ends the session ---------------------------------


async def test_a_backchannel_logout_signs_the_session_out_everywhere_it_is_read(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    client = await signed_in_client(app, idp)
    try:
        assert_home_page_signed_in(await client.get("/web/home"))
        response = await backchannel(web_client_without_cookies(app), logout_token(idp))
        assert response.status_code == 200
        assert "no-store" in response.headers["cache-control"]
        # The guard: a guarded page sends the browser to sign in.
        assert_signed_out_on_a_guarded_page(await client.get("/web"))
        assert_signed_out_on_a_guarded_page(await client.get("/web/account"))
        # get_web_session: a public page that reads the session sees none.
        assert_home_page_signed_out(await client.get("/web/home"))
        # require_web_session's form-POST callers: no session to check a token against.
        assert_signed_out_on_a_guarded_page(await client.post("/web/signout", data={"csrf_token": "x"}))
        # The admin panel's JSON API refuses it the way it refuses no session.
        api = await client.get("/admin/api/session")
        assert api.status_code == 401
        assert api.json() == {"error": "authentication required"}
    finally:
        await client.__aexit__(None, None, None)


def web_client_without_cookies(app):
    """Keycloak's call: a separate client, so no browser cookie rides along."""

    return web_client(app)


async def test_get_web_session_and_its_dependencies_refuse_an_ended_session(tmp_path, idp):
    """Each reader of the session on its own, called the way routes call it."""

    app = make_web_app(tmp_path, idp)
    router = APIRouter()

    @router.get("/probe/session")
    async def probe(request: Request):
        session = await get_web_session(request)
        return {"user": session.user if session else None}

    @router.get("/probe/required")
    async def required(session=Depends(require_web_session)):
        return {"user": session.user}

    app.include_router(router)
    client = await signed_in_client(app, idp)
    try:
        assert (await client.get("/probe/session")).json() == {"user": "alice"}
        assert (await client.get("/probe/required")).status_code == 200
        async with web_client(app) as keycloak:
            assert (await backchannel(keycloak, logout_token(idp))).status_code == 200
        assert (await client.get("/probe/session")).json() == {"user": None}
        required_after = await client.get("/probe/required")
        assert required_after.status_code == 303
        assert required_after.headers["location"].startswith("/web/signin?")
    finally:
        await client.__aexit__(None, None, None)


async def test_only_the_named_sid_ends(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    idp.sid = "realm-session-alice-laptop"
    laptop = await signed_in_client(app, idp)
    idp.sid = "realm-session-alice-phone"
    phone = await signed_in_client(app, idp)
    try:
        idp.sid = "realm-session-alice-laptop"
        async with web_client(app) as keycloak:
            assert (await backchannel(keycloak, logout_token(idp))).status_code == 200
        assert_signed_out_on_a_guarded_page(await laptop.get("/web"))
        assert (await phone.get("/web")).status_code == 200
    finally:
        await laptop.__aexit__(None, None, None)
        await phone.__aexit__(None, None, None)


async def test_signing_in_again_after_a_backchannel_logout_works(tmp_path, idp):
    """A new realm session is a new sid: the person is not locked out."""

    app = make_web_app(tmp_path, idp)
    client = await signed_in_client(app, idp)
    try:
        async with web_client(app) as keycloak:
            assert (await backchannel(keycloak, logout_token(idp))).status_code == 200
        assert_signed_out_on_a_guarded_page(await client.get("/web"))
        idp.sid = "realm-session-2"
        await sign_in(client, idp)
        assert (await client.get("/web")).status_code == 200
    finally:
        await client.__aexit__(None, None, None)


async def test_a_repeated_logout_for_the_same_sid_is_still_a_200(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as keycloak:
        assert (await backchannel(keycloak, logout_token(idp))).status_code == 200
        assert (await backchannel(keycloak, logout_token(idp))).status_code == 200


# --- every invalid token is a 400 that ends nothing -------------------------


def _id_token_as_logout_token(idp: _StubIdp) -> str:
    idp.nonce = "a-nonce"
    return idp.mint_id_token()


INVALID_TOKENS = {
    "wrong aud": lambda idp: logout_token(idp, aud="apollo-desktop"),
    "the billing client's aud": lambda idp: logout_token(idp, aud="subscription-manager-web"),
    "wrong iss": lambda idp: logout_token(idp, iss="https://keycloak.evil.example/realms/nebari"),
    "wrong key": lambda idp: logout_token(idp, key=ROGUE_KEY_PEM),
    "expired": lambda idp: logout_token(idp, exp=int(time.time()) - 60),
    "missing iat": lambda idp: logout_token(idp, drop=("iat",)),
    "stale iat": lambda idp: logout_token(idp, iat=int(time.time()) - oidc.LOGOUT_TOKEN_MAX_AGE_SECONDS - 60),
    "no events": lambda idp: logout_token(idp, drop=("events",)),
    "events without the back-channel event": lambda idp: logout_token(idp, events={"other": {}}),
    "the event not an object": lambda idp: logout_token(idp, events={BACKCHANNEL_EVENT: "yes"}),
    "events a list": lambda idp: logout_token(idp, events=[BACKCHANNEL_EVENT]),
    "nonce present": lambda idp: logout_token(idp, nonce="n"),
    "missing sid": lambda idp: logout_token(idp, drop=("sid",)),
    "empty sid": lambda idp: logout_token(idp, sid=""),
    "sid not a string": lambda idp: logout_token(idp, sid=12),
    "an ID token": _id_token_as_logout_token,
    "unsigned": lambda idp: jwt.encode(
        {"iss": idp.issuer, "aud": WEB_CLIENT_ID, "iat": int(time.time()), "sid": idp.sid}, None, algorithm="none"
    ),
    "HS256 with the public key's bytes": lambda idp: jwt.encode(
        {"iss": idp.issuer, "aud": WEB_CLIENT_ID, "iat": int(time.time()), "sid": idp.sid},
        "a-shared-secret-of-sufficient-length-0123456789",
        algorithm="HS256",
        headers={"kid": "web-key-1"},
    ),
    "not a JWT": lambda idp: "not-a-jwt",
}


@pytest.mark.parametrize("case", sorted(INVALID_TOKENS))
async def test_an_invalid_logout_token_is_a_400_that_ends_nothing(tmp_path, idp, caplog, case):
    app = make_web_app(tmp_path, idp)
    client = await signed_in_client(app, idp)
    token = INVALID_TOKENS[case](idp)
    caplog.set_level(logging.INFO, logger="frames_server.web")
    try:
        async with web_client(app) as keycloak:
            response = await backchannel(keycloak, token)
        assert response.status_code == 400
        assert response.content == b""
        assert "no-store" in response.headers["cache-control"]
        assert (await client.get("/web")).status_code == 200
        assert_home_page_signed_in(await client.get("/web/home"))
    finally:
        await client.__aexit__(None, None, None)
    refusals = [r for r in caplog.records if r.getMessage() == "web_backchannel_logout_refused"]
    assert len(refusals) == 1
    # A fixed reason, never anything from the token.
    assert token not in caplog.text
    assert idp.sid not in caplog.text


async def test_the_audience_is_checked_against_the_client_not_merely_the_realm(tmp_path, idp):
    """A token for this client among others still names this client, and is accepted."""

    app = make_web_app(tmp_path, idp)
    client = await signed_in_client(app, idp)
    try:
        async with web_client(app) as keycloak:
            response = await backchannel(keycloak, logout_token(idp, aud=["apollo-desktop", WEB_CLIENT_ID]))
        assert response.status_code == 200
        assert_signed_out_on_a_guarded_page(await client.get("/web"))
    finally:
        await client.__aexit__(None, None, None)


# --- malformed requests ------------------------------------------------------


async def test_malformed_requests_are_400s(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    client = await signed_in_client(app, idp)
    token = logout_token(idp)
    try:
        async with web_client(app) as keycloak:
            cases = {
                "no token": await keycloak.post(BACKCHANNEL_LOGOUT_PATH, content="", headers=FORM),
                "another field only": await keycloak.post(
                    BACKCHANNEL_LOGOUT_PATH, content=f"id_token={token}", headers=FORM
                ),
                "an empty token": await keycloak.post(BACKCHANNEL_LOGOUT_PATH, content="logout_token=", headers=FORM),
                "two tokens": await keycloak.post(
                    BACKCHANNEL_LOGOUT_PATH, content=f"logout_token={token}&logout_token={token}", headers=FORM
                ),
                "json": await keycloak.post(BACKCHANNEL_LOGOUT_PATH, json={"logout_token": token}),
                "multipart": await keycloak.post(BACKCHANNEL_LOGOUT_PATH, files={"logout_token": (None, token)}),
                "a type that only begins like a form": await keycloak.post(
                    BACKCHANNEL_LOGOUT_PATH,
                    content=f"logout_token={token}",
                    headers={"Content-Type": "application/x-www-form-urlencoded-not-really"},
                ),
                "no content type": await keycloak.post(BACKCHANNEL_LOGOUT_PATH, content=f"logout_token={token}"),
                "oversized": await keycloak.post(
                    BACKCHANNEL_LOGOUT_PATH,
                    content=f"logout_token={token}&pad={'x' * 20000}",
                    headers=FORM,
                ),
                "oversized, chunked": await keycloak.post(
                    BACKCHANNEL_LOGOUT_PATH,
                    content=_chunks(f"logout_token={token}&pad=".encode(), b"x" * 20000),
                    headers=FORM,
                ),
                "not ascii": await keycloak.post(
                    BACKCHANNEL_LOGOUT_PATH, content="logout_token=é".encode(), headers=FORM
                ),
            }
        for case, response in cases.items():
            assert response.status_code == 400, case
            assert response.content == b"", case
        # None of them ended anything.
        assert (await client.get("/web")).status_code == 200
    finally:
        await client.__aexit__(None, None, None)


async def _chunks(*parts: bytes):
    for part in parts:
        yield part


async def test_get_is_not_a_logout(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as keycloak:
        response = await keycloak.get(BACKCHANNEL_LOGOUT_PATH, params={"logout_token": logout_token(idp)})
    assert response.status_code == 405


async def test_a_form_charset_parameter_is_accepted(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as keycloak:
        response = await keycloak.post(
            BACKCHANNEL_LOGOUT_PATH,
            content=f"logout_token={logout_token(idp)}",
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
        )
    assert response.status_code == 200


# --- the endpoint needs no session and no CSRF token ------------------------


def test_the_endpoint_is_public_on_purpose():
    assert BACKCHANNEL_LOGOUT_PATH in PUBLIC_WEB_PATHS
    assert BACKCHANNEL_LOGOUT_PATH.startswith("/web/oidc/")


async def test_the_endpoint_needs_no_session_and_no_csrf_token(tmp_path, idp):
    """Keycloak holds neither; a hardened protection map does not block it either."""

    app = make_web_app(
        tmp_path,
        idp,
        security=hardened_map(),
    )
    client = await signed_in_client(app, idp)
    try:
        async with web_client(app) as keycloak:
            assert SESSION_COOKIE not in keycloak.cookies
            response = await keycloak.post(
                BACKCHANNEL_LOGOUT_PATH,
                content=f"logout_token={logout_token(idp)}",
                headers={**FORM, "Origin": "https://keycloak.example"},
            )
        assert response.status_code == 200
        assert_signed_out_on_a_guarded_page(await client.get("/web"))
    finally:
        await client.__aexit__(None, None, None)


# --- the Hub's own Log out ends its own sid ---------------------------------


async def test_the_hubs_log_out_ends_its_own_sid_even_if_the_browser_never_reaches_keycloak(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    client = await signed_in_client(app, idp)
    try:
        captured = client.cookies[SESSION_COOKIE]
        response = await client.post("/web/signout", data={"csrf_token": await rendered_csrf(client)})
        assert response.status_code == 200
        # The browser stops here: it never follows the page on to the realm.
        client.cookies.set(SESSION_COOKIE, captured, domain="web.test", path="/")
        assert_signed_out_on_a_guarded_page(await client.get("/web"))
        assert_home_page_signed_out(await client.get("/web/home"))
    finally:
        await client.__aexit__(None, None, None)
    assert app.state.web_surface.ended_sessions.has_ended(idp.sid)


async def test_the_hubs_log_out_still_reaches_the_realm_when_the_store_fails(tmp_path, idp, caplog):
    app = make_web_app(tmp_path, idp)

    class _Broken:
        def end(self, sid):
            raise RuntimeError("database down")

        def has_ended(self, sid):
            return False

    object.__setattr__(app.state.web_surface, "ended_sessions", _Broken())
    caplog.set_level(logging.ERROR, logger="frames_server.web")
    client = await signed_in_client(app, idp)
    try:
        response = await client.post("/web/signout", data={"csrf_token": await rendered_csrf(client)})
        assert response.status_code == 200
        assert "/protocol/openid-connect/logout" in response.text
    finally:
        await client.__aexit__(None, None, None)
    assert any(r.getMessage() == "web_signout_end_session_failed" for r in caplog.records)


async def test_a_store_that_cannot_record_the_logout_answers_400(tmp_path, idp):
    app = make_web_app(tmp_path, idp)

    class _Broken:
        def end(self, sid):
            raise RuntimeError("database down")

        def has_ended(self, sid):
            return False

    object.__setattr__(app.state.web_surface, "ended_sessions", _Broken())
    async with web_client(app) as keycloak:
        response = await backchannel(keycloak, logout_token(idp))
    assert response.status_code == 400
    assert response.content == b""


async def test_a_store_that_cannot_answer_refuses_rather_than_admits(tmp_path, idp):
    """'Cannot tell whether it ended' is never 'still signed in'."""

    app = make_web_app(tmp_path, idp)
    client = await signed_in_client(app, idp)

    class _Down:
        def end(self, sid):
            raise RuntimeError("database down")

        def has_ended(self, sid):
            raise RuntimeError("database down")

    object.__setattr__(app.state.web_surface, "ended_sessions", _Down())
    try:
        guarded = await client.get("/web")
        assert guarded.status_code == 503
        home = await client.get("/web/home")
        assert home.status_code == 503
        assert "Alice Example" not in home.text
    finally:
        await client.__aexit__(None, None, None)


# --- the in-memory store -------------------------------------------------------


def test_the_in_memory_store_prunes_rows_older_than_the_lifetime():
    now = [1_000_000.0]
    store = InMemoryEndedWebSessions(3600, clock=lambda: now[0])
    store.end("old")
    now[0] += 3601
    assert store.has_ended("old")  # pruning happens on end(), not on read
    store.end("new")
    assert not store.has_ended("old")
    assert store.has_ended("new")


def test_the_in_memory_store_keeps_the_first_ended_at():
    now = [1_000_000.0]
    store = InMemoryEndedWebSessions(3600, clock=lambda: now[0])
    store.end("sid")
    now[0] += 3000
    store.end("sid")
    now[0] += 700
    store.end("other")
    assert not store.has_ended("sid")


# --- a single place decides --------------------------------------------------


def test_only_get_web_session_decodes_the_session_cookie():
    """The ended-session check lives in ``get_web_session``. A second reader that
    decoded the cookie itself would accept an ended session, so there is none."""

    src = pathlib.Path(__file__).parent.parent / "src" / "collab_hub_api"
    readers = sorted(
        str(path.relative_to(src))
        for path in src.rglob("*.py")
        if re.search(r"\.decode_session\(", path.read_text()) and path.name != "session.py"
    )
    assert readers == ["web/authz.py"]
    cookie_readers = sorted(
        str(path.relative_to(src))
        for path in src.rglob("*.py")
        if re.search(r"cookies(\.get\(|\[)SESSION_COOKIE", path.read_text())
    )
    assert cookie_readers == ["web/authz.py"]


# --- live Postgres: every replica sees a logout ------------------------------

POSTGRES_URL = __import__("os").environ.get("COLLAB_HUB_TEST_POSTGRES_URL", "")

live_postgres = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="set COLLAB_HUB_TEST_POSTGRES_URL to a disposable database to run the live-Postgres tests",
)


@pytest.fixture
def live_db():
    from test_collab_schema import COLLAB_TABLES

    from collab_hub_api.frames.collab_schema import run_collab_schema_migrations
    from collab_hub_api.frames.db import PostgresDatabase

    database = PostgresDatabase(POSTGRES_URL, min_size=0, max_size=4, timeout_seconds=15.0)

    def drop():
        with database.connection() as conn:
            for table in COLLAB_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

    try:
        drop()
        run_collab_schema_migrations(database)
        yield database
        drop()
    finally:
        database.close()


@live_postgres
def test_live_two_store_instances_share_ended_sessions(live_db):
    from collab_hub_api.frames.db import PostgresDatabase

    other = PostgresDatabase(POSTGRES_URL, min_size=0, max_size=2, timeout_seconds=15.0)
    try:
        replica_a = PostgresEndedWebSessions(live_db, 3600)
        replica_b = PostgresEndedWebSessions(other, 3600)
        assert not replica_b.has_ended("realm-session-1")
        replica_a.end("realm-session-1")
        assert replica_b.has_ended("realm-session-1")
        assert not replica_b.has_ended("realm-session-2")
        replica_b.end("realm-session-1")  # idempotent
        assert replica_a.has_ended("realm-session-1")
    finally:
        other.close()


@live_postgres
def test_live_end_prunes_rows_older_than_the_lifetime(live_db):
    store = PostgresEndedWebSessions(live_db, 3600)
    with live_db.connection() as conn:
        conn.execute(
            f"INSERT INTO {ENDED_SESSIONS_TABLE} (sid, ended_at) VALUES"
            " ('stale', now() - interval '2 hours'), ('recent', now() - interval '30 minutes')"
        )
    store.end("fresh")
    assert not store.has_ended("stale")
    assert store.has_ended("recent")
    assert store.has_ended("fresh")


@pytest_asyncio.fixture
async def live_replicas(tmp_path, idp, live_db):
    """Two apps on one database and one secret: two replicas of the Hub."""

    apps = []
    for name in ("a", "b"):
        values = web_values(tmp_path / name, idp)
        values["frames"]["postgres"] = {"url": POSTGRES_URL, "auto_migrate": True}
        apps.append(make_app(Config.parse(values)))
    async with apps[0].router.lifespan_context(apps[0]), apps[1].router.lifespan_context(apps[1]):
        yield apps


@live_postgres
async def test_live_a_logout_heard_by_one_replica_ends_the_session_on_the_other(live_replicas, idp):
    replica_a, replica_b = live_replicas
    assert isinstance(replica_a.state.web_surface.ended_sessions, PostgresEndedWebSessions)
    client = await signed_in_client(replica_a, idp)
    try:
        cookie = client.cookies[SESSION_COOKIE]
        async with web_client(replica_b) as browser_on_b:
            browser_on_b.cookies.set(SESSION_COOKIE, cookie, domain="web.test", path="/")
            assert (await browser_on_b.get("/web")).status_code == 200
            async with web_client(replica_a) as keycloak:
                assert (await backchannel(keycloak, logout_token(idp))).status_code == 200
            assert_signed_out_on_a_guarded_page(await browser_on_b.get("/web"))
            assert_home_page_signed_out(await browser_on_b.get("/web/home"))
        assert_signed_out_on_a_guarded_page(await client.get("/web"))
    finally:
        await client.__aexit__(None, None, None)


@live_postgres
async def test_live_the_hubs_log_out_on_one_replica_ends_the_session_on_the_other(live_replicas, idp):
    replica_a, replica_b = live_replicas
    client = await signed_in_client(replica_a, idp)
    try:
        cookie = client.cookies[SESSION_COOKIE]
        response = await client.post("/web/signout", data={"csrf_token": await rendered_csrf(client)})
        assert response.status_code == 200
        async with web_client(replica_b) as browser_on_b:
            browser_on_b.cookies.set(SESSION_COOKIE, cookie, domain="web.test", path="/")
            assert_signed_out_on_a_guarded_page(await browser_on_b.get("/web"))
    finally:
        await client.__aexit__(None, None, None)
