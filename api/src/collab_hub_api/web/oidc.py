"""OIDC authorization-code flow against the deployment's confidential web client.

The desktop app signs in with PKCE as the public ``apollo-desktop`` client and
holds bearer tokens; a server-rendered page must not hand tokens to a browser,
so the web surface runs the code flow itself with its own **confidential**
client (``collab-web`` or similar) in the same realm, exchanges the code
server-side with the client secret, verifies the resulting ID token, and then
issues its own session cookie (``web.session``). PKCE is used *in addition to*
the client secret — it costs one hash and removes the authorization-code
injection class entirely.

Audience verification is not optional and is never inherited (issue #83's
lesson, restated as a rule): the ID token's ``aud`` must contain **this**
client's id, and when the token names an authorizing party it must be this
client too. A validly signed same-realm token minted for another client —
notably a desktop token, which shares issuer and keys — must never mint a web
session. ``verify_id_token`` therefore has no "no audience configured" path at
all: the audience argument is the client id, which is the switch that enables
the whole surface.
"""

from __future__ import annotations

import base64
import secrets
from hashlib import sha256
from urllib.parse import urlencode, urlsplit

import httpx

from ..frames.auth import TokenDecodeError, decode_verified_jwt

AUTHORIZE_ENDPOINT = "/protocol/openid-connect/auth"
TOKEN_ENDPOINT = "/protocol/openid-connect/token"
JWKS_ENDPOINT = "/protocol/openid-connect/certs"

TOKEN_REQUEST_TIMEOUT_SECONDS = 10.0
"""Socket timeout for the code exchange. Sign-in is interactive: a person is
watching, and a hung IdP should fail their sign-in rather than hold a worker."""


class OidcFlowError(Exception):
    """A sign-in attempt that cannot proceed.

    The message is written for a log line, not a page: routes render a fixed
    failure page and never echo this text to the browser, so no value derived
    from the IdP response or the request can reach the document.
    """


LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})
"""Hosts for which plain ``http://`` is permitted.

Exact matches only, and deliberately not a suffix rule: ``localhost.evil.com``
and ``127.0.0.1.evil.com`` are ordinary internet hosts that a suffix or
substring test would wave through.
"""


def _is_loopback(netloc: str) -> bool:
    host = netloc.rsplit(":", 1)[0] if _has_port(netloc) else netloc
    return host.lower() in LOOPBACK_HOSTS


def _has_port(netloc: str) -> bool:
    # "[::1]:8080" has a port; "[::1]" does not, and its colons are the
    # address. Outside brackets, a single trailing ":digits" is the port.
    tail = netloc.rsplit("]", 1)[-1]
    return ":" in tail


def _invalid_port_reason(url: str) -> str | None:
    """Reject a malformed port at startup rather than at first sign-in.

    ``urlsplit`` parses ``http://localhost:evil`` happily and only raises when
    ``.port`` is *read* — which nothing on the startup path did, so the
    loopback check saw a host of ``localhost`` and passed it. The failure then
    surfaced as an unexplained sign-in error long after deploy.
    """

    try:
        urlsplit(url).port
    except ValueError:
        return "its port is not a number"
    return None


def insecure_transport_reason(url: str) -> str | None:
    """Why ``url`` may not be reached over plain http, or ``None`` if it may.

    The confidential client secret is POSTed to the token endpoint derived
    from this URL, and the JWKS that verifies every ID token is fetched from
    it. Over plain http a network attacker reads the secret and the code, and
    can substitute the JWKS wholesale — which forges any identity it likes.
    So ``http://`` is confined to loopback development, where there is no
    network to be on, and everything else must be ``https://``.
    """

    parts = urlsplit(url)
    if parts.scheme != "http":
        return None
    if _is_loopback(parts.netloc):
        return None
    return (
        "it uses plain http:// to a non-loopback host, which would expose the"
        " confidential client secret and allow a forged JWKS"
    )


def invalid_realm_url_reason(url: str) -> str | None:
    """Why ``url`` cannot serve as a Keycloak realm URL, or ``None`` if it can.

    The three OIDC endpoints are derived by appending paths to this value, and
    concatenation onto a URL that carries a query, fragment, or path
    parameters silently produces data rather than a path — a deployment that
    starts and then dead-ends every sign-in. The shapes that concatenate badly
    are rejected here, at startup, instead. Raw delimiters are checked (not
    just parsed components) because a trailing ``?`` parses as an *empty*
    query and would pass a parsed-only check while still swallowing the
    appended endpoint path.

    Transport is part of "usable": see :func:`insecure_transport_reason`.
    """

    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return "it must start with http:// or https://"
    if not parts.netloc:
        return "it names no host"
    if "@" in parts.netloc:
        return "it must not embed credentials"
    for delimiter, label in (("?", "query string"), ("#", "fragment"), (";", "path parameters")):
        if delimiter in url:
            return f"it must not contain {delimiter!r} ({label})"
    port_reason = _invalid_port_reason(url)
    if port_reason:
        return port_reason
    return insecure_transport_reason(url)


def realm_endpoints(issuer_url: str) -> tuple[str, str, str]:
    """The (authorize, token, jwks) endpoint URLs for a validated realm URL."""

    realm = issuer_url.rstrip("/")
    return (
        f"{realm}{AUTHORIZE_ENDPOINT}",
        f"{realm}{TOKEN_ENDPOINT}",
        f"{realm}{JWKS_ENDPOINT}",
    )


def generate_state() -> str:
    return secrets.token_urlsafe(32)


def generate_nonce() -> str:
    return secrets.token_urlsafe(32)


def generate_pkce_verifier() -> str:
    # 48 bytes -> 64 URL-safe characters, inside RFC 7636's 43..128 bounds.
    return secrets.token_urlsafe(48)


def pkce_challenge(verifier: str) -> str:
    digest = sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def authorization_url(
    *,
    authorize_endpoint: str,
    client_id: str,
    redirect_uri: str,
    scope: str,
    state: str,
    nonce: str,
    code_challenge: str,
    prompt: str | None = None,
) -> str:
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    # `prompt` steers which IdP screen the flow starts on — `create` lands on
    # the registration form (verified against the deployed Keycloak 26.5).
    # Every response still comes back through the same code exchange, so the
    # parameter changes the first screen and nothing about the protocol.
    if prompt is not None:
        params["prompt"] = prompt
    return f"{authorize_endpoint}?{urlencode(params)}"


async def exchange_code(
    *,
    token_endpoint: str,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
) -> dict:
    """Redeem an authorization code for the token response, or raise.

    The client secret rides the POST body (``client_secret_post``), which is
    what Keycloak's "Client Id and Secret" authenticator expects. The response
    body of a failed exchange is summarized by status code only — it can
    contain attacker-influenced values and must not be echoed or logged
    verbatim.
    """

    async with httpx.AsyncClient(timeout=TOKEN_REQUEST_TIMEOUT_SECONDS) as client:
        try:
            response = await client.post(
                token_endpoint,
                data={
                    "grant_type": "authorization_code",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "code_verifier": code_verifier,
                },
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise OidcFlowError(f"token endpoint unreachable: {type(exc).__name__}") from exc
    if response.status_code != 200:
        raise OidcFlowError(f"token endpoint answered {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise OidcFlowError("token endpoint returned a non-JSON body") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("id_token"), str):
        raise OidcFlowError("token response carried no id_token")
    return payload


def verify_id_token(
    id_token: str,
    *,
    jwks_url: str,
    issuer: str,
    client_id: str,
    nonce: str,
) -> dict:
    """Verify the ID token and return its claims, or raise :class:`OidcFlowError`.

    Signature, issuer, and audience are enforced by the shared verifier in
    ``frames.auth`` (same JWKS caching and rotation behavior as the bearer
    path). On top of that, three checks specific to this flow:

    * ``aud`` **is this client** — passed as a required argument, so there is
      no configuration state in which the check is skipped (issue #83).
    * ``azp`` must be this client whenever ``aud`` names more than one
      audience, and must never contradict it when present. OIDC Core 3.1.3.7
      requires ``azp`` on a multi-audience ID token, so treating its *absence*
      there as acceptable would admit exactly the token this check exists to
      refuse: one minted for the desktop client with our id merely listed
      alongside. Absent ``azp`` with a single ``aud`` equal to this client is
      the ordinary case and is fine.
    * ``typ`` must be ``ID``. The surface is Keycloak-specific by
      construction (the endpoints are derived as ``/protocol/openid-connect/*``),
      and Keycloak stamps this on every ID token, so requiring it — rather
      than only rejecting a contradicting value — keeps any *other* same-realm
      JWT that happens to carry our audience from minting a session.
    * ``nonce`` must equal the value this flow generated, binding the token to
      the browser that started the sign-in.
    """

    try:
        claims = decode_verified_jwt(
            id_token,
            jwks_url=jwks_url,
            issuer=issuer,
            audience=client_id,
        )
    except TokenDecodeError as exc:
        raise OidcFlowError("id_token failed verification") from exc
    audience = claims.get("aud")
    multi_audience = isinstance(audience, (list, tuple)) and len(audience) > 1
    authorized_party = claims.get("azp")
    if authorized_party is None:
        if multi_audience:
            raise OidcFlowError("multi-audience id_token carries no azp")
    elif authorized_party != client_id:
        raise OidcFlowError("id_token was issued for a different client (azp mismatch)")
    if claims.get("typ") != "ID":
        raise OidcFlowError("token is not an ID token")
    token_nonce = claims.get("nonce")
    # Bytes, not str: compare_digest raises TypeError on non-ASCII strings,
    # and this value arrives inside a token — a failed comparison must be a
    # refusal, never an exception.
    if not isinstance(token_nonce, str) or not secrets.compare_digest(token_nonce.encode(), nonce.encode()):
        raise OidcFlowError("id_token nonce mismatch")
    return claims
