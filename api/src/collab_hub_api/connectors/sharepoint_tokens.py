from __future__ import annotations

import logging
from urllib.parse import parse_qs

import httpx
from fastapi import Request

from collab_hub_api.config import SharePointConnectorConfig
from collab_hub_api.connectors.google_tokens import (
    ConnectorNotConnected,
    ConnectorPermissionError,
    ConnectorReconnectRequired,
    ConnectorTokenError,
)
from collab_hub_api.frames.auth import get_bearer_token

logger = logging.getLogger("frames_server.connectors")


class SharePointTokenProvider:
    """Resolve a per-user Microsoft Graph access token.

    Microsoft Entra ID is a real OIDC provider, so the token is brokered through
    Keycloak's built-in ``microsoft`` identity provider (this class is
    near-identical to ``GitHubTokenProvider``). Graph access tokens live about
    an hour; Keycloak holds the refresh token (``offline_access``) and renews
    the stored token at the broker endpoint, so the Hub only ever reads. See
    docs/sharepoint-connector.md.
    """

    def __init__(self, config: SharePointConnectorConfig):
        self.config = config

    async def access_token(self, request: Request) -> str:
        if self.config.static_access_token:
            return self.config.static_access_token  # dev/CI escape hatch

        if not self.config.broker_token_url:
            raise ConnectorNotConnected("SharePoint connector token broker is not configured")

        hub_token = get_bearer_token(request)
        if not hub_token:
            raise ConnectorReconnectRequired("Hub bearer token is required for SharePoint connector token brokering")

        timeout = httpx.Timeout(self.config.request_timeout_seconds)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(
                    self.config.broker_token_url,
                    headers={"Authorization": f"Bearer {hub_token}", "Accept": "application/json"},
                )
        except httpx.HTTPError as exc:
            raise ConnectorTokenError("SharePoint token broker request failed") from exc

        logger.info("sharepoint broker token response status=%s", response.status_code)

        if response.status_code == 404:
            raise ConnectorNotConnected("Microsoft account is not linked")
        if response.status_code == 403:
            raise ConnectorPermissionError(
                "Keycloak denied broker token access. Grant the broker read-token role to normal Hub users."
            )
        if response.status_code in {400, 401}:
            raise ConnectorReconnectRequired("Microsoft account must be reconnected")
        if response.status_code >= 400:
            raise ConnectorTokenError("SharePoint token broker request failed")

        token = _extract_access_token(response)
        if not token:
            raise ConnectorReconnectRequired("SharePoint token broker did not return an access token")
        return token


def _extract_access_token(response: httpx.Response) -> str:
    """Pull the Graph access token from a Keycloak broker token response.

    Keycloak's Microsoft provider stores Entra's JSON token response
    (``access_token``/``refresh_token``/``expires_in``/``id_token``) and brokers
    it back as JSON. Accept the form-urlencoded shape too, like the GitHub
    provider does, so a Keycloak that re-serializes the stored token cannot
    strand the connector. Only ``access_token`` is read here.
    """
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        token = payload.get("access_token") or payload.get("token")
        if isinstance(token, str) and token:
            return token
    values = parse_qs(response.text or "").get("access_token")
    if values and isinstance(values[0], str) and values[0]:
        return values[0]
    return ""
