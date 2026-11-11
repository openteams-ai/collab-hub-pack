"""The install store conformance suite: every store records an install's life the same way.

Create, find, claim once, record each move of the install machine with its
events, ask for removal, and list what a controller has to act on, against
the in-memory, SQLite and Postgres stores. Postgres needs ``TEST_POSTGRES_URL``.
"""

from __future__ import annotations

import ast
import os
import re
import sqlite3
import threading
from pathlib import Path

import pytest

from collab_hub_execution.installs import (
    InMemoryInstallStore,
    InstallConflict,
    InstallExists,
    InstallRow,
    PostgresInstallStore,
    SqliteInstallStore,
    new_install_id,
)
from collab_hub_execution.states import InstallState

TEST_PG = os.environ.get("TEST_POSTGRES_URL")
_NEEDS_PG = pytest.mark.skipif(not TEST_PG, reason="set TEST_POSTGRES_URL to run the suite against Postgres")
REFERENCE = "hub.example/cogs/echo@sha256:" + "a" * 64


@pytest.fixture(params=["memory", "sqlite", pytest.param("postgres", marks=_NEEDS_PG)])
def store(request, tmp_path):
    if request.param == "memory":
        yield InMemoryInstallStore()
    elif request.param == "sqlite":
        path = tmp_path / "track.sqlite"
        SqliteInstallStore.ensure_schema(path)
        yield SqliteInstallStore(path)
    else:
        from psycopg_pool import ConnectionPool

        pool = ConnectionPool(TEST_PG, min_size=1, max_size=4, open=True)
        with pool.connection() as conn:
            for statement in ("DROP TABLE IF EXISTS collab_cog_install_events",
                              "DROP TABLE IF EXISTS collab_cog_installs",
                              "DROP SEQUENCE IF EXISTS collab_cog_install_event_sequence"):
                conn.execute(statement)
            PostgresInstallStore.ensure_schema(conn)
        yield PostgresInstallStore(pool)
        pool.close()


def _row(org="acme", reference=REFERENCE, **extra) -> InstallRow:
    return InstallRow(id=new_install_id(), org_id=org, reference=reference, digest=reference.rsplit("@", 1)[1],
                      cog_id="acme/echo", card={"kind": "complete", "requires": []}, requested_by="alice", **extra)


def test_an_install_is_created_published_found_and_listed_newest_first(store):
    first = store.create(_row())
    assert store.get(first.id) == first and first.state == "published" and first.live and not first.invokable
    assert store.find("acme", reference=REFERENCE) == first and store.find("acme", digest=first.digest) == first
    assert store.find("acme", reference=REFERENCE, invokable=True) is None
    assert store.find("other", reference=REFERENCE) is None
    second = store.create(_row(reference=REFERENCE.replace("a" * 64, "b" * 64)))
    assert [r.id for r in store.list(org_id="acme")] == [second.id, first.id]
    assert [r.id for r in store.list(org_id="acme", limit=1)] == [second.id]
    assert [r.id for r in store.list(org_id="acme", limit=1, offset=1)] == [first.id]
    assert store.list(org_id="acme", state="invokable") == ()
    assert first.run_id == f"install:{first.id}"


def test_an_organization_holds_one_live_install_of_a_digest(store):
    first = store.create(_row())
    with pytest.raises(InstallExists):
        store.create(_row())
    store.create(_row(org="other"))  # another organization's install of the same digest is its own
    machine = first.machine()
    store.record(first.id, machine.uninstall())
    again = store.create(_row())  # once uninstalled, the digest can be installed again
    assert store.find("acme", digest=first.digest) == again
    assert [r.id for r in store.list(org_id="acme")] == [again.id]
    assert {r.id for r in store.list(org_id="acme", include_uninstalled=True)} == {first.id, again.id}


def test_a_controller_claims_an_install_once(store):
    row = store.create(_row())
    assert store.claim(row.id, "one") is True
    assert store.claim(row.id, "two") is False
    assert store.get(row.id).controller == "one"
    assert store.claim("inst-missing", "one") is False


def test_each_move_of_the_machine_is_recorded_with_its_events_and_a_failing_check_leaves_it_bound(store):
    row = store.create(_row())
    install = row.machine()
    assert install.state is InstallState.PUBLISHED
    fetched = install.fetch()
    row = store.record(row.id, fetched, lock_digest="sha256:" + "1" * 64)
    assert row.state == "fetched" and row.lock_digest == "sha256:" + "1" * 64
    bound = fetched.after.admit_binding(binding="bnd-1")
    row = store.record(row.id, bound, binding_id="bnd-1", binding={"id": "bnd-1", "model": {"id": "model:x"}})
    assert row.state == "bound" and row.binding_id == "bnd-1" and row.binding["model"] == {"id": "model:x"}
    failed = bound.after.check_failed(step="check", reason="the model answered 503")
    row = store.record(row.id, failed, failure="check: the model answered 503", failed_step="check")
    assert row.state == "bound" and row.failed_step == "check" and "503" in row.failure
    assert row.machine().state is InstallState.BOUND and row.machine().binding == "bnd-1"
    passed = row.machine().check_passed()
    row = store.record(row.id, passed, failure=None, failed_step=None)
    assert row.invokable and row.failure is None and row.failed_step is None
    assert store.find("acme", reference=REFERENCE, invokable=True) == row
    events = store.events(row.id)
    assert [e.event_type for e in events] == ["install_fetched", "binding_admitted", "check_failed", "check_passed"]
    assert events[2].payload == {"reference": REFERENCE, "step": "check", "reason": "the model answered 503"}
    assert [e.sequence for e in events] == sorted(e.sequence for e in events)
    with pytest.raises(ValueError, match="cannot be changed"):
        store.record(row.id, passed, org_id="x")


def test_a_record_guarded_by_the_state_it_moves_from_is_dropped_when_another_controller_moved_the_install(store):
    """Controller A is checking; B records the uninstall asked for meanwhile; A's `invokable` is never written over
    it, and nothing of A's move, its events included, reaches the store."""
    row = store.create(_row())
    bound = row.machine().fetch().after.admit_binding(binding="bnd-1")
    row = store.record(row.id, row.machine().fetch(), lock_digest="sha256:" + "1" * 64, expected_state="published")
    row = store.record(row.id, bound, binding_id="bnd-1", expected_state="fetched")
    removed = store.record(row.id, row.machine().uninstall(), expected_state="bound")  # B
    assert removed.state == "uninstalled"
    with pytest.raises(InstallConflict, match="not bound"):
        store.record(row.id, bound.after.check_passed(), failure=None, failed_step=None, expected_state="bound")  # A
    assert store.get(row.id).state == "uninstalled"
    assert [e.event_type for e in store.events(row.id)] == ["install_fetched", "binding_admitted", "uninstalled"]


def test_in_progress_lists_what_a_controller_claimed_and_did_not_finish_and_a_dead_controllers_is_reclaimed(store):
    fresh = store.create(_row())
    claimed = store.create(_row(reference="hub.example/cogs/b@sha256:" + "b" * 64))
    failed = store.create(_row(reference="hub.example/cogs/c@sha256:" + "c" * 64))
    done = store.create(_row(reference="hub.example/cogs/d@sha256:" + "d" * 64))
    for asked in (claimed, failed, done):
        assert store.claim(asked.id, "one")
    store.record(failed.id, failed.machine().fetch(), expected_state="published")
    store.record(failed.id, store.get(failed.id).machine().refuse_binding(reason="no model"), failure="bind: no model",
                 failed_step="bind")
    store.record(done.id, done.machine().fetch())
    store.record(done.id, store.get(done.id).machine().admit_binding(binding="bnd-1"), binding_id="bnd-1")
    store.record(done.id, store.get(done.id).machine().check_passed())
    # Unclaimed, failed and finished installs are not in progress; a removal asked for takes a row out of it.
    assert [r.id for r in store.in_progress()] == [claimed.id]
    assert [r.id for r in store.pending()] == [fresh.id]
    assert store.reclaim(claimed.id, "two", "three") is False  # not two's to take
    assert store.reclaim(claimed.id, "one", "two") is True and store.get(claimed.id).controller == "two"
    assert store.reclaim(fresh.id, "one", "two") is False  # nobody claimed it
    assert store.reclaim(done.id, "one", "two") is False  # finished
    store.request_uninstall(claimed.id, "alice")
    assert store.in_progress() == ()


def test_a_removal_is_asked_once_and_pending_lists_what_a_controller_acts_on(store):
    asked = store.create(_row())
    assert [r.id for r in store.pending()] == [asked.id]
    store.claim(asked.id, "ctl")
    assert store.pending() == ()  # claimed: that controller is on it
    removal = store.request_uninstall(asked.id, "alice")
    assert removal.uninstall_requested_by == "alice"
    assert store.request_uninstall(asked.id, "bob").uninstall_requested_by == "alice"  # asked once
    assert [r.id for r in store.pending()] == [asked.id]
    store.record(asked.id, store.get(asked.id).machine().uninstall())
    assert store.pending() == () and store.request_uninstall(asked.id, "alice") is None
    assert store.request_uninstall("inst-missing", "alice") is None


def test_stores_are_safe_to_share_between_threads(store):
    rows = [store.create(_row(reference=REFERENCE.replace("a" * 64, f"{i:064x}"))) for i in range(8)]
    claimed = []

    def claim(controller):
        for row in rows:
            if store.claim(row.id, controller):
                claimed.append((row.id, controller))

    threads = [threading.Thread(target=claim, args=(f"c{i}",)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(claimed) == len(rows) and len({row_id for row_id, _ in claimed}) == len(rows)


# --- the DDL: the hub's migrations, the Postgres store and SQLite agree ------------------------------

REGISTRY = Path(__file__).resolve().parents[2] / "api" / "src" / "collab_hub_api" / "frames" / "collab_schema.py"


def _normalize(sql: str) -> str:
    return " ".join(sql.split())


def _registry_install_statements() -> list[str]:
    tree = ast.parse(REGISTRY.read_text())
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "COLLAB_SCHEMA_MIGRATIONS":
            migrations = ast.literal_eval(node.value)
            break
    else:  # pragma: no cover - the registry moved
        raise AssertionError(f"COLLAB_SCHEMA_MIGRATIONS not found in {REGISTRY}")
    return [_normalize(statement) for _, statements in sorted(migrations) for statement in statements
            if "collab_cog_install" in statement]


def _postgres_columns(statements) -> dict[str, list[str]]:
    tables: dict[str, list[str]] = {}
    for statement in map(_normalize, statements):
        created = re.match(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*)\)$", statement)
        if created:
            columns = [part.strip().split()[0] for part in created.group(2).split(",")]
            tables[created.group(1)] = [c for c in columns if c.upper() not in {"PRIMARY", "UNIQUE", "CHECK"}]
    return tables


def test_the_postgres_store_creates_exactly_what_the_hub_s_migrations_create():
    assert [_normalize(s) for s in PostgresInstallStore._SCHEMA] == _registry_install_statements()


def test_the_sqlite_store_has_the_same_tables_and_columns(tmp_path):
    path = tmp_path / "track.sqlite"
    SqliteInstallStore.ensure_schema(path)
    connection = sqlite3.connect(path)
    try:
        sqlite = {
            table: [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
            for (table,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'collab_cog_install%'")
        }
    finally:
        connection.close()
    assert sqlite == _postgres_columns(PostgresInstallStore._SCHEMA)
