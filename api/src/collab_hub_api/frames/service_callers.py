"""Authenticating add-on services, for the membership API (issue #270).

Everything else in this API authenticates *people*: a browser's IdToken cookie
or a native client's bearer token, resolved to a principal and, through
membership, to an organization. The membership API's callers are not people.
They are add-on services (for example usage control and a subscription
manager) that read who belongs to which organization, and each holds its own
identity-provider client and signs in with the client-credentials grant.

So this is a separate, narrower check, and it shares only the token
*verification* with the people path -- the same JWKS client, issuer and
algorithms (:func:`~.auth.decode_verified_jwt`). What it accepts:

- an ``Authorization: Bearer`` access token (``typ`` ``Bearer``; an ID token
  is refused) with an expiry, signed by the realm the hub trusts;
- whose ``azp`` is one of the configured add-on client ids, **and** which
  carries the same id in Keycloak's ``client_id`` claim (``clientId`` before
  Keycloak 25). Only the client-credentials grant sets that claim, so a person
  signing in through an add-on's client -- whose token has the same ``azp`` --
  is not taken for the service. ``azp`` alone is never enough;
- on a request carrying **no cookies**. The endpoints are for services, which
  send none; a request with cookies is a browser, and a browser session is
  never a way in here, whatever else the request carries.

Nothing here reads a forwarded user header, resolves a membership or records
the caller as a user: a service caller has no organization and is not a
person.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass

from fastapi import HTTPException, Request, status

from .auth import (
    TokenDecodeError,
    decode_jwt_payload,
    decode_verified_jwt,
    get_bearer_token,
    unsafe_auth_enabled,
)

logger = logging.getLogger("frames_server.service_callers")

ACCESS_TOKEN_TYPE = "Bearer"
"""Keycloak's ``typ`` for an access token. ID tokens say ``ID``, refresh tokens ``Refresh``."""

CLIENT_ID_CLAIMS = ("client_id", "clientId")
"""Where Keycloak puts the client id on a client-credentials token: ``client_id``
since Keycloak 25 (the ``service_account`` scope), ``clientId`` before."""

_CHALLENGE = {"WWW-Authenticate": "Bearer"}


@dataclass(frozen=True)
class ServiceCaller:
    """An authenticated add-on service: its client id, and nothing else."""

    client_id: str


def unsigned_bearer_tokens_allowed() -> bool:
    """The local-development door the people path uses for unsigned bearer tokens, read the same way."""

    return unsafe_auth_enabled() and os.environ.get("FRAMES_BEARER_ALLOW_UNSIGNED") == "true"


def _token_client_id(claims: dict) -> object:
    # First claim present wins, as usage control reads it: a token carrying
    # both spellings is judged on the current one.
    for name in CLIENT_ID_CLAIMS:
        if name in claims:
            return claims[name]
    return None


class ServiceCallerAuthenticator:
    """Accepts exactly the configured add-on clients' client-credentials tokens."""

    def __init__(self, client_ids: Sequence[str], *, audience: str | None = None) -> None:
        self.client_ids = frozenset(client_ids)
        self.audience = audience or None

    def _decode(self, token: str) -> dict:
        if unsigned_bearer_tokens_allowed():
            return decode_jwt_payload(token)
        return decode_verified_jwt(
            token,
            jwks_url=os.environ.get("FRAMES_BEARER_JWKS_URL"),
            issuer=os.environ.get("FRAMES_BEARER_ISSUER"),
            audience=self.audience,
        )

    def authenticate(self, request: Request) -> ServiceCaller:
        if request.cookies or request.headers.get("cookie"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This endpoint is for add-on services: browser sessions and cookies are refused",
            )
        token = get_bearer_token(request)
        if token is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="A service bearer token is required",
                headers=_CHALLENGE,
            )
        try:
            claims = self._decode(token)
        except TokenDecodeError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid bearer token",
                headers=_CHALLENGE,
            ) from None
        expiry = claims.get("exp")
        if claims.get("typ") != ACCESS_TOKEN_TYPE or not isinstance(expiry, int | float) or isinstance(expiry, bool):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid bearer token: an access token with an expiry is required",
                headers=_CHALLENGE,
            )
        azp = claims.get("azp")
        if isinstance(azp, str) and azp in self.client_ids and _token_client_id(claims) == azp:
            return ServiceCaller(client_id=azp)
        # Logged with the client it named (never the token) so an operator can
        # tell a misconfigured add-on from a person poking at the endpoint.
        logger.warning(
            "membership_api_caller_refused",
            extra={"azp": azp if isinstance(azp, str) else None, "path": request.url.path},
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This token is not a client-credentials token of an add-on service allowed to read membership",
        )


def get_service_caller(request: Request) -> ServiceCaller:
    """FastAPI dependency: the add-on service calling, or a 401/403."""

    authenticator: ServiceCallerAuthenticator = request.app.state.membership_api_authenticator
    return authenticator.authenticate(request)
