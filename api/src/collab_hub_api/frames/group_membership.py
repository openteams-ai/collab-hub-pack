"""Reading and changing Keycloak group membership, for the admin panel.

The one place in this codebase that writes to Keycloak. Everywhere else it is
a plain OIDC provider: the platform-role sync reads a claim and writes only to
this database, and the user directory reads.

How this differs from the invitation-time granter
-------------------------------------------------
:class:`~.keycloak_service_access.KeycloakServiceAccessGranter` exists to add a
newly-provisioned account to a service group, and was deliberately built unable
to do anything else -- it cannot read, so it cannot even resolve a group path,
and its group ids are configured for that reason.

The panel needs two things that granter cannot do: list who is in a group, and
take somebody out. Both need scopes it does not hold, so this is a separate
client with a separate credential rather than a widening of that one. Keeping
them apart means the invitation path's authority does not grow because an admin
screen needed something.

The scope boundary, which is the point
--------------------------------------
This credential needs ``Groups/view-members`` to list, and
``Groups/manage-membership`` plus ``Users/manage-group-membership`` to add and
remove.

It must **not** hold ``Groups/manage-members``. Over a member of a group that
scope also permits password reset, email change and deletion -- so a credential
that could both put somebody into the model group and rewrite their password
could take over any account it chose. The two halves are safe apart and are
account takeover together.

That rule is enforced here by construction rather than by trusting the realm:
the only requests this class can issue are a member listing and a membership
``PUT``/``DELETE``, there is no method that touches an account, and the group
ids it will act on are a configured set. A test pins the public surface, so
adding a method that could create or delete an account fails at unit speed.

Group ids are configured, not looked up, for the reason the granter documents:
resolving a path requires reading the group tree, and a deployment should not
have to widen a credential so that startup can ask a question the values file
already answers.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import quote

import httpx

__all__ = ["GroupMember", "GroupMembershipClient", "GroupMembershipError"]

DEFAULT_TIMEOUT_SECONDS = 10.0
MEMBER_PAGE_SIZE = 500


class GroupMembershipError(RuntimeError):
    """Keycloak refused, could not be reached, or the group is not managed here."""


@dataclass(frozen=True)
class GroupMember:
    id: str
    username: str | None
    email: str | None


@dataclass
class _AccessToken:
    value: str
    expires_at: float


class GroupMembershipClient:
    def __init__(
        self,
        *,
        token_url: str,
        admin_api_base_url: str,
        client_id: str,
        client_secret: str,
        group_ids: Mapping[str, str],
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._token_url = token_url
        self._base_url = admin_api_base_url.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._group_ids = dict(group_ids)
        self._client = httpx.Client(timeout=timeout_seconds, transport=transport)
        self._token: _AccessToken | None = None

    @property
    def configured(self) -> bool:
        return bool(self._token_url and self._base_url and self._client_id and self._group_ids)

    def list_members(self, group_path: str) -> list[GroupMember]:
        """Every member, read page by page until Keycloak returns a short one."""

        group_id = self._group_id(group_path)
        members: list[GroupMember] = []
        first = 0
        while True:
            payload = self._request(
                "GET",
                f"/groups/{group_id}/members",
                params={"first": first, "max": MEMBER_PAGE_SIZE},
            )
            if not isinstance(payload, list):
                raise GroupMembershipError("Keycloak returned an unrecognized member listing")
            members.extend(
                GroupMember(
                    id=str(entry.get("id")),
                    username=entry.get("username"),
                    email=entry.get("email"),
                )
                for entry in payload
                if isinstance(entry, dict) and entry.get("id")
            )
            if len(payload) < MEMBER_PAGE_SIZE:
                return members
            first += MEMBER_PAGE_SIZE

    def add_member(self, *, user_id: str, group_path: str) -> None:
        """Idempotent at the provider: adding an existing member is a no-op."""

        self._request("PUT", self._membership_path(user_id, group_path))

    def remove_member(self, *, user_id: str, group_path: str) -> None:
        """Also idempotent: removing somebody who is not a member succeeds."""

        self._request("DELETE", self._membership_path(user_id, group_path))

    def close(self) -> None:
        self._client.close()

    def _membership_path(self, user_id: str, group_path: str) -> str:
        """The one endpoint this client writes to, with *user_id* as one segment.

        The id is caller input, so it is encoded rather than trusted: a ``#``
        would otherwise cut the path at ``/users/<id>`` and turn the membership
        DELETE into Keycloak's delete-user call, and a ``/`` or ``?`` would
        reach other admin endpoints. Encoding leaves dots alone, so the dot
        segments are refused outright.
        """

        if user_id in ("", ".", ".."):
            raise GroupMembershipError(f"{user_id!r} is not a user id")
        return f"/users/{quote(user_id, safe='')}/groups/{self._group_id(group_path)}"

    def _group_id(self, group_path: str) -> str:
        """The configured id for *group_path*, refusing anything unmanaged.

        Checked before any request is made, so an unmanaged group is a refusal
        this process issues rather than a call whose outcome depends on how
        broadly the realm happened to scope the credential. Configuration is
        the boundary; the realm is the backstop.
        """

        group_id = self._group_ids.get(group_path)
        if not group_id:
            raise GroupMembershipError(f"{group_path!r} is not a group this deployment manages from the admin panel")
        return group_id

    def _request(self, method: str, path: str, *, params: dict | None = None):
        try:
            response = self._client.request(
                method,
                f"{self._base_url}{path}",
                params=params,
                headers={"Authorization": f"Bearer {self._access_token()}"},
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # The status, never the body: a Keycloak error page can quote the
            # request, and this request carries a bearer token.
            raise GroupMembershipError(
                f"Keycloak refused {method} {path.split('/')[1]}: {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise GroupMembershipError("Keycloak could not be reached") from exc
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise GroupMembershipError("Keycloak returned a body that is not JSON") from exc

    def _access_token(self) -> str:
        now = time.monotonic()
        if self._token is not None and self._token.expires_at > now:
            return self._token.value
        try:
            response = self._client.post(
                self._token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise GroupMembershipError("could not obtain a Keycloak access token") from exc
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise GroupMembershipError("Keycloak returned no access token")
        # Renew early: a token that expires between this check and the request
        # it authorizes would surface as an unexplained 401 mid-action.
        lifetime = float(payload.get("expires_in", 60))
        self._token = _AccessToken(value=token, expires_at=now + max(lifetime - 30.0, 5.0))
        return token
