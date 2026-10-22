"""JWKS client caching and key-rotation behavior for verified JWT decoding.

These exercise the real network path: a threaded HTTP server serves the JWK
set, so single-flight fetching, socket timeouts, and outages behave here the
way they do against an IdP. Only the clock is faked.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from collab_hub_api.frames import auth


def _make_key(kid: str) -> tuple[bytes, dict]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": kid, "alg": "RS256", "use": "sig"})
    return private_pem, jwk


# Key generation is slow enough to matter; every test reuses these two.
KEY_1_PEM, KEY_1_JWK = _make_key("key-1")
KEY_2_PEM, KEY_2_JWK = _make_key("key-2")

# A third key reusing key-1's id, for same-``kid`` replacement.
KEY_1B_PEM, KEY_1B_JWK = _make_key("key-1")


def _token(private_pem: bytes, kid: str, user: str = "signed-user") -> str:
    return jwt.encode({"preferred_username": user}, private_pem, algorithm="RS256", headers={"kid": kid})


class _JWKSEndpoint:
    """A real HTTP JWKS endpoint whose response and health are swappable."""

    def __init__(self) -> None:
        self.keys = [KEY_1_JWK]
        self.fetches = 0
        self.status = 200
        self.body: bytes | None = None
        """Raw response body, when a test needs one ``keys`` cannot express."""
        self.hanging = False
        """While true, requests stall instead of responding, as a hung IdP does."""
        self._release = threading.Event()
        self._lock = threading.Lock()

        endpoint = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                with endpoint._lock:
                    endpoint.fetches += 1
                    status, body, hanging = endpoint.status, endpoint._payload(), endpoint.hanging
                if hanging:
                    # Outlast any timeout under test, but never wedge teardown.
                    endpoint._release.wait(timeout=30)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:  # keep pytest output clean
                pass

        class _Server(ThreadingHTTPServer):
            # A released-late response writes to a socket the client already
            # gave up on; that is the scenario, not a test failure.
            def handle_error(self, *args) -> None:
                pass

        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/certs"

    def _payload(self) -> bytes:
        if self.body is not None:
            return self.body
        return json.dumps({"keys": self.keys}).encode()

    def hang(self) -> None:
        self._release.clear()
        self.hanging = True

    def resume(self) -> None:
        self.hanging = False
        self._release.set()

    def close(self) -> None:
        self.resume()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@pytest.fixture
def jwks(monkeypatch):
    """A live JWKS endpoint, isolated from the module-level client registry."""

    endpoint = _JWKSEndpoint()
    monkeypatch.setitem(auth.__dict__, "_jwks_clients", {})
    try:
        yield endpoint
    finally:
        endpoint.close()


@pytest.fixture
def clock(monkeypatch):
    """A monotonic clock the tests advance, so no test sleeps out an interval."""

    now = [10_000.0]

    def fake_monotonic() -> float:
        return now[0]

    def advance(seconds: float) -> None:
        now[0] += seconds

    # The throttle and PyJWT's cache expiry both read ``time.monotonic`` off
    # the module, so one patch keeps the two clocks consistent.
    monkeypatch.setattr(auth.time, "monotonic", fake_monotonic)
    return advance


def _decode(token: str, jwks_url: str) -> dict:
    return auth.decode_verified_jwt(token, jwks_url=jwks_url, issuer=None, audience=None)


def _client(jwks_url: str):
    return auth._jwks_clients[jwks_url]


def _concurrently(callers: int, work) -> list:
    """Run *work* on *callers* threads released together, and collect results."""

    ready = threading.Barrier(callers)

    def run():
        ready.wait(timeout=10)
        return work()

    with ThreadPoolExecutor(max_workers=callers) as pool:
        return [future.result(timeout=30) for future in [pool.submit(run) for _ in range(callers)]]


# --- steady state -----------------------------------------------------------


def test_steady_state_verification_fetches_jwks_once(jwks):
    token = _token(KEY_1_PEM, "key-1")

    for _ in range(3):
        assert _decode(token, jwks.url)["preferred_username"] == "signed-user"

    assert jwks.fetches == 1


def test_missing_jwks_url_is_a_decode_error():
    with pytest.raises(auth.TokenDecodeError):
        auth.decode_verified_jwt(_token(KEY_1_PEM, "key-1"), jwks_url=None, issuer=None, audience=None)


# --- IdToken cookie fallback inherits the bearer audience (issue #41) -------


def _audience_token(private_pem: bytes, kid: str, audience: str) -> str:
    return jwt.encode(
        {"preferred_username": "signed-user", "aud": audience},
        private_pem,
        algorithm="RS256",
        headers={"kid": kid},
    )


def test_id_token_fallback_inherits_the_bearer_audience(jwks, monkeypatch):
    # Bearer-only verifier config (no dedicated FRAMES_IDTOKEN_JWKS_URL):
    # cookie verification falls back to the bearer verifier and must inherit
    # its audience too — a same-realm token minted for a different audience
    # must not be accepted as a cookie, or the bearer audience restriction is
    # bypassed entirely.
    monkeypatch.delenv("FRAMES_IDTOKEN_JWKS_URL", raising=False)
    monkeypatch.delenv("FRAMES_IDTOKEN_ISSUER", raising=False)
    monkeypatch.delenv("FRAMES_IDTOKEN_AUDIENCE", raising=False)
    monkeypatch.setenv("FRAMES_BEARER_JWKS_URL", jwks.url)
    monkeypatch.setenv("FRAMES_BEARER_AUDIENCE", "apollo-desktop")

    accepted = auth.decode_id_token_payload(_audience_token(KEY_1_PEM, "key-1", "apollo-desktop"))
    assert accepted["preferred_username"] == "signed-user"

    with pytest.raises(auth.TokenDecodeError):
        auth.decode_id_token_payload(_audience_token(KEY_1_PEM, "key-1", "another-client"))


def test_id_token_own_audience_still_wins_over_bearer(jwks, monkeypatch):
    # Precedence pin, not a fix-discriminating test: FRAMES_IDTOKEN_AUDIENCE
    # was already honored when set, before and after this change (only the
    # no-dedicated-audience fallback changed). This guards that a dedicated
    # FRAMES_IDTOKEN_AUDIENCE, even without a dedicated FRAMES_IDTOKEN_JWKS_URL,
    # keeps winning over the bearer audience as the fallback path evolves —
    # the inherited-audience test above is the one that fails on the old code.
    monkeypatch.delenv("FRAMES_IDTOKEN_JWKS_URL", raising=False)
    monkeypatch.delenv("FRAMES_IDTOKEN_ISSUER", raising=False)
    monkeypatch.setenv("FRAMES_IDTOKEN_AUDIENCE", "browser-client")
    monkeypatch.setenv("FRAMES_BEARER_JWKS_URL", jwks.url)
    monkeypatch.setenv("FRAMES_BEARER_AUDIENCE", "apollo-desktop")

    accepted = auth.decode_id_token_payload(_audience_token(KEY_1_PEM, "key-1", "browser-client"))
    assert accepted["preferred_username"] == "signed-user"

    with pytest.raises(auth.TokenDecodeError):
        auth.decode_id_token_payload(_audience_token(KEY_1_PEM, "key-1", "apollo-desktop"))


def _issuer_token(private_pem: bytes, kid: str, issuer: str) -> str:
    return jwt.encode(
        {"preferred_username": "signed-user", "iss": issuer},
        private_pem,
        algorithm="RS256",
        headers={"kid": kid},
    )


def test_id_token_fallback_still_honors_a_dedicated_issuer(jwks, monkeypatch):
    # A shared JWKS with a distinct IdToken issuer worked before this change
    # and must keep working: the fallback path only gained audience
    # inheritance, it must not also start discarding FRAMES_IDTOKEN_ISSUER in
    # favor of the bearer issuer unconditionally (mirrors
    # identity.enforce_single_issuer_for_pin's derived-exactly contract).
    monkeypatch.delenv("FRAMES_IDTOKEN_JWKS_URL", raising=False)
    monkeypatch.delenv("FRAMES_IDTOKEN_AUDIENCE", raising=False)
    monkeypatch.setenv("FRAMES_IDTOKEN_ISSUER", "https://idp.example.com/realms/idtoken-realm")
    monkeypatch.setenv("FRAMES_BEARER_JWKS_URL", jwks.url)
    monkeypatch.setenv("FRAMES_BEARER_ISSUER", "https://idp.example.com/realms/bearer-realm")

    accepted = auth.decode_id_token_payload(
        _issuer_token(KEY_1_PEM, "key-1", "https://idp.example.com/realms/idtoken-realm")
    )
    assert accepted["preferred_username"] == "signed-user"

    with pytest.raises(auth.TokenDecodeError):
        auth.decode_id_token_payload(
            _issuer_token(KEY_1_PEM, "key-1", "https://idp.example.com/realms/bearer-realm")
        )


def test_separate_urls_do_not_share_a_client(jwks):
    other = _JWKSEndpoint()
    other.keys = [KEY_2_JWK]
    try:
        assert _decode(_token(KEY_1_PEM, "key-1"), jwks.url)
        assert _decode(_token(KEY_2_PEM, "key-2"), other.url)

        # Each URL fetched for itself; neither served the other's keys.
        assert jwks.fetches == 1
        assert other.fetches == 1
        with pytest.raises(auth.TokenDecodeError):
            _decode(_token(KEY_2_PEM, "key-2"), jwks.url)
    finally:
        other.close()


# --- concurrency: cold start and cache expiry -------------------------------


def test_cold_start_burst_makes_one_fetch(jwks):
    """The finding: 16 concurrent cold callers previously made 16 fetches."""

    token = _token(KEY_1_PEM, "key-1")

    results = _concurrently(16, lambda: _decode(token, jwks.url))

    assert all(result["preferred_username"] == "signed-user" for result in results)
    assert jwks.fetches == 1


def test_cache_expiry_burst_makes_one_fetch(jwks, clock):
    token = _token(KEY_1_PEM, "key-1")
    assert _decode(token, jwks.url)
    assert jwks.fetches == 1

    clock(auth.JWKS_CACHE_LIFESPAN_SECONDS + 1)

    assert all(_concurrently(16, lambda: _decode(token, jwks.url)))
    assert jwks.fetches == 2


def test_unknown_kid_burst_makes_one_extra_fetch(jwks):
    assert _decode(_token(KEY_1_PEM, "key-1"), jwks.url)
    assert jwks.fetches == 1

    forged = _token(KEY_2_PEM, "rogue-kid")

    def verify() -> bool:
        with pytest.raises(auth.TokenDecodeError):
            _decode(forged, jwks.url)
        return True

    assert all(_concurrently(16, verify))
    assert jwks.fetches == 2


# --- key rotation -----------------------------------------------------------


def test_rotated_key_is_picked_up_without_restart(jwks):
    assert _decode(_token(KEY_1_PEM, "key-1"), jwks.url)
    assert jwks.fetches == 1

    jwks.keys = [KEY_1_JWK, KEY_2_JWK]
    rotated = _token(KEY_2_PEM, "key-2")

    assert _decode(rotated, jwks.url)["preferred_username"] == "signed-user"
    assert jwks.fetches == 2

    # The rotated key is now cached; verifying again stays off the network.
    assert _decode(rotated, jwks.url)
    assert jwks.fetches == 2


def test_retired_key_is_rejected_after_the_set_is_refetched(jwks, clock):
    retired = _token(KEY_1_PEM, "key-1")
    assert _decode(retired, jwks.url)

    jwks.keys = [KEY_2_JWK]
    clock(auth.JWKS_CACHE_LIFESPAN_SECONDS + 1)

    # Removal takes effect on the next fetch — the old key is not kept around
    # by the last-known-good path once a usable set replaces it.
    with pytest.raises(auth.TokenDecodeError):
        _decode(retired, jwks.url)
    assert _decode(_token(KEY_2_PEM, "key-2"), jwks.url)


def test_same_kid_replacement_is_picked_up_on_expiry(jwks, clock):
    """An IdP that reuses a ``kid`` for new key material is not detectable by
    ``kid`` alone, so the old key stays valid until the cache expires."""

    assert _decode(_token(KEY_1_PEM, "key-1"), jwks.url)

    jwks.keys = [KEY_1B_JWK]
    replaced = _token(KEY_1B_PEM, "key-1")

    # No unknown ``kid``, so nothing forces a refresh: the stale key is served.
    with pytest.raises(auth.TokenDecodeError):
        _decode(replaced, jwks.url)
    assert jwks.fetches == 1

    clock(auth.JWKS_CACHE_LIFESPAN_SECONDS + 1)
    assert _decode(replaced, jwks.url)["preferred_username"] == "signed-user"
    assert jwks.fetches == 2


# --- forced-refresh throttle ------------------------------------------------


def test_unknown_kid_refetch_is_rate_limited(jwks):
    assert _decode(_token(KEY_1_PEM, "key-1"), jwks.url)
    assert jwks.fetches == 1

    forged = _token(KEY_2_PEM, "rogue-kid")

    with pytest.raises(auth.TokenDecodeError):
        _decode(forged, jwks.url)
    assert jwks.fetches == 2

    for _ in range(5):
        with pytest.raises(auth.TokenDecodeError):
            _decode(forged, jwks.url)
    assert jwks.fetches == 2

    # Legitimate tokens keep verifying from cache while forged ones are throttled.
    assert _decode(_token(KEY_1_PEM, "key-1"), jwks.url)
    assert jwks.fetches == 2


def test_forced_refresh_allowed_again_after_interval(jwks, clock):
    assert _decode(_token(KEY_1_PEM, "key-1"), jwks.url)

    forged = _token(KEY_2_PEM, "rogue-kid")
    with pytest.raises(auth.TokenDecodeError):
        _decode(forged, jwks.url)
    assert jwks.fetches == 2

    jwks.keys = [KEY_1_JWK, KEY_2_JWK]
    rotated = _token(KEY_2_PEM, "key-2")

    # Still inside the throttle: a rotation landing right after a forged-kid
    # burst waits, and is rejected meanwhile.
    with pytest.raises(auth.TokenDecodeError):
        _decode(rotated, jwks.url)
    assert jwks.fetches == 2

    clock(auth.JWKS_FORCED_REFRESH_MIN_INTERVAL_SECONDS + 1)

    assert _decode(rotated, jwks.url)["preferred_username"] == "signed-user"
    assert jwks.fetches == 3


def test_rejection_window_is_bounded_by_the_documented_budget(jwks, clock, monkeypatch):
    """The worst case: a forged ``kid`` consumes the allowance immediately
    before a real key is published, and the fetch that follows times out."""

    assert _decode(_token(KEY_1_PEM, "key-1"), jwks.url)

    elapsed = [0.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: 10_000.0 + elapsed[0])

    with pytest.raises(auth.TokenDecodeError):
        _decode(_token(KEY_2_PEM, "rogue-kid"), jwks.url)

    jwks.keys = [KEY_1_JWK, KEY_2_JWK]
    rotated = _token(KEY_2_PEM, "key-2")

    # Wait out the throttle, charging the budget for the time spent.
    elapsed[0] += auth.JWKS_FORCED_REFRESH_MIN_INTERVAL_SECONDS
    # The refetch is allowed now, and costs at most one fetch timeout.
    elapsed[0] += auth.JWKS_FETCH_TIMEOUT_SECONDS

    assert _decode(rotated, jwks.url)["preferred_username"] == "signed-user"
    assert elapsed[0] <= auth.JWKS_MAX_REJECTION_WINDOW_SECONDS


def test_fetch_timeout_is_explicit(jwks):
    assert auth.JWKS_FETCH_TIMEOUT_SECONDS < 30  # PyJWT's implicit default
    assert _decode(_token(KEY_1_PEM, "key-1"), jwks.url)
    assert _client(jwks.url).timeout == auth.JWKS_FETCH_TIMEOUT_SECONDS


def test_hung_endpoint_releases_the_caller_at_the_timeout(jwks):
    """A hung IdP fails the fetch at the configured timeout rather than
    blocking the request thread for PyJWT's 30s default."""

    known_good = _token(KEY_1_PEM, "key-1")
    assert _decode(known_good, jwks.url)

    # Shortened so the test does not spend the real timeout hanging; what is
    # under test is that the client honors its own value rather than PyJWT's.
    _client(jwks.url).timeout = 0.25
    jwks.hang()
    jwks.keys = [KEY_1_JWK, KEY_2_JWK]

    started = time.perf_counter()
    with pytest.raises(auth.TokenDecodeError):
        _decode(_token(KEY_2_PEM, "key-2"), jwks.url)
    assert time.perf_counter() - started < 10

    # The hung refresh cost nothing: the last validated set still verifies.
    jwks.resume()
    assert _decode(known_good, jwks.url)["preferred_username"] == "signed-user"


def test_hung_forced_refresh_does_not_stall_cached_verification(jwks, clock):
    """A forced refresh out against a hung IdP holds no lock that a token
    verifiable from cache would wait on. PyJWT 2.15's own ``get_signing_key``
    does hold one across the fetch."""

    known_good = _token(KEY_1_PEM, "key-1")
    assert _decode(known_good, jwks.url)

    # Past PyJWT's own 30s refresh cooldown, so its forced refresh would run too.
    clock(auth.JWKS_FORCED_REFRESH_MIN_INTERVAL_SECONDS + 1)
    # Long enough that the hung fetch is still out whatever the scheduling
    # delay; ``resume`` ends it, not the timeout.
    _client(jwks.url).timeout = 30
    jwks.hang()

    with ThreadPoolExecutor(max_workers=2) as pool:
        forced = pool.submit(_decode, _token(KEY_2_PEM, "rogue-kid"), jwks.url)
        deadline = time.perf_counter() + 5
        while jwks.fetches < 2:
            assert time.perf_counter() < deadline, "the forced refresh never went out"
            time.sleep(0.01)

        cached = pool.submit(_decode, known_good, jwks.url)
        try:
            assert cached.result(timeout=10)["preferred_username"] == "signed-user"
            assert not forced.done()
        finally:
            jwks.resume()
        with pytest.raises(auth.TokenDecodeError):
            forced.result(timeout=10)


# --- bad responses must not replace a working key set -----------------------


@pytest.mark.parametrize(
    "name, mutate",
    [
        ("empty key set", lambda endpoint: setattr(endpoint, "keys", [])),
        ("not a JSON object", lambda endpoint: setattr(endpoint, "body", b"[]")),
        ("keys without a kid", lambda endpoint: setattr(endpoint, "keys", [{**KEY_2_JWK, "kid": ""}])),
        ("unusable key material", lambda endpoint: setattr(endpoint, "keys", [{"kty": "RSA", "kid": "key-9"}])),
    ],
)
def test_malformed_refresh_does_not_poison_the_cache(jwks, clock, name, mutate):
    """The finding: PyJWT caches the response body before validating it, so a
    successful-but-unusable 200 used to reject known-good tokens with no way
    back until the process restarted."""

    known_good = _token(KEY_1_PEM, "key-1")
    assert _decode(known_good, jwks.url)

    mutate(jwks)

    # Force a refresh with an unknown ``kid``; it fails, as it should.
    with pytest.raises(auth.TokenDecodeError):
        _decode(_token(KEY_2_PEM, "rogue-kid"), jwks.url)
    assert jwks.fetches == 2

    # The key that was valid before the bad response is still valid after it.
    assert _decode(known_good, jwks.url)["preferred_username"] == "signed-user"
    assert jwks.fetches == 2


def test_recovery_after_a_malformed_response(jwks, clock):
    known_good = _token(KEY_1_PEM, "key-1")
    assert _decode(known_good, jwks.url)

    jwks.keys = []
    with pytest.raises(auth.TokenDecodeError):
        _decode(_token(KEY_2_PEM, "rogue-kid"), jwks.url)

    # Endpoint restored, now publishing a rotated key.
    jwks.keys = [KEY_2_JWK]
    clock(auth.JWKS_FORCED_REFRESH_MIN_INTERVAL_SECONDS + 1)

    rotated = _token(KEY_2_PEM, "key-2")
    assert _decode(rotated, jwks.url)["preferred_username"] == "signed-user"


def test_outage_keeps_serving_the_last_validated_set(jwks, clock):
    known_good = _token(KEY_1_PEM, "key-1")
    assert _decode(known_good, jwks.url)

    jwks.status = 500

    with pytest.raises(auth.TokenDecodeError):
        _decode(_token(KEY_2_PEM, "rogue-kid"), jwks.url)

    # A 500 during the forced refresh does not cost the known-good key...
    assert _decode(known_good, jwks.url)

    # ...nor does one at cache expiry, when nothing forced the refresh.
    clock(auth.JWKS_CACHE_LIFESPAN_SECONDS + 1)
    assert _decode(known_good, jwks.url)["preferred_username"] == "signed-user"


def test_connection_refused_keeps_serving_the_last_validated_set(jwks, clock):
    known_good = _token(KEY_1_PEM, "key-1")
    assert _decode(known_good, jwks.url)

    jwks.close()  # nothing is listening on the port any more

    clock(auth.JWKS_CACHE_LIFESPAN_SECONDS + 1)
    assert _decode(known_good, jwks.url)["preferred_username"] == "signed-user"


def test_cold_burst_against_a_hung_endpoint_costs_one_timeout(jwks):
    """The finding: waiters queued behind a failing fetch each ran their own on
    the way through, so a hung endpoint cost one timeout per caller, in series."""

    client = auth._get_jwks_client(jwks.url)
    # Shortened so the test does not sit out the real timeout; what is under
    # test is that 16 callers pay it once between them, not 16 times.
    client.timeout = 0.5
    jwks.hang()

    token = _token(KEY_1_PEM, "key-1")

    def verify() -> bool:
        with pytest.raises(auth.TokenDecodeError):
            _decode(token, jwks.url)
        return True

    started = time.perf_counter()
    assert all(_concurrently(16, verify))
    elapsed = time.perf_counter() - started

    assert jwks.fetches == 1
    assert elapsed < 4 * client.timeout


def test_cold_burst_shares_one_failed_fetch_and_retries_after_it(jwks):
    """Every caller in a failing cold burst fails closed off a single fetch,
    and the next request once that fetch has landed retries immediately."""

    jwks.keys = []  # a successful 200 carrying nothing usable
    jwks.hang()  # held until every caller is inside the flight
    token = _token(KEY_1_PEM, "key-1")
    callers = 16
    entered = threading.Semaphore(0)

    def verify() -> bool:
        entered.release()
        with pytest.raises(auth.TokenDecodeError):
            _decode(token, jwks.url)
        return True

    def release_once_everyone_is_in() -> None:
        for _ in range(callers):
            assert entered.acquire(timeout=10)
        while jwks.fetches < 1:
            time.sleep(0.01)
        time.sleep(0.05)  # the last caller's few instructions to the lock
        jwks.resume()

    releaser = threading.Thread(target=release_once_everyone_is_in)
    releaser.start()
    try:
        assert all(_concurrently(callers, verify))
    finally:
        releaser.join(timeout=15)

    assert jwks.fetches == 1

    # Nothing was cached and no interval has to pass: the endpoint coming back
    # is picked up by the very next request.
    jwks.keys = [KEY_1_JWK]
    assert _decode(token, jwks.url)["preferred_username"] == "signed-user"
    assert jwks.fetches == 2


# --- the last validated set does not outlive a withdrawn key ----------------


def test_sole_key_removal_takes_effect_within_the_stale_bound(jwks, clock):
    """The finding: reinstating the last validated set also refreshed its cache
    timestamp, so a key withdrawn from a JWKS response that never again carried
    a usable one kept verifying tokens for as long as the process ran."""

    withdrawn = _token(KEY_1_PEM, "key-1")
    assert _decode(withdrawn, jwks.url)

    jwks.keys = []  # the sole key is pulled; every response is unusable now

    # One failed refresh cycle is ridden out, the way an IdP blip should be.
    clock(auth.JWKS_CACHE_LIFESPAN_SECONDS + 1)
    assert _decode(withdrawn, jwks.url)

    # Now JWKS_MAX_STALE_SECONDS + 1 past the last confirmed fetch: the
    # fallback is dropped rather than renewed again.
    clock(auth.JWKS_MAX_STALE_SECONDS - auth.JWKS_CACHE_LIFESPAN_SECONDS)
    with pytest.raises(auth.TokenDecodeError):
        _decode(withdrawn, jwks.url)

    # ...and stays dropped, however many expiry cycles pass.
    for _ in range(3):
        clock(auth.JWKS_CACHE_LIFESPAN_SECONDS + 1)
        with pytest.raises(auth.TokenDecodeError):
            _decode(withdrawn, jwks.url)


def test_a_confirmed_fetch_restarts_the_stale_bound(jwks, clock):
    """The bound runs from the last fetch the endpoint confirmed, so an outage
    that ends inside it does not shorten the next one."""

    known_good = _token(KEY_1_PEM, "key-1")
    assert _decode(known_good, jwks.url)

    jwks.status = 500
    clock(auth.JWKS_CACHE_LIFESPAN_SECONDS + 1)
    assert _decode(known_good, jwks.url)  # inside the bound, still served

    jwks.status = 200
    clock(auth.JWKS_MAX_STALE_SECONDS - auth.JWKS_CACHE_LIFESPAN_SECONDS)
    assert _decode(known_good, jwks.url)  # the endpoint confirms the set again

    jwks.status = 500
    clock(auth.JWKS_CACHE_LIFESPAN_SECONDS + 1)
    assert _decode(known_good, jwks.url)["preferred_username"] == "signed-user"


def test_stale_bound_is_a_documented_multiple_of_the_lifespan(jwks):
    assert auth.JWKS_MAX_STALE_SECONDS == 2 * auth.JWKS_CACHE_LIFESPAN_SECONDS


def test_first_fetch_failure_has_no_set_to_fall_back_to(jwks):
    jwks.keys = []

    with pytest.raises(auth.TokenDecodeError):
        _decode(_token(KEY_1_PEM, "key-1"), jwks.url)

    # Once the endpoint is healthy the client picks it up on the next request:
    # a failed first fetch caches nothing.
    jwks.keys = [KEY_1_JWK]
    assert _decode(_token(KEY_1_PEM, "key-1"), jwks.url)["preferred_username"] == "signed-user"


def test_a_dedicated_id_token_verifier_is_used_when_configured(jwks, monkeypatch):
    # The dedicated-verifier path is unchanged by the fallback work: with
    # FRAMES_IDTOKEN_JWKS_URL set, cookie verification uses the dedicated
    # verifier and its own audience — the bearer audience plays no part, so a
    # token minted for the bearer client is refused even though the bearer
    # config would have accepted it.
    monkeypatch.setenv("FRAMES_IDTOKEN_JWKS_URL", jwks.url)
    monkeypatch.delenv("FRAMES_IDTOKEN_ISSUER", raising=False)
    monkeypatch.setenv("FRAMES_IDTOKEN_AUDIENCE", "browser-client")
    monkeypatch.delenv("FRAMES_BEARER_JWKS_URL", raising=False)
    monkeypatch.setenv("FRAMES_BEARER_AUDIENCE", "apollo-desktop")

    accepted = auth.decode_id_token_payload(_audience_token(KEY_1_PEM, "key-1", "browser-client"))
    assert accepted["preferred_username"] == "signed-user"

    with pytest.raises(auth.TokenDecodeError):
        auth.decode_id_token_payload(_audience_token(KEY_1_PEM, "key-1", "apollo-desktop"))


# ---------------------------------------------------------------------------
# Startup https requirement for the JWKS URLs (issue #77)
# ---------------------------------------------------------------------------
#
# The two URLs are handed straight to PyJWKClient, which fetches over whatever
# scheme they carry. Signing keys fetched over cleartext http can be substituted
# by an on-path attacker, so make_app refuses to start on a non-https URL —
# except under FRAMES_UNSAFE_AUTH_ENABLED=true (local development), where http
# is permitted and logged loudly.


def _config(tmp_path):
    from collab_hub_api.config import Config

    return Config.parse(
        {
            "storage": {"frames_path": str(tmp_path / "frames")},
            "frames": {
                "active_state": {"backend": "memory"},
                "history": {"backend": "memory"},
                "groups": {"backend": "memory"},
                "usage": {"backend": "memory"},
                "mcp_session_manager_enabled": False,
            },
            "tasks": {"backend": "memory"},
        }
    )


@pytest.fixture
def _clean_jwks_env(monkeypatch):
    for env_name in auth.JWKS_URL_ENVS:
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.delenv("FRAMES_UNSAFE_AUTH_ENABLED", raising=False)


@pytest.mark.parametrize("env_name", auth.JWKS_URL_ENVS)
def test_https_jwks_url_is_accepted(_clean_jwks_env, monkeypatch, env_name):
    monkeypatch.setenv(env_name, "https://keycloak.internal/realms/nebari/protocol/openid-connect/certs")
    auth.enforce_https_jwks_urls()


def test_unset_jwks_urls_pass_vacuously(_clean_jwks_env):
    # A missing URL is "this verifier is not configured", which already fails
    # closed per token — not a startup error.
    auth.enforce_https_jwks_urls()


@pytest.mark.parametrize("env_name", auth.JWKS_URL_ENVS)
def test_http_jwks_url_is_rejected(_clean_jwks_env, monkeypatch, env_name):
    monkeypatch.setenv(env_name, "http://keycloak.internal/realms/nebari/protocol/openid-connect/certs")
    with pytest.raises(RuntimeError, match=f"{env_name} must be an https:// URL"):
        auth.enforce_https_jwks_urls()


def test_non_http_scheme_is_rejected_even_under_unsafe_auth(_clean_jwks_env, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_BEARER_JWKS_URL", "ftp://keycloak.internal/certs")
    with pytest.raises(RuntimeError, match="must be an https:// URL"):
        auth.enforce_https_jwks_urls()


def test_http_jwks_url_is_allowed_under_unsafe_auth_and_logged(_clean_jwks_env, monkeypatch, caplog):
    # The dev exception: the same door the unsigned-token shortcuts sit behind,
    # and it announces itself.
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_BEARER_JWKS_URL", "http://localhost:8080/realms/nebari/protocol/openid-connect/certs")
    with caplog.at_level("WARNING", logger="frames_server.auth"):
        auth.enforce_https_jwks_urls()
    assert any("UNSAFE" in record.message for record in caplog.records)


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@keycloak.internal/certs",
        "https://user@keycloak.internal/certs",
    ],
)
def test_jwks_url_with_userinfo_is_rejected(_clean_jwks_env, monkeypatch, url):
    monkeypatch.setenv("FRAMES_IDTOKEN_JWKS_URL", url)
    with pytest.raises(RuntimeError, match="userinfo"):
        auth.enforce_https_jwks_urls()


def test_jwks_url_with_fragment_is_rejected(_clean_jwks_env, monkeypatch):
    monkeypatch.setenv("FRAMES_BEARER_JWKS_URL", "https://keycloak.internal/certs#frag")
    with pytest.raises(RuntimeError, match="fragment"):
        auth.enforce_https_jwks_urls()


def test_userinfo_is_rejected_even_under_unsafe_auth(_clean_jwks_env, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_BEARER_JWKS_URL", "http://user:secret@localhost:8080/certs")
    with pytest.raises(RuntimeError, match="userinfo"):
        auth.enforce_https_jwks_urls()


def test_jwks_url_without_hostname_is_rejected(_clean_jwks_env, monkeypatch):
    monkeypatch.setenv("FRAMES_BEARER_JWKS_URL", "https:///certs")
    with pytest.raises(RuntimeError, match="no hostname"):
        auth.enforce_https_jwks_urls()


def test_jwks_url_with_invalid_port_is_rejected(_clean_jwks_env, monkeypatch):
    monkeypatch.setenv("FRAMES_BEARER_JWKS_URL", "https://keycloak.internal:not-a-port/certs")
    with pytest.raises(RuntimeError):
        auth.enforce_https_jwks_urls()


def test_startup_rejects_a_cleartext_jwks_url(_clean_jwks_env, tmp_path, monkeypatch):
    # The whole point: the misconfiguration fails the rollout, visibly, rather
    # than silently fetching signing keys over cleartext.
    from collab_hub_api.core import make_app

    monkeypatch.setenv("FRAMES_BEARER_JWKS_URL", "http://keycloak.internal/certs")
    with pytest.raises(RuntimeError, match="must be an https:// URL"):
        make_app(_config(tmp_path))


def test_startup_accepts_https_jwks_urls(_clean_jwks_env, tmp_path, monkeypatch):
    from collab_hub_api.core import make_app

    monkeypatch.setenv("FRAMES_BEARER_JWKS_URL", "https://keycloak.internal/certs")
    monkeypatch.setenv("FRAMES_IDTOKEN_JWKS_URL", "https://keycloak.internal/certs")
    make_app(_config(tmp_path))
