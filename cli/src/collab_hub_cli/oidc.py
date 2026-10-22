"""The OAuth side of signing in: discovery, authorization code with PKCE, refresh and sign-out.

The CLI signs in the way the Collab desktop does, against the same realm and
the same public client: the authorization code flow with PKCE (S256) and a
loopback redirect (RFC 8252). A listener on ``127.0.0.1`` and a port the
system picks receives the redirect at ``/callback``, the code is exchanged
with the verifier only this process holds, and ``offline_access`` asks the
realm for a refresh token. Every endpoint comes from the issuer's discovery
document. Signing out revokes the refresh token at the realm's
``revocation_endpoint`` (RFC 7009), and first ends the realm session through
its ``end_session_endpoint`` with that token, as the desktop does.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .config import UsageError, is_loopback

SCOPES = ("openid", "profile", "email", "offline_access")
LOGIN_TIMEOUT_SECONDS = 300

# Module-level so tests can move time.
clock: Callable[[], float] = time.time


class AuthError(Exception):
    """Not signed in, or the session cannot be used: exit code 5."""


class RealmError(Exception):
    """The realm could not be reached, failed, or cannot be trusted: exit code 1.

    Unlike :class:`AuthError` it says nothing about the session itself, so a
    session is never forgotten for it: the next command can try again.
    """


# RFC 6749 §5.2: the realm refused the grant itself, so the token is dead.
REFUSED_GRANT = frozenset({"invalid_grant", "invalid_client", "unauthorized_client"})


@dataclass(frozen=True)
class Tokens:
    access_token: str
    refresh_token: str | None
    expires_at: float | None


def _secure(url: str, insecure: bool = False) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" or (parsed.scheme == "http" and (insecure or is_loopback(parsed.hostname)))


def discover(http: httpx.Client, issuer: str, insecure: bool = False) -> dict:
    """The issuer's discovery document, checked before any of it is used.

    The issuer came from the hub, so the document must name that same issuer
    (OpenID Connect Discovery §4.3), and every endpoint the sign-in sends a
    code, a verifier or a token to must be ``https``, or loopback ``http``
    (any ``http`` with ``insecure``).
    """

    if not _secure(issuer, insecure):
        raise RealmError(f"refusing the issuer {issuer}: it is neither https nor on this machine")
    try:
        response = http.get(f"{issuer.rstrip('/')}/.well-known/openid-configuration")
    except httpx.HTTPError as exc:
        raise RealmError(f"cannot reach the issuer {issuer}: {exc}") from exc
    if response.status_code != 200:
        raise RealmError(f"the issuer {issuer} has no discovery document (HTTP {response.status_code})")
    try:
        metadata = response.json()
    except ValueError as exc:
        raise RealmError(f"the issuer {issuer} answered its discovery document with something not JSON") from exc
    if not isinstance(metadata, dict) or str(metadata.get("issuer", "")).rstrip("/") != issuer.rstrip("/"):
        found = metadata.get("issuer") if isinstance(metadata, dict) else None
        raise RealmError(f"the discovery document at {issuer} names another issuer ({found!r}); refusing it")
    for name in ("authorization_endpoint", "token_endpoint", "end_session_endpoint", "revocation_endpoint"):
        endpoint = metadata.get(name)
        if endpoint is not None and not _secure(str(endpoint), insecure):
            raise RealmError(f"refusing the realm's {name} {endpoint}: it is neither https nor on this machine")
    return metadata


def _post(http: httpx.Client, url: str, data: dict) -> httpx.Response:
    try:
        return http.post(url, data=data)
    except httpx.HTTPError as exc:
        raise RealmError(f"cannot reach the realm at {url}: {exc}") from exc


def session_id(token: str | None) -> str | None:
    """The realm session a token belongs to (its ``sid`` claim), read without verifying it."""

    try:
        payload = str(token).split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return None
    sid = claims.get("sid") if isinstance(claims, dict) else None
    return sid if isinstance(sid, str) and sid else None


def _token_body(response: httpx.Response, endpoint: str) -> dict:
    """The token endpoint's JSON answer; a realm error otherwise, never a traceback."""
    try:
        body = response.json()
    except ValueError:
        body = None
    token = body.get("access_token") if isinstance(body, dict) else None
    if not isinstance(token, str) or not token:
        raise RealmError(f"the realm's token endpoint {endpoint} answered without tokens "
                         f"({response.headers.get('content-type', 'no content type')})")
    refresh = body.get("refresh_token")
    if refresh is not None and (not isinstance(refresh, str) or not refresh):
        raise RealmError(f"the realm's token endpoint {endpoint} answered with a refresh_token that is not one")
    if body.get("expires_in") is not None and _seconds(body["expires_in"]) is None:
        raise RealmError(f"the realm's token endpoint {endpoint} answered with an expires_in that is not a "
                         "number of seconds")
    return body


def _seconds(value: object) -> float | None:
    """A token's lifetime in seconds, or ``None`` when it is not one.

    RFC 6749 sends ``expires_in`` as a number; some realms send a string of
    ASCII digits, which is accepted too, and nothing else is (no sign, no
    decimal point, no other script's digits). A value too large to be a float
    is refused rather than raised.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    if isinstance(value, str) and not (value.isascii() and value.isdigit()):
        return None
    try:
        seconds = float(int(value) if isinstance(value, str) else value)
    except (ValueError, OverflowError):
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _tokens(body: dict) -> Tokens:
    expires_in = body.get("expires_in")
    return Tokens(
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token"),
        expires_at=clock() + _seconds(expires_in) if expires_in is not None else None,
    )


def _error(response: httpx.Response) -> tuple[str, str]:
    try:
        body = response.json()
    except ValueError:
        return "http_error", f"HTTP {response.status_code}"
    return body.get("error", "http_error"), body.get("error_description") or ""


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")


_DONE_PAGE = (b"<!doctype html><meta charset=utf-8><title>collab-hub</title>"
              b"<p>collab-hub: %s You can close this tab.</p>")


class _Callback:
    """The one redirect the loopback listener waits for."""

    def __init__(self, state: str):
        self.state = state
        self.params: dict[str, str] | None = None
        self.arrived = threading.Event()

    def handler(self) -> type[BaseHTTPRequestHandler]:
        callback = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - the stdlib's name
                url = urlparse(self.path)
                if url.path != "/callback":
                    self.send_error(404)
                    return
                params = {key: values[0] for key, values in parse_qs(url.query).items()}
                if params.get("state") != callback.state:
                    # Not the redirect this sign-in started: never accept a code for another one.
                    self.send_error(400, "state mismatch")
                    return
                callback.params = params
                ok = "code" in params
                self.send_response(200 if ok else 400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(_DONE_PAGE % (b"signed in." if ok else b"the sign-in did not complete."))
                callback.arrived.set()

            def log_message(self, *_args) -> None:
                pass

        return Handler


def browser_login(
    http: httpx.Client,
    metadata: dict,
    client_id: str,
    open_browser: Callable[[str], object],
    show: Callable[[str], None],
    timeout: float = LOGIN_TIMEOUT_SECONDS,
    port: int = 0,
) -> Tokens:
    """Sign in through the browser: authorization code with PKCE, redirected to a loopback listener.

    The listener takes ``port`` on ``127.0.0.1``, or one the system picks when
    it is 0. A fixed port is what lets an SSH forward (``ssh -L port:127.0.0.1:port``)
    be set up before the sign-in starts, for a CLI on a remote host.
    """

    verifier, challenge = _pkce()
    callback = _Callback(secrets.token_urlsafe(32))
    try:
        server = HTTPServer(("127.0.0.1", port), callback.handler())
    except OSError as exc:
        raise UsageError(f"cannot listen on 127.0.0.1:{port} for the sign-in: {exc.strerror or exc}") from exc
    redirect_uri = f"http://127.0.0.1:{server.server_address[1]}/callback"
    url = f"{metadata['authorization_endpoint']}?" + urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(SCOPES),
        "state": callback.state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    try:
        show(url)
        open_browser(url)
        if not callback.arrived.wait(timeout):
            raise AuthError(f"no sign-in arrived within {int(timeout)} seconds; run `collab-hub login` again")
    finally:
        server.shutdown()
        server.server_close()

    params = callback.params or {}
    if "code" not in params:
        detail = params.get("error_description") or params.get("error") or "no code"
        raise AuthError(f"the realm did not sign you in: {detail}")
    response = _post(http, metadata["token_endpoint"], {
        "grant_type": "authorization_code",
        "code": params["code"],
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": verifier,
    })
    if response.status_code != 200:
        code, detail = _error(response)
        if response.status_code >= 500:
            raise RealmError(f"the realm failed to exchange the sign-in code: HTTP {response.status_code}")
        raise AuthError(f"the realm refused the sign-in code: {code} {detail}".rstrip())
    return _tokens(_token_body(response, metadata["token_endpoint"]))


def refresh(http: httpx.Client, metadata: dict, client_id: str, refresh_token: str) -> Tokens:
    response = _post(http, metadata["token_endpoint"], {
        "grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id,
    })
    if response.status_code != 200:
        code, _ = _error(response)
        if response.status_code in (400, 401) and code in REFUSED_GRANT:
            raise AuthError(f"the session could not be renewed ({code}); run `collab-hub login` again")
        # A realm restarting or a proxy error page: the session may be fine, so keep it.
        raise RealmError(f"the realm could not renew the session right now (HTTP {response.status_code}, {code}); "
                         "try again")
    tokens = _tokens(_token_body(response, metadata["token_endpoint"]))
    # A realm that does not rotate refresh tokens omits the new one: keep the old.
    return Tokens(tokens.access_token, tokens.refresh_token or refresh_token, tokens.expires_at)


def sign_out(http: httpx.Client, metadata: dict, client_id: str, refresh_token: str) -> None:
    """Make a refresh token unusable at the realm, and end the realm session behind it.

    Revocation (RFC 7009) is what guarantees the token never renews again, so
    it is required. Ending the session first, through the end-session
    endpoint with the refresh token, is how the desktop signs out of
    Keycloak; it is done when the realm offers it, and a token it already
    ended is still accepted by revocation, which answers 200 for a token that
    is no longer valid.
    """

    revocation = metadata.get("revocation_endpoint")
    if not revocation:
        raise RealmError("the realm publishes no revocation endpoint, so the session cannot be revoked")
    ended = metadata.get("end_session_endpoint")
    if ended:
        response = _post(http, ended, {"client_id": client_id, "refresh_token": refresh_token})
        if response.status_code not in (200, 204):
            code, detail = _error(response)
            raise RealmError(f"the realm did not end the session: {code} {detail}".rstrip())
    response = _post(http, revocation, {
        "client_id": client_id, "token": refresh_token, "token_type_hint": "refresh_token",
    })
    if response.status_code != 200:
        code, detail = _error(response)
        raise RealmError(f"the realm did not revoke the refresh token: {code} {detail}".rstrip())
