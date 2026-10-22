"""The browser web surface (issue #88): OIDC session, CSRF, and scaffolding.

These run the real flow against a live stub IdP: a threaded HTTP server
serves the realm's JWKS and token endpoint, ID tokens are really RS256-signed
and really verified (signature, issuer, **audience**, azp, nonce), and the
session/transient cookies travel through httpx's cookie jar over an https
base URL exactly as a browser's Secure cookies would. Only Keycloak's login
UI is elided: tests jump from the authorize redirect straight to the
callback, carrying the state the app minted.

The audience tests are the issue #83 regression this surface must never
grow: a validly signed same-realm token minted for a *different* client
(the desktop's, say) must not mint a web session.
"""

from __future__ import annotations

import functools
import html
import json
import logging
import pathlib
import re
import threading
import time
from base64 import urlsafe_b64encode
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from starlette.routing import Mount, Route, WebSocketRoute

from collab_hub_api.config import WEB_SESSION_LIFETIME_CEILING_SECONDS, Config
from collab_hub_api.core import make_app
from collab_hub_api.frames import auth
from collab_hub_api.frames.orgs import ROLE_MEMBER, ROLE_OWNER, InMemoryOrgStore
from collab_hub_api.routers.web import make_router, sanitize_next_path, session_gated_router
from collab_hub_api.web.authz import (
    offending_web_routes,
    on_web_surface,
    platform_role_source_name,
    require_csrf,
    require_operator,
    require_org_owner,
    require_web_session,
    stale_csrf_exemptions,
    stray_page_routes,
    unprotected_web_routes,
    unwrap_dependency,
    verify_web_route_protection,
)
from collab_hub_api.web.data_statement import DATA_STATEMENT_CONTACT, DATA_STATEMENT_TEXT
from collab_hub_api.web.forms import MAX_FORM_BYTES
from collab_hub_api.web.legal_documents import PLACEHOLDER_NOTICE
from collab_hub_api.web.privacy_statement import (
    PRIVACY_STATEMENT_IS_PLACEHOLDER,
    PRIVACY_STATEMENT_TEXT,
    privacy_statement_page,
)
from collab_hub_api.web.session import (
    SESSION_COOKIE,
    SESSION_PURPOSE,
    TRANSIENT_COOKIE,
    TRANSIENT_PURPOSE,
    SessionCodec,
    WebSession,
)
from collab_hub_api.web.surface import (
    PRIVACY_PATH,
    PUBLIC_WEB_PATHS,
    TERMS_PATH,
    WEB_LOGO_PATH,
    WEB_SURFACE_PREFIXES,
    WebSurface,
    blocked_web_route_paths,
    build_web_surface,
    clamped_session_lifetime,
    enforce_web_surface_map_access,
)
from collab_hub_api.web.terms_of_service import (
    TERMS_OF_SERVICE_IS_PLACEHOLDER,
    TERMS_OF_SERVICE_TEXT,
    terms_of_service_page,
)

WEB_CLIENT_ID = "collab-web"
OTHER_CLIENT_ID = "apollo-desktop"
# A realistic secret: the config now refuses low-distinctness values like
# "a" * 48, which carry no entropy while satisfying a length rule.
SESSION_SECRET = "kQ7pR2vX9mZ4tLbN6wJ3hF8sD5gY1cA0uE-oI_TnVpQ"


def _make_key(kid: str) -> tuple[bytes, dict]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": kid, "alg": "RS256", "use": "sig"})
    return private_pem, jwk


# Key generation is slow enough to matter; every test reuses these.
REALM_KEY_PEM, REALM_KEY_JWK = _make_key("web-key-1")
ROGUE_KEY_PEM, _ROGUE_KEY_JWK = _make_key("web-key-1")  # same kid, foreign key


class _StubIdp:
    """A live Keycloak-shaped realm: JWKS plus a scriptable token endpoint."""

    def __init__(self) -> None:
        self.token_requests: list[dict] = []
        self.token_status = 200
        self.omit_id_token = False
        self.claims_override: dict = {}
        self.drop_claims: set[str] = set()
        self.signing_pem = REALM_KEY_PEM
        self.nonce: str | None = None
        self.sub = "subject-alice"

        idp = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path.endswith("/protocol/openid-connect/certs"):
                    body = json.dumps({"keys": [REALM_KEY_JWK]}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
                idp.token_requests.append(form)
                if idp.token_status != 200:
                    body = json.dumps({"error": "invalid_grant"}).encode()
                    self.send_response(idp.token_status)
                else:
                    payload = {
                        "access_token": "stub-access-token",
                        "token_type": "Bearer",
                    }
                    if not idp.omit_id_token:
                        payload["id_token"] = idp.mint_id_token()
                    body = json.dumps(payload).encode()
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:  # keep pytest output clean
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        port = self._server.server_address[1]
        self.issuer = f"http://127.0.0.1:{port}/realms/nebari"

    def mint_id_token(self) -> str:
        now = int(time.time())
        claims = {
            "iss": self.issuer,
            "aud": WEB_CLIENT_ID,
            "azp": WEB_CLIENT_ID,
            "typ": "ID",
            "sub": self.sub,
            "preferred_username": "alice",
            "name": "Alice Example",
            "email": "alice@example.com",
            "iat": now,
            "exp": now + 300,
        }
        if self.nonce is not None:
            claims["nonce"] = self.nonce
        claims.update(self.claims_override)
        for name in self.drop_claims:
            claims.pop(name, None)
        return jwt.encode(claims, self.signing_pem, algorithm="RS256", headers={"kid": "web-key-1"})

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _isolated_bearer_issuer(monkeypatch):
    # The web issuer must match FRAMES_BEARER_ISSUER when both are set; a
    # developer's shell must not leak one into these tests.
    monkeypatch.delenv("FRAMES_BEARER_ISSUER", raising=False)


@pytest.fixture
def idp(monkeypatch):
    endpoint = _StubIdp()
    # Each test's realm lives on a fresh port; the module-level JWKS client
    # registry must not serve one test's keys to another's URL.
    monkeypatch.setitem(auth.__dict__, "_jwks_clients", {})
    try:
        yield endpoint
    finally:
        endpoint.close()


def web_values(tmp_path, idp: _StubIdp, *, security: dict | None = None, web: dict | None = None) -> dict:
    values: dict = {
        "storage": {"frames_path": str(tmp_path / "frames")},
        "frames": {
            "active_state": {"backend": "memory"},
            "history": {"backend": "memory"},
            "usage": {"backend": "memory"},
            "orgs": {"backend": "memory"},
            "mcp_session_manager_enabled": False,
        },
        "tasks": {"backend": "memory"},
        "web": {
            "client_id": WEB_CLIENT_ID,
            "client_secret": "confidential-secret",
            "issuer_url": idp.issuer,
            "session_secret": SESSION_SECRET,
            **(web or {}),
        },
    }
    if security is not None:
        values["security"] = security
    return values


def make_web_app(tmp_path, idp: _StubIdp, *, security: dict | None = None, web: dict | None = None):
    return make_app(Config.parse(web_values(tmp_path, idp, security=security, web=web)))


def web_client(app) -> AsyncClient:
    # https base URL: the cookies are Secure, and httpx's jar (correctly)
    # refuses to send Secure cookies over plain http. A two-label host,
    # because http.cookiejar files single-label hosts under "<host>.local",
    # which would strand cookies the tests set themselves under "test".
    return AsyncClient(transport=ASGITransport(app=app), base_url="https://web.test")


def authorize_params(location: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}


async def start_signin(client: AsyncClient, idp: _StubIdp, next_path: str = "/web") -> dict[str, str]:
    response = await client.get("/web/signin", params={"next": next_path})
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith(f"{idp.issuer}/protocol/openid-connect/auth?")
    params = authorize_params(location)
    idp.nonce = params["nonce"]
    return params


async def sign_in(client: AsyncClient, idp: _StubIdp, next_path: str = "/web"):
    params = await start_signin(client, idp, next_path)
    return await client.get(
        "/web/oidc/callback", params={"code": "stub-code", "state": params["state"]}
    )


# --- session and transient cookie codec ------------------------------------


def test_codec_round_trips_a_session():
    codec = SessionCodec(SESSION_SECRET)
    now = int(time.time())
    session = WebSession(
        user="subject-alice",
        name="Alice",
        email="alice@example.com",
        csrf="csrf-token",
        issued_at=now,
        expires_at=now + 60,
    )
    decoded = codec.decode_session(codec.encode_session(session))
    assert decoded == session


def test_codec_rejects_tampering_expiry_and_foreign_secrets():
    codec = SessionCodec(SESSION_SECRET)
    other = SessionCodec("Zx4Kq8Lm2Pw7Rt5Ns9Bv3Hj6Fd1Gc0Ya-Ue_IoQpXn")
    now = int(time.time())
    value = codec.encode(SESSION_PURPOSE, {"sub": "alice", "csrf": "x", "iat": now, "exp": now + 60})
    assert codec.decode(SESSION_PURPOSE, value) is not None
    # Flip one character of the payload.
    tampered = ("A" if value[0] != "A" else "B") + value[1:]
    assert codec.decode(SESSION_PURPOSE, tampered) is None
    # Signed by a different secret.
    foreign = other.encode(SESSION_PURPOSE, {"sub": "alice", "csrf": "x", "iat": now, "exp": now + 60})
    assert codec.decode(SESSION_PURPOSE, foreign) is None
    # Expired, and issued in the future beyond skew.
    expired = codec.encode(SESSION_PURPOSE, {"sub": "a", "csrf": "x", "iat": now - 120, "exp": now - 60})
    assert codec.decode(SESSION_PURPOSE, expired) is None
    future = codec.encode(SESSION_PURPOSE, {"sub": "a", "csrf": "x", "iat": now + 3600, "exp": now + 7200})
    assert codec.decode(SESSION_PURPOSE, future) is None
    # Garbage shapes.
    assert codec.decode(SESSION_PURPOSE, "") is None
    assert codec.decode(SESSION_PURPOSE, "not-a-cookie") is None
    assert codec.decode(SESSION_PURPOSE, "a.b.c") is None


def test_purpose_tags_are_not_interchangeable():
    # A transient OIDC-flow blob must never verify as a session cookie, or a
    # sign-in that never completed could be replayed into a signed-in state.
    codec = SessionCodec(SESSION_SECRET)
    now = int(time.time())
    transient = codec.encode(TRANSIENT_PURPOSE, {"sub": "alice", "csrf": "x", "iat": now, "exp": now + 60})
    assert codec.decode(SESSION_PURPOSE, transient) is None
    assert codec.decode(TRANSIENT_PURPOSE, transient) is not None


def test_codec_refuses_a_weak_secret():
    with pytest.raises(ValueError):
        SessionCodec("short")


# --- next-path sanitization --------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "https://evil.example.com/",
        "//evil.example.com/",
        "/\\evil.example.com",
        "\\/evil",
        "relative/path",
        "/ok\r\nSet-Cookie: x=y",
        "/with:colon/in/path",
        "x" * 2001,
        # Self-referential targets: `renew=1` deliberately skips the
        # already-signed-in shortcut, so a `next` naming the sign-in route
        # sent a browser around silent SSO forever. Nothing else in the flow
        # broke the cycle — it ended when the person closed the tab.
        "/web/signin",
        "/web/signin?renew=1&next=%2Fweb%2Fsignin%3Frenew%3D1",
        "/web/signin/",
        # Normalized before comparing, because a browser normalizes dot
        # segments before it sends: this *is* a request for /web/signin.
        "/a/../web/signin?renew=1",
        "/web/oidc/callback?code=x",
        # Percent-encoded dot segments. The URL standard normalizes `%2e` as
        # `.` when identifying dot segments, so a browser turns each of these
        # into /web/signin — while the raw string starts with a slash and
        # sails past a shape-only check. The first is what a review found; the
        # rest are the same trick at other depths.
        "/%2e%2e/web/signin?renew=1",
        "/%252e%252e/web/signin?renew=1",
        "/%25252e%25252e/web/signin?renew=1",
        "/%2E%2E/web/signin",
        "/a/%2e%2e/web/signin",
        "/%2e/web/signin",
    ],
)
def test_unsafe_next_targets_fall_back_to_the_overview(value):
    assert sanitize_next_path(value) == "/web"


async def test_a_next_naming_the_signin_route_does_not_loop(tmp_path, idp):
    # The end-to-end shape of the same thing: the redirect must leave the
    # sign-in route, not point back at it.
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        await sign_in(client, idp)
        response = await client.get(
            "/web/signin", params={"renew": "1", "next": "/web/signin?renew=1"}
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith(idp.issuer)
        params = authorize_params(response.headers["location"])
        idp.nonce = params["nonce"]
        callback = await client.get(
            "/web/oidc/callback", params={"code": "stub-code", "state": params["state"]}
        )
    assert callback.status_code == 303
    assert callback.headers["location"] == "/web"


def test_safe_next_targets_are_preserved():
    assert sanitize_next_path("/admin/invitations") == "/admin/invitations"


@pytest.mark.parametrize(
    "value",
    [
        "/invite/accept",
        "/admin/invitations",
        # Decoding is only ever used to decide a *refusal*, so a legitimate
        # target that merely mentions the sign-in path in its query must
        # survive intact — the query is stripped before the comparison.
        "/web/reports?back=%2Fweb%2Fsignin",
        "/web/a%20b/c",
    ],
)
def test_decoding_for_the_refusal_does_not_rewrite_a_safe_target(value):
    # And the value is returned unchanged: the browser does its own decoding,
    # so handing it a pre-decoded path would change where it goes.
    assert sanitize_next_path(value) == value


def test_the_decode_loop_terminates_and_stays_app_relative():
    """Bounded passes, so a value built to keep decoding cannot spin.

    Deep nesting is not itself the danger, and it is worth being precise about
    why: a browser decodes **once**, so `%25252e` reaches it as `%252e`, which
    is not a dot segment and forms no loop. Two layers are what matter — the
    query-string decode, then the browser's dot-segment normalization — and
    that is the case the reject list above covers. This one only has to end,
    and end with an app-relative answer.
    """

    tail = "../web/signin"
    for _ in range(5):
        tail = quote(tail, safe="")
    value = "/" + tail
    assert len(value) < 2000  # not refused by the length rule instead

    started = time.monotonic()
    result = sanitize_next_path(value)
    assert time.monotonic() - started < 1
    assert result.startswith("/")
    assert not result.startswith("//")
    assert sanitize_next_path("/org/invitations?page=2") == "/org/invitations?page=2"


# --- the sign-in flow --------------------------------------------------------


async def test_surface_is_absent_when_not_configured(client):
    # The default conftest app configures no web client; the routes must not
    # exist at all rather than half-exist.
    response = await client.get("/web/signin")
    assert response.status_code in (401, 404)
    assert "location" not in response.headers


async def test_signin_redirects_to_keycloak_with_code_flow_parameters(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        response = await client.get("/web/signin", params={"next": "/web"})
        assert response.status_code == 303
        params = authorize_params(response.headers["location"])
        assert params["client_id"] == WEB_CLIENT_ID
        assert params["response_type"] == "code"
        assert params["scope"] == "openid email profile"
        assert params["redirect_uri"] == "https://web.test/web/oidc/callback"
        assert params["code_challenge_method"] == "S256"
        assert len(params["state"]) >= 32
        assert len(params["nonce"]) >= 32
        # The transient cookie is scoped like a credential.
        set_cookie = response.headers["set-cookie"]
        assert TRANSIENT_COOKIE in set_cookie
        assert "HttpOnly" in set_cookie
        assert "Secure" in set_cookie
        assert "SameSite=lax" in set_cookie
        assert "Path=/" in set_cookie


async def test_signin_register_flag_starts_the_flow_on_the_registration_form(tmp_path, idp):
    """`register=1` adds `prompt=create` and changes nothing else (#144).

    The plain flow must stay prompt-free — sending every sign-in to the
    registration form would be the inverse of the bug being fixed.
    """
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        plain = await client.get("/web/signin", params={"next": "/web"})
        register = await client.get(
            "/web/signin", params={"next": "/web", "register": "1"}
        )
    plain_params = authorize_params(plain.headers["location"])
    register_params = authorize_params(register.headers["location"])
    assert "prompt" not in plain_params
    assert register_params["prompt"] == "create"
    # Same protected flow either way: only the first IdP screen differs.
    for key in ("client_id", "response_type", "scope", "redirect_uri", "code_challenge_method"):
        assert register_params[key] == plain_params[key]


async def test_full_sign_in_mints_a_session_and_lands_on_next(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        response = await sign_in(client, idp)
        assert response.status_code == 303
        assert response.headers["location"] == "/web"
        set_cookie = response.headers["set-cookie"]
        assert SESSION_COOKIE in set_cookie
        assert "HttpOnly" in set_cookie
        assert "Secure" in set_cookie
        assert "SameSite=lax" in set_cookie

        overview = await client.get("/web")
        assert overview.status_code == 200
        assert "Alice Example" in overview.text
        assert "Sign out" in overview.text


async def test_token_exchange_carries_the_client_secret_and_pkce_verifier(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        params = await start_signin(client, idp)
        await client.get("/web/oidc/callback", params={"code": "stub-code", "state": params["state"]})
    (token_request,) = idp.token_requests
    assert token_request["grant_type"] == "authorization_code"
    assert token_request["client_id"] == WEB_CLIENT_ID
    assert token_request["client_secret"] == "confidential-secret"
    assert token_request["code"] == "stub-code"
    assert token_request["redirect_uri"] == "https://web.test/web/oidc/callback"
    # The verifier really is the preimage of the challenge the flow sent.
    challenge = urlsafe_b64encode(sha256(token_request["code_verifier"].encode()).digest())
    assert challenge.decode().rstrip("=") == params["code_challenge"]


async def test_unauthenticated_page_redirects_through_signin_and_back(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        bounce = await client.get("/web")
        assert bounce.status_code == 303
        assert bounce.headers["location"] == "/web/signin?next=%2Fweb"
        response = await sign_in(client, idp, next_path="/web")
        assert response.headers["location"] == "/web"
        assert (await client.get("/web")).status_code == 200


async def test_callback_refuses_a_wrong_state(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        await start_signin(client, idp)
        response = await client.get(
            "/web/oidc/callback", params={"code": "stub-code", "state": "forged-state"}
        )
        assert response.status_code == 400
        assert idp.token_requests == []
        # No session was minted anywhere along the way.
        assert SESSION_COOKIE not in dict(client.cookies)
        assert (await client.get("/web")).status_code == 303


async def test_callback_without_a_started_flow_is_refused(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        response = await client.get(
            "/web/oidc/callback", params={"code": "stub-code", "state": "whatever"}
        )
        assert response.status_code == 400
        assert idp.token_requests == []


async def test_callback_cannot_be_replayed(tmp_path, idp):
    # The transient cookie is cleared by the first attempt, so a replayed
    # callback URL (history, logs) finds no flow to finish.
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        params = await start_signin(client, idp)
        first = await client.get(
            "/web/oidc/callback", params={"code": "stub-code", "state": params["state"]}
        )
        assert first.status_code == 303
        replay = await client.get(
            "/web/oidc/callback", params={"code": "stub-code", "state": params["state"]}
        )
        assert replay.status_code == 400
        assert len(idp.token_requests) == 1


async def test_idp_error_response_fails_without_touching_the_token_endpoint(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        await start_signin(client, idp)
        response = await client.get(
            "/web/oidc/callback",
            params={"error": "access_denied", "error_description": "<script>alert(1)</script>"},
        )
        assert response.status_code == 400
        assert "<script>" not in response.text
        assert idp.token_requests == []


async def test_failed_token_exchange_renders_the_fixed_failure_page(tmp_path, idp):
    idp.token_status = 400
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        response = await sign_in(client, idp)
        assert response.status_code == 400
        assert "Sign-in did not complete" in response.text
        assert (await client.get("/web")).status_code == 303


# --- ID token verification (the issue #83 regression tests) ------------------


async def test_a_same_realm_token_for_another_client_is_refused(tmp_path, idp):
    # Validly signed, right issuer, right nonce — wrong audience. This is the
    # desktop-token shape, and it must never mint a web session (issue #83).
    idp.claims_override = {"aud": OTHER_CLIENT_ID, "azp": OTHER_CLIENT_ID}
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        response = await sign_in(client, idp)
        assert response.status_code == 400
        assert (await client.get("/web")).status_code == 303


async def test_a_multi_audience_token_issued_for_another_client_is_refused(tmp_path, idp):
    # aud *contains* us, so the audience check alone passes — azp says the
    # token was issued for another client, and azp must win.
    idp.claims_override = {"aud": [WEB_CLIENT_ID, OTHER_CLIENT_ID], "azp": OTHER_CLIENT_ID}
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        assert (await sign_in(client, idp)).status_code == 400


async def test_a_wrong_issuer_is_refused(tmp_path, idp):
    idp.claims_override = {"iss": "https://other-realm.example.com/realms/nebari"}
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        assert (await sign_in(client, idp)).status_code == 400


async def test_a_token_signed_by_a_foreign_key_is_refused(tmp_path, idp):
    # Same kid, key not in the realm's JWKS: signature verification must fail.
    idp.signing_pem = ROGUE_KEY_PEM
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        assert (await sign_in(client, idp)).status_code == 400


async def test_a_wrong_nonce_is_refused(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        params = await start_signin(client, idp)
        idp.nonce = "a-nonce-from-some-other-flow"
        response = await client.get(
            "/web/oidc/callback", params={"code": "stub-code", "state": params["state"]}
        )
        assert response.status_code == 400


async def test_a_missing_nonce_is_refused(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        params = await start_signin(client, idp)
        idp.nonce = None
        response = await client.get(
            "/web/oidc/callback", params={"code": "stub-code", "state": params["state"]}
        )
        assert response.status_code == 400


async def test_a_bearer_typed_token_is_refused(tmp_path, idp):
    idp.claims_override = {"typ": "Bearer"}
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        assert (await sign_in(client, idp)).status_code == 400


# --- session cookie handling --------------------------------------------------


async def test_a_tampered_session_cookie_is_a_signed_out_browser(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        await sign_in(client, idp)
        value = dict(client.cookies)[SESSION_COOKIE]
        client.cookies.set(SESSION_COOKIE, value[:-4] + "AAAA", domain="web.test", path="/")
        assert (await client.get("/web")).status_code == 303


async def test_an_expired_session_cookie_is_a_signed_out_browser(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    codec = SessionCodec(SESSION_SECRET)
    now = int(time.time())
    expired = codec.encode(
        SESSION_PURPOSE,
        {"sub": "subject-alice", "csrf": "x", "iat": now - 7200, "exp": now - 3600},
    )
    async with web_client(app) as client:
        client.cookies.set(SESSION_COOKIE, expired, domain="web.test", path="/")
        assert (await client.get("/web")).status_code == 303


async def test_the_web_session_cookie_is_not_api_credentials(tmp_path, idp):
    # Two axes, both directions: a web session must not authenticate the API,
    # and an API IdToken cookie must not appear as a web session.
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        await sign_in(client, idp)
        api = await client.get("/v1/frames")
        assert api.status_code == 401


async def test_an_api_idtoken_cookie_is_not_a_web_session(tmp_path, idp, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_IDTOKEN_ALLOW_UNSIGNED", "true")
    app = make_web_app(tmp_path, idp)
    header = urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).decode().rstrip("=")
    payload = urlsafe_b64encode(
        json.dumps({"preferred_username": "alice", "org_id": "o", "workspace_id": "w"}).encode()
    ).decode().rstrip("=")
    async with web_client(app) as client:
        client.cookies.set("IdToken-test", f"{header}.{payload}.", domain="web.test", path="/")
        response = await client.get("/web")
        assert response.status_code == 303
        assert response.headers["location"].startswith("/web/signin?")


# --- CSRF and sign-out ---------------------------------------------------------


async def test_signout_without_a_csrf_token_is_refused(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        await sign_in(client, idp)
        response = await client.post("/web/signout", data={})
        assert response.status_code == 403
        # The session survives a refused sign-out.
        assert (await client.get("/web")).status_code == 200


async def test_signout_with_a_wrong_csrf_token_is_refused(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        await sign_in(client, idp)
        response = await client.post("/web/signout", data={"csrf_token": "guessed"})
        assert response.status_code == 403
        assert (await client.get("/web")).status_code == 200


async def test_signout_with_the_rendered_csrf_token_ends_the_session(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        await sign_in(client, idp)
        page = (await client.get("/web")).text
        match = re.search(r'name="csrf_token" value="([^"]+)"', page)
        assert match, "the layout must render the CSRF token into the sign-out form"
        response = await client.post("/web/signout", data={"csrf_token": match.group(1)})
        assert response.status_code == 303
        assert response.headers["location"] == "/web/signed-out"
        signed_out = await client.get("/web/signed-out")
        assert signed_out.status_code == 200
        assert (await client.get("/web")).status_code == 303


@pytest.mark.parametrize(
    ("query", "expected_href"),
    [
        ("?next=/admin/", "/web/signin?next=%2Fadmin%2F"),
        ("", "/web/signin"),
        ("?next=https://evil.example/", "/web/signin"),
        ("?next=//evil.example/", "/web/signin"),
        ("?next=/web/signin", "/web/signin"),
    ],
)
async def test_signing_in_again_returns_to_where_the_person_signed_out_from(
    tmp_path, idp, query, expected_href
):
    """The signed-out page passes a safe ``next`` on to sign-in, so leaving the
    admin panel and signing in again lands back on the panel. Anything the
    sign-in redirect would refuse is dropped here too."""

    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        page = await client.get(f"/web/signed-out{query}")

    assert page.status_code == 200
    links = re.findall(r'<a href="([^"]+)">Sign in again</a>', page.text)
    assert [html.unescape(link) for link in links] == [expected_href]


async def test_the_csrf_token_is_accepted_as_a_header_too(tmp_path, idp):
    # Future POST endpoints of this surface may be called with fetch; the
    # header form must be equivalent to the form field.
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        await sign_in(client, idp)
        page = (await client.get("/web")).text
        token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        response = await client.post("/web/signout", headers={"X-CSRF-Token": token})
        assert response.status_code == 303


async def test_a_csrf_token_from_another_session_is_refused(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as first:
        await sign_in(first, idp)
        first_token = re.search(
            r'name="csrf_token" value="([^"]+)"', (await first.get("/web")).text
        ).group(1)
    async with web_client(app) as second:
        await sign_in(second, idp)
        response = await second.post("/web/signout", data={"csrf_token": first_token})
        assert response.status_code == 403


# --- issue #119: the CSRF form fallback reads under the shared bound -------------
#
# `require_csrf` falls back to reading the form when no `X-CSRF-Token` header
# is presented, and that read used to be `request.form()` — unbounded, so any
# POST route that took the dependency could be made to buffer an arbitrarily
# large body by an authenticated caller. The read now goes through
# `web.forms.form_fields`: counted against MAX_FORM_BYTES rather than trusted
# from `Content-Length`, urlencoded only, refused with 413/415 plus
# `Connection: close` before the body is consumed. These pin each property on
# the one live route through the fallback (`POST /web/signout`) and then on a
# synthetic route, so the bound is demonstrably the dependency's rather than
# something /web/signout arranges.

FORM_TYPE_HEADER = {"Content-Type": "application/x-www-form-urlencoded"}


def oversized_form_stream():
    """A chunked urlencoded body far past the cap, counting what was pulled.

    Returns ``(stream, sent)`` where ``sent()`` reports how many bytes the
    server actually read — the buffering assertion, which a status code alone
    cannot make.
    """

    state = {"sent": 0}

    async def stream():
        for _ in range(64):
            chunk = b"a" * 4096
            state["sent"] += len(chunk)
            yield chunk

    return stream(), lambda: state["sent"]


async def test_an_oversized_signout_form_is_refused_not_buffered(tmp_path, idp):
    """#119's acceptance line, on the live route: chunked — so no
    ``Content-Length`` exists to consult — and far past the cap."""

    stream, sent = oversized_form_stream()
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        await sign_in(client, idp)
        response = await client.post("/web/signout", content=stream, headers=FORM_TYPE_HEADER)
        assert response.request.headers.get("transfer-encoding") == "chunked"
        assert response.status_code == 413
        assert "too large" in response.text
        # Refused before the body was consumed, so the response must close the
        # connection rather than leave it stalled mid-body (see
        # web.request_limits).
        assert response.headers.get("connection") == "close"
        # ...and it stopped reading rather than draining the whole thing.
        assert sent() <= MAX_FORM_BYTES + 8192, f"read {sent()} bytes past a {MAX_FORM_BYTES} cap"
        # A refused sign-out signs nothing out.
        assert (await client.get("/web")).status_code == 200


async def test_a_declared_oversize_form_is_refused_from_the_fast_path(tmp_path, idp):
    # httpx sets an accurate Content-Length here, so this refusal comes from
    # the declaration alone, before any read starts.
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        await sign_in(client, idp)
        response = await client.post(
            "/web/signout",
            content=b"csrf_token=" + b"a" * (MAX_FORM_BYTES * 2),
            headers=FORM_TYPE_HEADER,
        )
        assert response.status_code == 413
        assert response.headers.get("connection") == "close"
        assert (await client.get("/web")).status_code == 200


async def test_a_content_length_that_understates_the_form_does_not_help(tmp_path, idp):
    """The header is a fast path, never the gate — lying in it gains nothing."""

    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        await sign_in(client, idp)
        response = await client.post(
            "/web/signout",
            content=b"csrf_token=" + b"a" * (MAX_FORM_BYTES * 2),
            headers={**FORM_TYPE_HEADER, "Content-Length": "12"},
        )
        assert response.status_code == 413
        assert response.headers.get("connection") == "close"


async def test_a_multipart_body_is_refused_not_parsed(tmp_path, idp):
    """Multipart's parsing cost is not bounded by its byte count alone, and no
    form of this surface submits it — so it is refused before the read, never
    handed to a parser. (The old fallback parsed it.)"""

    pulled = 0

    async def multipart_body():
        nonlocal pulled
        pulled += 1
        yield b'--x\r\nContent-Disposition: form-data; name="csrf_token"\r\n\r\nt\r\n--x--\r\n'

    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        await sign_in(client, idp)
        response = await client.post(
            "/web/signout",
            content=multipart_body(),
            headers={"Content-Type": "multipart/form-data; boundary=x"},
        )
        assert response.status_code == 415
        assert response.headers.get("connection") == "close"
        # Refused before the read, not after it: the server pulled nothing.
        assert pulled == 0
        assert (await client.get("/web")).status_code == 200


async def test_a_token_refusal_is_still_the_403_page_not_a_body_refusal(tmp_path, idp):
    """The failure shapes stay distinct: being too large says nothing about a
    token, and a bad token is not a body the surface refused to read."""

    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        await sign_in(client, idp)
        # A well-shaped form with a wrong token keeps the 403 page...
        wrong = await client.post("/web/signout", data={"csrf_token": "guessed"})
        assert wrong.status_code == 403
        # ...and a JSON body presenting no header is a missing token (403),
        # not something the form fallback reads: only a request that claims a
        # form shape reaches the bounded read at all, so the server pulls
        # none of this body.
        pulled = 0

        async def json_body_stream():
            nonlocal pulled
            pulled += 1
            yield json.dumps({"csrf_token": "irrelevant"}).encode()

        json_body = await client.post(
            "/web/signout",
            content=json_body_stream(),
            headers={"Content-Type": "application/json"},
        )
        assert json_body.status_code == 403
        assert pulled == 0


@pytest.mark.parametrize(
    "content_type",
    ["APPLICATION/X-WWW-FORM-URLENCODED", "application/x-www-form-urlencoded; charset=UTF-8"],
    ids=["uppercase", "charset_param"],
)
async def test_the_fallback_reads_the_form_type_however_it_is_spelled(tmp_path, idp, content_type):
    """The fallback compares the media type, not a prefix of the header (#71):
    case and parameters do not change the type, so both of these are forms
    and their token is read. The uppercase spelling used to miss the prefix
    match and fall through to a 403 for a token that was right."""

    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        await sign_in(client, idp)
        page = (await client.get("/web")).text
        token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        response = await client.post(
            "/web/signout",
            content=f"csrf_token={token}".encode(),
            headers={"Content-Type": content_type},
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/web/signed-out"


@pytest.mark.parametrize(
    "content_type",
    ["application/x-www-form-urlencoded-not-really", "multipart/form-data-ish; boundary=x"],
    ids=["form_suffix_extended", "multipart_suffix_extended"],
)
async def test_a_type_that_only_begins_like_a_form_is_not_read_as_one(tmp_path, idp, content_type):
    """A type that merely *begins* with a form type does not claim a form
    (#71). Both used to pass the fallback's prefix match: the urlencoded one
    then had its body read and parsed (and its token honoured), the multipart
    one was refused 415 by ``form_fields``. Each is now what any other
    non-form type is — a missing token, the plain 403, none of its body
    pulled."""

    pulled = 0

    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        await sign_in(client, idp)
        page = (await client.get("/web")).text
        token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)

        async def form_shaped_body():
            nonlocal pulled
            pulled += 1
            yield f"csrf_token={token}".encode()

        response = await client.post(
            "/web/signout", content=form_shaped_body(), headers={"Content-Type": content_type}
        )
        assert response.status_code == 403
        assert pulled == 0
        # A refused sign-out signs nothing out.
        assert (await client.get("/web")).status_code == 200


async def test_an_undecodable_form_fails_the_token_check_not_the_server(tmp_path, idp):
    # Bytes that are not UTF-8 parse to no fields; the CSRF check fails closed
    # on the empty mapping rather than anything raising into a 500.
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        await sign_in(client, idp)
        response = await client.post("/web/signout", content=b"\xff\xfe\xfd", headers=FORM_TYPE_HEADER)
        assert response.status_code == 403


async def test_a_padded_form_under_the_cap_still_signs_out(tmp_path, idp):
    """The bound must not have moved for the legitimate client: the layout's
    urlencoded hidden field — with room to spare under the cap — still works."""

    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        await sign_in(client, idp)
        page = (await client.get("/web")).text
        token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        response = await client.post(
            "/web/signout", data={"csrf_token": token, "padding": "p" * 3000}
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/web/signed-out"


async def test_any_route_that_takes_the_dependency_inherits_the_bound(tmp_path, idp):
    """The point of fixing the dependency rather than each route: a future
    POST is protected by declaring ``Depends(require_csrf)``, with nothing to
    arrange first — the ordering constraint #90 had to write out (its
    content-type gate provably preceding the unbounded fallback) no longer
    exists for new routes."""

    ran = False
    router = session_gated_router()

    @router.post("/web/future-post")
    async def future_post(session=Depends(require_csrf)):
        nonlocal ran
        ran = True
        return {"ok": True}

    app = make_web_app(tmp_path, idp)
    register_ahead_of_the_mcp_mount(app, router)
    # Properly gated, says the rollout check — and the gate carries the bound.
    verify_web_route_protection(app.routes)
    stream, sent = oversized_form_stream()
    async with web_client(app) as client:
        await sign_in(client, idp)
        response = await client.post("/web/future-post", content=stream, headers=FORM_TYPE_HEADER)
        assert response.status_code == 413
        assert response.headers.get("connection") == "close"
        assert sent() <= MAX_FORM_BYTES + 8192, f"read {sent()} bytes past a {MAX_FORM_BYTES} cap"
        assert ran is False, "the endpoint ran on a body that should have been refused"


# --- security headers -----------------------------------------------------------


async def test_every_response_of_the_surface_carries_the_security_headers(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        signin = await client.get("/web/signin")
        callback_bounce = await client.get("/web/oidc/callback")
        page_bounce = await client.get("/web")
        await sign_in(client, idp)
        overview = await client.get("/web")
        stylesheet = await client.get("/web/app.css")
        signed_out = await client.get("/web/signed-out")
    for response in (signin, callback_bounce, page_bounce, overview, stylesheet, signed_out):
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "no-store" in response.headers["cache-control"]
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
    for response in (overview, signed_out):
        csp = response.headers["content-security-policy"]
        assert "default-src 'none'" in csp
        assert "frame-ancestors 'none'" in csp
        assert "form-action 'self'" in csp
        assert "script" not in csp  # no script source: the surface serves none


# --- authorization helpers --------------------------------------------------------


def mount_role_gated_test_pages(app) -> None:
    router = APIRouter()

    @router.get("/web/test/operator-only")
    def operator_page(session=Depends(require_operator)):
        return {"user": session.user, "body": "operator data"}

    @router.get("/web/test/owner-only")
    def owner_page(session=Depends(require_org_owner)):
        return {"user": session.user}

    # make_app has already mounted the MCP catch-all at "/", so a router
    # appended now would sit behind it and never match; slot the test pages
    # in ahead of it, exactly where a real page router would be included.
    before = len(app.router.routes)
    app.include_router(router)
    added = app.router.routes[before:]
    del app.router.routes[before:]
    app.router.routes[:0] = added


async def test_operator_pages_never_grant_without_a_role_source(tmp_path, idp):
    # The platform-role table is issue #87's. With no source the dependency
    # must not grant — and must not quietly 403 either, which would be
    # indistinguishable from a correct refusal; see the loudness test below.
    app = make_web_app(tmp_path, idp)
    mount_role_gated_test_pages(app)
    async with web_client(app) as client:
        await sign_in(client, idp)
        response = await client.get("/web/test/operator-only")
        assert response.status_code == 503
        assert "operator data" not in response.text


async def test_operator_pages_admit_operators_and_refuse_others(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    mount_role_gated_test_pages(app)
    # The issue #87 seam: a resolver installed on app.state. The session
    # principal follows the deployment's identity policy (user_from_claims):
    # unpinned here, so it is the preferred_username, exactly as on the API.
    app.state.platform_role_resolver = lambda user: ("operator" if user == "alice" else None)
    async with web_client(app) as client:
        await sign_in(client, idp)
        response = await client.get("/web/test/operator-only")
        assert response.status_code == 200
        assert response.json()["user"] == "alice"
    idp.sub = "subject-bob"
    idp.claims_override = {"preferred_username": "bob"}
    async with web_client(app) as client:
        await sign_in(client, idp)
        assert (await client.get("/web/test/operator-only")).status_code == 403


async def test_operator_pages_redirect_anonymous_browsers_to_signin(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    mount_role_gated_test_pages(app)
    response_transport = web_client(app)
    async with response_transport as client:
        response = await client.get("/web/test/operator-only")
        assert response.status_code == 303
        assert response.headers["location"] == "/web/signin?next=%2Fweb%2Ftest%2Foperator-only"


async def test_owner_pages_check_the_live_membership_role(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    mount_role_gated_test_pages(app)
    # The store normally lands on app.state in the lifespan, which these
    # ASGI-transport tests do not run; install it the same way make_app does.
    org_store = InMemoryOrgStore()
    app.state.org_store = org_store
    org_store.set_membership("alice", "org-1", role=ROLE_OWNER)
    org_store.set_membership("bob", "org-1", role=ROLE_MEMBER)
    async with web_client(app) as client:
        await sign_in(client, idp)
        assert (await client.get("/web/test/owner-only")).status_code == 200
    idp.claims_override = {"preferred_username": "bob"}
    async with web_client(app) as client:
        await sign_in(client, idp)
        assert (await client.get("/web/test/owner-only")).status_code == 403
    idp.claims_override = {"preferred_username": "nobody"}
    async with web_client(app) as client:
        await sign_in(client, idp)
        assert (await client.get("/web/test/owner-only")).status_code == 403


async def test_revoking_a_role_locks_out_an_existing_session(tmp_path, idp):
    # The stateless cookie's design premise: authorization is live, so a
    # session that outlives its role grants nothing.
    app = make_web_app(tmp_path, idp)
    mount_role_gated_test_pages(app)
    allowed = {"alice"}
    app.state.platform_role_resolver = lambda user: "operator" if user in allowed else None
    async with web_client(app) as client:
        await sign_in(client, idp)
        assert (await client.get("/web/test/operator-only")).status_code == 200
        allowed.clear()
        assert (await client.get("/web/test/operator-only")).status_code == 403


# --- startup preconditions ---------------------------------------------------------


def test_a_confidential_client_requires_a_secret(tmp_path, idp):
    values = web_values(tmp_path, idp, web={"client_secret": ""})
    with pytest.raises(Exception, match="client_secret"):
        Config.parse(values)


def test_a_weak_session_secret_is_refused(tmp_path, idp):
    values = web_values(tmp_path, idp, web={"session_secret": "short"})
    with pytest.raises(Exception, match="session_secret"):
        Config.parse(values)


def test_a_scope_without_openid_is_refused(tmp_path, idp):
    values = web_values(tmp_path, idp, web={"scope": "email profile"})
    with pytest.raises(Exception, match="openid"):
        Config.parse(values)


def test_a_missing_issuer_fails_the_rollout(tmp_path, idp, monkeypatch):
    monkeypatch.delenv("FRAMES_BEARER_ISSUER", raising=False)
    with pytest.raises(RuntimeError, match="realm URL"):
        make_web_app(tmp_path, idp, web={"issuer_url": ""})


def test_the_issuer_falls_back_to_the_bearer_issuer(tmp_path, idp, monkeypatch):
    monkeypatch.setenv("FRAMES_BEARER_ISSUER", idp.issuer)
    app = make_web_app(tmp_path, idp, web={"issuer_url": ""})
    assert app.state.web_surface.issuer_url == idp.issuer


def test_an_issuer_naming_a_different_realm_than_the_bearer_is_refused(tmp_path, idp, monkeypatch):
    monkeypatch.setenv("FRAMES_BEARER_ISSUER", "https://elsewhere.example.com/realms/other")
    with pytest.raises(RuntimeError, match="same realm"):
        make_web_app(tmp_path, idp)


def test_an_issuer_with_a_query_string_is_refused(tmp_path, idp):
    with pytest.raises(RuntimeError, match="unusable"):
        make_web_app(tmp_path, idp, web={"issuer_url": "https://auth.example.com/realms/r?x=1"})


def test_a_protection_map_that_blocks_the_web_prefix_fails_the_rollout(tmp_path, idp):
    security = {
        "default_access": "authenticated",
        "paths": [
            {"path": "/health", "match": "exact", "access": "public"},
            {"path": "/health/db", "match": "exact", "access": "public"},
        ],
    }
    with pytest.raises(RuntimeError, match="protection map"):
        make_web_app(tmp_path, idp, security=security)


def test_a_protection_map_that_blocks_the_invite_prefix_fails_the_rollout(tmp_path, idp):
    # The acceptance page (#90) is the one surface path whose audience has no
    # API credential at all, so a map that authenticates it turns every
    # invitee away before the route that exists for them is reached.
    security = {
        "default_access": "authenticated",
        "paths": [
            {"path": "/health", "match": "exact", "access": "public"},
            {"path": "/health/db", "match": "exact", "access": "public"},
            {"path": "/web", "match": "prefix", "access": "public"},
        ],
    }
    with pytest.raises(RuntimeError, match="/invite/accept"):
        make_web_app(tmp_path, idp, security=security)


async def test_a_hardened_map_with_a_public_web_prefix_serves_the_surface(tmp_path, idp):
    security = {
        "default_access": "authenticated",
        "paths": [
            {"path": "/health", "match": "exact", "access": "public"},
            {"path": "/health/db", "match": "exact", "access": "public"},
            {"path": "/web", "match": "prefix", "access": "public"},
            {"path": "/invite", "match": "prefix", "access": "public"},
            # The operator pages (#91). Map-public for the same reason as the
            # rest of the surface — an operator holds a browser session, not a
            # bearer token — with the role checked in-app, per request.
            {"path": "/admin", "match": "prefix", "access": "public"},
        ],
    }
    app = make_web_app(tmp_path, idp, security=security)
    async with web_client(app) as client:
        # The sign-in flow works with no API credentials...
        response = await sign_in(client, idp)
        assert response.status_code == 303
        assert (await client.get("/web")).status_code == 200
        # ...while the rest of the app is still protected by the map.
        assert (await client.get("/", follow_redirects=False)).status_code == 401


async def test_the_public_base_url_overrides_request_derived_redirects(tmp_path, idp):
    app = make_web_app(tmp_path, idp, web={"public_base_url": "https://frames.example.com"})
    async with web_client(app) as client:
        response = await client.get("/web/signin")
        params = authorize_params(response.headers["location"])
        assert params["redirect_uri"] == "https://frames.example.com/web/oidc/callback"


# --- non-ASCII hostile values must refuse, never crash ---------------------------


async def test_a_non_ascii_state_is_refused_not_crashed(tmp_path, idp):
    # compare_digest raises TypeError on non-ASCII str; the comparisons must
    # run over bytes so a hostile value is a 400, not a 500.
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        await start_signin(client, idp)
        response = await client.get("/web/oidc/callback", params={"code": "c", "state": "жетон"})
        assert response.status_code == 400


async def test_a_non_ascii_nonce_claim_is_refused_not_crashed(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        params = await start_signin(client, idp)
        idp.claims_override = {"nonce": "жетон"}
        response = await client.get(
            "/web/oidc/callback", params={"code": "stub-code", "state": params["state"]}
        )
        assert response.status_code == 400


async def test_a_non_ascii_csrf_token_is_refused_not_crashed(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        await sign_in(client, idp)
        response = await client.post("/web/signout", data={"csrf_token": "жетон"})
        assert response.status_code == 403
        assert (await client.get("/web")).status_code == 200


# --- codex round 1: audience, azp, typ, and transport ---------------------------


async def test_a_multi_audience_token_with_no_azp_is_refused(tmp_path, idp):
    # THE round-1 major. aud names us *and* the desktop client, and azp is
    # omitted — OIDC requires azp on a multi-audience ID token, so its absence
    # here means the token was minted for whoever else is in that list.
    idp.claims_override = {"aud": [WEB_CLIENT_ID, OTHER_CLIENT_ID]}
    idp.drop_claims = {"azp"}
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        assert (await sign_in(client, idp)).status_code == 400
        assert (await client.get("/web")).status_code == 303


async def test_a_single_audience_token_with_no_azp_is_accepted(tmp_path, idp):
    # The ordinary Keycloak shape must keep working: one aud, equal to us.
    idp.claims_override = {"aud": WEB_CLIENT_ID}
    idp.drop_claims = {"azp"}
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        assert (await sign_in(client, idp)).status_code == 303
        assert (await client.get("/web")).status_code == 200


async def test_a_single_element_audience_list_with_no_azp_is_accepted(tmp_path, idp):
    idp.claims_override = {"aud": [WEB_CLIENT_ID]}
    idp.drop_claims = {"azp"}
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        assert (await sign_in(client, idp)).status_code == 303


async def test_a_token_with_no_typ_claim_is_refused(tmp_path, idp):
    # typ is a barrier, so its absence must refuse rather than pass.
    idp.drop_claims = {"typ"}
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        assert (await sign_in(client, idp)).status_code == 400


async def test_a_token_response_without_an_id_token_is_refused(tmp_path, idp):
    idp.omit_id_token = True
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        assert (await sign_in(client, idp)).status_code == 400
        assert (await client.get("/web")).status_code == 303


@pytest.mark.parametrize(
    "issuer",
    [
        "http://auth.example.com/realms/nebari",
        "http://192.0.2.10/realms/nebari",
        # Suffix traps: these are ordinary internet hosts.
        "http://localhost.evil.example.com/realms/nebari",
        "http://127.0.0.1.evil.example.com/realms/nebari",
    ],
)
def test_a_non_loopback_http_issuer_fails_the_rollout(tmp_path, idp, issuer):
    # The confidential client secret is POSTed to this realm's token endpoint
    # and its JWKS verifies every ID token; plain http off-loopback hands both
    # to the network.
    with pytest.raises(RuntimeError, match="unusable"):
        make_web_app(tmp_path, idp, web={"issuer_url": issuer})


@pytest.mark.parametrize(
    "issuer",
    [
        "http://localhost:8080/realms/nebari",
        "http://127.0.0.1:8080/realms/nebari",
        "http://[::1]:8080/realms/nebari",
    ],
)
def test_loopback_http_issuers_are_permitted_for_development(tmp_path, idp, issuer):
    app = make_web_app(tmp_path, idp, web={"issuer_url": issuer})
    assert app.state.web_surface.issuer_url == issuer


def test_an_https_issuer_is_permitted(tmp_path, idp):
    app = make_web_app(tmp_path, idp, web={"issuer_url": "https://auth.example.com/realms/nebari"})
    assert app.state.web_surface.issuer_url == "https://auth.example.com/realms/nebari"


def test_an_insecure_public_base_url_is_refused(tmp_path, idp):
    with pytest.raises(RuntimeError, match="unusable"):
        make_web_app(tmp_path, idp, web={"public_base_url": "http://frames.example.com"})


# --- codex round 2: fail-closed routing, headers, lifetime, resolver -------------


def web_routes(app) -> list:
    # Every guarded prefix, not just /web: the acceptance page (#90) lives
    # under /invite, and a route-walking check that only looked at /web would
    # have silently stopped covering the surface the moment it grew.
    return [
        route
        for route in app.routes
        if on_web_surface(getattr(route, "path", ""), WEB_SURFACE_PREFIXES)
        and getattr(route, "path", "") != "/web/test/never-registered"
    ]


def test_every_web_route_outside_the_allowlist_requires_a_session(tmp_path, idp):
    # Belt and braces beside the router-level default: a future router that
    # registers a /web route without the dependency is caught here.
    app = make_web_app(tmp_path, idp)
    checked = 0
    for route in web_routes(app):
        if route.path in PUBLIC_WEB_PATHS:
            continue
        checked += 1
        dependency_calls = [d.call for d in route.dependant.dependencies]
        assert require_web_session in dependency_calls, (
            f"{route.path} is not in PUBLIC_WEB_PATHS and does not require a web session"
        )
    assert checked, "expected at least one session-gated /web route"


def test_the_public_allowlist_is_exactly_what_is_registered_public(tmp_path, idp):
    # The other direction: nothing quietly public that the allowlist does not
    # name, and no stale allowlist entry that no longer exists.
    app = make_web_app(tmp_path, idp)
    registered_public = {
        route.path
        for route in web_routes(app)
        if require_web_session not in [d.call for d in route.dependant.dependencies]
    }
    assert registered_public == set(PUBLIC_WEB_PATHS)


@pytest.mark.parametrize("build_page_router", [APIRouter, session_gated_router])
async def test_a_page_router_added_without_thought_is_authenticated(tmp_path, idp, build_page_router):
    # The seam #90-#92 use. Both ways of building a page router must come out
    # authenticated: session_gated_router carries the dependency itself, and a
    # bare APIRouter inherits it by being included inside the gated router.
    page_router = build_page_router()

    @page_router.get("/admin/invitations")
    async def pretend_operator_page():
        return {"secret": "operator data"}

    config = Config.parse(web_values(tmp_path, idp))
    surface = build_web_surface(config)
    app = make_web_app(tmp_path, idp)
    before = len(app.router.routes)
    app.include_router(make_router(surface, page_routers=[page_router]))
    added = app.router.routes[before:]
    del app.router.routes[before:]
    app.router.routes[:0] = added

    async with web_client(app) as client:
        response = await client.get("/admin/invitations")
        assert response.status_code == 303
        assert response.headers["location"].startswith("/web/signin?")
        assert "operator data" not in response.text


async def test_security_headers_cover_unmatched_paths_and_methods(tmp_path, idp):
    # Not just the routes this package wrote: a 405, an unmatched /web path
    # (which the mounted MCP catch-all answers, issue #86), and a redirect.
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        unsupported_method = await client.post("/web/signin")
        unmatched = await client.get("/web/does-not-exist")
        redirect = await client.get("/web")
        stylesheet = await client.get("/web/app.css")
    for response in (unsupported_method, unmatched, redirect, stylesheet):
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "no-store" in response.headers["cache-control"]
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "default-src 'none'" in response.headers["content-security-policy"]


async def test_the_headers_do_not_leak_onto_the_api(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        response = await client.get("/health")
    assert "content-security-policy" not in response.headers
    assert "referrer-policy" not in response.headers


async def test_session_cookies_carry_no_domain_attribute(tmp_path, idp):
    # A Domain attribute would widen the cookie to every sibling subdomain,
    # and __Host- forbids it outright — browsers reject such a cookie, which
    # would break sign-in silently.
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        started = await client.get("/web/signin")
        finished = await sign_in(client, idp)
    for response in (started, finished):
        for value in response.headers.get_list("set-cookie"):
            assert "domain=" not in value.lower()
            assert "Path=/" in value
            assert "Secure" in value
            assert "HttpOnly" in value


def test_the_session_lifetime_ceiling_is_enforced(tmp_path, idp):
    # The documented stateless-exposure bound must be the real one.
    assert WEB_SESSION_LIFETIME_CEILING_SECONDS == 8 * 3600
    with pytest.raises(Exception, match="session_lifetime_seconds"):
        Config.parse(web_values(tmp_path, idp, web={"session_lifetime_seconds": 7 * 24 * 3600}))
    with pytest.raises(Exception, match="session_lifetime_seconds"):
        Config.parse(
            web_values(
                tmp_path, idp, web={"session_lifetime_seconds": WEB_SESSION_LIFETIME_CEILING_SECONDS + 1}
            )
        )
    lowered = Config.parse(web_values(tmp_path, idp, web={"session_lifetime_seconds": 900}))
    assert lowered.web.session_lifetime_seconds == 900


async def test_the_session_cookie_expires_at_the_configured_lifetime(tmp_path, idp):
    app = make_web_app(tmp_path, idp, web={"session_lifetime_seconds": 900})
    async with web_client(app) as client:
        response = await sign_in(client, idp)
    session_cookie = next(
        value for value in response.headers.get_list("set-cookie") if SESSION_COOKIE in value
    )
    assert "Max-Age=900" in session_cookie


def test_a_low_entropy_session_secret_is_refused(tmp_path, idp):
    for secret in ("a" * 48, " " * 48, "abababababababababababababababababab"):
        with pytest.raises(Exception, match="session_secret"):
            Config.parse(web_values(tmp_path, idp, web={"session_secret": secret}))


def test_a_whitespace_only_client_secret_is_refused(tmp_path, idp):
    with pytest.raises(Exception, match="client_secret"):
        Config.parse(web_values(tmp_path, idp, web={"client_secret": "   "}))


async def test_a_missing_platform_role_source_is_loud_not_a_silent_refusal(tmp_path, idp, caplog):
    # A 503 and an error log, never a 403: a store without #87's
    # resolve_principal must not be indistinguishable from "you are not an
    # operator". #87 has landed, so every real store now provides the source
    # — producing the missing-source shape takes a store that deliberately
    # lacks it. The defensive path stays load-bearing regardless: a mis-wired
    # deployment or a future store backend can still be in this state.
    app = make_web_app(tmp_path, idp)
    mount_role_gated_test_pages(app)

    class _SourcelessStore(InMemoryOrgStore):
        resolve_principal = None  # the pre-#87 shape: no source at all

    app.state.org_store = _SourcelessStore()
    assert platform_role_source_name(app.state.org_store) is None
    async with web_client(app) as client:
        await sign_in(client, idp)
        with caplog.at_level("ERROR", logger="frames_server.web"):
            response = await client.get("/web/test/operator-only")
        assert response.status_code == 503
        assert "web_authorization_unavailable" in caplog.text


def test_a_store_providing_resolve_principal_is_reported_at_startup(tmp_path, idp):
    # The shape issue #87 landed: OrgStore.resolve_principal answering
    # membership and platform role together. Now that it *has* landed, the
    # default store provides it and startup reports the source instead of
    # logging the missing-source error.
    app = make_web_app(tmp_path, idp)

    class _PrincipalStore(InMemoryOrgStore):
        def resolve_principal(self, user_id):
            raise NotImplementedError

    assert platform_role_source_name(_PrincipalStore()) == "_PrincipalStore.resolve_principal"
    assert platform_role_source_name(InMemoryOrgStore()) == "InMemoryOrgStore.resolve_principal"
    assert app.state.web_platform_role_source == "InMemoryOrgStore.resolve_principal"


async def test_the_org_store_resolve_principal_path_decides_operator_authority(tmp_path, idp):
    # Exercised through the real dependency, in #87's shape.
    from dataclasses import dataclass

    @dataclass
    class _Principal:
        membership: object | None
        platform_role: str | None

    class _PrincipalStore(InMemoryOrgStore):
        def __init__(self, roles):
            super().__init__()
            self.roles = roles

        def resolve_principal(self, user_id):
            return _Principal(membership=self.get_membership(user_id), platform_role=self.roles.get(user_id))

    app = make_web_app(tmp_path, idp)
    mount_role_gated_test_pages(app)
    roles = {"alice": "operator"}
    app.state.org_store = _PrincipalStore(roles)
    async with web_client(app) as client:
        await sign_in(client, idp)
        assert (await client.get("/web/test/operator-only")).status_code == 200
        # A revoked grant (issue #87 collapses revoked to None) locks out at once.
        roles.clear()
        assert (await client.get("/web/test/operator-only")).status_code == 403


async def test_a_raising_platform_role_source_does_not_grant(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    mount_role_gated_test_pages(app)

    def _explode(_user):
        raise RuntimeError("platform role backend is down")

    app.state.platform_role_resolver = _explode
    async with web_client(app) as client:
        await sign_in(client, idp)
        response = await client.get("/web/test/operator-only")
        assert response.status_code == 500
        assert "operator data" not in response.text
        # Even the worst-case response keeps the surface's headers.
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "default-src 'none'" in response.headers["content-security-policy"]


async def test_a_junk_platform_role_source_does_not_grant(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    mount_role_gated_test_pages(app)
    for junk in ("operator", 42, object()):
        app.state.platform_role_resolver = junk
        async with web_client(app) as client:
            await sign_in(client, idp)
            response = await client.get("/web/test/operator-only")
            assert response.status_code == 500
            assert "operator data" not in response.text


# --- codex re-gate: bypasses of the fail-closed router --------------------------
#
# Both of these were demonstrated live against the previous revision, each
# returning 200 with an anonymous body. The construction helpers cannot stop
# them — they happen outside the construction path — so the guard has to
# inspect the registered result instead.


def register_ahead_of_the_mcp_mount(app, router) -> None:
    """Include *router* where a page router would sit: before the "/" mount."""

    before = len(app.router.routes)
    app.include_router(router)
    added = app.router.routes[before:]
    del app.router.routes[before:]
    app.router.routes[:0] = added


def anonymous_page_router() -> APIRouter:
    router = APIRouter(include_in_schema=False)

    @router.get("/web/future-page")
    async def future_page():
        return {"anonymous": True}

    return router


async def test_bypass_direct_include_router_is_authenticated_by_the_guard(tmp_path, idp):
    # codex bypass 1: app.include_router(APIRouter with a /web route).
    #
    # Under the inversion the route is not denied — it is *authenticated*. The
    # page author forgot the dependency; the guard supplies the boundary, so
    # the body is unreachable without a session and reachable with one.
    app = make_web_app(tmp_path, idp)
    register_ahead_of_the_mcp_mount(app, anonymous_page_router())
    async with web_client(app) as client:
        anonymous = await client.get("/web/future-page")
        assert anonymous.status_code == 303
        assert anonymous.headers["location"].startswith("/web/signin?")
        assert "anonymous" not in anonymous.text
        await sign_in(client, idp)
        assert (await client.get("/web/future-page")).status_code == 200


async def test_bypass_appending_to_the_returned_router_is_authenticated(tmp_path, idp):
    # codex bypass 2: append a route to the router make_router returned.
    config = Config.parse(web_values(tmp_path, idp))
    surface = build_web_surface(config)
    returned = make_router(surface)

    @returned.get("/web/future-page")
    async def future_page():
        return {"anonymous": True}

    app = make_web_app(tmp_path, idp)
    register_ahead_of_the_mcp_mount(app, returned)
    async with web_client(app) as client:
        response = await client.get("/web/future-page")
        assert response.status_code == 303
        assert "anonymous" not in response.text


@pytest.mark.parametrize("path", ["/web/future-page", "/admin/invitations", "/org/invitations"])
async def test_the_guard_covers_every_surface_prefix(tmp_path, idp, path):
    # #91 and #92 live at /admin and /org, so the guard has to be in place
    # before those pages arrive rather than after.
    router = APIRouter(include_in_schema=False)

    @router.get(path)
    async def anonymous_page():
        return {"anonymous": True}

    app = make_web_app(tmp_path, idp)
    register_ahead_of_the_mcp_mount(app, router)
    async with web_client(app) as client:
        response = await client.get(path)
        assert response.status_code == 303
        assert "anonymous" not in response.text


async def test_the_guard_refuses_at_boot_not_only_at_request_time(tmp_path, idp):
    # Preferred failure mode: the deploy fails, rather than the first
    # anonymous request being the thing that notices.
    app = make_web_app(tmp_path, idp)
    register_ahead_of_the_mcp_mount(app, anonymous_page_router())
    with pytest.raises(RuntimeError, match="/web/future-page"):
        async with app.router.lifespan_context(app):
            pass


def test_the_verifier_reports_offenders_regardless_of_registration_path(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    assert unprotected_web_routes(app.routes) == []
    register_ahead_of_the_mcp_mount(app, anonymous_page_router())
    assert unprotected_web_routes(app.routes) == ["/web/future-page"]
    with pytest.raises(RuntimeError, match="PUBLIC_WEB_PATHS"):
        verify_web_route_protection(app.routes)


def test_the_verifier_sees_through_nested_dependencies(tmp_path, idp):
    # A page gated with require_operator reaches require_web_session one level
    # down; a top-level-only walk would have called it unprotected.
    app = make_web_app(tmp_path, idp)
    mount_role_gated_test_pages(app)
    assert unprotected_web_routes(app.routes) == []


async def test_a_correctly_registered_page_still_serves(tmp_path, idp):
    # The guard must not deny the legitimate case it shares a path prefix with.
    page_router = session_gated_router()

    @page_router.get("/admin/invitations")
    async def admin_page(session=Depends(require_web_session)):
        return {"user": session.user}

    config = Config.parse(web_values(tmp_path, idp))
    surface = build_web_surface(config)
    app = make_web_app(tmp_path, idp)
    register_ahead_of_the_mcp_mount(app, make_router(surface, page_routers=[page_router]))
    async with web_client(app) as client:
        anonymous = await client.get("/admin/invitations")
        assert anonymous.status_code == 303
        await sign_in(client, idp)
        signed_in = await client.get("/admin/invitations")
        assert signed_in.status_code == 200
        assert signed_in.json()["user"] == "alice"


# --- codex re-gate: the lifetime ceiling as a WebSurface invariant ---------------


def a_surface(**overrides) -> dict:
    return {
        "client_id": WEB_CLIENT_ID,
        "client_secret": "confidential-secret",
        "scope": "openid email profile",
        "issuer_url": "https://auth.example.com/realms/nebari",
        "authorize_endpoint": "https://auth.example.com/realms/nebari/protocol/openid-connect/auth",
        "token_endpoint": "https://auth.example.com/realms/nebari/protocol/openid-connect/token",
        "jwks_url": "https://auth.example.com/realms/nebari/protocol/openid-connect/certs",
        "codec": SessionCodec(SESSION_SECRET),
        "session_lifetime_seconds": 3600,
        "public_base_url": "",
        **overrides,
    }


def test_a_directly_constructed_surface_cannot_exceed_the_ceiling():
    # The config validator binds the parse path only; this is the object the
    # routes actually mint cookies from.
    with pytest.raises(ValueError, match="ceiling"):
        WebSurface(**a_surface(session_lifetime_seconds=7 * 24 * 3600))
    with pytest.raises(ValueError, match="positive"):
        WebSurface(**a_surface(session_lifetime_seconds=0))
    within = WebSurface(**a_surface(session_lifetime_seconds=900))
    assert within.session_max_age() == 900


def test_the_mint_time_accessor_clamps_even_if_the_invariant_is_evaded():
    # object.__setattr__ past the frozen dataclass, as a future caller might
    # contrive; the value used at mint time is still bounded.
    surface = WebSurface(**a_surface())
    object.__setattr__(surface, "session_lifetime_seconds", 7 * 24 * 3600)
    assert surface.session_max_age() == WEB_SESSION_LIFETIME_CEILING_SECONDS


async def test_no_session_cookie_can_outlive_the_ceiling(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    object.__setattr__(app.state.web_surface, "session_lifetime_seconds", 7 * 24 * 3600)
    async with web_client(app) as client:
        response = await sign_in(client, idp)
        session_cookie = next(
            value for value in response.headers.get_list("set-cookie") if SESSION_COOKIE in value
        )
        assert f"Max-Age={WEB_SESSION_LIFETIME_CEILING_SECONDS}" in session_cookie
        # And the signed exp agrees with the cookie's Max-Age.
        decoded = SessionCodec(SESSION_SECRET).decode_session(dict(client.cookies)[SESSION_COOKIE])
        assert decoded.expires_at - decoded.issued_at == WEB_SESSION_LIFETIME_CEILING_SECONDS


# --- codex re-gate: headers on the exception path --------------------------------


async def test_an_exception_from_a_page_still_carries_the_security_headers(tmp_path, idp):
    router = session_gated_router()

    @router.get("/web/boom")
    async def boom():
        raise RuntimeError("deliberate")

    config = Config.parse(web_values(tmp_path, idp))
    surface = build_web_surface(config)
    app = make_web_app(tmp_path, idp)
    register_ahead_of_the_mcp_mount(app, make_router(surface, page_routers=[router]))
    async with web_client(app) as client:
        await sign_in(client, idp)
        response = await client.get("/web/boom")
    assert response.status_code == 500
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "no-store" in response.headers["cache-control"]
    assert response.headers["x-frame-options"] == "DENY"
    assert "default-src 'none'" in response.headers["content-security-policy"]
    # The traceback is logged, never rendered.
    assert "RuntimeError" not in response.text
    assert "deliberate" not in response.text


async def test_an_exception_off_the_surface_is_not_swallowed(tmp_path, idp):
    # The middleware must not become a global error handler.
    router = APIRouter(include_in_schema=False)

    @router.get("/not-web/boom")
    async def boom():
        raise RuntimeError("deliberate")

    app = make_web_app(tmp_path, idp)
    register_ahead_of_the_mcp_mount(app, router)
    async with web_client(app) as client:
        with pytest.raises(RuntimeError, match="deliberate"):
            await client.get("/not-web/boom")


# --- codex re-gate: role typing, precedence, secret trimming, port ----------------


async def test_a_non_string_role_that_compares_equal_is_refused(tmp_path, idp, caplog):
    # codex's exploit: an object whose __eq__("operator") is True obtained
    # operator access. The role must be a real str before any comparison.
    class _AlwaysOperator:
        def __eq__(self, other):
            return True

        def __bool__(self):
            return True

    app = make_web_app(tmp_path, idp)
    mount_role_gated_test_pages(app)
    app.state.platform_role_resolver = lambda _user: _AlwaysOperator()
    async with web_client(app) as client:
        await sign_in(client, idp)
        with caplog.at_level("ERROR", logger="frames_server.web"):
            response = await client.get("/web/test/operator-only")
        assert response.status_code == 403
        assert "operator data" not in response.text
        assert "web_platform_role_not_a_string" in caplog.text


async def test_the_store_outranks_the_app_state_override(tmp_path, idp):
    # A stray app.state assignment must not grant authority the server's own
    # table does not record.
    from dataclasses import dataclass

    @dataclass
    class _Principal:
        membership: object | None
        platform_role: str | None

    class _PrincipalStore(InMemoryOrgStore):
        def resolve_principal(self, user_id):
            return _Principal(membership=None, platform_role=None)

    app = make_web_app(tmp_path, idp)
    mount_role_gated_test_pages(app)
    app.state.org_store = _PrincipalStore()
    app.state.platform_role_resolver = lambda _user: "operator"
    async with web_client(app) as client:
        await sign_in(client, idp)
        response = await client.get("/web/test/operator-only")
        assert response.status_code == 403
        assert "operator data" not in response.text


def test_the_client_secret_is_forwarded_trimmed(tmp_path, idp):
    config = Config.parse(web_values(tmp_path, idp, web={"client_secret": "  s3cret  "}))
    assert config.web.client_secret == "s3cret"
    assert build_web_surface(config).client_secret == "s3cret"


async def test_the_trimmed_secret_is_what_keycloak_receives(tmp_path, idp):
    app = make_web_app(tmp_path, idp, web={"client_secret": "  s3cret  "})
    async with web_client(app) as client:
        await sign_in(client, idp)
    assert idp.token_requests[0]["client_secret"] == "s3cret"


def test_a_malformed_port_is_caught_at_startup(tmp_path, idp):
    # Availability nit: urlsplit parses this happily and only raises when
    # .port is read, so the loopback check saw host "localhost" and passed.
    with pytest.raises(RuntimeError, match="port"):
        make_web_app(tmp_path, idp, web={"issuer_url": "http://localhost:evil"})
    with pytest.raises(RuntimeError, match="port"):
        make_web_app(tmp_path, idp, web={"issuer_url": "https://auth.example.com:notaport"})


# --- codex gate 3: route types the walker could not introspect -------------------
#
# Both were demonstrated live: a mounted child app served /web/secret to
# anybody with `offenders []` and startup verification passing, and an
# anonymous /web/live socket accepted and sent. The lesson is the same one
# twice — a walker that only understands APIRoute must fail closed on the
# route types it cannot inspect, not skip them.


def anonymous_child_app() -> FastAPI:
    child = FastAPI()

    @child.get("/secret")
    async def secret():
        return {"anonymous": True}

    return child


def mount_anonymous_child(app) -> None:
    mount = Mount("/web", app=anonymous_child_app())
    app.router.routes.insert(0, mount)


async def test_a_mounted_child_app_under_the_surface_is_authenticated(tmp_path, idp):
    # codex bypass 3: app.mount("/web", child). Startup refuses the mount
    # outright (below); if one is nonetheless present at request time, the
    # guard authenticates its paths like any other — a mount's opacity stops
    # mattering once the boundary is path-based rather than route-based.
    app = make_web_app(tmp_path, idp)
    mount_anonymous_child(app)
    async with web_client(app) as client:
        response = await client.get("/web/secret")
        assert response.status_code == 303
        assert "anonymous" not in response.text


def test_a_mount_under_the_surface_is_an_offender(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    assert unprotected_web_routes(app.routes) == []
    mount_anonymous_child(app)
    assert unprotected_web_routes(app.routes) == ["/web"]
    with pytest.raises(RuntimeError, match="sub-application"):
        verify_web_route_protection(app.routes)


async def test_a_mount_under_the_surface_refuses_at_boot(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    mount_anonymous_child(app)
    with pytest.raises(RuntimeError, match="sub-application"):
        async with app.router.lifespan_context(app):
            pass


def test_the_mcp_mount_at_root_is_not_treated_as_a_surface_mount(tmp_path, idp):
    # "/" is not under a guarded prefix; the catch-all must stay unaffected.
    app = make_web_app(tmp_path, idp)
    assert any(isinstance(route, Mount) and route.path == "" for route in app.routes)
    assert unprotected_web_routes(app.routes) == []


def test_an_allowlisted_mount_is_permitted(tmp_path, idp, monkeypatch):
    # The escape hatch exists, and using it is a deliberate, reviewed act.
    import collab_hub_api.web.surface as surface_module

    app = make_web_app(tmp_path, idp)
    mount_anonymous_child(app)
    monkeypatch.setattr(surface_module, "ALLOWED_WEB_MOUNTS", frozenset({"/web"}))
    assert unprotected_web_routes(app.routes) == []


def add_anonymous_websocket(app, path: str = "/web/live") -> None:
    async def live(websocket):
        await websocket.accept()
        await websocket.send_text("anonymous")
        await websocket.close()

    app.router.routes.insert(0, WebSocketRoute(path, endpoint=live))


def test_a_websocket_under_the_surface_is_an_offender(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    add_anonymous_websocket(app)
    assert unprotected_web_routes(app.routes) == ["/web/live"]
    with pytest.raises(RuntimeError, match="WebSocket"):
        verify_web_route_protection(app.routes)


async def test_a_websocket_under_the_surface_refuses_at_boot(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    add_anonymous_websocket(app)
    with pytest.raises(RuntimeError, match="WebSocket"):
        async with app.router.lifespan_context(app):
            pass


def test_an_anonymous_websocket_connection_is_refused(tmp_path, idp):
    # codex bypass 4, and the part startup verification cannot catch: a socket
    # appended after the snapshot. BaseHTTPMiddleware never sees a websocket
    # scope at all, which is why the guard is raw ASGI.
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    app = make_web_app(tmp_path, idp)
    with TestClient(app) as client:
        # Appended *after* boot, so only the request-time guard can stop it.
        add_anonymous_websocket(app)
        with pytest.raises(WebSocketDisconnect) as refusal:
            with client.websocket_connect("/web/live") as socket:
                socket.receive_text()
    assert refusal.value.code == 1008


def test_a_websocket_off_the_surface_still_connects(tmp_path, idp):
    # The policy is surface-scoped, not app-wide.
    from starlette.testclient import TestClient

    app = make_web_app(tmp_path, idp)
    add_anonymous_websocket(app, path="/not-web/live")
    with TestClient(app) as client:
        with client.websocket_connect("/not-web/live") as socket:
            assert socket.receive_text() == "anonymous"


# --- codex gate 3: the false positive that denied a protected route ---------------


async def test_a_partial_wrapped_session_dependency_is_recognized(tmp_path, idp):
    # MEDIUM: identity comparison classified a genuinely protected route as an
    # offender and returned 503 to a validly signed-in browser. Fail-closed is
    # for what cannot be resolved, not for what can.
    router = APIRouter(
        include_in_schema=False,
        dependencies=[Depends(functools.partial(require_web_session))],
    )

    @router.get("/web/partial-gated")
    async def partial_gated():
        return {"ok": True}

    app = make_web_app(tmp_path, idp)
    register_ahead_of_the_mcp_mount(app, router)
    assert unprotected_web_routes(app.routes) == []
    async with web_client(app) as client:
        anonymous = await client.get("/web/partial-gated")
        assert anonymous.status_code == 303
        await sign_in(client, idp)
        signed_in = await client.get("/web/partial-gated")
        assert signed_in.status_code == 200
        assert signed_in.json() == {"ok": True}


async def test_a_forged_wraps_marker_proves_nothing(tmp_path, idp):
    # MAJOR: @wraps(require_web_session) on a dependency that enforces nothing
    # was read as protected and served an anonymous 200. __wrapped__ is a
    # label, not a constraint, so the lint no longer follows it — and, far more
    # importantly, the lint is no longer what protects the route.
    @functools.wraps(require_web_session)
    def wears_the_name_only(request):
        return None

    router = APIRouter(include_in_schema=False, dependencies=[Depends(wears_the_name_only)])

    @router.get("/web/wraps-forged")
    async def wraps_forged():
        return {"anonymous": True}

    app = make_web_app(tmp_path, idp)
    register_ahead_of_the_mcp_mount(app, router)
    # The lint is not fooled...
    assert unprotected_web_routes(app.routes) == ["/web/wraps-forged"]
    # ...and even if it were, the guard authenticates the path regardless.
    async with web_client(app) as client:
        response = await client.get("/web/wraps-forged")
        assert response.status_code == 303
        assert "anonymous" not in response.text


def test_unwrapping_terminates_on_a_wrapped_cycle():
    # A __wrapped__ cycle must not hang the startup verifier.
    def a():
        pass

    def b():
        pass

    a.__wrapped__ = b
    b.__wrapped__ = a
    assert unwrap_dependency(a) in (a, b)


def test_an_unrelated_dependency_is_still_an_offender(tmp_path, idp):
    # Unwrapping must not turn into "any dependency counts".
    def looks_official(request):
        return None

    router = APIRouter(include_in_schema=False, dependencies=[Depends(looks_official)])

    @router.get("/web/not-really-gated")
    async def not_really_gated():
        return {"anonymous": True}

    app = make_web_app(tmp_path, idp)
    register_ahead_of_the_mcp_mount(app, router)
    assert unprotected_web_routes(app.routes) == ["/web/not-really-gated"]


# --- codex gate 3: the ceiling is not shadowable ----------------------------------


def test_the_accessor_cannot_be_shadowed_on_the_instance():
    # LOW: object.__setattr__(surface, "session_max_age", lambda: 604800)
    # replaced the method and the mint path called the impostor. slots=True
    # makes an undeclared attribute unassignable.
    surface = WebSurface(**a_surface())
    with pytest.raises(AttributeError):
        object.__setattr__(surface, "session_max_age", lambda: 7 * 24 * 3600)


async def test_the_mint_path_does_not_call_through_an_overridable_accessor(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    surface = app.state.web_surface
    # Even with the field itself forced past the ceiling, the minted cookie is
    # bounded — the mint path clamps the field through a module function.
    object.__setattr__(surface, "session_lifetime_seconds", 7 * 24 * 3600)
    async with web_client(app) as client:
        response = await sign_in(client, idp)
    session_cookie = next(
        value for value in response.headers.get_list("set-cookie") if SESSION_COOKIE in value
    )
    assert f"Max-Age={WEB_SESSION_LIFETIME_CEILING_SECONDS}" in session_cookie


def test_the_clamp_is_a_module_function_on_the_raw_value():
    assert clamped_session_lifetime(900) == 900
    assert clamped_session_lifetime(7 * 24 * 3600) == WEB_SESSION_LIFETIME_CEILING_SECONDS
    assert clamped_session_lifetime(0) == 1
    assert clamped_session_lifetime(-5) == 1


# --- codex gate 4: the guard IS the boundary --------------------------------------
#
# Three rounds of "prove the route is protected by inspecting it" were beaten
# by forging whatever was inspected. The boundary is now the guard itself:
# path in, session out, no route consulted. These tests assert that property
# directly rather than asserting anything about route structure.


def forged_route_object():
    """A non-APIRoute that fakes the marker the lint used to accept.

    codex's MAJOR 2: a duck-typed ``.dependant`` was enough to be treated as a
    verified APIRoute, so a fabricated dependency tree passed startup
    verification and served an anonymous 200.
    """

    from types import SimpleNamespace

    async def endpoint(request):
        from starlette.responses import JSONResponse

        return JSONResponse({"anonymous": True})

    route = Route("/web/forged", endpoint=endpoint)
    # The forgery: claim a dependency tree naming the real session dependency.
    route.dependant = SimpleNamespace(
        dependencies=[SimpleNamespace(call=require_web_session, dependencies=[])]
    )
    return route


def test_a_forged_dependant_on_a_non_apiroute_is_refused_by_the_lint(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    app.router.routes.insert(0, forged_route_object())
    # The documented rule was always "APIRoute only"; now the code says so, so
    # the forged tree is not even consulted.
    assert unprotected_web_routes(app.routes) == ["/web/forged"]
    with pytest.raises(RuntimeError, match="Route"):
        verify_web_route_protection(app.routes)


async def test_a_forged_dependant_is_authenticated_by_the_guard_anyway(tmp_path, idp):
    # The point of the inversion: even if the lint were fooled, the request
    # path never reads the forged marker.
    app = make_web_app(tmp_path, idp)
    app.router.routes.insert(0, forged_route_object())
    async with web_client(app) as client:
        anonymous = await client.get("/web/forged")
        assert anonymous.status_code == 303
        assert "anonymous" not in anonymous.text
        await sign_in(client, idp)
        assert (await client.get("/web/forged")).status_code == 200


async def test_the_guard_authenticates_by_path_not_by_route(tmp_path, idp):
    # A route with no dependencies at all, registered the wrong way, on each
    # guarded prefix: the guard neither knows nor cares what the route says.
    router = APIRouter(include_in_schema=False)

    for path in ("/web/a", "/admin/b", "/org/c"):

        @router.get(path)
        async def page():
            return {"anonymous": True}

    app = make_web_app(tmp_path, idp)
    register_ahead_of_the_mcp_mount(app, router)
    async with web_client(app) as client:
        for path in ("/web/a", "/admin/b", "/org/c"):
            assert (await client.get(path)).status_code == 303
        await sign_in(client, idp)
        for path in ("/web/a", "/admin/b", "/org/c"):
            assert (await client.get(path)).status_code == 200


async def test_the_data_statement_page_is_readable_without_an_account(tmp_path, idp):
    """#146: the audience is deciding whether to create an account at all.

    The full statement text and a mailto for the deletion contact, served to
    a browser with no session and no cookies — the sweep tests already prove
    the path is allowlisted; this one proves the content is there.
    """

    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        page = await client.get("/web/data-statement")
    assert page.status_code == 200
    assert "What we store, and who can see it" in page.text
    assert html.escape(DATA_STATEMENT_TEXT) in page.text
    assert 'href="mailto:collab-support@openteams.com"' in page.text
    # #95: the short form links to the long-form documents rather than
    # absorbing them, so an invitee reading this can reach the Terms they are
    # about to be asked to accept without going back to their email.
    assert f'href="{TERMS_PATH}"' in page.text
    assert f'href="{PRIVACY_PATH}"' in page.text


async def test_the_legal_documents_are_readable_without_an_account(tmp_path, idp):
    """#95: the Keycloak acceptance step links here *before* it issues a token.

    This surface's session is minted from that token, so a session gate would
    mean the documents could only be read by someone who had already accepted
    them. Served to a browser with no session and no cookies.
    """

    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        terms = await client.get(TERMS_PATH)
        privacy = await client.get(PRIVACY_PATH)

    assert terms.status_code == 200
    assert "Terms of Service" in terms.text
    assert privacy.status_code == 200
    assert "Privacy Statement" in privacy.text


async def test_the_legal_documents_carry_the_surface_security_headers(tmp_path, idp):
    # Anonymous does not mean unpoliced: these answer with the same headers as
    # every other page of the surface, including the script-free CSP.
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        for path in (TERMS_PATH, PRIVACY_PATH):
            response = await client.get(path)
            assert response.headers["referrer-policy"] == "no-referrer"
            assert "no-store" in response.headers["cache-control"]
            assert response.headers["x-frame-options"] == "DENY"
            policy = response.headers["content-security-policy"]
            assert "default-src 'none'" in policy
            # Plain documents. The acceptance page is the surface's only
            # script budget, and these must not have quietly acquired one.
            assert "script-src" not in policy


def test_the_legal_document_paths_are_allowlisted() -> None:
    # Anonymity costs the reviewed line in the allowlist; `make_router`
    # refuses a public page route whose path is not in this set, so these two
    # assertions are what keep the routes registrable at all.
    assert TERMS_PATH in PUBLIC_WEB_PATHS
    assert PRIVACY_PATH in PUBLIC_WEB_PATHS


def test_each_document_renders_every_section_of_its_one_constant() -> None:
    """#95 keeps each document in a *single* module-level constant.

    That is what makes the git history usable as the acceptance record —
    Keycloak stores only a timestamp, with no document version — and it only
    holds if the page is rendered from the constant rather than from a second
    copy of the prose in the template. Every heading and every paragraph of
    the constant has to appear on the page.
    """

    pages = (
        (terms_of_service_page(), TERMS_OF_SERVICE_TEXT),
        (privacy_statement_page(), PRIVACY_STATEMENT_TEXT),
    )
    for page, document in pages:
        assert document, "a document with no sections would pass vacuously"
        for heading, paragraphs in document:
            assert html.escape(heading) in page
            for paragraph in paragraphs:
                assert html.escape(paragraph) in page


def test_placeholder_copy_is_labelled_as_placeholder() -> None:
    # Unreviewed legal text is linked from a real consent gate, so it must say
    # so on its face. The banner is driven by each document's own flag, and
    # clearing that flag is the act that asserts counsel has seen the copy.
    assert TERMS_OF_SERVICE_IS_PLACEHOLDER is (
        html.escape(PLACEHOLDER_NOTICE) in terms_of_service_page()
    )
    assert PRIVACY_STATEMENT_IS_PLACEHOLDER is (
        html.escape(PLACEHOLDER_NOTICE) in privacy_statement_page()
    )


def test_the_documents_publish_the_data_statement_contact() -> None:
    # One address across all three documents: they are read by the same person
    # minutes apart, and a second spelling is how they come to disagree.
    assert DATA_STATEMENT_CONTACT in terms_of_service_page()
    assert DATA_STATEMENT_CONTACT in privacy_statement_page()


def test_the_documents_cross_link_each_other_and_the_data_statement() -> None:
    assert f'href="{PRIVACY_PATH}"' in terms_of_service_page()
    assert f'href="{TERMS_PATH}"' in privacy_statement_page()
    for page in (terms_of_service_page(), privacy_statement_page()):
        assert 'href="/web/data-statement"' in page


def test_the_documents_honour_a_root_path() -> None:
    # Every link on these pages is app-relative, so a deployment mounted under
    # a prefix must not emit links that escape it.
    page = terms_of_service_page(root_path="/hub")
    assert f'href="/hub{PRIVACY_PATH}"' in page
    assert f'href="{PRIVACY_PATH}"' not in page.replace(f"/hub{PRIVACY_PATH}", "")


async def test_the_guard_leaves_the_public_allowlist_reachable(tmp_path, idp):
    # The allowlist is what the guard consults instead of route structure, so
    # sign-in must still work with no session at all.
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        for path in sorted(PUBLIC_WEB_PATHS):
            response = await client.get(path)
            assert response.status_code in (200, 303, 400), path
            if response.status_code == 303:
                # Only the callback bounces, and never back to sign-in.
                assert not response.headers["location"].startswith("/web/signin")


async def test_an_invalid_session_cookie_is_treated_as_none_by_the_guard(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    register_ahead_of_the_mcp_mount(app, anonymous_page_router())
    async with web_client(app) as client:
        await sign_in(client, idp)
        value = dict(client.cookies)[SESSION_COOKIE]
        client.cookies.set(SESSION_COOKIE, value[:-4] + "AAAA", domain="web.test", path="/")
        response = await client.get("/web/future-page")
        assert response.status_code == 303
        assert "anonymous" not in response.text


async def test_the_guard_preserves_the_next_target_including_query(tmp_path, idp):
    router = APIRouter(include_in_schema=False)

    @router.get("/admin/invitations")
    async def page():
        return {"anonymous": True}

    app = make_web_app(tmp_path, idp)
    register_ahead_of_the_mcp_mount(app, router)
    async with web_client(app) as client:
        response = await client.get("/admin/invitations", params={"page": "2"})
        assert response.status_code == 303
        assert response.headers["location"] == (
            "/web/signin?next=%2Fadmin%2Finvitations%3Fpage%3D2"
        )


async def test_the_guard_does_not_touch_paths_off_the_surface(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        assert (await client.get("/health")).status_code == 200
        # The API keeps answering with its own credential semantics, not a
        # browser redirect.
        assert (await client.get("/v1/frames")).status_code == 401


async def test_the_guard_fails_closed_without_a_surface_on_state(tmp_path, idp):
    # MINOR: an absent surface used to mean an empty route table and a
    # permitted request. "Cannot check" must never be served as "no check".
    app = make_web_app(tmp_path, idp)
    register_ahead_of_the_mcp_mount(app, anonymous_page_router())
    del app.state.web_surface
    async with web_client(app) as client:
        response = await client.get("/web/future-page")
        assert response.status_code == 503
        assert "anonymous" not in response.text


async def test_guard_denials_are_observable(tmp_path, idp, caplog):
    # MINOR: denials ran outside RequestObservabilityMiddleware, so the events
    # an operator most needs to see carried no request id and no access log.
    app = make_web_app(tmp_path, idp)
    register_ahead_of_the_mcp_mount(app, anonymous_page_router())
    with caplog.at_level("INFO", logger="frames_server.access"):
        async with web_client(app) as client:
            response = await client.get("/web/future-page")
    assert response.status_code == 303
    assert "x-request-id" in response.headers
    assert any(
        record.name == "frames_server.access"
        and getattr(record, "path", None) == "/web/future-page"
        for record in caplog.records
    )


async def test_websocket_refusals_are_observable(tmp_path, idp, caplog):
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    app = make_web_app(tmp_path, idp)
    with TestClient(app) as client:
        add_anonymous_websocket(app)
        with caplog.at_level("WARNING", logger="frames_server.web"):
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect("/web/live") as socket:
                    socket.receive_text()
    assert "web_websocket_refused" in caplog.text


async def test_an_allowlisted_mount_is_still_session_guarded(tmp_path, idp, monkeypatch):
    # MINOR: the allowlist used to be an unconditional bypass. It now only
    # suppresses the startup refusal — the guard still authenticates the
    # mounted app's paths.
    import collab_hub_api.web.surface as surface_module

    monkeypatch.setattr(surface_module, "ALLOWED_WEB_MOUNTS", frozenset({"/web"}))
    app = make_web_app(tmp_path, idp)
    mount_anonymous_child(app)
    assert unprotected_web_routes(app.routes) == []
    async with web_client(app) as client:
        anonymous = await client.get("/web/secret")
        assert anonymous.status_code == 303
        assert "anonymous" not in anonymous.text


def test_there_is_no_websocket_enable_flag(tmp_path, idp):
    # MINOR: a boolean that permits every socket with no session check is a
    # footgun, not a feature. Enabling sockets means writing enforcement.
    import collab_hub_api.web.surface as surface_module

    assert not hasattr(surface_module, "WEBSOCKETS_ALLOWED_ON_SURFACE")


def test_the_lint_still_reports_a_page_missing_its_dependency(tmp_path, idp):
    # The lint remains useful: the handler will not get its typed session.
    app = make_web_app(tmp_path, idp)
    register_ahead_of_the_mcp_mount(app, anonymous_page_router())
    assert unprotected_web_routes(app.routes) == ["/web/future-page"]


# --- codex gate 5: the guard's path must be the router's path --------------------
#
# The blocker: root_path stripping was hand-rolled with a raw startswith while
# Starlette's is segment-aware. With root_path="/", a request for /web/secret
# reduced to "web/secret" — outside the guarded prefix — so the guard skipped
# it while the router dispatched it. An anonymous 200 from two functions
# disagreeing about what "the path" means.
#
# These tests assert the property that makes the class of bug impossible:
# whatever the router routes, the guard guarded.


async def probe(app, path: str, root_path: str = "", cookies: dict | None = None):
    """Issue a request through the ASGI app with an explicit root_path."""

    transport = ASGITransport(app=app, root_path=root_path)
    async with AsyncClient(transport=transport, base_url="https://web.test") as client:
        if cookies:
            for name, value in cookies.items():
                client.cookies.set(name, value, domain="web.test", path="/")
        return await client.get(path)


@pytest.mark.parametrize("root_path", ["", "/", "/w", "/api", "/web/"])
async def test_the_guard_and_the_router_agree_under_every_root_path(tmp_path, idp, root_path):
    # An anonymous page registered at /web/secret. Whatever root_path the
    # server reports, the request must not reach the handler without a session.
    router = APIRouter(include_in_schema=False)

    @router.get("/web/secret")
    async def secret():
        return {"anonymous": True}

    app = make_web_app(tmp_path, idp)
    register_ahead_of_the_mcp_mount(app, router)
    response = await probe(app, "/web/secret", root_path=root_path)
    assert "anonymous" not in response.text, f"leaked under root_path={root_path!r}"
    assert response.status_code == 303, f"root_path={root_path!r}"


async def test_a_root_path_that_actually_prefixes_the_app(tmp_path, idp):
    # root_path="/web" means the app is mounted there, so its own /web/signin
    # is reached at /web/web/signin and get_route_path yields "/web/signin".
    # The guard must follow that, not the raw path.
    router = APIRouter(include_in_schema=False)

    @router.get("/web/secret")
    async def secret():
        return {"anonymous": True}

    app = make_web_app(tmp_path, idp)
    register_ahead_of_the_mcp_mount(app, router)
    response = await probe(app, "/web/web/secret", root_path="/web")
    assert response.status_code == 303
    assert "anonymous" not in response.text


@pytest.mark.parametrize(
    "path",
    [
        "/web/secret",
        "/web//secret",
        "/web/./secret",
        "/web/sub/../secret",
        "/web/%73ecret",
        "/web/%2e/secret",
        "/web/%252e/secret",
        "/WEB/secret",
        "/web/secret/",
        "/web/secret%2f",
        "//web/secret",
    ],
)
async def test_no_normalization_variant_reaches_a_handler_anonymously(tmp_path, idp, path):
    # Re-probed against get_route_path's output rather than the old strip.
    # The assertion is deliberately one-sided: a variant may 404 or redirect,
    # but it must never produce the handler's body without a session.
    router = APIRouter(include_in_schema=False)

    @router.get("/web/secret")
    async def secret():
        return {"anonymous": True}

    app = make_web_app(tmp_path, idp)
    register_ahead_of_the_mcp_mount(app, router)
    async with web_client(app) as client:
        response = await client.get(path)
    assert "anonymous" not in response.text, f"{path} leaked the handler body"


async def test_a_raw_path_disagreeing_with_path_does_not_help(tmp_path, idp):
    # scope['raw_path'] is attacker-influenced and Starlette routes on
    # scope['path']; the guard must key on the same one the router does.
    router = APIRouter(include_in_schema=False)

    @router.get("/web/secret")
    async def secret():
        return {"anonymous": True}

    app = make_web_app(tmp_path, idp)
    register_ahead_of_the_mcp_mount(app, router)

    received: dict = {}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            received["status"] = message["status"]
        elif message["type"] == "http.response.body":
            received.setdefault("body", b"")
            received["body"] += message.get("body", b"")

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/web/secret",
            "raw_path": b"/health",
            "root_path": "",
            "query_string": b"",
            "headers": [(b"host", b"web.test")],
            "client": ("127.0.0.1", 1234),
            "server": ("web.test", 443),
        },
        receive,
        send,
    )
    assert received["status"] == 303
    assert b"anonymous" not in received.get("body", b"")


async def test_a_signed_in_browser_still_reaches_pages_under_a_root_path(tmp_path, idp):
    # The guard must not become a wall: the agreement has to hold in the
    # allowing direction too.
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        await sign_in(client, idp)
        cookie = dict(client.cookies)[SESSION_COOKIE]
    for root_path in ("", "/", "/w"):
        response = await probe(app, "/web", root_path=root_path, cookies={SESSION_COOKIE: cookie})
        assert response.status_code == 200, f"root_path={root_path!r}"


# --- codex gate 5: the remaining minors -------------------------------------------


async def test_the_canonical_slash_redirect_is_left_to_the_router(tmp_path, idp):
    # MINOR: exact public matching intercepted Starlette's own slash redirect,
    # so /web/signin/ answered with a redirect *to sign in* — terminating, but
    # the shape of a loop.
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        for path in ("/web/signin/", "/web/app.css/", "/web/oidc/callback/"):
            response = await client.get(path)
            location = response.headers.get("location", "")
            assert not location.startswith("/web/signin?"), f"{path} bounced to sign-in"


async def test_the_trailing_slash_form_of_a_public_path_cannot_loop(tmp_path, idp):
    # Following it must terminate, and not by arriving back at sign-in.
    #
    # Starlette's canonical-slash redirect never actually fires on this app:
    # the MCP application is mounted at "/" and matches whatever the routers
    # did not, so it answers first (issue #86, not this branch's to fix). The
    # trailing-slash form therefore ends at that catch-all's refusal rather
    # than at the stylesheet. That is a fine terminal answer — it carries the
    # surface's security headers — and it is emphatically not an auth bounce.
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        response = await client.get("/web/app.css/", follow_redirects=True)
        assert response.status_code in (200, 401, 404)
        assert not response.history or not response.url.path.startswith("/web/signin")
        assert response.headers["referrer-policy"] == "no-referrer"


async def test_a_broken_codec_answers_the_documented_503(tmp_path, idp):
    # MINOR: this was fail-closed but as a 500 from outer error handling, not
    # the 503 the docs promise, and a 500 is where internals leak.
    class _BrokenCodec:
        def decode_session(self, value):
            raise RuntimeError("hsm unreachable: key handle 0xdeadbeef")

    app = make_web_app(tmp_path, idp)
    register_ahead_of_the_mcp_mount(app, anonymous_page_router())
    async with web_client(app) as client:
        await sign_in(client, idp)
        object.__setattr__(app.state.web_surface, "codec", _BrokenCodec())
        response = await client.get("/web/future-page")
    assert response.status_code == 503
    assert "anonymous" not in response.text
    assert "hsm unreachable" not in response.text
    assert "0xdeadbeef" not in response.text
    assert "Traceback" not in response.text


async def test_websocket_refusals_carry_a_request_id_and_a_metric(tmp_path, idp, caplog):
    # MINOR: socket denials bypass BaseHTTPMiddleware, so they had no request
    # id and no metrics sample, unlike every HTTP denial.
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from collab_hub_api.frames.observability import REQUEST_COUNT, UNMATCHED_PATH_LABEL

    def refusal_count() -> float:
        return REQUEST_COUNT.labels(
            method="WEBSOCKET", path=UNMATCHED_PATH_LABEL, status="1008"
        )._value.get()

    before = refusal_count()
    app = make_web_app(tmp_path, idp)
    with TestClient(app) as client:
        add_anonymous_websocket(app)
        with caplog.at_level("INFO"):
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect("/web/live") as socket:
                    socket.receive_text()
    assert refusal_count() == before + 1
    access_records = [
        record
        for record in caplog.records
        if record.name == "frames_server.access" and getattr(record, "path", None) == "/web/live"
    ]
    assert access_records, "socket refusal produced no access-log line"
    assert getattr(access_records[0], "request_id", None)


def test_a_page_route_outside_every_guarded_prefix_fails_the_rollout(tmp_path, idp):
    # MINOR: the guard keys on WEB_SURFACE_PREFIXES, so a page at a fourth
    # prefix would be reachable without a session no matter what its
    # dependencies say. Make that loud rather than silent.
    router = session_gated_router()

    @router.get("/reports/usage")
    async def a_page_at_a_new_prefix():
        return {"sensitive": True}

    app = make_web_app(tmp_path, idp)
    register_ahead_of_the_mcp_mount(app, router)
    assert stray_page_routes(app.routes) == ["/reports/usage"]
    with pytest.raises(RuntimeError, match="WEB_SURFACE_PREFIXES"):
        verify_web_route_protection(app.routes)


async def test_a_stray_page_route_refuses_at_boot(tmp_path, idp):
    router = session_gated_router()

    @router.get("/reports/usage")
    async def a_page_at_a_new_prefix():
        return {"sensitive": True}

    app = make_web_app(tmp_path, idp)
    register_ahead_of_the_mcp_mount(app, router)
    with pytest.raises(RuntimeError, match="/reports/usage"):
        async with app.router.lifespan_context(app):
            pass


def test_an_api_route_outside_the_prefixes_is_not_a_stray_page(tmp_path, idp):
    # The API is not a browser page: only routes depending on the web session
    # are page routes, so the check must not fire on the whole application.
    app = make_web_app(tmp_path, idp)
    assert stray_page_routes(app.routes) == []


# --- codex gate 6: metric cardinality and correlation forgery --------------------
#
# The risk I flagged about my own work, and it was broader than I said: the
# HTTP guard redirects BEFORE routing, so its denials had no route template
# either and the raw path became a label. An unauthenticated client could mint
# one Prometheus series per path it invented — memory exhaustion of the
# metrics store, reachable without credentials.


def request_series(metric) -> set[tuple]:
    """Every label combination currently registered on a metric."""

    return {labels for labels in metric._metrics}


async def test_many_anonymous_paths_produce_one_metric_series(tmp_path, idp):
    from collab_hub_api.frames.observability import REQUEST_COUNT, UNMATCHED_PATH_LABEL

    before = request_series(REQUEST_COUNT)
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        for n in range(25):
            response = await client.get(f"/web/cardinality-{n}")
            assert response.status_code == 303
    added = request_series(REQUEST_COUNT) - before
    assert len(added) <= 1, f"anonymous requests minted {len(added)} series: {added}"
    for labels in added:
        assert UNMATCHED_PATH_LABEL in labels
        assert "cardinality-" not in "".join(str(part) for part in labels)


def test_many_refused_sockets_produce_one_metric_series(tmp_path, idp):
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from collab_hub_api.frames.observability import REQUEST_COUNT

    before = request_series(REQUEST_COUNT)
    app = make_web_app(tmp_path, idp)
    with TestClient(app) as client:
        for n in range(25):
            add_anonymous_websocket(app, path=f"/web/cardinality-socket-{n}")
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect(f"/web/cardinality-socket-{n}") as socket:
                    socket.receive_text()
    added = request_series(REQUEST_COUNT) - before
    assert len(added) <= 1, f"socket refusals minted {len(added)} series: {added}"


async def test_pre_routing_credential_refusals_do_not_mint_series(tmp_path, idp):
    # The same exposure on the shared path-protection middleware, which also
    # answers before routing. Under a hardened map, an unauthenticated caller
    # reaches it on any path it likes.
    from collab_hub_api.frames.observability import REQUEST_COUNT

    security = {
        "default_access": "authenticated",
        "paths": [
            {"path": "/health", "match": "exact", "access": "public"},
            {"path": "/health/db", "match": "exact", "access": "public"},
            {"path": "/web", "match": "prefix", "access": "public"},
        ],
    }
    security["paths"].append({"path": "/invite", "match": "prefix", "access": "public"})
    security["paths"].append({"path": "/admin", "match": "prefix", "access": "public"})
    before = request_series(REQUEST_COUNT)
    app = make_web_app(tmp_path, idp, security=security)
    async with web_client(app) as client:
        for n in range(25):
            assert (await client.get(f"/invented-{n}")).status_code == 401
    added = request_series(REQUEST_COUNT) - before
    assert len(added) <= 1, f"pre-routing refusals minted {len(added)} series: {added}"


async def test_routed_requests_still_label_by_route_template(tmp_path, idp):
    # The bound must not cost the labels that make the metric useful. The
    # registry is process-global, so assert the series exists and its counter
    # moved rather than that it was newly created by this test.
    from collab_hub_api.frames.observability import REQUEST_COUNT

    sample = REQUEST_COUNT.labels(method="GET", path="/health", status="200")
    before = sample._value.get()
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        assert (await client.get("/health")).status_code == 200
    assert sample._value.get() == before + 1
    assert ("GET", "/health", "200") in request_series(REQUEST_COUNT)


async def test_the_access_log_still_records_the_real_refused_path(tmp_path, idp, caplog):
    # Bounding the metric must not blind the operator: the log is retained,
    # not held in memory forever, so it keeps the path that was actually hit.
    app = make_web_app(tmp_path, idp)
    with caplog.at_level("INFO", logger="frames_server.access"):
        async with web_client(app) as client:
            await client.get("/web/some-invented-path")
    assert any(
        getattr(record, "path", None) == "/web/some-invented-path" for record in caplog.records
    )


async def test_an_inbound_request_id_does_not_become_the_correlation_id(tmp_path, idp, caplog):
    # MINOR: a client supplying a known id could stamp its own requests with
    # it, making a victim's trail and an attacker's indistinguishable in the
    # records an operator would use to investigate.
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    app = make_web_app(tmp_path, idp)
    with TestClient(app) as client:
        add_anonymous_websocket(app)
        with caplog.at_level("INFO"):
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect(
                    "/web/live", headers={"x-request-id": "victim-correlation-id"}
                ) as socket:
                    socket.receive_text()
    records = [
        record
        for record in caplog.records
        if getattr(record, "path", None) == "/web/live" and hasattr(record, "request_id")
    ]
    assert records
    for record in records:
        assert record.request_id != "victim-correlation-id"
        # The caller's value is kept, but under a name that says whose it is.
        assert getattr(record, "client_request_id", None) == "victim-correlation-id"


@pytest.mark.parametrize(
    "forged",
    ["x" * 500, "has spaces", "newline\ninjected", "semi;colon", "", "brackets<>"],
)
def test_an_unusable_client_request_id_is_dropped(forged):
    from collab_hub_api.web.guard import _client_request_id

    scope = {"headers": [(b"x-request-id", forged.encode("latin-1", "replace"))]}
    assert _client_request_id(scope) is None


def test_an_id_shaped_client_request_id_is_kept():
    from collab_hub_api.web.guard import _client_request_id

    scope = {"headers": [(b"x-request-id", b"7f3a-9b2c.trace_1")]}
    assert _client_request_id(scope) == "7f3a-9b2c.trace_1"


# --- codex gate 7: the method label is client-controlled too ----------------------
#
# Closing the path axis left the method axis open, reachable by the same
# unauthenticated caller: HTTP permits arbitrary extension tokens as methods
# and the parser accepts them, so `method=request.method` minted a fresh
# series per invented verb. This was my own flagged residual — I noted I had
# not verified Starlette constrained the method, and it does not.


async def test_many_invented_methods_produce_one_metric_series(tmp_path, idp):
    from collab_hub_api.frames.observability import OTHER_METHOD_LABEL, REQUEST_COUNT

    before = request_series(REQUEST_COUNT)
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        for n in range(10):
            await client.request(f"X{n}", "/web/invented")
    added = request_series(REQUEST_COUNT) - before
    assert len(added) <= 1, f"invented methods minted {len(added)} series: {added}"
    for labels in added:
        assert OTHER_METHOD_LABEL in labels
        assert not any(str(part).startswith("X") for part in labels), labels


async def test_invented_methods_and_paths_together_stay_one_series(tmp_path, idp):
    # Both axes at once, which is what an attacker would actually do.
    from collab_hub_api.frames.observability import REQUEST_COUNT

    before = request_series(REQUEST_COUNT)
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        for n in range(10):
            await client.request(f"Q{n}", f"/web/pair-{n}")
    added = request_series(REQUEST_COUNT) - before
    assert len(added) <= 1, f"minted {len(added)} series: {added}"


async def test_ordinary_methods_still_label_normally(tmp_path, idp):
    # The bound must not flatten the methods an operator actually reads.
    from collab_hub_api.frames.observability import REQUEST_COUNT

    sample = REQUEST_COUNT.labels(method="GET", path="/health", status="200")
    before = sample._value.get()
    app = make_web_app(tmp_path, idp)
    async with web_client(app) as client:
        assert (await client.get("/health")).status_code == 200
    assert sample._value.get() == before + 1


def test_the_method_label_bound_maps_only_unknown_verbs():
    from collab_hub_api.frames.observability import OTHER_METHOD_LABEL, metric_method

    for method in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"):
        assert metric_method(method) == method
    # The guard's pseudo-method for a refused handshake must not be flattened.
    assert metric_method("WEBSOCKET") == "WEBSOCKET"
    for method in ("X0", "get", "PROPFIND", "", "GET ", "A" * 500):
        assert metric_method(method) == OTHER_METHOD_LABEL


def test_the_unmatched_sentinel_cannot_be_a_route_template():
    # The stated reason must be the true one: Starlette requires a route path
    # to start with "/", and the sentinel does not. NOT the angle brackets —
    # "/<unmatched>" is a perfectly legal template, and an earlier version of
    # this rationale wrongly claimed otherwise.
    from starlette.routing import Route

    from collab_hub_api.frames.observability import UNMATCHED_PATH_LABEL

    async def endpoint(request):
        return None

    assert Route("/<unmatched>", endpoint=endpoint).path == "/<unmatched>"
    assert not UNMATCHED_PATH_LABEL.startswith("/")
    with pytest.raises(AssertionError):
        Route(UNMATCHED_PATH_LABEL, endpoint=endpoint)


# --- second-reader finding 1: CSRF is the last dependency a page can forget ------
#
# The guard inversion made a forgotten `require_web_session` harmless: the
# middleware authenticates by path whatever the route says. It did nothing for
# a forgotten `require_csrf`, and nothing else does either — SameSite=Lax
# bounds it but does not close it, because the registrable domain is
# `openteams.app` and a sibling subdomain is *same-site*. So a POST added by a
# later page with no token check was silently unprotected. These tests pin the
# startup refusal that replaces the silence.


def csrf_less_post_router() -> APIRouter:
    router = session_gated_router()

    @router.post("/web/forgot-csrf")
    async def forgot(session=Depends(require_web_session)):
        return {"changed": True}

    return router


def test_a_post_without_require_csrf_fails_the_rollout(tmp_path, idp):
    app = make_web_app(tmp_path, idp)
    assert unprotected_web_routes(app.routes) == []
    register_ahead_of_the_mcp_mount(app, csrf_less_post_router())
    assert unprotected_web_routes(app.routes) == ["/web/forgot-csrf"]
    with pytest.raises(RuntimeError, match="CSRF_ENFORCED_IN_ROUTE"):
        verify_web_route_protection(app.routes)


def test_the_csrf_offence_names_the_methods_it_refused(tmp_path, idp):
    router = session_gated_router()

    @router.api_route("/web/mutating", methods=["GET", "POST", "DELETE"])
    async def mutating(session=Depends(require_web_session)):
        return {"ok": True}

    app = make_web_app(tmp_path, idp)
    register_ahead_of_the_mcp_mount(app, router)
    reasons = [reason for _, reason in offending_web_routes(app.routes)]
    assert len(reasons) == 1
    assert "DELETE, POST" in reasons[0]
    assert "GET" not in reasons[0]


def test_a_post_carrying_require_csrf_passes_the_check(tmp_path, idp):
    router = session_gated_router()

    @router.post("/web/gated-post")
    async def gated(session=Depends(require_csrf)):
        return {"ok": True}

    app = make_web_app(tmp_path, idp)
    register_ahead_of_the_mcp_mount(app, router)
    assert unprotected_web_routes(app.routes) == []
    verify_web_route_protection(app.routes)


def test_require_csrf_is_found_through_a_nested_dependency(tmp_path, idp):
    # A page will wrap it — `require_csrf` plus a role check in one dependency
    # is the obvious next move for #91/#92 — and a top-level-only walk would
    # call that route unprotected, wrongly, at startup.
    def operator_action(session=Depends(require_csrf), _op=Depends(require_operator)):
        return session

    router = session_gated_router()

    @router.post("/admin/nested")
    async def nested(session=Depends(operator_action)):
        return {"ok": True}

    app = make_web_app(tmp_path, idp)
    register_ahead_of_the_mcp_mount(app, router)
    assert unprotected_web_routes(app.routes) == []


def test_a_route_checking_csrf_in_route_is_exempt_only_by_declaration(tmp_path, idp, monkeypatch):
    # The pages built on this surface have real reasons to call the check by
    # hand (#90 answers a refusal in JSON; #91 renders its own page), and no
    # dependency walk can see that. The exemption is
    # therefore a declaration, reviewed like PUBLIC_WEB_PATHS — and it has to
    # be *made*: an in-route check alone still fails the rollout.
    import collab_hub_api.web.surface as surface_module

    router = session_gated_router()

    @router.post("/web/checks-itself")
    async def checks_itself(request: Request, session=Depends(require_web_session)):
        await require_csrf(request, session)
        return {"ok": True}

    app = make_web_app(tmp_path, idp)
    register_ahead_of_the_mcp_mount(app, router)
    assert unprotected_web_routes(app.routes) == ["/web/checks-itself"]

    monkeypatch.setattr(
        surface_module,
        "CSRF_ENFORCED_IN_ROUTE",
        surface_module.CSRF_ENFORCED_IN_ROUTE | {"/web/checks-itself"},
    )
    assert unprotected_web_routes(app.routes) == []
    verify_web_route_protection(app.routes)


def test_the_shipped_csrf_exemptions_are_exactly_the_reviewed_one():
    # Pinned to the exact set, so adding an exemption stays a visible edit to
    # a reviewed list rather than something that can accrete.
    #
    # All three are carried here rather than split across the changes that add
    # their routes. The rule that would have left #91's two to #91 — an
    # exemption for a route nobody can point at is a claim that cannot be
    # checked — is sound, and loses to a plainer one: the check and the routes
    # land in different PRs, so getting this count wrong costs a *second
    # broken build* rather than a wrong answer. Carrying all three removes the
    # coordination dependency on #91 remembering.
    from collab_hub_api.web.surface import ACCEPT_REDEEM_PATH, CSRF_ENFORCED_IN_ROUTE

    assert CSRF_ENFORCED_IN_ROUTE == frozenset(
        {
            "/invite/accept/redeem",
            "/admin/invitations",
            "/admin/invitations/revoke",
            # #142's owner page: the same in-route predicate as #91's two,
            # for the same page-shaped-refusal reason.
            "/web/org/invitations",
            "/web/org/invitations/revoke",
            # #44's first-invite naming POST: same page, same predicate.
            "/web/org/invitations/name",
        }
    )
    # Spelled literally above and compared against the constant here: #90's
    # path has a constant on this branch, #91's two do not, and inventing them
    # locally would be the second-spelling drift this module removed for the
    # stylesheet.
    assert ACCEPT_REDEEM_PATH == "/invite/accept/redeem"


def redeem_style_router(path: str) -> APIRouter:
    """A POST that checks CSRF in-route, the way #90's redemption endpoint does."""

    router = session_gated_router()

    @router.post(path)
    async def redeem(request: Request, session=Depends(require_web_session)):
        # The shape that keeps the dependency out of reach here: a
        # content-type gate ahead of the in-route call, so every refusal of
        # this endpoint is JSON-shaped (and, before #119 bounded the
        # fallback, so the form parse was provably unreachable).
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            return {"outcome": "unsupported_media_type"}
        await require_csrf(request, session)
        return {"outcome": "accepted"}

    return router


def test_the_shipped_exemption_actually_covers_the_redemption_endpoint(tmp_path, idp):
    # The entry must not be inert. #90 has landed, so this runs against the
    # real mounted route under the real prefixes — no scaffolding. An earlier
    # revision of this test synthesised both, because /invite was not yet a
    # guarded prefix when it was written.
    app = make_web_app(tmp_path, idp)
    assert unprotected_web_routes(app.routes) == []
    verify_web_route_protection(app.routes)


def test_the_exemption_is_load_bearing_not_a_vacuous_pass(tmp_path, idp):
    # Negative control for the test above: a handler shaped exactly like #90's,
    # at a path the set does not name, is refused. So the pass there is the
    # entry doing work rather than the CSRF check failing to look.
    app = make_web_app(tmp_path, idp)
    register_ahead_of_the_mcp_mount(app, redeem_style_router("/invite/accept/redeem-elsewhere"))
    assert unprotected_web_routes(app.routes) == ["/invite/accept/redeem-elsewhere"]
    with pytest.raises(RuntimeError, match="CSRF_ENFORCED_IN_ROUTE"):
        verify_web_route_protection(app.routes)


def test_signout_is_the_only_state_changing_route_and_it_is_gated(tmp_path, idp):
    from collab_hub_api.web.authz import route_enforces_csrf, route_unsafe_methods

    app = make_web_app(tmp_path, idp)
    mutating = [
        route
        for route in app.routes
        if isinstance(route, APIRoute)
        and route.path.startswith("/web")
        and route_unsafe_methods(route)
    ]
    assert [route.path for route in mutating] == ["/web/signout"]
    assert route_enforces_csrf(mutating[0])


# --- second-reader finding 2: the guard covers three prefixes, the map check one --
#
# WEB_SURFACE_PREFIXES promised /web, /admin and /org while the startup
# precondition only knew the six /web paths, so on a hardened map an /admin
# page passed the session guard and was then 401'd by the API credential check
# a browser cannot satisfy — PathProtectionMiddleware is added first and so
# runs innermost, *after* the guard. The fix derives the paths from the route
# table, which cannot drift from itself.


def hardened_map(*extra: str) -> dict:
    """A hardened map covering the prefixes the surface actually serves.

    ``/invite`` is in the base set rather than an ``extra``: #90's acceptance
    page and redemption endpoint are mounted, so a map without it fails the
    route-derived check before a test gets to whatever it meant to assert.
    ``/admin`` stays an ``extra`` — nothing is mounted there yet, so a test
    that wants it must ask.
    """

    return {
        "default_access": "authenticated",
        "paths": [
            {"path": "/health", "match": "exact", "access": "public"},
            {"path": "/health/db", "match": "exact", "access": "public"},
            {"path": "/web", "match": "prefix", "access": "public"},
            {"path": "/invite", "match": "prefix", "access": "public"},
            *({"path": path, "match": "prefix", "access": "public"} for path in extra),
        ],
    }


def admin_page_router() -> APIRouter:
    router = session_gated_router()

    @router.get("/admin/invitations")
    async def admin_page(session=Depends(require_operator)):
        return {"user": session.user}

    return router


def test_an_admin_page_the_map_would_401_fails_the_rollout(tmp_path, idp):
    app = make_web_app(tmp_path, idp, security=hardened_map())
    config = Config.parse(web_values(tmp_path, idp, security=hardened_map()))
    assert blocked_web_route_paths(app.routes, config) == []
    register_ahead_of_the_mcp_mount(app, admin_page_router())
    assert blocked_web_route_paths(app.routes, config) == ["/admin/invitations"]
    with pytest.raises(RuntimeError, match="/admin/invitations") as refusal:
        enforce_web_surface_map_access(app.routes, config)
    # The message must name the entry to add, not merely the problem.
    assert "path: /admin" in str(refusal.value)


def test_the_map_check_passes_once_the_prefix_is_public(tmp_path, idp):
    security = hardened_map("/admin")
    app = make_web_app(tmp_path, idp, security=security)
    config = Config.parse(web_values(tmp_path, idp, security=security))
    register_ahead_of_the_mcp_mount(app, admin_page_router())
    assert blocked_web_route_paths(app.routes, config) == []
    enforce_web_surface_map_access(app.routes, config)


def test_a_guarded_prefix_with_no_routes_asks_nothing_of_the_map(tmp_path, idp):
    # /org is a guarded prefix with no pages yet (#92). Demanding that every
    # deployment open it now would be asking operators to widen a map for
    # paths that do not exist — and a request there falls through to the MCP
    # catch-all, which runs its own McpAuthMiddleware.
    from collab_hub_api.web.surface import WEB_SURFACE_PREFIXES

    assert "/org" in WEB_SURFACE_PREFIXES
    security = hardened_map()
    app = make_web_app(tmp_path, idp, security=security)
    config = Config.parse(web_values(tmp_path, idp, security=security))
    assert not any(
        isinstance(route, APIRoute) and route.path.startswith("/org") for route in app.routes
    )
    enforce_web_surface_map_access(app.routes, config)


async def test_the_map_check_runs_at_boot_for_routes_added_after_make_app(tmp_path, idp):
    # Same two moments as verify_web_route_protection: make_app sees what it
    # registered, the lifespan sees whatever arrived afterwards.
    app = make_web_app(tmp_path, idp, security=hardened_map())
    register_ahead_of_the_mcp_mount(app, admin_page_router())
    with pytest.raises(RuntimeError, match="/admin/invitations"):
        async with app.router.lifespan_context(app):
            pass


# --- second-reader finding 3: the identity pin is not reconciled -----------------


def test_a_legacy_identity_deployment_warns_rather_than_refusing(tmp_path, idp, monkeypatch, caplog):
    # `user_from_claims` honours FRAMES_AUTH_IDENTITY_CLAIM; unset means the
    # legacy precedence (preferred_username, email, sub), while Gate E keys
    # collab_platform_roles.user_id on the OIDC sub. The row will not match and
    # require_operator refuses — correctly, and unreadably.
    #
    # Not a startup refusal: the pin governs every ACL principal in the Frames
    # API, `frames.identity` documents that leaving it unset keeps an existing
    # deployment unaffected, and refusing here would take sign-in and the
    # invitee-facing pages down over a condition that grants nobody anything.
    monkeypatch.delenv("FRAMES_AUTH_IDENTITY_CLAIM", raising=False)
    with caplog.at_level(logging.WARNING, logger="frames_server.web"):
        app = make_web_app(tmp_path, idp)
    assert app.state.web_identity_pinned_to_sub is False
    assert "web_identity_not_pinned_to_sub" in caplog.text
    # And it still serves: the surface is usable for everything that does not
    # need operator authority.
    assert any(
        isinstance(route, APIRoute) and route.path == "/web/signin" for route in app.routes
    )


def test_a_pinned_deployment_is_silent(tmp_path, idp, monkeypatch, caplog):
    monkeypatch.setenv("FRAMES_AUTH_IDENTITY_CLAIM", "sub")
    monkeypatch.setenv("FRAMES_BEARER_ISSUER", idp.issuer)
    with caplog.at_level(logging.WARNING, logger="frames_server.web"):
        app = make_web_app(tmp_path, idp)
    assert app.state.web_identity_pinned_to_sub is True
    assert "web_identity_not_pinned_to_sub" not in caplog.text


# --- second-reader finding 5: one definition of the stylesheet path --------------


def test_the_stylesheet_path_has_a_single_definition():
    # Two independent spellings of "/web/app.css" existed, and only the
    # surface one fed PUBLIC_WEB_PATHS and the startup precondition — so a
    # drift would have moved the route without moving its public exemption and
    # quietly required a session for the stylesheet.
    from collab_hub_api.web import pages as pages_module
    from collab_hub_api.web import surface as surface_module

    assert pages_module.STYLE_PATH is surface_module.STYLE_ASSET_PATH
    assert surface_module.STYLE_ASSET_PATH in surface_module.PUBLIC_WEB_PATHS


# --- the chart's own defaults must satisfy the map check -------------------------
#
# Review of #120 found the gap this section closes: the chart shipped a
# hardened default map naming only /health, /health/db and / , so a hardened
# install (api.ingress.enabled, or security.enforce: true) that enabled the web
# surface would fail the route-derived check the moment it started. Unhardened
# installs were never affected — the chart passes PATHS="[]" and
# DEFAULT_ACCESS="public" when enforcement resolves false — which is precisely
# why the gap was invisible. Reading the values file is the point: asserting
# against a copy of the defaults would prove nothing about what ships.

CHART_VALUES = pathlib.Path(__file__).resolve().parents[2] / "helm" / "collab-hub" / "values.yaml"


def chart_security_defaults() -> dict:
    """The chart's default `security` map, parsed without a YAML dependency.

    Deliberately narrow and deliberately brittle: it understands exactly the
    shape this block is written in, and raises rather than guessing if that
    shape changes. A parser that silently returned {} on an unfamiliar file
    would turn this test into one that passes because it checked nothing.
    """

    lines = CHART_VALUES.read_text().splitlines()
    start = lines.index("security:")
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i] and not lines[i][0].isspace()),
        len(lines),
    )
    block = [line for line in lines[start:end] if line.strip() and not line.lstrip().startswith("#")]

    paths: list[dict] = []
    in_paths = False
    scalars: dict[str, str] = {}
    for line in block:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if stripped == "paths:" and indent == 2:
            in_paths = True
            continue
        if in_paths and indent <= 2:
            in_paths = False
        if in_paths:
            key, _, value = stripped.lstrip("- ").partition(": ")
            if stripped.startswith("- "):
                paths.append({})
            if not paths:
                raise AssertionError(f"security.paths entry before any list item: {line!r}")
            paths[-1][key] = value.strip()
            continue
        if indent == 2 and ": " in stripped:
            key, _, value = stripped.partition(": ")
            scalars[key] = value.strip()

    if not paths or any(set(entry) != {"path", "match", "access"} for entry in paths):
        raise AssertionError(f"unexpected security.paths shape in {CHART_VALUES}: {paths}")
    return {"paths": paths, **scalars}


def test_the_parser_reads_the_entries_the_chart_actually_ships():
    # Guards the test below against a parser that quietly reads nothing.
    defaults = chart_security_defaults()
    assert defaults["defaultAccess"] == "authenticated"
    assert {"path": "/health", "match": "exact", "access": "public"} in defaults["paths"]
    assert {"path": "/", "match": "exact", "access": "authenticated"} in defaults["paths"]


def test_the_chart_default_map_satisfies_the_route_check_with_the_surface_on(tmp_path, idp):
    # The case review identified: a hardened install enabling the web surface.
    # The chart renders security.paths verbatim and appends its /metrics rule
    # (`collab-hub.security-paths`), so this is the map such an install runs.
    defaults = chart_security_defaults()
    security = {
        "default_access": defaults["defaultAccess"],
        "paths": [
            *defaults["paths"],
            {"path": "/metrics", "match": "exact", "access": defaults["metricsAccess"]},
        ],
    }
    app = make_web_app(tmp_path, idp, security=security)
    config = Config.parse(web_values(tmp_path, idp, security=security))
    assert blocked_web_route_paths(app.routes, config) == []
    enforce_web_surface_map_access(app.routes, config)


async def test_the_chart_default_map_serves_the_surface_end_to_end(tmp_path, idp):
    # And the surface is really reachable under that map, rather than merely
    # passing a startup check: sign-in works with no API credentials, while /
    # stays protected.
    defaults = chart_security_defaults()
    security = {
        "default_access": defaults["defaultAccess"],
        "paths": [
            *defaults["paths"],
            {"path": "/metrics", "match": "exact", "access": defaults["metricsAccess"]},
        ],
    }
    app = make_web_app(tmp_path, idp, security=security)
    async with web_client(app) as client:
        assert (await sign_in(client, idp)).status_code == 303
        assert (await client.get("/web")).status_code == 200
        assert (await client.get("/", follow_redirects=False)).status_code == 401


def test_the_chart_leaves_the_invite_prefix_public_for_the_acceptance_page():
    # #90's acceptance page and its redemption endpoint land under /invite, and
    # both must be map-public for the same reason the rest of the surface is —
    # an invitee holds no API credential at all. Shipped here rather than on
    # #90 for the same ordering reason as the CSRF exemption.
    paths = chart_security_defaults()["paths"]
    assert {"path": "/web", "match": "prefix", "access": "public"} in paths
    assert {"path": "/invite", "match": "prefix", "access": "public"} in paths


def test_the_chart_ships_admin_ahead_of_its_routes():
    # #91 adds /admin routes and does not carry a map entry for them. With the
    # entry on neither change, release/public is transiently broken between the
    # two merges: the operator page lands, the startup check finds /admin
    # resolving to authenticated, and every hardened install refuses to start.
    # The prefix is inert until those routes exist, so shipping it early is the
    # cheaper mistake.
    paths = chart_security_defaults()["paths"]
    assert {"path": "/admin", "match": "prefix", "access": "public"} in paths


def test_the_chart_still_omits_org():
    # /org is routeless with no change adding routes to it, so there is no
    # merge for an early entry to be early *for*. It goes in with #92.
    paths = {entry["path"] for entry in chart_security_defaults()["paths"]}
    assert "/org" not in paths


def test_an_admin_route_would_pass_the_map_check_on_chart_defaults(tmp_path, idp):
    # The property the early entry buys, asserted rather than argued: mount a
    # route where #91 will put one, under the map the chart actually ships, and
    # the check that would otherwise refuse the rollout passes.
    defaults = chart_security_defaults()
    security = {
        "default_access": defaults["defaultAccess"],
        "paths": [
            *defaults["paths"],
            {"path": "/metrics", "match": "exact", "access": defaults["metricsAccess"]},
        ],
    }
    app = make_web_app(tmp_path, idp, security=security)
    config = Config.parse(web_values(tmp_path, idp, security=security))
    register_ahead_of_the_mcp_mount(app, admin_page_router())
    assert blocked_web_route_paths(app.routes, config) == []
    enforce_web_surface_map_access(app.routes, config)


# --- the startup checks are a rollout gate, not a runtime control ----------------
#
# The gate flagged that the lint runs before the lifespan yield and never
# again, so a route registered after the server starts is not rechecked. That
# is accepted rather than closed, and these tests make the accepted property
# executable: the boundary is asserted in both directions, so a later reader
# finds a decision rather than an oversight.


async def test_a_route_added_before_the_yield_is_still_caught(tmp_path, idp):
    # The covered half: anything a caller of make_app registers before traffic.
    app = make_web_app(tmp_path, idp)
    register_ahead_of_the_mcp_mount(app, csrf_less_post_router())
    with pytest.raises(RuntimeError, match="CSRF_ENFORCED_IN_ROUTE"):
        async with app.router.lifespan_context(app):
            pass


async def test_a_route_added_after_the_yield_is_deliberately_not_rechecked(tmp_path, idp):
    # The uncovered half, asserted so the docstring and the code agree.
    #
    # Accepted because the actor is different from the session case. CSRF
    # defends against a cross-origin page, and a cross-origin page cannot
    # register a route; the only actor who can is code already running in this
    # process, which is not a deployment path. A request-time CSRF middleware
    # would close it by re-deriving on every mutating request what the
    # dependency already decided, against nobody.
    app = make_web_app(tmp_path, idp)
    async with app.router.lifespan_context(app):
        # Boot passed. Now do the thing the checks cannot see.
        register_ahead_of_the_mcp_mount(app, csrf_less_post_router())
        assert unprotected_web_routes(app.routes) == ["/web/forgot-csrf"]
    # Nothing raised on the way through: the checks had already run.
    #
    # The session half of the same route *is* still covered at runtime, which
    # is the asymmetry worth keeping straight — the guard authenticates by
    # path and never consults the route table.
    async with web_client(app) as client:
        refused = await client.post("/web/forgot-csrf")
        assert refused.status_code == 303
        assert refused.headers["location"].startswith("/web/signin?")


# --- MINOR: a declared exemption must keep describing its route -----------------


def test_an_exemption_whose_route_gained_the_dependency_fails_the_rollout(tmp_path, idp):
    # The rot the check exists for: a route that *gains* Depends(require_csrf)
    # leaves behind an entry claiming an in-route check that is no longer
    # there, and the registry stops describing reality with nothing failing.
    import collab_hub_api.web.surface as surface_module

    router = session_gated_router()

    @router.post("/web/now-declares-it")
    async def now_declares_it(session=Depends(require_csrf)):
        return {"ok": True}

    monkeypatch_path = "/web/now-declares-it"
    app = make_web_app(tmp_path, idp)
    register_ahead_of_the_mcp_mount(app, router)
    original = surface_module.CSRF_ENFORCED_IN_ROUTE
    surface_module.CSRF_ENFORCED_IN_ROUTE = original | {monkeypatch_path}
    try:
        assert stale_csrf_exemptions(app.routes) == [
            f"{monkeypatch_path} already declares Depends(require_csrf), so the exemption"
            " is redundant and now misdescribes the route"
        ]
        with pytest.raises(RuntimeError, match="no longer true"):
            verify_web_route_protection(app.routes)
    finally:
        surface_module.CSRF_ENFORCED_IN_ROUTE = original


def test_an_exemption_on_a_route_with_no_unsafe_method_fails_the_rollout(tmp_path, idp):
    import collab_hub_api.web.surface as surface_module

    app = make_web_app(tmp_path, idp)
    original = surface_module.CSRF_ENFORCED_IN_ROUTE
    # /web is a GET-only page; exempting it claims something untrue.
    surface_module.CSRF_ENFORCED_IN_ROUTE = original | {"/web"}
    try:
        assert stale_csrf_exemptions(app.routes) == [
            "/web answers no state-changing method, so it needs no exemption"
        ]
    finally:
        surface_module.CSRF_ENFORCED_IN_ROUTE = original


def test_an_exemption_naming_an_unmounted_path_is_tolerated(tmp_path, idp):
    # Deliberately not an error. make_app mounts the operator router only when
    # org_source_resolves_membership(), so on a claims-sourced deployment #91's
    # /admin routes are legitimately absent while its entries are correctly
    # present — and failing on absence would refuse every such deployment. It
    # also has to tolerate an entry landing one PR ahead of its route, which is
    # the arrangement this branch was in for /invite/accept/redeem.
    import collab_hub_api.web.surface as surface_module

    app = make_web_app(tmp_path, idp)
    original = surface_module.CSRF_ENFORCED_IN_ROUTE
    surface_module.CSRF_ENFORCED_IN_ROUTE = original | {"/admin/not-mounted-yet"}
    try:
        assert stale_csrf_exemptions(app.routes) == []
        verify_web_route_protection(app.routes)
    finally:
        surface_module.CSRF_ENFORCED_IN_ROUTE = original


def test_a_typo_is_not_silent_because_the_real_route_is_still_caught(tmp_path, idp):
    # Why tolerating an unmounted entry costs safety nothing: the misspelled
    # entry exempts nothing, so the route it was meant to cover is still
    # refused by the primary check, loudly, naming the real path.
    import collab_hub_api.web.surface as surface_module

    app = make_web_app(tmp_path, idp)
    register_ahead_of_the_mcp_mount(app, redeem_style_router("/web/needs-exemption"))
    original = surface_module.CSRF_ENFORCED_IN_ROUTE
    surface_module.CSRF_ENFORCED_IN_ROUTE = original | {"/web/needs-exemtion"}  # typo
    try:
        with pytest.raises(RuntimeError, match="/web/needs-exemption") as refusal:
            verify_web_route_protection(app.routes)
        assert "CSRF_ENFORCED_IN_ROUTE" in str(refusal.value)
    finally:
        surface_module.CSRF_ENFORCED_IN_ROUTE = original


def test_the_shipped_exemption_names_a_route_that_really_is_mounted(tmp_path, idp):
    # The registry as shipped, checked against the real app rather than by
    # reading: #90's redemption endpoint is mounted, on the surface, answers
    # POST, and does not declare the dependency — every condition that makes
    # the entry both necessary and accurate.
    from collab_hub_api.web.authz import route_enforces_csrf, route_unsafe_methods
    from collab_hub_api.web.surface import ACCEPT_REDEEM_PATH, CSRF_ENFORCED_IN_ROUTE

    app = make_web_app(tmp_path, idp)
    mounted = {
        route.path: route
        for route in app.routes
        if isinstance(route, APIRoute) and route.path in CSRF_ENFORCED_IN_ROUTE
    }
    assert set(mounted) == {ACCEPT_REDEEM_PATH}
    route = mounted[ACCEPT_REDEEM_PATH]
    assert on_web_surface(ACCEPT_REDEEM_PATH, WEB_SURFACE_PREFIXES)
    assert route_unsafe_methods(route) == {"POST"}
    assert not route_enforces_csrf(route)
    assert stale_csrf_exemptions(app.routes) == []


def test_the_shipped_admin_entries_lead_their_routes_without_failing(tmp_path, idp):
    # The tolerance for unmatched entries is now load-bearing rather than
    # hypothetical: two of the three shipped entries name routes #91 has not
    # landed, so a check that required every entry to be mounted would refuse
    # to start this very branch.
    #
    # It stays load-bearing after #91 too, for a different reason: make_app
    # mounts the operator router only under org_source_resolves_membership(), so on a
    # claims-sourced deployment those routes are absent while the entries are
    # correctly present.
    from collab_hub_api.web.surface import CSRF_ENFORCED_IN_ROUTE

    app = make_web_app(tmp_path, idp)
    mounted_paths = {
        route.path for route in app.routes if isinstance(route, APIRoute)
    }
    unmounted = {path for path in CSRF_ENFORCED_IN_ROUTE if path not in mounted_paths}
    assert unmounted == {
        "/admin/invitations",
        "/admin/invitations/revoke",
        # #142's owner page mounts under the same org_source_resolves_membership()
        # gate as #91's, so its entries are likewise correctly present while
        # a claims-sourced deployment serves neither route.
        "/web/org/invitations",
        "/web/org/invitations/revoke",
        "/web/org/invitations/name",
    }
    # Neither the registry check nor the route lint objects to them.
    assert stale_csrf_exemptions(app.routes) == []
    verify_web_route_protection(app.routes)


def test_the_admin_entries_will_cover_the_routes_when_they_arrive(tmp_path, idp):
    # And the entries are not merely tolerated — they do the job they are for.
    # Mount #91-shaped handlers at both paths, checking CSRF in-route the way
    # `_csrf_ok` does, and the lint that would otherwise refuse them passes.
    app = make_web_app(tmp_path, idp)
    for path in ("/admin/invitations", "/admin/invitations/revoke"):
        register_ahead_of_the_mcp_mount(app, redeem_style_router(path))
    assert unprotected_web_routes(app.routes) == []
    assert stale_csrf_exemptions(app.routes) == []
    verify_web_route_protection(app.routes)



# ---------------------------------------------------------------------------
# The wordmark on the server-rendered pages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_pages_carry_the_wordmark_and_are_allowed_to_show_it(tmp_path, idp):
    """A logo needs both an asset to fetch and a policy that permits fetching it.

    The surface shipped `img-src 'none'`, which is the right default for pages
    that had no images. Putting the product wordmark on them means widening
    that by exactly one source -- this origin -- and it is worth asserting both
    halves together: a page referencing an image its own policy forbids renders
    a broken mark and looks like a deploy fault.
    """

    app = make_web_app(tmp_path, idp)

    async with app.router.lifespan_context(app), web_client(app) as client:
        # A page that renders rather than redirecting into the OIDC flow.
        page = await client.get("/web/data-statement")
        logo = await client.get(WEB_LOGO_PATH)

    assert logo.status_code == 200
    assert logo.headers["content-type"].startswith("image/")
    assert WEB_LOGO_PATH in page.text

    csp = page.headers["content-security-policy"]
    assert "img-src 'self'" in csp
    assert "default-src 'none'" in csp
    assert "script-src" not in csp


def test_the_surface_stylesheet_shouts_at_nobody():
    """Nothing in this product is capitalised for emphasis."""

    from collab_hub_api.web.pages import STYLESHEET

    assert "text-transform: uppercase" not in STYLESHEET
