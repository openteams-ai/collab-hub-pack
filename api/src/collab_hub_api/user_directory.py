from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

import httpx
from pydantic import BaseModel

from .frames.invitations import ascii_folded_bytes


class UserDirectoryUnavailableError(RuntimeError):
    """Raised when the configured user directory cannot serve a request."""


class UserDirectoryUser(BaseModel):
    id: str
    username: str
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    enabled: bool = True


class UserDirectoryGroup(BaseModel):
    id: str
    name: str
    path: str | None = None


def folded_email(value: str) -> str:
    """An address in the form a stored value and Gate B both use.

    Imported rather than re-implemented: ``ascii_folded_bytes`` is the single
    definition of this rule (#157), and folding case in two places is how the
    two spellings drift apart. ASCII-only, so the Kelvin sign is not "k" and
    the Turkish dotless i keeps its own identity.
    """

    return ascii_folded_bytes(value).decode("utf-8", "surrogatepass")


class AmbiguousAccountError(UserDirectoryUnavailableError):
    """One address resolved to more than one account.

    Its own type rather than a plain directory failure (raised by the review of
    #179): the two need different responses. An outage is retried; two accounts
    for one address is a realm that needs a human, and retrying never improves
    it. Subclasses the unavailable error so existing handlers keep working,
    while a caller that wants to distinguish them now can.
    """


class ProvisionedAccountRecord(BaseModel):
    """What reconciliation needs to know about one address, and nothing else.

    Deliberately **not** :class:`UserDirectoryUser`. That model is the
    ``GET /v1/user-directory/users`` response shape, so widening it to carry
    provisioning markers would put internal bookkeeping on a public endpoint.
    This one is internal and carries only what classifies a retry: the account
    id, whether the address is verified, and the two markers.

    Nothing here is a name, a group, or a role. A reconciliation path should not
    be able to enumerate the directory, and each field it does not carry is a
    field a future caller cannot come to depend on.
    """

    id: str
    email: str
    email_verified: bool = False
    provisioned_for: str | None = None
    """The invitation this account was created for (#172), or ``None`` for an
    account this workflow did not create. ``None`` is the load-bearing value:
    it means *do not write to this account*, because it belongs to somebody who
    arrived another way."""

    provisioning_complete: str | None = None
    """Set once the invitee has a usable setup path. Also the predicate for *do
    not touch*: cleanup removes a completed account from the staging group, and
    the provisioning credential's authority is evaluated against current group
    membership -- so a write attempted after completion is refused. Classify on
    this **before** looking at which invitation the marker names."""


PROVISIONED_FOR_ATTRIBUTE = "collab.provisioned-for"
PROVISIONING_COMPLETE_ATTRIBUTE = "collab.provisioning-complete"
"""The attribute names, spelled once.

**These must be declared in the realm's user profile** or Keycloak discards
them silently: with `unmanagedAttributePolicy` unset, an undeclared attribute in
a create payload returns 201 and stores nothing. Declared administrator-only,
with no protocol mapper, so neither an ordinary user nor a token ever sees them
(an internal issue owns that declaration).
"""


class UserDirectoryClient(Protocol):
    def search_users(self, query: str | None = None, *, limit: int = 50, first: int = 0) -> list[UserDirectoryUser]: ...

    def search_groups(self, query: str | None = None, *, limit: int = 50) -> list[UserDirectoryGroup]: ...

    def find_provisioned_account(self, email: str) -> ProvisionedAccountRecord | None: ...

    def close(self) -> None: ...


class DisabledUserDirectoryClient:
    def search_users(self, query: str | None = None, *, limit: int = 50, first: int = 0) -> list[UserDirectoryUser]:
        raise UserDirectoryUnavailableError("User directory is not configured")

    def search_groups(self, query: str | None = None, *, limit: int = 50) -> list[UserDirectoryGroup]:
        raise UserDirectoryUnavailableError("User directory is not configured")

    def find_provisioned_account(self, email: str) -> ProvisionedAccountRecord | None:
        raise UserDirectoryUnavailableError("User directory is not configured")

    def close(self) -> None:
        return None


@dataclass
class _AccessToken:
    value: str
    expires_at: float


class KeycloakUserDirectoryClient:
    def __init__(
        self,
        *,
        token_url: str,
        admin_api_base_url: str,
        client_id: str,
        client_secret: str,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.token_url = token_url.rstrip("/")
        self.admin_api_base_url = admin_api_base_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self._client = httpx.Client(timeout=timeout, transport=transport)
        self._token: _AccessToken | None = None

    def search_users(self, query: str | None = None, *, limit: int = 50, first: int = 0) -> list[UserDirectoryUser]:
        """Up to *limit* matching users, starting *first* results in."""

        params: dict[str, str | int | bool] = {"first": first, "max": limit}
        if query:
            params["search"] = query
        payload = self._admin_get("/users", params=params)
        if not isinstance(payload, list):
            raise UserDirectoryUnavailableError("Keycloak users response was not a list")
        return [_user_from_keycloak(item) for item in payload if isinstance(item, dict)]

    def list_user_records_page(self, *, first: int = 0, limit: int = 100) -> list[dict]:
        """Return one offset page of the realm's users as raw Keycloak records.

        ``search_users`` answers the member picker's question ("who matches what
        this person typed"), which is always a bounded top-N. Enumerating the
        *whole* realm is a different question, asked only by the read-only
        identity inventory (issue #65), and it needs the offset Keycloak calls
        ``first``. Same ``GET /users`` endpoint and the same read-only
        ``query-users``/``view-users`` the client already holds — this adds no
        permission, only pagination.

        It returns the untouched representation rather than
        :class:`UserDirectoryUser` because the inventory needs fields the API
        model deliberately does not carry — notably ``createdTimestamp``, which
        is how a mapping can be checked against the possibility that an
        address was reassigned to a newer account. ``UserDirectoryUser`` is the
        ``GET /v1/user-directory/users`` response model, and widening a public
        response shape is not this issue's business.
        """

        params: dict[str, str | int | bool] = {"first": first, "max": limit}
        payload = self._admin_get("/users", params=params)
        if not isinstance(payload, list):
            raise UserDirectoryUnavailableError("Keycloak users response was not a list")
        return [item for item in payload if isinstance(item, dict)]

    def search_groups(self, query: str | None = None, *, limit: int = 50) -> list[UserDirectoryGroup]:
        params: dict[str, str | int | bool] = {"max": limit, "briefRepresentation": True}
        if query:
            params["search"] = query
        payload = self._admin_get("/groups", params=params)
        if not isinstance(payload, list):
            raise UserDirectoryUnavailableError("Keycloak groups response was not a list")
        return [_group_from_keycloak(item) for item in payload if isinstance(item, dict)]

    def find_provisioned_account(self, email: str) -> ProvisionedAccountRecord | None:
        """One account by **exact** address, with its provisioning markers.

        ``search_users`` cannot answer this. It passes Keycloak's ``search``
        parameter, which is an infix match across username, email, first and
        last name, and :func:`_user_from_keycloak` drops attributes entirely --
        so a fuzzy hit without the markers cannot classify a retry, and a fuzzy
        hit is not evidence of identity in the first place.

        ``exact=true`` with ``email`` is the whole point: it either names this
        address or it names nothing. Keycloak still answers with a list, and a
        realm holding two accounts for one address is a state this code refuses
        to guess about -- writing to the wrong one of a pair is worse than
        failing loudly.

        Reads only. Same ``view-users`` the client already holds, no new
        permission, and the provisioning credential is *not* used here -- it
        cannot read users at all, which is what keeps the read and write
        authorities mechanically separate rather than separate by convention.
        """

        # Folded before querying, and kept even though it turned out not to be
        # required. Measured on Keycloak 26.5 (2026-08-21): `exact=true` on
        # `email` matches **case-insensitively** -- `SingleFlow@Taozend.COM`
        # returns the account stored as `singleflow@taozend.com`. So a
        # differently-cased retry would have found its account regardless.
        #
        # It stays because the reason to fold does not depend on that: this is
        # the form Gate B matches and the provisioning claim keys on (#157), and
        # one address should have one spelling across every comparison in the
        # invitation path. Relying on the provider's collation instead would
        # make our matching a property of somebody else's default.
        payload = self._admin_get("/users", params={"email": folded_email(email), "exact": True, "max": 2})
        if not isinstance(payload, list):
            raise UserDirectoryUnavailableError("Keycloak users response was not a list")
        records = [item for item in payload if isinstance(item, dict)]
        if not records:
            return None
        if len(records) > 1:
            raise AmbiguousAccountError(
                f"Keycloak returned {len(records)} accounts for one exact address; refusing to choose"
            )
        return _provisioned_account_from_keycloak(records[0])

    def close(self) -> None:
        self._client.close()

    def _admin_get(self, path: str, *, params: dict[str, str | int | bool]) -> object:
        try:
            response = self._admin_get_once(path, params=params)
            if response.status_code == 401:
                self._token = None
                response = self._admin_get_once(path, params=params)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            raise UserDirectoryUnavailableError(
                f"Keycloak user directory request failed with HTTP {status_code}"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise UserDirectoryUnavailableError("Keycloak user directory request failed") from exc

    def _admin_get_once(self, path: str, *, params: dict[str, str | int | bool]) -> httpx.Response:
        return self._client.get(
            f"{self.admin_api_base_url}{path}",
            headers={"Authorization": f"Bearer {self._access_token()}"},
            params=params,
        )

    def _access_token(self) -> str:
        now = time.time()
        if self._token and self._token.expires_at > now + 30:
            return self._token.value

        try:
            response = self._client.post(
                self.token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise UserDirectoryUnavailableError("Keycloak token request failed") from exc

        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise UserDirectoryUnavailableError("Keycloak token response did not include an access token")
        expires_in = payload.get("expires_in")
        ttl = expires_in if isinstance(expires_in, int | float) else 60
        self._token = _AccessToken(value=access_token, expires_at=now + max(float(ttl), 0.0))
        return access_token


def _first_attribute(attributes: object, name: str) -> str | None:
    """One attribute value, or None.

    Keycloak returns attributes as ``{name: [values]}`` even for values that
    are conceptually single, so the list is unwrapped here rather than at three
    call sites. A present-but-empty list reads as absent, which is the same
    thing for every decision made from these markers.
    """

    if not isinstance(attributes, dict):
        return None
    values = attributes.get(name)
    if isinstance(values, list):
        for value in values:
            if isinstance(value, str) and value:
                return value
        return None
    return values if isinstance(values, str) and values else None


def _provisioned_account_from_keycloak(payload: dict) -> ProvisionedAccountRecord:
    attributes = payload.get("attributes")
    return ProvisionedAccountRecord(
        id=str(payload.get("id") or ""),
        email=str(payload.get("email") or ""),
        email_verified=bool(payload.get("emailVerified", False)),
        provisioned_for=_first_attribute(attributes, PROVISIONED_FOR_ATTRIBUTE),
        provisioning_complete=_first_attribute(attributes, PROVISIONING_COMPLETE_ATTRIBUTE),
    )


def _user_from_keycloak(payload: dict) -> UserDirectoryUser:
    return UserDirectoryUser(
        id=str(payload.get("id") or ""),
        username=str(payload.get("username") or ""),
        email=_optional_str(payload.get("email")),
        first_name=_optional_str(payload.get("firstName")),
        last_name=_optional_str(payload.get("lastName")),
        enabled=bool(payload.get("enabled", True)),
    )


def _group_from_keycloak(payload: dict) -> UserDirectoryGroup:
    return UserDirectoryGroup(
        id=str(payload.get("id") or ""),
        name=str(payload.get("name") or ""),
        path=_optional_str(payload.get("path")),
    )


def _optional_str(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None
