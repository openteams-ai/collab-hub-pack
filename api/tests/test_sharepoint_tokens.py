"""Unit tests for SharePointTokenProvider.

The connector's route tests exercise the static-token dev hatch, which
short-circuits the Keycloak broker path. These tests drive the provider
directly so the broker call and every error mapping are covered.
"""

from __future__ import annotations

import httpx
import pytest
from starlette.requests import Request

from collab_hub_api.config import SharePointConnectorConfig
from collab_hub_api.connectors.google_tokens import (
    ConnectorNotConnected,
    ConnectorPermissionError,
    ConnectorReconnectRequired,
    ConnectorTokenError,
)
from collab_hub_api.connectors.sharepoint_tokens import (
    SharePointTokenProvider,
    _extract_access_token,
)

BROKER = "https://keycloak.test/realms/nebari/broker/microsoft/token"


def _request(authorization: str | None = None) -> Request:
    headers = []
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    return Request({"type": "http", "headers": headers})


def _mock_httpx(monkeypatch, handler) -> None:
    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


async def test_static_token_short_circuits_broker(monkeypatch):
    def handler(request):  # pragma: no cover - must never be called
        raise AssertionError("static token must not call the broker")

    _mock_httpx(monkeypatch, handler)
    provider = SharePointTokenProvider(
        SharePointConnectorConfig(static_access_token="eyJ-static", broker_token_url=BROKER)
    )
    assert await provider.access_token(_request("Bearer hub")) == "eyJ-static"


async def test_empty_broker_url_is_not_connected():
    provider = SharePointTokenProvider(SharePointConnectorConfig(broker_token_url=""))
    with pytest.raises(ConnectorNotConnected):
        await provider.access_token(_request("Bearer hub"))


async def test_missing_hub_bearer_is_reconnect_required():
    provider = SharePointTokenProvider(SharePointConnectorConfig(broker_token_url=BROKER))
    with pytest.raises(ConnectorReconnectRequired):
        await provider.access_token(_request(None))


async def test_broker_success_returns_graph_token(monkeypatch):
    def handler(request):
        assert request.headers["authorization"] == "Bearer hub"
        assert request.url == httpx.URL(BROKER)
        # Keycloak brokers Entra's stored token response back as JSON.
        return httpx.Response(
            200,
            json={"access_token": "eyJ-brokered", "refresh_token": "rt", "expires_in": 3599, "id_token": "eyJ-id"},
        )

    _mock_httpx(monkeypatch, handler)
    provider = SharePointTokenProvider(SharePointConnectorConfig(broker_token_url=BROKER))
    assert await provider.access_token(_request("Bearer hub")) == "eyJ-brokered"


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (404, ConnectorNotConnected),
        (403, ConnectorPermissionError),
        (400, ConnectorReconnectRequired),
        (401, ConnectorReconnectRequired),
        (500, ConnectorTokenError),
    ],
)
async def test_broker_error_status_maps_to_exception(monkeypatch, status_code, expected):
    _mock_httpx(monkeypatch, lambda request: httpx.Response(status_code, json={}))
    provider = SharePointTokenProvider(SharePointConnectorConfig(broker_token_url=BROKER))
    with pytest.raises(expected):
        await provider.access_token(_request("Bearer hub"))


async def test_broker_transport_error_is_token_error(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("broker down", request=request)

    _mock_httpx(monkeypatch, handler)
    provider = SharePointTokenProvider(SharePointConnectorConfig(broker_token_url=BROKER))
    with pytest.raises(ConnectorTokenError):
        await provider.access_token(_request("Bearer hub"))


async def test_broker_success_without_token_is_reconnect_required(monkeypatch):
    _mock_httpx(monkeypatch, lambda request: httpx.Response(200, json={"not_a_token": "x"}))
    provider = SharePointTokenProvider(SharePointConnectorConfig(broker_token_url=BROKER))
    with pytest.raises(ConnectorReconnectRequired):
        await provider.access_token(_request("Bearer hub"))


def test_extract_access_token_variants():
    assert _extract_access_token(httpx.Response(200, json={"access_token": "a"})) == "a"
    # Falls back to a bare ``token`` key.
    assert _extract_access_token(httpx.Response(200, json={"token": "b"})) == "b"
    # Form-urlencoded, in case a broker re-serializes the stored token that way.
    assert _extract_access_token(httpx.Response(200, content=b"access_token=c&token_type=bearer")) == "c"
    # Present but empty / wrong type -> no token.
    assert _extract_access_token(httpx.Response(200, json={"access_token": ""})) == ""
    assert _extract_access_token(httpx.Response(200, json={"access_token": 123})) == ""
    # Non-dict JSON and non-JSON bodies -> no token.
    assert _extract_access_token(httpx.Response(200, json=["not", "a", "dict"])) == ""
    assert _extract_access_token(httpx.Response(200, content=b"not json")) == ""
