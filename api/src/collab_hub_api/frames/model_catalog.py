"""Reading the model catalogue from the serving layer.

The catalogue is the serving pack's, and this reads it over its OpenAI-shaped
``GET /v1/models``. Nothing here writes a model into this database. That is the
whole design: a copy would drift, and a panel showing a model the gateway will
not serve (or hiding one it will) is worse than a panel that is occasionally
unavailable.

Which is why an unreachable endpoint raises rather than returning an empty
list. "This hub serves no models" and "I could not ask" are different answers,
and only one of them should make an operator start deleting things.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

__all__ = ["CatalogModel", "ModelCatalogClient", "ModelCatalogError"]

DEFAULT_TIMEOUT_SECONDS = 5.0
"""Short on purpose: this call sits between an operator and a page render, and
a serving layer that is slow to answer should degrade the models section rather
than hang the panel.
"""


class ModelCatalogError(RuntimeError):
    """The catalogue could not be read: unreachable, refused, or malformed."""


@dataclass(frozen=True)
class CatalogModel:
    id: str
    owned_by: str | None


class ModelCatalogClient:
    """Reads ``GET /v1/models`` from the serving layer.

    No credential. The catalogue is the list of models this hub offers, which
    every signed-in person can already discover by using the product; what is
    protected is *access* to each model, and that is enforced at the serving
    gateway rather than by hiding names here.
    """

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout_seconds, transport=transport) if base_url else None

    @property
    def configured(self) -> bool:
        return self._client is not None

    def list_models(self) -> list[CatalogModel]:
        if self._client is None:
            raise ModelCatalogError("this deployment has no model catalogue endpoint configured")
        try:
            response = self._client.get(f"{self._base_url}/v1/models")
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise ModelCatalogError(
                f"the serving layer refused the catalogue read: {exc.response.status_code}"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ModelCatalogError("the serving layer could not be reached") from exc

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            # Half-reading a payload of the wrong shape would show an operator
            # a catalogue that is missing entries without saying so.
            raise ModelCatalogError("the serving layer returned an unrecognized catalogue shape")

        models = []
        for entry in data:
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                raise ModelCatalogError("the serving layer returned a model with no id")
            owned_by = entry.get("owned_by")
            models.append(CatalogModel(id=entry["id"], owned_by=owned_by if isinstance(owned_by, str) else None))
        return models

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
