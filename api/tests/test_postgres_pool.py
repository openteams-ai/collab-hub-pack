"""Behavior of the shared psycopg connection pools.

Covers the acceptance criteria beyond what test_postgres_auto_migration
asserts (stores never open direct per-request connections):

- one pool is shared per database URL;
- Postgres down and pool exhaustion both surface as ``PoolTimeout`` after the
  configured bounded wait — never an indefinite hang — and the app maps that
  to an explicit 503 ``database_unavailable`` response;
- a *healthy but saturated* pool queues waiters, bounds that queue
  (``max_waiting`` → ``TooManyRequests``), and keeps best-effort usage capture
  off the critical path so authenticated HTTP and MCP traffic stay responsive;
- a *healthy but stalled* server — one that answers the checkout and then never
  answers the statement — likewise delays neither authenticated, MCP, nor
  unrelated traffic, because best-effort writes do not run on request threads
  at all;
- the cancel that bounds such a stalled statement stays aimed at it: a
  connection never goes back to the pool with a cancel still in flight, so the
  next borrower's query cannot be killed by the previous borrower's watchdog;
- ``/health/db`` reports pool status without leaking connection URLs.

The saturation and stall tests run against ``stub_postgres_server``, a real
listener that completes the psycopg handshake, so the pool genuinely hands out,
queues, and blocks on connections. Real-Postgres integration coverage (schema,
migrations, query behavior) is deferred to a separate harness — there is no
Postgres service in CI today.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

psycopg = pytest.importorskip("psycopg")

from psycopg_pool import PoolTimeout, TooManyRequests  # noqa: E402
from stub_postgres_server import StubPostgresServer  # noqa: E402

from collab_hub_api.config import Config  # noqa: E402
from collab_hub_api.core import make_app  # noqa: E402
from collab_hub_api.frames import best_effort, usage  # noqa: E402
from collab_hub_api.frames import db as frames_db  # noqa: E402
from collab_hub_api.frames.best_effort import BestEffortWriter, InlineBestEffortWriter  # noqa: E402
from collab_hub_api.frames.db import PostgresDatabase, PostgresPools  # noqa: E402
from collab_hub_api.frames.usage import PostgresUsageStore  # noqa: E402

# Port 9 (discard) has no listener, so connection attempts fail immediately
# and the pool gives up after its wait timeout instead of hanging the test.
UNREACHABLE_URL = "postgresql://nexus:secret@127.0.0.1:9/frames"
POOL_TIMEOUT_SECONDS = 0.25
# The wait a saturated pool would impose on callers without a best-effort
# budget. Kept long on purpose: the saturation tests assert nothing waits it.
SATURATED_POOL_TIMEOUT_SECONDS = 5.0
# Generous ceiling for "did not wait for the pool timeout" assertions; the
# real cost is BEST_EFFORT_TIMEOUT_SECONDS (50ms) plus scheduling noise.
RESPONSIVE_SECONDS = 1.0


def _jwt(payload: dict) -> str:
    def encode(part: dict) -> str:
        raw = json.dumps(part, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode(payload)}."


def auth_cookie(user: str, org: str = "org-a", workspace: str = "workspace-a") -> dict[str, str]:
    return {"IdToken-test": _jwt({"preferred_username": user, "org_id": org, "workspace_id": workspace})}


def _down_db_config(tmp_path) -> Config:
    return Config.parse(
        {
            "storage": {"frames_path": str(tmp_path / "frames")},
            "frames": {
                "postgres": {
                    "url": UNREACHABLE_URL,
                    "pool": {"timeout_seconds": POOL_TIMEOUT_SECONDS},
                },
                "active_state": {"backend": "memory"},
                "mcp_session_manager_enabled": False,
            },
            "tasks": {"backend": "memory"},
        }
    )


@pytest_asyncio.fixture
async def down_db_client(tmp_path, monkeypatch):
    """A live app whose relational stores point at an unreachable Postgres."""

    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_IDTOKEN_ALLOW_UNSIGNED", "true")
    app = make_app(_down_db_config(tmp_path))
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


def test_pools_share_one_database_per_url():
    pools = PostgresPools()

    first = pools.database("postgresql://stub/one")
    again = pools.database("postgresql://stub/one")
    other = pools.database("postgresql://stub/two")

    assert first is again
    assert first is not other


def test_unreachable_database_raises_pool_timeout_after_bounded_wait():
    # Exhaustion and an unreachable server share this failure mode: callers
    # wait at most timeout_seconds for a connection, then get PoolTimeout.
    db = PostgresDatabase(UNREACHABLE_URL, min_size=0, max_size=2, timeout_seconds=0.2)
    try:
        with pytest.raises(PoolTimeout):
            with db.connection():
                pass
    finally:
        db.close()


async def test_postgres_down_startup_survives_and_requests_get_503(down_db_client):
    # The app must come up with Postgres unreachable (the fixture's lifespan
    # already proved that); the frame itself lives on local-fs storage so the
    # mutation succeeds, with the history write best-effort skipped.
    created = await down_db_client.post(
        "/v1/frames",
        cookies=auth_cookie("alice"),
        json={"name": "Pooled", "tags": ["team"], "body": "# Body"},
    )
    assert created.status_code == 201

    response = await down_db_client.get(
        f"/v1/frames/{created.json()['id']}/history",
        cookies=auth_cookie("alice"),
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "database_unavailable"


async def test_health_db_reports_unavailable_without_leaking_urls(down_db_client):
    response = await down_db_client.get("/health/db")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["pools"][0]["status"] == "error"
    assert "127.0.0.1" not in response.text
    assert "secret" not in response.text


async def test_health_db_not_configured(client):
    # The shared conftest client runs on the in-memory backends with no
    # Postgres URL, so there are no pools to report on.
    response = await client.get("/health/db")

    assert response.status_code == 200
    assert response.json() == {"status": "not_configured"}


# --- Healthy but saturated pools --------------------------------------------
#
# Everything above points the pool at a dead port, which only exercises the
# connect-failure path. These tests saturate a pool whose server answers, so
# callers really do queue for a connection that another caller is holding.


@pytest.fixture
def stub_postgres():
    """A listening server that completes the psycopg handshake and stores nothing."""

    server = StubPostgresServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@contextmanager
def hold_every_connection(db: PostgresDatabase, count: int):
    """Check out the pool's whole capacity for the duration of the block."""

    with ExitStack() as stack:
        for _ in range(count):
            stack.enter_context(db.connection())
        assert db.stats()["pool_available"] == 0
        yield


def test_saturated_pool_queues_waiters_until_a_connection_comes_back(stub_postgres):
    db = PostgresDatabase(
        stub_postgres.url,
        min_size=0,
        max_size=1,
        timeout_seconds=SATURATED_POOL_TIMEOUT_SECONDS,
    )
    served = threading.Event()

    def wait_for_a_connection() -> None:
        with db.connection():
            served.set()

    waiter = threading.Thread(target=wait_for_a_connection, daemon=True)
    try:
        with hold_every_connection(db, 1):
            waiter.start()
            # The caller is queued, not rejected and not served: the single
            # connection is busy for as long as this block holds it.
            assert not served.wait(0.3)
            assert db.stats()["requests_waiting"] == 1

        assert served.wait(SATURATED_POOL_TIMEOUT_SECONDS)
    finally:
        waiter.join(timeout=SATURATED_POOL_TIMEOUT_SECONDS)
        db.close()


def test_best_effort_checkout_gives_up_instead_of_waiting_out_a_saturated_pool(stub_postgres):
    # This is the acquisition budget the auth-path usage write runs on: with
    # the pool saturated it must cost milliseconds, not timeout_seconds.
    db = PostgresDatabase(
        stub_postgres.url,
        min_size=0,
        max_size=1,
        timeout_seconds=SATURATED_POOL_TIMEOUT_SECONDS,
    )
    try:
        with hold_every_connection(db, 1):
            started = time.monotonic()
            with pytest.raises(PoolTimeout):
                with db.best_effort_connection():
                    pass
            waited = time.monotonic() - started

        assert waited < RESPONSIVE_SECONDS
        # The pool is healthy, not poisoned: the returned connection serves again.
        with db.connection() as conn:
            assert not conn.closed
    finally:
        db.close()


def test_max_waiting_rejects_callers_beyond_the_bounded_queue(stub_postgres):
    db = PostgresDatabase(
        stub_postgres.url,
        min_size=0,
        max_size=1,
        timeout_seconds=SATURATED_POOL_TIMEOUT_SECONDS,
        max_waiting=1,
    )
    queued = threading.Thread(target=lambda: _swallow(db.connection), daemon=True)
    try:
        with hold_every_connection(db, 1):
            queued.start()
            _wait_until(lambda: db.stats()["requests_waiting"] == 1)

            # The one waiting slot is taken, so the next caller is rejected
            # immediately rather than piling up behind the saturated pool.
            started = time.monotonic()
            with pytest.raises(TooManyRequests):
                with db.connection():
                    pass

            assert time.monotonic() - started < RESPONSIVE_SECONDS
    finally:
        queued.join(timeout=SATURATED_POOL_TIMEOUT_SECONDS)
        db.close()


def _swallow(checkout) -> None:
    try:
        with checkout():
            pass
    except Exception:
        pass


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not reached in time")


def _saturation_config(tmp_path, database_url: str) -> Config:
    """An app whose *only* pooled store is the auth-path usage store."""

    return Config.parse(
        {
            "storage": {"frames_path": str(tmp_path / "frames")},
            "frames": {
                "postgres": {
                    "url": database_url,
                    "pool": {
                        "min_size": 0,
                        "max_size": 1,
                        "timeout_seconds": SATURATED_POOL_TIMEOUT_SECONDS,
                    },
                },
                # Keep history/groups/active-state off the pool so the only
                # database work these requests do is the best-effort usage write.
                "history": {"backend": "memory"},
                "groups": {"backend": "memory"},
                "active_state": {"backend": "memory"},
            },
            "tasks": {"backend": "memory"},
        }
    )


def _list_frames(client: TestClient, user: str) -> float:
    """One authenticated HTTP request; returns what the caller waited."""

    started = time.monotonic()
    response = client.get("/v1/frames", cookies=auth_cookie(user))
    assert response.status_code == 200
    return time.monotonic() - started


def _initialize_mcp(client: TestClient, user: str) -> float:
    started = time.monotonic()
    response = client.post(
        "/mcp",
        headers={
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
            # MCP authenticates through the same synchronous authenticator.
            "Cookie": "; ".join(f"{name}={value}" for name, value in auth_cookie(user).items()),
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        },
    )
    assert response.status_code == 200
    return time.monotonic() - started


def test_saturated_pool_keeps_authenticated_and_mcp_traffic_responsive(tmp_path, monkeypatch, stub_postgres):
    # The regression the pool introduced: get_auth_context records seen users
    # synchronously, and the async MCP middleware calls that same authenticator
    # directly. With the pool saturated, a best-effort accounting write must not
    # hold an authenticated request — or the MCP event loop — for the pool
    # timeout. Every request below uses a distinct user so the seen-user
    # throttle never short-circuits the write attempt.
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_IDTOKEN_ALLOW_UNSIGNED", "true")
    app = make_app(_saturation_config(tmp_path, stub_postgres.url))
    callers = [f"user-{index}" for index in range(6)]

    with TestClient(app) as client:
        db = app.state.postgres_pools.database(stub_postgres.url)
        with hold_every_connection(db, 1):
            with ThreadPoolExecutor(max_workers=len(callers) * 2) as executor:
                started = time.monotonic()
                futures = [executor.submit(_list_frames, client, user) for user in callers]
                futures += [executor.submit(_initialize_mcp, client, user) for user in callers]
                latencies = [future.result(timeout=SATURATED_POOL_TIMEOUT_SECONDS * 2) for future in futures]
                elapsed = time.monotonic() - started

        # No single request — HTTP or MCP — waited on the saturated pool, and
        # the whole burst finished inside one pool timeout rather than one
        # timeout per request.
        assert max(latencies) < RESPONSIVE_SECONDS
        assert elapsed < SATURATED_POOL_TIMEOUT_SECONDS
        # The writes really were attempted and really did fail on the saturated
        # pool: this test would pass vacuously against a no-op usage store.
        app.state.usage_store._writer.wait_idle(timeout=SATURATED_POOL_TIMEOUT_SECONDS)
        assert app.state.usage_store._seen_failures


# --- A server that answers the checkout but not the statement ----------------
#
# Everything above bounds *acquisition*. The finding these cover is what
# happens after a connection has been handed over: a server that accepts the
# usage statement and withholds its response used to hold the authenticated
# request — and the MCP event loop — until the socket closed.


def test_stalled_server_delays_neither_authenticated_mcp_nor_unrelated_requests(
    tmp_path, monkeypatch, stub_postgres
):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_IDTOKEN_ALLOW_UNSIGNED", "true")
    app = make_app(_saturation_config(tmp_path, stub_postgres.url))
    callers = [f"stalled-{index}" for index in range(6)]

    def unrelated_health(client: TestClient, _user: str) -> float:
        started = time.monotonic()
        assert client.get("/health").status_code == 200
        return time.monotonic() - started

    try:
        with TestClient(app) as client:
            # The pool is healthy and idle: the write gets a connection, sends
            # its statement, and never hears back.
            stub_postgres.stall()

            with ThreadPoolExecutor(max_workers=len(callers) * 3) as executor:
                started = time.monotonic()
                futures = [executor.submit(_list_frames, client, user) for user in callers]
                futures += [executor.submit(_initialize_mcp, client, user) for user in callers]
                futures += [executor.submit(unrelated_health, client, user) for user in callers]
                latencies = [future.result(timeout=SATURATED_POOL_TIMEOUT_SECONDS * 2) for future in futures]
                elapsed = time.monotonic() - started

            # Nothing waited on the stalled statement — not the authenticated
            # requests that queued it, not MCP, not the unrelated ones.
            assert max(latencies) < RESPONSIVE_SECONDS
            assert elapsed < RESPONSIVE_SECONDS * 2
            # ...and the write really did reach the server and stall there, so
            # this is not passing because nothing was attempted.
            _wait_until(lambda: stub_postgres.statements_received >= 1)

            stub_postgres.resume()
    finally:
        stub_postgres.resume()


def test_stalled_server_does_not_hold_up_shutdown(tmp_path, monkeypatch, stub_postgres):
    """Workers parked on a server that will not answer are daemons closed on a
    bound, so shutdown costs the bound and not the stall."""

    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_IDTOKEN_ALLOW_UNSIGNED", "true")
    app = make_app(_saturation_config(tmp_path, stub_postgres.url))

    try:
        started = time.monotonic()
        with TestClient(app) as client:
            stub_postgres.stall()
            assert client.get("/v1/frames", cookies=auth_cookie("shutdown-user")).status_code == 200
            _wait_until(lambda: stub_postgres.statements_received >= 1)
        elapsed = time.monotonic() - started
    finally:
        stub_postgres.resume()

    assert elapsed < RESPONSIVE_SECONDS + best_effort.BEST_EFFORT_SHUTDOWN_TIMEOUT_SECONDS * 2


# --- The cancel watchdog must outlive nothing ---------------------------------
#
# The statement deadline above is enforced from another thread, because the
# thread that issued the statement is the one stuck on it. That makes disarming
# a race: a callback already inside ``cancel_safe`` keeps running after
# ``Timer.cancel()`` returns, and if the checkout finished meanwhile the
# connection is back in the pool — so the stale cancel would abort the *next*
# borrower's query instead of the stalled one.


class _CancellableConnection:
    """A connection whose cancel can be held in flight for as long as a test likes."""

    def __init__(self):
        self.cancelling = threading.Event()
        self.release = threading.Event()
        self.cancels = 0

    def cancel_safe(self, *, timeout=None):
        self.cancels += 1
        self.cancelling.set()
        self.release.wait(timeout=5)


def test_watchdog_disarm_waits_out_a_cancel_already_in_flight():
    conn = _CancellableConnection()
    watchdog = frames_db._CancelWatchdog(conn, after_seconds=0.01, cancel_timeout_seconds=0.5)
    watchdog.start()
    assert conn.cancelling.wait(timeout=5)

    disarmed = threading.Event()
    disarming = threading.Thread(target=lambda: (watchdog.disarm(), disarmed.set()), daemon=True)
    try:
        disarming.start()

        # A bare Timer.cancel() would return right here, freeing the connection
        # while the callback can still reach it.
        assert not disarmed.wait(0.3)

        conn.release.set()
        assert disarmed.wait(timeout=5)
    finally:
        conn.release.set()
        disarming.join(timeout=5)

    assert watchdog.fired
    assert conn.cancels == 1


def test_watchdog_disarmed_before_firing_never_touches_the_connection():
    conn = _CancellableConnection()
    watchdog = frames_db._CancelWatchdog(conn, after_seconds=0.05, cancel_timeout_seconds=0.5)
    watchdog.start()

    watchdog.disarm()
    time.sleep(0.15)  # past when the timer would have elapsed

    assert not watchdog.fired
    assert conn.cancels == 0


def test_watchdog_cannot_cancel_the_next_borrower(stub_postgres, monkeypatch):
    """A connection may not go back to the pool with a cancel still in flight.

    Reproduces the reported failure against a single-connection pool: the
    watchdog fires on a stalled statement, the checkout ends, and the next
    borrower gets the very same connection. Before the fix that borrower's
    query was killed by the stale cancel.
    """

    events: list[str] = []
    events_lock = threading.Lock()
    cancelling = threading.Event()
    release_cancel = threading.Event()

    def record(name: str) -> None:
        with events_lock:
            events.append(name)

    cancelled: list[object] = []

    def slow_cancel(self, *, timeout=None):
        cancelled.append(self)  # the connection the watchdog is aimed at
        record("cancel-started")
        cancelling.set()
        release_cancel.wait(timeout=5)
        record("cancel-finished")

    monkeypatch.setattr(psycopg.Connection, "cancel_safe", slow_cancel)
    monkeypatch.setattr(frames_db, "BEST_EFFORT_CANCEL_AFTER_SECONDS", 0.05)

    db = PostgresDatabase(
        stub_postgres.url,
        min_size=0,
        max_size=1,
        timeout_seconds=SATURATED_POOL_TIMEOUT_SECONDS,
    )

    def stalled_best_effort_write() -> None:
        # The server takes the statement and withholds its answer, so the
        # watchdog fires while this thread is still inside the checkout.
        try:
            with db.best_effort_connection():
                pass
        except Exception:
            pass

    stub_postgres.stall()
    writer = threading.Thread(target=stalled_best_effort_write, daemon=True)
    try:
        writer.start()
        assert cancelling.wait(timeout=5)
        # Let the stalled statement come back: the checkout now ends, and its
        # exit must not release the connection while the cancel is in flight.
        stub_postgres.resume()

        releaser = threading.Timer(0.3, release_cancel.set)
        releaser.daemon = True
        releaser.start()

        started = time.monotonic()
        with db.connection() as second:
            waited = time.monotonic() - started
            record("second-borrower-served")
            assert not second.closed

        assert events == ["cancel-started", "cancel-finished", "second-borrower-served"]
        # It really is the same pooled connection the watchdog was aimed at.
        assert cancelled and second is cancelled[0]
        # ...and the second borrower genuinely waited for the cancel to finish
        # rather than being handed the connection out from under it.
        assert waited >= 0.2
    finally:
        release_cancel.set()
        stub_postgres.resume()
        writer.join(timeout=5)
        db.close()


@contextmanager
def park_the_only_worker(writer: BestEffortWriter):
    """Occupy *writer*'s single worker for the duration of the block."""

    running = threading.Event()
    release = threading.Event()

    def park() -> None:
        running.set()
        release.wait(timeout=10)

    try:
        assert writer.submit(park)
        assert running.wait(timeout=5)
        yield
    finally:
        release.set()


def test_writer_drops_work_instead_of_blocking_a_full_queue():
    """The dispatch bound: once the queue is full, submitting costs nothing."""

    writer = BestEffortWriter(workers=1, queue_size=2)
    try:
        with park_the_only_worker(writer):
            assert writer.submit(lambda: None)
            assert writer.submit(lambda: None)

            started = time.monotonic()
            assert not writer.submit(lambda: None)

            assert time.monotonic() - started < RESPONSIVE_SECONDS
            assert writer.dropped == 1
    finally:
        writer.close()


def test_writer_close_is_bounded_by_its_timeout():
    writer = BestEffortWriter(workers=1, queue_size=10)
    with park_the_only_worker(writer):
        writer.submit(lambda: None)

        started = time.monotonic()
        writer.close(timeout=0.2)
        waited = time.monotonic() - started

        # It waited for its bound and moved on rather than for the parked write.
        assert 0.1 < waited < RESPONSIVE_SECONDS
        # Work that had not started was abandoned, and nothing new is accepted.
        assert not writer.submit(lambda: None)


# --- Best-effort usage capture on the request path ---------------------------


class _NullConnection:
    """A pooled connection that accepts statements and does nothing with them."""

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params=None):
        return self


class _BestEffortOnlyDatabase:
    """A pooled-database stand-in that refuses full-budget checkouts.

    Seen-user capture runs inside auth, so a full-budget checkout there is the
    regression itself; this fake turns one into a test failure.
    """

    database_url = "postgresql://stub/example"

    def __init__(self, error: Exception | None = None):
        self.error = error
        self.checkouts = 0

    def connection(self, timeout=None):
        raise AssertionError("auth-path usage capture must use the best-effort budget")

    @contextmanager
    def best_effort_connection(self):
        self.checkouts += 1
        if self.error is not None:
            raise self.error
        yield _NullConnection()


class _FakeClock:
    """Stands in for the usage module's ``time`` so backoff needs no sleeping."""

    def __init__(self):
        self._now = 1_000.0

    def monotonic(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def test_seen_user_capture_checks_out_on_the_best_effort_budget():
    db = _BestEffortOnlyDatabase()
    store = PostgresUsageStore(db, writer=InlineBestEffortWriter())

    store.record_user_seen("org-a", "workspace-a", "alice", None)
    store.record_user_seen("org-a", "workspace-a", "alice", None)

    # One write, then the throttle window: auth never pays per request.
    assert db.checkouts == 1


def test_failed_seen_user_write_backs_off_instead_of_retrying_next_request(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(usage, "time", clock)
    db = _BestEffortOnlyDatabase(error=PoolTimeout("pool saturated"))
    # Inline so the write has landed by the time record_user_seen returns; the
    # backoff itself is the same whichever thread the failure comes back on.
    store = PostgresUsageStore(db, writer=InlineBestEffortWriter())

    store.record_user_seen("org-a", "workspace-a", "alice", None)
    assert db.checkouts == 1

    # Requests arriving right behind the failure do not retry the doomed write.
    clock.advance(usage.SEEN_RETRY_BACKOFF_SECONDS / 2)
    for _ in range(5):
        store.record_user_seen("org-a", "workspace-a", "alice", None)
    assert db.checkouts == 1

    # Once the backoff expires the write is retried exactly once...
    clock.advance(usage.SEEN_RETRY_BACKOFF_SECONDS)
    store.record_user_seen("org-a", "workspace-a", "alice", None)
    assert db.checkouts == 2

    # ...and the next backoff is longer, so a lasting outage costs less and less.
    clock.advance(usage.SEEN_RETRY_BACKOFF_SECONDS * 1.5)
    store.record_user_seen("org-a", "workspace-a", "alice", None)
    assert db.checkouts == 2

    # Recovery clears the backoff: the next window behaves like a first sight.
    db.error = None
    clock.advance(usage.SEEN_WRITE_INTERVAL_SECONDS)
    store.record_user_seen("org-a", "workspace-a", "alice", None)
    assert db.checkouts == 3
