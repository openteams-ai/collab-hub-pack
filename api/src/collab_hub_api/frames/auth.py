"""Authentication helpers for gateway-validated Nebari requests."""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass
from functools import cache
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, status

from . import error_codes
from .identity import (
    DISPLAY_CLAIMS,
    LEGACY_IDENTITY_CLAIM_PRECEDENCE,
    PINNED_IDENTITY_CLAIM,
    identity_pinned_to_sub,
)
from .org_source import (
    IDENTITY_PROVIDER_CLAIM,
    SingleOrgDeclaration,
    org_source_resolves_membership,
    single_org_declaration,
)
from .orgs import OrgStore, OrgsUnavailableError

auth_logger = logging.getLogger("frames_server.auth")

usage_logger = logging.getLogger("frames_server.usage")
logger = logging.getLogger("frames_server.auth")

WORKSPACE_DEFAULT = "default"
"""The only workspace id on a membership-resolving deployment.

Workspaces are deliberately not modeled — there is no table, no row, and no
API. Keeping ``workspace_id`` as a constant rather than dropping it keeps every
existing store signature and composite key intact, and isolation is unaffected
because every access check pairs workspace with org; nothing keys on
``workspace_id`` alone.
"""


@dataclass(frozen=True)
class DisplayIdentity:
    """Human-readable labels for the caller. **Never an ACL principal.**

    Display strings live in their own object rather than as peer fields of
    ``AuthContext.user`` so the separation is structural instead of a naming
    convention: there is no ``auth.email``/``auth.name`` sitting next to
    ``auth.user`` for an access check to reach for by accident or by
    autocomplete, and a reviewer can see at a glance that anything written into
    an ACL came from ``auth.user``. Both values here are mutable
    (``preferred_username``, ``name``) or self-asserted (``email``) — persisting
    either as a principal is the bug this module's pin exists to prevent
    (``frames.identity``).

    Legitimate uses: rendering a person in a UI, the usage roster's
    display-only ``email`` column, and — paired with ``email_verified`` —
    invitation acceptance's match against the invited address (issue #89).
    That last one is not an exception to the rule above: the principal is
    still ``sub``, and the address is a value the invitation is *matched
    against*, never a key anything is stored under.
    """

    name: str | None = None
    email: str | None = None
    email_verified: bool = False
    """Whether the IdP asserted that ``email`` is verified — the boolean
    ``email_verified`` claim, and only the boolean (a string ``"true"``
    resolves to ``False`` here).

    It lives next to ``email`` rather than on :class:`AuthContext` so the
    address and the one fact that makes it usable as a match target cannot be
    read apart: an unverified address is a self-asserted string, and the
    whole of Gate B's acceptance rule rests on not treating it as more than
    that. Defaults to ``False``, so every context built without deciding the
    question — hand-built test contexts, the dev-auth shortcut, the claims
    path — is unverified.
    """


@dataclass(frozen=True)
class AuthContext:
    """Authenticated caller identity and workspace scope.

    ``user`` is *the* ACL principal — the only field of this object that may be
    compared against, or written into, an owners/readers/actor/``created_by``
    value. Which claim it comes from is decided by ``frames.identity``.
    """

    user: str
    home_org_id: str | None
    """The caller's home organization, or ``None`` for the one context shape
    that legitimately has no organization: a platform operator whose login
    holds no active membership (issue #89's bootstrap — on a fresh deployment
    the first organization only exists after the operator's first invitation
    is accepted).

    Org-scoped code must keep reading :attr:`org_id`, which raises for the
    hub-scoped shape. Read this field directly only where "no organization"
    is an expected, explicitly-handled state: the operator surface, the
    audited-execution primitive, and the authorization wrappers.
    """
    workspace_id: str
    display: DisplayIdentity = DisplayIdentity()
    """Display-only labels for this caller; see :class:`DisplayIdentity`."""
    org_role: str | None = None
    """The caller's role (``owner``/``member``) in the organization named by
    ``org_id``, when that organization came from membership resolution
    (``frames.org_source``); ``None`` under claims-sourced auth, where there is
    no server-owned role to read.

    Authority *inside one organization*. It is not a deployment-wide role and
    must not be used as one — see the module note on the platform-role axis.
    Frame and group ACL checks never read it: those key on ``user`` alone.
    """
    platform_role: str | None = None
    """The caller's deployment-wide role (``operator``), from their active
    ``collab_platform_roles`` row, when the deployment resolves organizations
    from membership (``frames.org_source``); ``None`` under claims-sourced
    auth, where the ``collab_`` tables are never read.

    The **second authority axis** (issue #87): ``org_role`` is authority inside
    the one organization named by ``org_id``; this is authority across all of
    them. The two are deliberately separate fields checked by separate
    wrappers (:mod:`.authorization`) — do not merge them, and do not treat
    either as implying the other. Frame and group ACL checks never read it.
    """

    @property
    def org_id(self) -> str:
        """The organization this caller acts in — raising when there is none.

        A property rather than the stored field so that the hub-scoped
        operator context (``home_org_id is None``, issue #89) fails **closed
        at the point of consumption**: every pre-existing org-scoped surface —
        frames, groups, tasks, usage, MCP tools — reads ``auth.org_id``, and
        rather than handing them a ``None`` to compare against, write into a
        store key, or format into a path, this raises
        :class:`NoOrganizationError`. The HTTP layers — route dependencies,
        the path-protection middleware, the MCP *authentication* middleware —
        map it to the ``no_organization`` 403 envelope. One layer does not:
        a read inside a mounted MCP **tool body** raises after the transport
        has answered, so FastMCP renders it as a JSON-RPC tool error carrying
        this exception's message rather than the envelope's machine-readable
        code (pinned by test). The refusal is still total either way: no
        org-scoped endpoint can be *used* with a missing organization,
        because none can read one.

        Code for which "no organization" is an expected state opts in by
        reading :attr:`home_org_id` instead — an explicit, greppable choice.
        """

        if self.home_org_id is None:
            raise NoOrganizationError()
        return self.home_org_id

    @property
    def email(self) -> str | None:
        """Display-only view of ``display.email``, kept for usage reporting.

        A read-only property rather than a field, so an email can no longer be
        *constructed* into the same tier as ``user``.
        """

        return self.display.email


current_auth_context: ContextVar[AuthContext | None] = ContextVar(
    "frames_server_current_auth",
    default=None,
)


class NoOrganizationError(HTTPException):
    """Authenticated, but not an active member of any organization (403).

    A subclass of ``HTTPException`` so that every existing ``except
    HTTPException`` — FastAPI's dependency machinery, the path-protection
    middleware, the MCP auth middleware — keeps behaving, while the handlers
    registered for this class emit the distinct ``no_organization`` code
    instead of the generic ``forbidden``.

    Deliberately **not** a 401. The desktop maps 401 to "sign in", and a
    pending invitee or a removed member is already signed in; answering 401
    produces a re-login loop that cannot succeed. The message is written for a
    person, but clients are expected to branch on the code and render their own
    text — apollo-desktop#637 does exactly that and does not surface this
    string.
    """

    error_code = error_codes.NO_ORGANIZATION
    """Carried on the exception so a renderer that has no idea this class exists
    still emits the right code — the credential check runs in more than one
    place, and a surface that fell back to ``unauthorized`` would hand the
    desktop the one answer this state exists to avoid."""

    def __init__(self) -> None:
        super().__init__(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Your account is not part of an organization yet. Accept your invitation, or"
                " contact your organization owner if you were expecting access."
            ),
        )


class TokenDecodeError(ValueError):
    """Raised when an auth token cannot be decoded or verified."""

    pass


def decode_jwt_payload(token: str) -> dict:
    """Decode a JWT payload without verifying the signature."""
    parts = token.split(".")
    if len(parts) != 3:
        return {}

    payload = parts[1]
    padding = 4 - len(payload) % 4
    if padding != 4:
        payload += "=" * padding

    try:
        decoded = base64.urlsafe_b64decode(payload)
        return json.loads(decoded)
    except (ValueError, json.JSONDecodeError):
        return {}


def unsafe_auth_enabled() -> bool:
    """Return whether local/test-only auth shortcuts may be used."""

    return os.environ.get("FRAMES_UNSAFE_AUTH_ENABLED") == "true"


JWKS_URL_ENVS = ("FRAMES_BEARER_JWKS_URL", "FRAMES_IDTOKEN_JWKS_URL")
"""The two JWKS URLs the verifiers fetch signing keys from.

Chart values ``frames.auth.bearer.jwksUrl`` and ``frames.auth.idToken.jwksUrl``.
"""


def enforce_https_jwks_urls() -> None:
    """Refuse to start with a JWKS URL that fetches signing keys over cleartext (issue #77).

    Both URLs are handed straight to PyJWT's ``PyJWKClient``, which will
    happily fetch over plain ``http`` — and a key set fetched over cleartext
    can be substituted by an on-path attacker, who then mints tokens the
    verifiers accept. A *malformed* URL already fails closed (every fetch
    fails, every token is rejected), but a well-formed ``http`` URL would be
    accepted silently, so the scheme is checked here, at startup, where the
    misconfiguration surfaces in the pod's events rather than as unexplained
    401s on the first authenticated request.

    ``http`` is allowed only when :func:`unsafe_auth_enabled` is on — the same
    local-development door the unsigned-token shortcuts sit behind — and using
    it is logged loudly. URLs carrying userinfo (``user:pass@host``) or a
    fragment are refused unconditionally: no JWKS endpoint is addressed that
    way, so their only appearance here is a mistake or something trying to
    confuse a parser.

    An unset or empty URL passes: that verifier is simply not configured, and
    ``decode_verified_jwt`` already fails closed on every token it is asked to
    check. Hostname allowlists are deliberately out of scope — internal
    Keycloak hostnames and cluster-local service URLs are legitimate here.
    """

    for env_name in JWKS_URL_ENVS:
        url = os.environ.get(env_name, "").strip()
        if not url:
            continue
        try:
            parts = urlsplit(url)
            parts.port  # noqa: B018 — validated lazily; raises ValueError on a bad port
        except ValueError as exc:
            raise RuntimeError(f"{env_name} is not a valid URL: {url!r} ({exc})") from exc
        if parts.username is not None or parts.password is not None:
            raise RuntimeError(
                f"{env_name} must not carry userinfo (user:password@): got {url!r}. "
                "JWKS endpoints are not addressed with credentials in the URL."
            )
        if parts.fragment:
            raise RuntimeError(
                f"{env_name} must not carry a fragment: got {url!r}. "
                "A fragment is never sent to the server, so it can only be a mistake."
            )
        if not parts.hostname:
            raise RuntimeError(f"{env_name} has no hostname: got {url!r}.")
        if parts.scheme == "https":
            continue
        if parts.scheme == "http" and unsafe_auth_enabled():
            logger.warning(
                "UNSAFE: %s uses cleartext http (%s). Signing keys fetched over http can be "
                "substituted by an on-path attacker. This is permitted only because "
                "FRAMES_UNSAFE_AUTH_ENABLED=true (local development); production deployments "
                "must use https.",
                env_name,
                url,
            )
            continue
        raise RuntimeError(
            f"{env_name} must be an https:// URL: got {url!r}. Signing keys fetched over "
            "cleartext http can be substituted by an on-path attacker, who can then mint "
            "tokens this deployment accepts. For local development only, http is permitted "
            "when FRAMES_UNSAFE_AUTH_ENABLED=true."
        )


def decode_bearer_payload(token: str) -> dict:
    """Verify and decode a native-client bearer token.

    Gateway cookies are already verified by Envoy before they reach the app.
    Bearer tokens can be supplied by native clients, so they need an explicit
    verification path unless tests opt into unsigned tokens.
    """

    if unsafe_auth_enabled() and os.environ.get("FRAMES_BEARER_ALLOW_UNSIGNED") == "true":
        return decode_jwt_payload(token)

    return decode_verified_jwt(
        token,
        jwks_url=os.environ.get("FRAMES_BEARER_JWKS_URL"),
        issuer=os.environ.get("FRAMES_BEARER_ISSUER"),
        audience=os.environ.get("FRAMES_BEARER_AUDIENCE"),
    )


def decode_id_token_payload(token: str) -> dict:
    """Verify and decode a gateway/native IdToken cookie.

    The gateway normally verifies IdToken cookies before forwarding browser
    traffic, but some routes intentionally bypass gateway auth so native clients
    can use bearer tokens. Since those routes can still receive caller-supplied
    cookies, the app verifies cookie signatures before trusting claims.
    """

    if unsafe_auth_enabled() and os.environ.get("FRAMES_IDTOKEN_ALLOW_UNSIGNED") == "true":
        return decode_jwt_payload(token)

    id_token_jwks_url = os.environ.get("FRAMES_IDTOKEN_JWKS_URL")
    if id_token_jwks_url:
        return decode_verified_jwt(
            token,
            jwks_url=id_token_jwks_url,
            issuer=os.environ.get("FRAMES_IDTOKEN_ISSUER") or os.environ.get("FRAMES_BEARER_ISSUER"),
            audience=os.environ.get("FRAMES_IDTOKEN_AUDIENCE"),
        )

    # No dedicated IdToken verifier: cookies fall back to the bearer JWKS —
    # but issuer and audience each still let a dedicated FRAMES_IDTOKEN_*
    # value win over the bearer one first. A deployment with a shared JWKS
    # but a distinct IdToken issuer must keep working exactly as before;
    # only the previously-missing audience inheritance changes (issue #41).
    # Inheriting audience unconditionally (dropping the dedicated override)
    # would accept a same-realm token minted for a different audience as a
    # cookie, bypassing the bearer audience restriction.
    return decode_verified_jwt(
        token,
        jwks_url=os.environ.get("FRAMES_BEARER_JWKS_URL"),
        issuer=os.environ.get("FRAMES_IDTOKEN_ISSUER") or os.environ.get("FRAMES_BEARER_ISSUER"),
        audience=os.environ.get("FRAMES_IDTOKEN_AUDIENCE") or os.environ.get("FRAMES_BEARER_AUDIENCE"),
    )


JWKS_FORCED_REFRESH_MIN_INTERVAL_SECONDS = 30.0
"""Minimum seconds between forced JWKS refetches triggered by unknown key ids.

PyJWT already refreshes the cached JWK set once when a token references an
unknown ``kid`` (how rotated keys get picked up without a restart), but that
refresh is otherwise unbounded — a flood of forged tokens would turn every
failed verification into an outbound JWKS fetch. This bound keeps the failure
path local while still letting a genuine rotation land within the interval.
"""

JWKS_CACHE_LIFESPAN_SECONDS = 300.0
"""How long a validated JWK set is served before it is fetched again.

Set explicitly rather than inherited from PyJWT so the rotation window is a
stated number: a key published after the last fetch is picked up either by the
next unknown-``kid`` forced refresh or, failing that, when this lifespan
expires.
"""

JWKS_FETCH_TIMEOUT_SECONDS = 5.0
"""Socket timeout for one outbound JWKS fetch.

PyJWT's default is 30s, longer than any caller waits on an auth check: a cold
start against a hung IdP would block the request thread for the full 30s.
Fetches are single-flighted, so this bounds one caller's wait, not each
caller's.
"""

JWKS_MAX_STALE_SECONDS = 2 * JWKS_CACHE_LIFESPAN_SECONDS
"""How long a validated JWK set keeps verifying after the endpoint last confirmed it.

A fetch that fails, times out, or returns no usable keys leaves the last
validated set in place, so an IdP blip cannot revoke keys that still work. That
fallback has to be bounded, or it renews itself: reinstating the set also
refreshes its cache timestamp, so an endpoint that never again returns a usable
key set would keep a withdrawn key verifying tokens forever. Past this point the
set is dropped and verification fails closed instead. Two lifespans, so a single
failed refresh cycle is ridden out and the second one is not.
"""

JWKS_MAX_REJECTION_WINDOW_SECONDS = JWKS_FORCED_REFRESH_MIN_INTERVAL_SECONDS + JWKS_FETCH_TIMEOUT_SECONDS
"""Worst-case seconds a token signed by a freshly published key can be rejected.

The bad case is a forged unknown ``kid`` arriving just before the IdP publishes
a real key: the forged token consumes the forced-refresh allowance, so the
legitimate token waits out the remaining interval and then the fetch it
triggers. Operators size the IdP's publish-before-use delay against this.
"""

_jwks_clients: dict[str, object] = {}
_jwks_clients_lock = threading.Lock()


@cache
def _shared_jwk_client_class():
    """Build the shared-client subclass once, on first use.

    Deferred so importing this module never requires ``jwt``;
    :func:`decode_verified_jwt` reports a missing dependency as a
    :class:`TokenDecodeError`.
    """

    from jwt import PyJWKClient
    from jwt.api_jwk import PyJWKSet
    from jwt.exceptions import PyJWKClientError, PyJWTError

    class _Flight:
        """One outbound JWKS fetch, shared by every caller waiting on it.

        Serializing fetches is not enough on its own: callers queued behind a
        failing fetch would each run their own on the way through, so a cold
        burst against a broken or hung endpoint still costs one request per
        caller (and, when it hangs, one timeout each in series). Waiters take
        the flight's outcome — including its failure — so a burst costs one
        request whichever way it goes.
        """

        __slots__ = ("done", "value", "error")

        def __init__(self) -> None:
            self.done = threading.Event()
            self.value: PyJWKSet | None = None
            self.error: BaseException | None = None

        def result(self) -> PyJWKSet:
            self.done.wait()
            if self.error is not None:
                # A fresh exception per waiter: re-raising one instance from
                # several threads would have them all mutating its traceback.
                raise PyJWKClientError(f"Shared JWKS fetch failed: {self.error}") from self.error
            return self.value

    class _SharedJWKClient(PyJWKClient):
        """A ``PyJWKClient`` safe to share across concurrent requests.

        PyJWT's client is written to be constructed per use, so three things it
        leaves to the caller start to matter once one instance serves every
        request:

        * **Single flight.** ``get_jwk_set`` fetches whenever the cache is cold
          or expired, with nothing serializing concurrent callers, so a burst of
          N requests at startup or at cache expiry costs N outbound fetches on
          every replica. One fetch runs at a time and its waiters share the
          outcome, so a burst costs one request whether it succeeds or fails.

        * **Last known good, but not forever.** ``fetch_data`` writes the
          response to the cache *before* anything checks it has a key we can
          verify with. A successful HTTP 200 carrying an empty or unusable
          key set therefore replaces a working cache with one that fails for
          the rest of the lifespan — rejecting tokens signed by keys that are
          still valid, with no path back. Validating before adopting, and
          reinstating the last validated set otherwise, keeps a bad response
          from taking auth down; ``JWKS_MAX_STALE_SECONDS`` keeps that fallback
          from outliving a withdrawn key indefinitely.

        * **Bounded forced refresh.** See
          ``JWKS_FORCED_REFRESH_MIN_INTERVAL_SECONDS``.
        """

        def __init__(self, uri: str) -> None:
            super().__init__(
                uri,
                lifespan=JWKS_CACHE_LIFESPAN_SECONDS,
                timeout=JWKS_FETCH_TIMEOUT_SECONDS,
            )
            # Guards every field below it. Never held across a fetch.
            self._flight_lock = threading.Lock()
            self._flight: _Flight | None = None
            self._last_forced_refresh = float("-inf")
            self._validated_set = None
            self._validated_at = 0.0

        def get_jwk_set(self, refresh: bool = False) -> PyJWKSet:
            if not refresh:
                cached = self._cached_jwk_set()
                if cached is not None:
                    return cached

            with self._flight_lock:
                if refresh and not self._claim_forced_refresh():
                    refresh = False
                if not refresh:
                    # Re-read under the lock: a caller that raced us to it may
                    # have already fetched.
                    cached = self._cached_jwk_set()
                    if cached is not None:
                        return cached
                flight = self._flight
                if flight is not None:
                    # A fetch is already out; its answer is ours too, including
                    # a failure. Retrying behind it instead is what turned a
                    # cold burst into one request per caller. A forced refresh
                    # that joins a flight predating the key it is looking for
                    # still fails inside JWKS_MAX_REJECTION_WINDOW_SECONDS,
                    # having spent the allowance. A request arriving after this
                    # flight lands starts a fresh one.
                    leading = False
                else:
                    flight = self._flight = _Flight()
                    leading = True

            if leading:
                return self._fly(flight)
            return flight.result()

        def get_signing_key(self, kid: str):
            # PyJWT 2.15 wraps this lookup in a per-client lock held across
            # the fetch, and skips the unknown-``kid`` refresh for 30s after
            # *any* fetch. The first would stall every request behind one hung
            # forced refresh; the second would hide a key rotated just after a
            # cache-expiry fetch. ``get_jwk_set`` already single-flights
            # fetches and bounds forced refreshes, so neither is needed here.
            signing_key = self.match_kid(self.get_signing_keys(), kid)
            if signing_key is None:
                signing_key = self.match_kid(self.get_signing_keys(refresh=True), kid)
                if signing_key is None:
                    raise PyJWKClientError(f'Unable to find a signing key that matches: "{kid}"')
            return signing_key

        def _fly(self, flight: _Flight) -> PyJWKSet:
            """Run *flight*'s fetch and hand the outcome to everyone waiting."""

            try:
                try:
                    flight.value = self._fetch_jwk_set()
                except BaseException as exc:
                    flight.error = exc
                    raise
            finally:
                with self._flight_lock:
                    if self._flight is flight:
                        self._flight = None
                # Cleared before waking the waiters, so the next caller in is
                # free to retry immediately rather than joining a landed flight.
                flight.done.set()
            return flight.value

        def _cached_jwk_set(self) -> PyJWKSet | None:
            """Return the cached set when it is present, unexpired, and ours."""

            if self.jwk_set_cache is None:
                return None
            cached = self.jwk_set_cache.get()
            if cached is None:
                return None
            if cached is not self._validated_set:
                # Either ``fetch_data`` has just cached a response the in-flight
                # fetch has not adopted yet, or a set we dropped is still in the
                # cache. Neither is ours to serve: treating it as a miss sends
                # this caller to the lock, where the flight decides. The check
                # is against the parsed set we put back, not the raw response:
                # PyJWT 2.15 parses whatever ``fetch_data`` caches, so the raw
                # dict never comes back out of the cache.
                return None
            if time.monotonic() - self._validated_at > JWKS_MAX_STALE_SECONDS:
                # Unconfirmed for too long. Stop serving it from cache so the
                # next fetch either revalidates it or drops it.
                return None
            return cached

        def _claim_forced_refresh(self) -> bool:
            """Consume the forced-refresh allowance if it is available."""

            now = time.monotonic()
            if now - self._last_forced_refresh < JWKS_FORCED_REFRESH_MIN_INTERVAL_SECONDS:
                return False
            self._last_forced_refresh = now
            return True

        def _fetch_jwk_set(self) -> PyJWKSet:
            """Fetch and adopt a JWK set, or keep serving the last good one."""

            try:
                data = self.fetch_data()
            except Exception:
                # A transient outage must not start rejecting tokens that the
                # keys already in hand can verify.
                fallback = self._stale_fallback()
                if fallback is None:
                    raise
                return fallback

            jwk_set = self._usable_jwk_set(data)
            if jwk_set is None:
                fallback = self._stale_fallback()
                if fallback is None:
                    raise PyJWKClientError("The JWKS endpoint did not return a usable JWK set")
                return fallback

            self._validated_set = jwk_set
            self._validated_at = time.monotonic()
            if self.jwk_set_cache is not None:
                # Replace what ``fetch_data`` cached with the set we validated,
                # so cache hits serve keys already parsed and are recognisably
                # ours.
                self.jwk_set_cache.put(jwk_set)
            return jwk_set

        def _stale_fallback(self) -> PyJWKSet | None:
            """Reinstate the last validated set, unless it has gone stale.

            ``fetch_data`` may have just cached an unusable response, and the
            reinstatement overwrites it. Refreshing that cache timestamp is
            deliberate — while the endpoint is broken we keep serving keys that
            work and retry a lifespan later rather than on every request — but
            it is why the fallback needs its own deadline: the timestamp that
            decides it is ``_validated_at``, set only by a fetch the endpoint
            actually confirmed, so no amount of reinstating extends it. Past
            ``JWKS_MAX_STALE_SECONDS`` the set is dropped and callers fail
            closed, which is what makes removing the last usable key from the
            JWKS response take effect.
            """

            if self._validated_set is None:
                return None
            if time.monotonic() - self._validated_at > JWKS_MAX_STALE_SECONDS:
                self._validated_set = None
                if self.jwk_set_cache is not None:
                    self.jwk_set_cache.put(None)
                return None
            if self.jwk_set_cache is not None:
                self.jwk_set_cache.put(self._validated_set)
            return self._validated_set

        @staticmethod
        def _usable_jwk_set(data) -> PyJWKSet | None:
            """Parse *data* into a JWK set, or return ``None`` if unusable."""

            if not isinstance(data, dict):
                return None
            try:
                jwk_set = PyJWKSet.from_dict(data)
            except PyJWTError:
                return None
            if not any(key.key_id and key.public_key_use in ("sig", None) for key in jwk_set.keys):
                # ``get_signing_keys`` would reject this set anyway; rejecting it
                # here means the last validated set survives instead.
                return None
            return jwk_set

    return _SharedJWKClient


def _get_jwks_client(jwks_url: str):
    """Return the shared JWKS client for a URL, creating it on first use.

    Sharing one client per URL keeps steady-state verification free of network
    I/O: PyJWT caches the fetched JWK set on the client, so per-request
    construction would have discarded that cache every time. The registry lock
    covers only the dictionary — never a fetch, which each client serializes
    on its own lock.
    """

    with _jwks_clients_lock:
        client = _jwks_clients.get(jwks_url)
        if client is None:
            client = _shared_jwk_client_class()(jwks_url)
            _jwks_clients[jwks_url] = client
    return client


def decode_verified_jwt(token: str, *, jwks_url: str | None, issuer: str | None, audience: str | None) -> dict:
    """Verify a JWT signature and decode its payload."""

    if not jwks_url:
        raise TokenDecodeError("JWT verification is not configured")

    try:
        import jwt
    except ImportError as exc:
        raise TokenDecodeError("JWT verification dependencies are missing") from exc

    decode_options = {} if audience else {"verify_aud": False}

    try:
        signing_key = _get_jwks_client(jwks_url).get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256", "ES256"],
            audience=audience,
            issuer=issuer,
            options=decode_options,
        )
    except Exception as exc:
        raise TokenDecodeError("Invalid JWT") from exc


def get_id_token(request: Request) -> str | None:
    """Return the gateway-set IdToken cookie when present."""

    for name, value in request.cookies.items():
        if name.startswith("IdToken-"):
            return value
    return None


def get_bearer_token(request: Request) -> str | None:
    """Return a Nebari-issued bearer token when native clients provide one."""

    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() == "bearer" and token:
        return token
    return None


def user_from_claims(claims: dict) -> str | None:
    """Choose the ACL principal from accepted JWT claims.

    Under ``FRAMES_AUTH_IDENTITY_CLAIM=sub`` the verified ``sub`` is the only
    accepted identity and a token without one is not authenticated: falling back
    to ``preferred_username`` or ``email`` would persist a mutable or
    self-asserted string into ACLs that have no rename path. Otherwise the
    legacy precedence applies unchanged, so an existing deployment is
    unaffected. See ``frames.identity`` for why this is per-deployment.

    Human-readable identifiers never reach this return value under the pin; they
    are carried separately on :class:`DisplayIdentity`.
    """

    if identity_pinned_to_sub():
        value = claims.get(PINNED_IDENTITY_CLAIM)
        if isinstance(value, str) and value:
            return value
        return None

    for key in LEGACY_IDENTITY_CLAIM_PRECEDENCE:
        value = claims.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def display_identity_from_claims(claims: dict) -> DisplayIdentity:
    """Collect the caller's display-only labels.

    Separate from :func:`user_from_claims` on purpose — these claims are the
    ones that must *not* become principals, so nothing that resolves them is
    shared with the code path that resolves the principal.
    """

    return DisplayIdentity(
        name=_claim_value(claims, DISPLAY_CLAIMS),
        email=_claim_value(claims, ("email",)),
        # `is True`, not truthiness: an IdP that renders claims as strings
        # sends "false", which is a non-empty string and therefore truthy.
        # Anything that is not the boolean true is treated as unverified.
        email_verified=claims.get("email_verified") is True,
    )


def _claim_value(claims: dict, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = claims.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _env_value(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def auth_context_from_claims(claims: dict) -> AuthContext | None:
    """Build workspace-scoped auth context from accepted JWT claims.

    The ``claims`` org source only (see ``frames.org_source``): org and
    workspace come from the token, with the ``FRAMES_AUTH_DEFAULT_*``
    fallbacks. Membership-resolving deployments retire both — see
    :func:`auth_context_from_membership`.
    """

    user = user_from_claims(claims)
    if not user:
        return None
    org_id = _claim_value(claims, ("org_id", "organization_id", "org")) or _env_value("FRAMES_AUTH_DEFAULT_ORG")
    workspace_id = _claim_value(claims, ("workspace_id", "workspace")) or _env_value(
        "FRAMES_AUTH_DEFAULT_WORKSPACE"
    )
    if not org_id or not workspace_id:
        return None
    return AuthContext(
        user=user,
        home_org_id=org_id,
        workspace_id=workspace_id,
        display=display_identity_from_claims(claims),
    )


def auth_context_from_membership(
    claims: dict,
    org_store: OrgStore,
    auto_admit: SingleOrgDeclaration | None = None,
) -> AuthContext | None:
    """Resolve the caller's organization from their ``collab_org_members`` row.

    Identity is still the verified token's subject (the identity pin is a
    startup precondition of this mode). Everything organizational comes from
    the server's own tables: ``(org_id, org_role)`` from the one home-org row,
    ``workspace_id`` from :data:`WORKSPACE_DEFAULT`. Any ``org_id`` or
    ``workspace_id`` claim in the token is **ignored** — the whole point is
    that a caller cannot assert their own tenancy.

    Four outcomes, deliberately distinguishable:

    * ``None`` — the token carries no acceptable identity. The caller turns
      this into a 401, exactly as on the claims path.
    * **An org-scoped context** — an active membership row: ``(org_id,
      org_role)`` from it, plus the platform role resolved in the same store
      read.
    * **A hub-scoped context** — no active membership, but an **active
      platform role** (issue #89's bootstrap: on a fresh deployment the
      operator's organization does not exist until their first invitation is
      accepted). ``home_org_id`` is ``None`` and ``org_role`` is ``None``:
      the operator is authenticated and holds platform authority, while every
      org-scoped surface fails closed through the :attr:`AuthContext.org_id`
      property. A *removed* membership grants no org context here either —
      the platform axis stands on its own, and revoking an operator means
      revoking the platform role, not their membership.
    * :class:`NoOrganizationError` — authenticated, but neither an active
      membership nor an active platform role. A removed row stays bound to
      its organization but grants nothing, so removal takes effect on the
      very next request.

    **Anything the store raises propagates untouched.** A Postgres outage
    must surface as 503 ``database_unavailable`` — never a 401 (which would
    sign every user out of a healthy deployment over a transient database
    blip) and never ``no_organization`` (the membership is *unknown*, not
    absent). This path fails closed and holds no cache; see
    :class:`~.orgs.PostgresOrgStore`.

    What removal does and does not do: dropping ``internal`` access is
    mechanical — ``internal`` frames are scoped by ``org_id``, and a removed
    caller no longer has one. An **explicit** ``readers`` grant naming that
    subject is untouched by this, because reader grants are user-scoped by
    design and are not evaluated against organization membership. Removal is
    therefore not total revocation, and must not be described as such.

    **Platform roles are the second axis, resolved here alongside membership**
    (issue #87). An organization owner has authority inside one organization; a
    deployment operator has authority across all of them, which ``org_role``
    cannot express. Both axes come back from one store read
    (:meth:`~.orgs.OrgStore.resolve_principal` — one connection, one round
    trip), so the answer is never partial: a store failure fails the whole
    request closed rather than resolving a membership whose platform role is
    unknown, which nothing downstream could tell from "not an operator".

    **Auto-admission (the ``single`` org source, issue #91).** With an
    *auto_admit* declaration, a caller with **no membership row at all** whose
    token names a declared identity source gets a real ``member`` row written
    right here, before the outcomes above are decided — resolution, not a
    side effect, because every outcome below reads the row it creates. Three
    boundaries hold it in shape: an existing row of any kind wins (a
    ``removed`` row is never resurrected — removal keeps taking effect on the
    next request — and an active row in another organization is never moved);
    a token without the :data:`~.org_source.IDENTITY_PROVIDER_CLAIM`, or
    naming an undeclared provider, falls through to exactly the outcomes a
    membership-mode caller gets; and the write itself fails closed with the
    store, never best-effort. A platform operator signing in through a
    declared source is admitted like anyone else — membership and the platform
    axis remain independent.
    """

    user = user_from_claims(claims)
    if not user:
        return None
    principal = org_store.resolve_principal(user)
    membership = principal.membership
    display = display_identity_from_claims(claims)
    if membership is None and auto_admit is not None:
        membership = _auto_admit_member(claims, user, display, org_store, auto_admit)
    if membership is not None and membership.is_active:
        return AuthContext(
            user=user,
            home_org_id=membership.org_id,
            workspace_id=WORKSPACE_DEFAULT,
            display=display,
            org_role=membership.role,
            platform_role=principal.platform_role,
        )
    if principal.platform_role is not None:
        # The hub-scoped operator shape; see the docstring. org_role is None —
        # platform authority never manufactures org authority — and every
        # org-scoped read of this context raises through the org_id property.
        return AuthContext(
            user=user,
            home_org_id=None,
            workspace_id=WORKSPACE_DEFAULT,
            display=display,
            org_role=None,
            platform_role=principal.platform_role,
        )
    raise NoOrganizationError()


def _auto_admit_member(
    claims: dict,
    user: str,
    display: DisplayIdentity,
    org_store: OrgStore,
    declaration: SingleOrgDeclaration,
):
    """Write the declared organization's membership row for a declared sign-in.

    Returns the row now standing, or ``None`` when this token does not qualify
    — no :data:`~.org_source.IDENTITY_PROVIDER_CLAIM`, or an undeclared alias.
    Absence and mismatch fail closed identically on purpose: a realm missing
    the session-note mapper must not be distinguishable, to a caller, from a
    login that is simply not eligible.
    """

    provider = claims.get(IDENTITY_PROVIDER_CLAIM)
    if not isinstance(provider, str) or provider not in declaration.member_sources:
        return None
    membership, created = org_store.provision_member(
        user,
        declaration.org_id,
        declaration.org_name,
        # The email column mirrors invitation acceptance's discipline: stored
        # only when the IdP asserted it verified, otherwise it is a
        # self-asserted string with no place in a member roster.
        email=display.email if display.email_verified else None,
        display_name=display.name,
    )
    if created:
        auth_logger.info(
            "single_org_member_admitted",
            extra={"user": user, "org": declaration.org_id, "identity_provider": provider},
        )
    return membership


def _request_org_store(request: Request) -> OrgStore:
    """Return the org store owned by the app serving this request, failing closed.

    Read off ``request.app.state`` for the same reason the usage store is: the
    mounted MCP app has its own state object, and nothing here may hold global
    state. An app whose state has no store denies rather than falling back to
    claims — ``make_app`` will not start membership mode without one, so this
    only fires for state assembled some other way, and a silent fallback there
    would be a tenancy bypass.
    """

    store = getattr(getattr(request.app, "state", None), "org_store", None)
    if store is None:
        raise OrgsUnavailableError("Organization storage is not available on this app")
    return store


def resolve_auth_context(request: Request, claims: dict) -> AuthContext | None:
    """Build the auth context for accepted claims via the configured org source."""

    if org_source_resolves_membership():
        return auth_context_from_membership(
            claims, _request_org_store(request), auto_admit=single_org_declaration()
        )
    return auth_context_from_claims(claims)


def record_user_seen(request: Request, auth_context: AuthContext) -> AuthContext:
    """Best-effort usage capture of an authenticated caller.

    Reads the usage store off the app owning the request (the FastAPI app for
    HTTP routes, the mounted MCP app for MCP traffic) so nothing here holds
    global state. Never raises: a usage-write failure must not break auth.
    """

    state = getattr(request.app, "state", None)
    usage_store = getattr(state, "usage_store", None)
    if usage_store is None:
        return auth_context
    if auth_context.home_org_id is None:
        # The hub-scoped operator shape: usage rows are org-scoped rosters,
        # and an operator acting outside any organization has no row to
        # write. Skipped explicitly rather than letting the org_id property
        # raise into the except below, which would log a spurious
        # usage-write failure on every hub-scoped request.
        return auth_context
    try:
        usage_store.record_user_seen(
            auth_context.org_id,
            auth_context.workspace_id,
            auth_context.user,
            auth_context.email,
        )
    except Exception:
        from .observability import USAGE_WRITE_FAILURES

        USAGE_WRITE_FAILURES.labels(kind="user_seen").inc()
        usage_logger.exception(
            "usage_user_seen_write_failed",
            extra={"user": auth_context.user},
        )
    return auth_context


AUTH_CONTEXT_SCOPE_KEY = "frames_server_auth_context"
"""ASGI scope key holding the context already resolved for *this* request.

Authentication runs more than once per request by construction: the
path-protection middleware authenticates before routing, and then the route's
own ``Depends(get_auth_context)`` authenticates again. That was merely
duplicated signature verification before; under membership resolution it would
be a second Postgres round trip on every protected request. The resolved
context is therefore memoized in the request's ASGI scope — created by the
server per request and never shared between them, so this cannot leak one
caller's context into another's request, and it is not reachable from anything
a client sends.

Only successful resolutions are stored. A denial (401, ``no_organization``, a
database error) aborts the request where it is raised, so there is nothing to
reuse, and re-raising on a later call would be indistinguishable anyway.
"""


def _credential_claims(request: Request) -> tuple[dict, str] | None:
    """The claims of whichever credential this request carries, if any.

    Returns ``(claims, detail)`` where ``detail`` is the 401 message to use
    when those claims yield no usable identity — the two credential kinds
    keep their distinct messages. ``None`` means the request carries neither,
    which is the caller's cue to try the dev shortcut.

    Extracted so :func:`get_auth_context` and :func:`get_caller_identity`
    accept exactly the same credentials on exactly the same terms. Two
    hand-copied extraction paths would be one JWKS or cookie-name change away
    from a surface that authenticates differently from the rest of the app.
    """

    id_token = get_id_token(request)
    if id_token:
        try:
            return decode_id_token_payload(id_token), "Invalid IdToken cookie"
        except TokenDecodeError:
            return {}, "Invalid IdToken cookie"

    bearer_token = get_bearer_token(request)
    if bearer_token:
        try:
            return decode_bearer_payload(bearer_token), "Invalid bearer token"
        except TokenDecodeError:
            return {}, "Invalid bearer token"

    return None


def _dev_auth_user() -> str | None:
    """The local/dev shortcut's subject, or ``None`` when it is not enabled."""

    dev_user = os.environ.get("DEV_AUTH_USER")
    if unsafe_auth_enabled() and os.environ.get("DEV_AUTH_ENABLED") == "true" and dev_user:
        return dev_user
    return None


@dataclass(frozen=True)
class CallerIdentity:
    """Who is calling, with **nothing** resolved about their organization.

    The weaker of the two authenticated shapes, and the only one invitation
    acceptance can use: an invitee has no membership row and no platform
    role, which is precisely the state
    :func:`auth_context_from_membership` answers with
    :class:`NoOrganizationError`. Asking that path about them would refuse the
    one request the whole invitation flow exists to serve.

    It carries no ``org_id`` — not even an optional one — so nothing
    org-scoped can be reached with it by accident: a surface that needs an
    organization cannot take this object at all. Holding it grants nothing on
    its own; every route that accepts it does its own authorization
    afterwards (for acceptance: holding the secret, and controlling the
    verified mailbox it was sent to).
    """

    user: str
    display: DisplayIdentity = DisplayIdentity()


CALLER_IDENTITY_SCOPE_KEY = "frames_server_caller_identity"
"""ASGI scope key memoizing the identity resolved for *this* request.

Same construction and the same reasoning as
:data:`AUTH_CONTEXT_SCOPE_KEY`: identity-level authentication also runs twice
per request (the path-protection middleware, then the route dependency), and
each run is a signature verification.
"""


def get_caller_identity(request: Request) -> CallerIdentity:
    """Authenticate the caller **without** resolving their organization.

    Same credentials, same verification, same 401s as
    :func:`get_auth_context` — the difference is only that membership is
    never looked up, so no ``no_organization`` refusal and no database read
    stands between an invitee and the accept endpoint.

    A request that already resolved a full :class:`AuthContext` reuses it
    rather than re-verifying: an operator or an existing member hitting the
    accept endpoint is authenticated the same way as anyone else.
    """

    cached = request.scope.get(CALLER_IDENTITY_SCOPE_KEY)
    if isinstance(cached, CallerIdentity):
        return cached
    context = request.scope.get(AUTH_CONTEXT_SCOPE_KEY)
    if isinstance(context, AuthContext):
        return CallerIdentity(user=context.user, display=context.display)

    credential = _credential_claims(request)
    if credential is not None:
        claims, detail = credential
        user = user_from_claims(claims)
        if user:
            identity = CallerIdentity(user=user, display=display_identity_from_claims(claims))
            request.scope[CALLER_IDENTITY_SCOPE_KEY] = identity
            return identity
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)

    dev_user = _dev_auth_user()
    if dev_user:
        # No claims to read, so no verified email: the dev shortcut can reach
        # the accept endpoint and will be refused by its email check, which
        # is the correct outcome for a subject nobody's IdP vouched for.
        identity = CallerIdentity(user=dev_user)
        request.scope[CALLER_IDENTITY_SCOPE_KEY] = identity
        return identity

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
    )


def get_auth_context(request: Request) -> AuthContext:
    """Resolve authenticated caller and workspace scope from gateway or dev auth."""

    cached = request.scope.get(AUTH_CONTEXT_SCOPE_KEY)
    if isinstance(cached, AuthContext):
        return cached

    credential = _credential_claims(request)
    if credential is not None:
        claims, detail = credential
        # NoOrganizationError and store errors deliberately propagate rather
        # than collapsing into the 401 below: "authenticated but unaffiliated"
        # (403 no_organization) and "membership unknown" (503) must stay
        # distinguishable from "this token is not valid" (401).
        auth_context = resolve_auth_context(request, claims)
        if auth_context:
            return _remember(request, auth_context)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)

    # One predicate for the dev shortcut, shared with get_caller_identity: two
    # hand-copied spellings of "is dev auth on" would be one environment-flag
    # change away from the two entry points authenticating differently.
    dev_user = _dev_auth_user()
    if dev_user:
        # The local/dev shortcut names its own org through DEV_AUTH_ORG, which
        # is precisely the claim-asserted tenancy membership mode exists to
        # stop honoring — so in membership mode the dev user is resolved
        # through the same membership lookup as anyone else (a dev user with no
        # row gets no_organization, not a private organization of one). Keeping
        # a bypass here would leave a tenancy escape hatch behind a single
        # environment variable.
        if org_source_resolves_membership():
            # No identity_provider claim is minted for the dev subject, so on a
            # single-org deployment the dev user is not auto-admitted either:
            # a shortcut nobody's IdP vouched for must not mint membership.
            auth_context = auth_context_from_membership(
                {PINNED_IDENTITY_CLAIM: dev_user},
                _request_org_store(request),
            )
        else:
            auth_context = AuthContext(
                user=dev_user,
                home_org_id=os.environ.get("DEV_AUTH_ORG", "dev-org"),
                workspace_id=os.environ.get("DEV_AUTH_WORKSPACE", "default"),
            )
        if auth_context:
            return _remember(request, auth_context)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
    )


def _remember(request: Request, auth_context: AuthContext) -> AuthContext:
    """Record usage for a freshly resolved context and memoize it on the request."""

    request.scope[AUTH_CONTEXT_SCOPE_KEY] = auth_context
    return record_user_seen(request, auth_context)


def get_current_user(request: Request) -> str:
    """Resolve the authenticated user id for legacy call sites."""

    return get_auth_context(request).user
