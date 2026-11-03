"""The keyed claim's client: a step attempt acts once, however often the hub invokes it.

Every task the hub sends carries an idempotency key, stable for one step
attempt. Before an entry point acts, the worker reserves that key on the hub
(``POST /v1/claims/{key}/reserve``, with its run token); after it answered,
the worker commits the envelope (``POST /v1/claims/{key}/commit``). A replay
of a committed key is answered with the committed envelope and the entry point
never runs; a replay of a key another worker reserved and never committed, one
lost between its side effect and its result, fails the step ``outcome-unknown``
and runs nothing either. The hub is ``COLLAB_HUB_URL`` in the worker's
environment; a worker given none claims nothing, and acts on every invocation.
Standard library only, like the rest of the SDK.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

HUB_ENV = "COLLAB_HUB_URL"


class ClaimUnavailable(Exception):
    """The hub's claim endpoint could not be reached, or did not answer as one: the claim is unknown."""


@dataclass(frozen=True, slots=True)
class Claim:
    """What the hub says of a key: its state, whether this worker is to act, and a committed envelope."""

    key: str
    state: str
    """``reserved``, ``committed``, ``outcome_unknown`` or ``recorded``."""
    yours: bool
    """This worker holds the reservation: act, then commit."""
    envelope: Mapping[str, Any] | None = None


class ClaimClient:
    """Reserve and commit keys on the hub, as the worker whose run token this is."""

    def __init__(self, hub_url: str, token: str, *, timeout: float = 30.0) -> None:
        self.hub_url = hub_url.rstrip("/")
        self._token = token
        self.timeout = timeout

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> ClaimClient | None:
        """The client for the hub the environment names, or ``None`` when it names none."""
        hub_url, token = environ.get(HUB_ENV, ""), environ.get("COLLAB_RUN_TOKEN", "")
        return cls(hub_url, token) if hub_url and token else None

    def _call(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(f"{self.hub_url}{path}", data=data, method=method, headers={
            "Authorization": f"Bearer {self._token}", "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - the hub's URL
                return response.status, self._decoded(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, self._decoded(exc.read())
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise ClaimUnavailable(f"the hub at {self.hub_url} could not be reached for the claim: {exc}") from exc

    @staticmethod
    def _decoded(raw: bytes) -> dict[str, Any]:
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    @staticmethod
    def _claim(status: int, body: Mapping[str, Any], key: str, what: str) -> Claim:
        state = body.get("state")
        if status not in (200, 201) or not isinstance(state, str):
            error = body.get("error") if isinstance(body.get("error"), Mapping) else {}
            raise ClaimUnavailable(f"the hub answered HTTP {status} to the {what} of key {key!r}: "
                                   f"{error.get('code') or 'no claim'}: {error.get('message') or body}")
        return Claim(key=key, state=state, yours=bool(body.get("yours")), envelope=body.get("envelope"))

    def reserve(self, key: str) -> Claim:
        """Reserve ``key`` before acting. ``yours`` says to act; ``committed`` carries the envelope to answer
        with; ``outcome_unknown`` says nothing acts again."""
        status, body = self._call("POST", f"/v1/claims/{urllib.parse.quote(key, safe='')}/reserve")
        return self._claim(status, body, key, "reserve")

    def commit(self, key: str, envelope: Mapping[str, Any]) -> Claim:
        """Commit the envelope the entry point answered with, under the key this worker reserved."""
        status, body = self._call("POST", f"/v1/claims/{urllib.parse.quote(key, safe='')}/commit",
                                  {"envelope": dict(envelope)})
        return self._claim(status, body, key, "commit")

    def get(self, key: str) -> Claim | None:
        """What became of ``key``, or ``None`` for a key never reserved."""
        status, body = self._call("GET", f"/v1/claims/{urllib.parse.quote(key, safe='')}")
        if status == 404:
            return None
        return self._claim(status, body, key, "read")


def log(message: str) -> None:
    print(f"claim: {message}", file=sys.stderr, flush=True)
