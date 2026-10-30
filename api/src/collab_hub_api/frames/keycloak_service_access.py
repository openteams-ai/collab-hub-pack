"""Adding accounts to service groups in Keycloak (issue #180).

The one implementation of :class:`~.account_provisioning.ServiceAccessGranter`
that can actually write. Everything about its authority is deliberately narrow,
and the narrowness was measured rather than assumed (#172):

* it holds ``Groups/manage-membership`` on the granted groups and
  ``Users/manage-group-membership``. Both are required -- the group-side scope
  alone refuses every call, including the one it is named for;
* it does **not** hold ``Groups/manage-members``. That scope, over a member of
  the group, also permits password reset, email change and deletion. A
  credential that could grant inference access *and* rewrite the password of
  everyone who has it would be a much larger thing than this needs to be;
* the same credential may hold ``manage-members`` on a **different** group --
  the account-creation staging group -- because the two only compose into
  takeover on the *same* group. That rule is why this is a separate protocol
  from the account provisioner rather than another method on it.

Where the group id comes from
-----------------------------
Keycloak's membership endpoint needs a group **id**, and configuration names
groups the way a person reads them (``/llm``), so something has to bridge the
two. There are two ways, and which one a deployment uses decides how much
authority this credential needs.

**Configured ids (preferred).** ``group_ids`` maps each path to its id, and
nothing is looked up. This is what collab-hub does, and the reason is not
convenience: resolving a path requires *reading* groups, and this credential
deliberately cannot read anything. Its measured boundary is writes to the
granted groups and 403 on every read -- including ``GET /groups`` -- so a
deployment that made startup depend on that lookup would have to widen the
credential to permit it. Configured ids keep the credential exactly as narrow
as it was proven to be, and remove any startup call to the identity provider
at all, so a Keycloak blip during a rollout cannot stop the API from starting.

**Looking the path up (fallback).** For a deployment whose credential *does*
hold group-read authority, an unmapped path is resolved once at startup and
cached. A configured group that does not exist then refuses to start, which
follows the house rule applied to ``appInstructions`` and
``web.public_base_url``: a deployment that cannot do what its configuration
says should fail where somebody is watching.

The cost of configured ids is honest and small: a values file carries a UUID,
and an id that goes stale -- which takes deleting and recreating the group,
a deliberate act rather than drift -- fails at grant time instead of at
startup. That failure is not silent, because #180 records it as a durable
``failed`` row a reconciler finds.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass

import httpx

from .account_provisioning import ServiceAccessError


@dataclass
class _AccessToken:
    value: str
    expires_at: float


class KeycloakServiceAccessGranter:
    """Adds an account to a group over Keycloak's admin API.

    Constructed with the group paths it is allowed to touch. That is not the
    same list as the one an acceptance grants -- the caller decides what to
    grant -- but it is what gets resolved at startup, so a path this granter
    was never told about fails as a programming error rather than as a lookup
    against a realm.
    """

    configured = True

    def __init__(
        self,
        *,
        token_url: str,
        admin_api_base_url: str,
        client_id: str,
        client_secret: str,
        group_paths: tuple[str, ...] = (),
        group_ids: Mapping[str, str] | None = None,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.token_url = token_url.rstrip("/")
        self.admin_api_base_url = admin_api_base_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self._client = httpx.Client(timeout=timeout, transport=transport)
        self._token: _AccessToken | None = None
        # Seeded from configuration, then filled in by any lookup that is still
        # needed. One cache either way, so `grant` has one place to read from.
        self._group_ids: dict[str, str] = dict(group_ids or {})
        self._known_paths = tuple(group_paths)

    # -- lifecycle ---------------------------------------------------------

    def resolve_groups(self) -> dict[str, str]:
        """Make sure every known path has an id, looking up only what is missing.

        Called at startup by the builder. Raises :class:`ServiceAccessError`
        naming the missing path, because the useful message is *which* group is
        wrong -- "a group is missing" sends the reader to compare two lists by
        hand.

        **A path whose id is configured is not looked up**, which is what lets a
        deployment with a write-only credential start at all: see the module
        docstring. When every path is configured this method performs no
        request, so startup does not depend on the identity provider being
        reachable.
        """

        for path in self._known_paths:
            if self._group_ids.get(path):
                continue
            self._group_ids[path] = self._lookup_group_id(path)
        return dict(self._group_ids)

    def close(self) -> None:
        self._client.close()

    # -- the one write -----------------------------------------------------

    def grant(self, *, user_id: str, group_path: str) -> None:
        """Add ``user_id`` to ``group_path``.

        Idempotent at the provider: Keycloak answers 204 whether or not the
        account was already a member, which is what makes a retry safe and
        reconciliation cheap. This method does not read the membership first --
        a read to avoid a no-op write would be two calls to save nothing, and
        it would introduce exactly the check-then-act shape that had to be
        removed from the provisioning claim.
        """

        group_id = self._group_ids.get(group_path)
        if group_id is None:
            # Not resolved at startup: either a path nobody configured, or
            # `resolve_groups` was never called. Both are wiring mistakes, and
            # neither should be papered over with a lookup here -- that would
            # let a typo become a per-acceptance failure again.
            raise ServiceAccessError(
                f"group path {group_path!r} was not resolved at startup; "
                f"known paths: {sorted(self._group_ids) or sorted(self._known_paths)}"
            )
        self._put(f"/users/{user_id}/groups/{group_id}")

    # -- HTTP --------------------------------------------------------------

    def _lookup_group_id(self, path: str) -> str:
        """One group's id, by exact path.

        ``search`` matches names rather than paths and matches loosely, so the
        result is filtered on the full path. A group named ``llm`` nested
        somewhere unexpected must not silently satisfy a configured ``/llm``:
        the whole point of granting by path is that it says where in the tree
        the group is.
        """

        name = path.rstrip("/").rsplit("/", 1)[-1]
        if not name:
            raise ServiceAccessError(f"group path {path!r} names no group")
        payload = self._get("/groups", params={"search": name, "briefRepresentation": True})
        if not isinstance(payload, list):
            raise ServiceAccessError("Keycloak groups response was not a list")
        for candidate in _flatten_groups(payload):
            if candidate.get("path") == path:
                group_id = str(candidate.get("id") or "")
                if not group_id:
                    raise ServiceAccessError(f"group {path!r} has no id in the Keycloak response")
                return group_id
        raise ServiceAccessError(f"configured service group {path!r} does not exist in the realm")

    def _get(self, path: str, *, params: dict[str, object]) -> object:
        return self._request("GET", path, params=params).json()

    def _put(self, path: str) -> None:
        self._request("PUT", path)

    def _request(self, method: str, path: str, *, params: dict[str, object] | None = None) -> httpx.Response:
        try:
            response = self._send(method, path, params=params)
            if response.status_code == 401:
                # One retry on a fresh token, the same shape the directory
                # client uses: a token can expire between the check and the
                # call, and that is not a failure worth surfacing.
                self._token = None
                response = self._send(method, path, params=params)
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            raise ServiceAccessError(
                f"Keycloak service-access request failed with HTTP {exc.response.status_code}"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ServiceAccessError("Keycloak service-access request failed") from exc

    def _send(self, method: str, path: str, *, params: dict[str, object] | None) -> httpx.Response:
        return self._client.request(
            method,
            f"{self.admin_api_base_url}{path}",
            headers={"Authorization": f"Bearer {self._access_token()}"},
            params=params,
        )

    def _access_token(self) -> str:
        now = time.monotonic()
        if self._token is not None and self._token.expires_at > now:
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
            raise ServiceAccessError("Keycloak service-access token request failed") from exc
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise ServiceAccessError("Keycloak returned no access token for service access")
        # 30s of headroom, so a token does not expire mid-call and turn a grant
        # into a retry for no reason.
        lifetime = payload.get("expires_in")
        seconds = float(lifetime) if isinstance(lifetime, (int, float)) else 60.0
        self._token = _AccessToken(value=token, expires_at=now + max(seconds - 30.0, 5.0))
        return token


def _flatten_groups(payload: list) -> list[dict]:
    """Every group in a Keycloak group response, subgroups included.

    ``GET /groups`` returns a tree, so a nested service group (``/services/llm``)
    appears as a child rather than at the top level. Flattening here keeps the
    nesting decision -- one parent for service groups, or several top-level ones
    -- a realm choice rather than something this client constrains.
    """

    flat: list[dict] = []
    stack = [item for item in payload if isinstance(item, dict)]
    while stack:
        group = stack.pop()
        flat.append(group)
        children = group.get("subGroups")
        if isinstance(children, list):
            stack.extend(child for child in children if isinstance(child, dict))
    return flat
