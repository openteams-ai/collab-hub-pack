"""Calls to the hub's REST API, with the profile's session when there is one.

The session is sent only to the hub it was obtained for, and renewed before it
expires. A request the hub refuses as unauthenticated is an :class:`AuthError`
(exit 5); any other refusal or failure is a :class:`HubError` (exit 1).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import httpx

from . import credentials, oidc
from .config import Target
from .oidc import AuthError

# Module-level so tests can route every request to a stub.
transport: httpx.BaseTransport | None = None

RENEW_BEFORE_SECONDS = 30
TIMEOUT_SECONDS = 30


class HubError(Exception):
    """The hub refused or failed a request, or could not be reached: exit code 1."""


def http_client() -> httpx.Client:
    return httpx.Client(transport=transport, timeout=TIMEOUT_SECONDS)


def _is_html(response: httpx.Response) -> bool:
    return response.headers.get("content-type", "").split(";")[0].strip().lower() in ("text/html",
                                                                                       "application/xhtml+xml")


def not_the_hub(url: str, path: str, response: httpx.Response) -> str:
    """Why an answer that is not JSON means ``url`` is not a Collab Hub API, and what answered instead.

    A web app in front of the hub — a portal serving the same page at every
    path — answers 200 with HTML, which says nothing about the hub.
    """
    kind = response.headers.get("content-type", "").split(";")[0].strip() or "no content type"
    title = re.search(r"<title[^>]*>\s*([^<]{1,120}?)\s*</title>", response.text, re.IGNORECASE)
    what = f'a web page titled "{title.group(1)}"' if title else kind
    return (f"{url} is not a Collab Hub API: it answered {path} with {what} (HTTP {response.status_code}), "
            "not the hub's JSON. Check the hub's address: the API's own host, not a web app in front of it")


def error_message(response: httpx.Response) -> str:
    """The hub's own words for a refusal: its error envelope, FastAPI's ``detail``, or the status."""

    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            return f"{error['message']}{_refused(error.get('details'))} (HTTP {response.status_code})"
        if isinstance(body.get("detail"), str):
            return f"{body['detail']} (HTTP {response.status_code})"
    return f"HTTP {response.status_code}"


def _refused(details: object) -> str:
    """What a validation error refused, from the envelope's details: ``: name: String should match ...``."""

    if not isinstance(details, list) or not details or not isinstance(details[0], dict):
        return ""
    where = [str(part) for part in details[0].get("loc", []) if part not in ("body", "query", "path")]
    message = details[0].get("msg")
    if not message:
        return ""
    return f": {'.'.join(where)}: {message}" if where else f": {message}"


class Hub:
    def __init__(self, target: Target):
        self.target = target
        self.url = target.require_hub()
        self.http = http_client()
        stored = credentials.load(target.directory, target.profile)
        # Never hand a session to a hub it was not obtained for.
        self.session = stored if stored is not None and stored.hub == self.url else None

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> Hub:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # --- the session ------------------------------------------------------------------------

    def _renew_if_due(self) -> None:
        session = self.session
        if session is None or session.expires_at is None:
            return
        if oidc.clock() < session.expires_at - RENEW_BEFORE_SECONDS:
            return
        if not session.refresh_token or not session.issuer or not session.client_id:
            raise AuthError(f"the token for {self.url} has expired; sign in again with `collab-hub login`")
        # A realm that is down or failing raises RealmError and keeps the session,
        # so the next command can renew it; only a grant the realm refused ends it.
        metadata = oidc.discover(self.http, session.issuer, self.target.insecure)
        try:
            tokens = oidc.refresh(self.http, metadata, session.client_id, session.refresh_token)
        except AuthError:
            # The realm refused the refresh token itself: it will not work next time either.
            credentials.delete(self.target.directory, self.target.profile)
            self.session = None
            raise
        session.access_token, session.refresh_token, session.expires_at = (
            tokens.access_token, tokens.refresh_token, tokens.expires_at)
        credentials.save(self.target.directory, self.target.profile, session)

    # --- requests ---------------------------------------------------------------------------

    def request(self, method: str, path: str, *, authenticate: bool = True, **kwargs: Any) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}) or {})
        if authenticate:
            self._renew_if_due()
            if self.session is not None:
                headers["Authorization"] = f"Bearer {self.session.access_token}"
        try:
            response = self.http.request(method, f"{self.url}{path}", headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise HubError(f"cannot reach the hub at {self.url}: {exc}") from exc
        if 400 <= response.status_code < 500 and _is_html(response):
            # The hub refuses in JSON, always: a refusal that is a web page comes from a web server or
            # a portal at this URL, not the hub. A 5xx page is left as it is: a proxy's error page in
            # front of a hub that is failing says nothing about the address.
            raise HubError(not_the_hub(self.url, path, response))
        if not authenticate and response.status_code in (401, 404):
            # Asked without credentials, so a 401 is not about a session: the hub has no such public route.
            raise HubError(f"{self.url} does not offer {path} (HTTP {response.status_code}); "
                           "it may predate this version of collab-hub and need upgrading")
        if response.status_code == 401:
            if self.session is None:
                raise AuthError(f"not signed in to {self.url}; run `collab-hub --hub {self.url} login`")
            raise AuthError(f"{self.url} refused the session ({error_message(response)}); "
                            "run `collab-hub login` again")
        if response.status_code >= 400:
            raise HubError(error_message(response))
        return response

    def request_json(self, method: str, path: str, **kwargs: Any) -> Any:
        """A request whose answer is the hub's JSON; anything else means the URL is not the hub's API."""
        response = self.request(method, path, **kwargs)
        try:
            return response.json()
        except ValueError:
            raise HubError(not_the_hub(self.url, path, response)) from None

    def get_json(self, path: str, **kwargs: Any) -> Any:
        return self.request_json("GET", path, **kwargs)

    def pages(self, path: str, params: dict[str, Any], limit: int = 200) -> Iterator[dict]:
        """Every item of a paged listing (``items`` + ``next_offset``), following pages to the end."""

        offset = 0
        while True:
            page = self.get_json(path, params={**params, "limit": limit, "offset": offset})
            yield from page["items"]
            if page.get("next_offset") is None:
                return
            offset = page["next_offset"]
