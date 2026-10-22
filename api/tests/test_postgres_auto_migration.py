"""Startup-path coverage for the Postgres stores' auto-migration.

The API test suite runs everything else on the in-memory backends, so nothing
here exercised ``auto_migrate=True`` construction — the configuration real
deployments use. A refactor once swallowed
``PostgresActiveFrameStore._ensure_schema`` into an adjacent method, and pods
with auto-migration enabled would have crashed at startup with
``AttributeError``. These tests construct every Postgres store against a
stubbed pooled database and assert the migration DDL actually runs — and that
no store ever opens a direct ``psycopg.connect`` — everything goes through the
shared connection pool.

The stub keeps this a fast startup-path test that needs no database. Verifying
the DDL against a real server (that the statements are valid Postgres and are
idempotent across restarts) needs a real-Postgres harness, which CI does not
have yet; that is tracked separately. Pool behavior under saturation is covered
against a real pool in ``test_postgres_pool.py``.

The second half of this module (issue #42) covers the fix for the race that
sits behind that untested startup path: bare ``CREATE TABLE IF NOT EXISTS`` is
not concurrency-safe in Postgres, so two replicas starting at once could both
pass the existence check and race the catalog insert, crashing one pod with a
duplicate-key error on ``pg_type``/``pg_class``. Every store's ``_ensure_schema``
now opens its DDL transaction through
``collab_hub_api.frames.db.locked_schema_connection``, which takes
``pg_advisory_xact_lock`` before touching the catalog at all. ``LockEnforcingServer``
below is the same enforcement device as ``FakeServer`` in
``test_collab_schema.py`` — a real ``threading.Lock`` that refuses any
statement issued by a thread that does not currently hold it — applied here to
the five pre-existing ``frames_server_``/``nexus_task_`` stores instead of the
``collab_`` migration runner.

A third, opt-in layer (``COLLAB_HUB_TEST_POSTGRES_URL``, same convention as
``test_collab_schema.py``) races real replicas of every store against a real
server, which is the only way to observe the actual failure mode the issue
describes rather than a modeled stand-in for it.
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

psycopg = pytest.importorskip("psycopg")

from collab_hub_api.frames.active_state import PostgresActiveFrameStore  # noqa: E402
from collab_hub_api.frames.db import FRAMES_SERVER_SCHEMA_LOCK_KEY  # noqa: E402
from collab_hub_api.frames.groups import PostgresFrameGroupStore  # noqa: E402
from collab_hub_api.frames.history import PostgresFrameHistoryStore  # noqa: E402
from collab_hub_api.frames.usage import PostgresUsageStore  # noqa: E402
from collab_hub_api.tasks.store import PostgresTaskStore  # noqa: E402

POSTGRES_STORE_CLASSES = (
    PostgresActiveFrameStore,
    PostgresFrameGroupStore,
    PostgresFrameHistoryStore,
    PostgresUsageStore,
    PostgresTaskStore,
)


class FakeConnection:
    """A stand-in connection recording every executed statement."""

    def __init__(self, executed: list[str]):
        self._executed = executed

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params=None):
        self._executed.append(" ".join(str(sql).split()))
        return self


class FakeDatabase:
    """A stand-in for ``db.PostgresDatabase`` handing out fake pooled connections."""

    database_url = "postgresql://stub/example"

    def __init__(self, executed: list[str]):
        self._executed = executed

    def connection(self, timeout=None):
        return FakeConnection(self._executed)

    def best_effort_connection(self):
        # The same connection; on the real pool this one has a near-zero
        # acquisition budget (used by the auth-path usage write).
        return FakeConnection(self._executed)


@pytest.mark.parametrize("store_cls", POSTGRES_STORE_CLASSES)
def test_auto_migration_runs_schema_ddl_at_construction(monkeypatch, store_cls):
    executed: list[str] = []

    def forbid_direct_connect(*args, **kwargs):
        raise AssertionError(f"{store_cls.__name__} opened a direct psycopg.connect instead of using the pool")

    monkeypatch.setattr(psycopg, "connect", forbid_direct_connect)

    store_cls(FakeDatabase(executed), auto_migrate=True)

    assert any(sql.startswith("CREATE TABLE IF NOT EXISTS") for sql in executed), (
        f"{store_cls.__name__} did not run its schema migration on construction"
    )


# --------------------------------------------------------------------------
# Advisory-lock coverage (issue #42)
# --------------------------------------------------------------------------
#
# ``FakeDatabase``/``FakeConnection`` above only record what ran; they cannot
# tell an ordering bug (lock taken after the first CREATE, or not taken at
# all) from a correct migration, because nothing stops an unguarded statement
# from being recorded. ``LockEnforcingServer`` closes that gap the same way
# ``test_collab_schema.py``'s ``FakeServer`` does: it is backed by a real
# ``threading.Lock``, and it raises if any statement — including the schema's
# own version-agnostic ``CREATE TABLE IF NOT EXISTS`` — reaches it from a
# thread that is not the current lock holder. A store whose ``_ensure_schema``
# regressed to a bare ``db.connection()`` would fail these tests immediately;
# one that raced under genuine concurrency would fail
# ``test_concurrent_ensure_schema_across_different_stores_does_not_race``
# rather than merely passing by luck.


class LockEnforcingServer:
    """Refuses any statement issued without the caller holding the real lock."""

    def __init__(self):
        self.lock = threading.Lock()
        self.statements: list[str] = []
        self.holder: int | None = None
        self._guard = threading.Lock()

    def record(self, statement: str) -> None:
        with self._guard:
            if self.holder != threading.get_ident():
                raise AssertionError(f"statement issued without holding the schema advisory lock: {statement}")
            self.statements.append(statement)


class LockEnforcingConnection:
    def __init__(self, server: LockEnforcingServer, lock_key: int):
        self.server = server
        self.lock_key = lock_key
        self._locked = False
        self._pending = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        # Transaction end: pg_advisory_xact_lock releases on commit/rollback.
        if self._locked:
            self.server.holder = None
            self.server.lock.release()
            self._locked = False
        return False

    def execute(self, sql, params=None):
        statement = " ".join(str(sql).split())
        if "pg_advisory_xact_lock" in statement:
            assert params == (self.lock_key,)
            self.server.lock.acquire()
            self._locked = True
            self.server.holder = threading.get_ident()
            self._pending = None
            return self
        self.server.record(statement)
        # Widen the window a concurrent racer would have to lose.
        time.sleep(0.005)
        self._pending = None
        return self

    def fetchone(self):
        return self._pending

    def fetchall(self):
        return []


class LockEnforcingDatabase:
    """A stand-in for ``db.PostgresDatabase`` whose connections enforce the lock."""

    database_url = "postgresql://stub/example"

    def __init__(self, server: LockEnforcingServer, lock_key: int = FRAMES_SERVER_SCHEMA_LOCK_KEY):
        self.server = server
        self.lock_key = lock_key

    def connection(self, timeout=None):
        return LockEnforcingConnection(self.server, self.lock_key)

    def best_effort_connection(self):
        return LockEnforcingConnection(self.server, self.lock_key)


@pytest.mark.parametrize("store_cls", POSTGRES_STORE_CLASSES)
def test_ensure_schema_takes_the_advisory_lock_before_any_ddl(store_cls):
    server = LockEnforcingServer()

    store_cls(LockEnforcingDatabase(server), auto_migrate=True)

    # LockEnforcingServer already raised if any DDL reached it without the
    # lock held; this just confirms the DDL actually ran under it.
    assert any(sql.startswith("CREATE TABLE IF NOT EXISTS") for sql in server.statements), (
        f"{store_cls.__name__} did not run its schema DDL"
    )


@pytest.mark.parametrize("store_cls", POSTGRES_STORE_CLASSES)
def test_ensure_schema_uses_the_shared_frames_server_lock_key(store_cls):
    """Each store locks with the one documented key, not a private one of its own."""

    server = LockEnforcingServer()
    wrong_key = FRAMES_SERVER_SCHEMA_LOCK_KEY + 1

    with pytest.raises(AssertionError):
        store_cls(LockEnforcingDatabase(server, lock_key=wrong_key), auto_migrate=True)


def test_concurrent_ensure_schema_within_one_store_does_not_race():
    """Two replicas constructing the same store at once must still serialize."""

    server = LockEnforcingServer()
    database = LockEnforcingDatabase(server)
    start = threading.Barrier(8)

    def build():
        start.wait()
        PostgresUsageStore(database, auto_migrate=True)

    with ThreadPoolExecutor(max_workers=8) as pool:
        for future in [pool.submit(build) for _ in range(8)]:
            future.result()

    # Reaching here already proves no interleaving (LockEnforcingServer raises
    # on that); also confirm every replica's DDL genuinely ran.
    created = " ".join(server.statements)
    assert created.count("CREATE TABLE IF NOT EXISTS frames_server_usage_users") == 8


def test_concurrent_ensure_schema_across_different_stores_does_not_race():
    """Different stores share one lock key, so their startup DDL serializes too.

    This is the case the issue is actually about: a deployment's replicas
    start every configured store at once, not just one of them, so the shared
    key has to genuinely mediate across stores and not merely within one.
    """

    server = LockEnforcingServer()
    database = LockEnforcingDatabase(server)
    start = threading.Barrier(len(POSTGRES_STORE_CLASSES))

    def build(store_cls):
        start.wait()
        store_cls(database, auto_migrate=True)

    with ThreadPoolExecutor(max_workers=len(POSTGRES_STORE_CLASSES)) as pool:
        futures = [pool.submit(build, store_cls) for store_cls in POSTGRES_STORE_CLASSES]
        for future in futures:
            future.result()

    created = " ".join(server.statements)
    for store_cls, table in (
        (PostgresActiveFrameStore, "frames_server_active_frames"),
        (PostgresFrameGroupStore, "frames_server_groups"),
        (PostgresFrameHistoryStore, "frames_server_history"),
        (PostgresUsageStore, "frames_server_usage_users"),
        (PostgresTaskStore, "nexus_task_state"),
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in created, f"{store_cls.__name__} DDL did not run"


# --------------------------------------------------------------------------
# Live-Postgres coverage (opt in with COLLAB_HUB_TEST_POSTGRES_URL)
# --------------------------------------------------------------------------

POSTGRES_URL = os.environ.get("COLLAB_HUB_TEST_POSTGRES_URL", "")

live_postgres = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="set COLLAB_HUB_TEST_POSTGRES_URL to a disposable database to run the live-Postgres race test",
)

LIVE_FRAMES_SERVER_TABLES = (
    "frames_server_active_frames",
    "frames_server_groups",
    "frames_server_history",
    "frames_server_usage_users",
    "frames_server_usage_events",
    "nexus_task_state",
    "nexus_task_devices",
)


def _live_database(max_size: int = 20):
    from collab_hub_api.frames.db import PostgresDatabase

    # A generous pool: the point of the test below is genuinely concurrent
    # replicas, so a small pool would itself serialize them and mask the race
    # this is meant to exercise.
    return PostgresDatabase(POSTGRES_URL, min_size=0, max_size=max_size, timeout_seconds=10.0)


@pytest.fixture
def clean_live_database():
    """A live database with every ``frames_server_``/``nexus_task_`` object dropped."""

    def drop_all() -> None:
        with database.connection() as conn:
            for table in LIVE_FRAMES_SERVER_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

    database = _live_database()
    try:
        drop_all()
        yield database
        drop_all()
    finally:
        database.close()


@live_postgres
def test_live_concurrent_replica_startup_does_not_race(clean_live_database):
    """The failure this issue describes, reproduced against a real server.

    Before the fix, N threads racing ``_ensure_schema`` for the same store
    against real Postgres reliably crashed at least one of them with a
    duplicate-key error on ``pg_type``/``pg_class`` — two callers can each
    pass ``CREATE TABLE IF NOT EXISTS``'s existence check before either
    commits the catalog insert. Every store now takes
    ``pg_advisory_xact_lock`` first (``locked_schema_connection``), so 20
    "replicas" — 4 rounds of all 5 stores — starting at the same instant must
    all complete cleanly and land exactly the schema each store expects.
    """

    database = clean_live_database
    store_classes = list(POSTGRES_STORE_CLASSES) * 4
    start = threading.Barrier(len(store_classes))
    errors: list[BaseException] = []
    lock = threading.Lock()

    def build(store_cls) -> None:
        try:
            start.wait()
            store_cls(database, auto_migrate=True)
        except BaseException as exc:  # noqa: BLE001 - the failure mode itself is under test
            with lock:
                errors.append(exc)

    with ThreadPoolExecutor(max_workers=len(store_classes)) as pool:
        futures = [pool.submit(build, store_cls) for store_cls in store_classes]
        for future in futures:
            future.result()

    assert not errors, [repr(exc) for exc in errors]

    with database.connection() as conn:
        tables = {
            row["table_name"]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
            ).fetchall()
        }
    assert set(LIVE_FRAMES_SERVER_TABLES) <= tables


@live_postgres
def test_live_ensure_schema_is_idempotent_across_restarts(clean_live_database):
    """A later replica joining after the schema already exists is a no-op."""

    database = clean_live_database

    PostgresUsageStore(database, auto_migrate=True)
    with database.connection() as conn:
        conn.execute(
            "INSERT INTO frames_server_usage_users (org_id, workspace_id, user_id) VALUES ('o', 'w', 'u')"
        )

    # A second (and third) replica running the same startup DDL must not
    # disturb data the first one already wrote.
    PostgresUsageStore(database, auto_migrate=True)
    PostgresUsageStore(database, auto_migrate=True)

    with database.connection() as conn:
        count = conn.execute("SELECT count(*) AS n FROM frames_server_usage_users").fetchone()["n"]
    assert count == 1
