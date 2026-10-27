"""A run whose controller never starts again is ended by a live one, and a host's start costs what is unfinished."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest
from locations_support import packages

from collab_hub_execution import (
    InMemoryCogExecutor,
    InMemoryTrackStore,
    LifecycleRunner,
    OpDefinition,
    OpStep,
    RunState,
    SqliteTrackStore,
    intents,
)
from collab_hub_execution.controller import RunController, hold_id, id_held

BY = {"user": "alice", "org_id": "acme"}
TEST_PG = os.environ.get("TEST_POSTGRES_URL")


def _types(track, run_id):
    return [event.event_type for event in track.replay(run_id)]


def _until(check, what, within=30.0):
    deadline = time.monotonic() + within
    while not check():
        assert time.monotonic() < deadline, what
        time.sleep(0.05)


def _base(tmp_path, spec):
    return [sys.executable, "-m", "collab_hub_execution.controller", "--track", spec, "--packages",
            str(packages(tmp_path)), "--work-dir", str(tmp_path / "runs"), "--poll-interval", "0.02",
            "--environment", "host", "--interaction-timeout", "0"]


def _start(base, name):
    return subprocess.Popen([*base, "--id", name], stderr=subprocess.DEVNULL, start_new_session=True)


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.skipif(
    not TEST_PG, reason="set TEST_POSTGRES_URL to run the Postgres takeover test"))])
def track_spec(request, tmp_path):
    if request.param == "sqlite":
        path = tmp_path / "track.sqlite"
        SqliteTrackStore.ensure_schema(path)
        yield str(path), SqliteTrackStore(path)
        return
    from psycopg_pool import ConnectionPool

    from collab_hub_execution import PostgresTrackStore

    pool = ConnectionPool(TEST_PG, min_size=1, max_size=4, open=True)
    with pool.connection() as conn:
        for statement in ("DROP TABLE IF EXISTS collab_track_payloads", "DROP TABLE IF EXISTS collab_track_runs",
                          "DROP TABLE IF EXISTS collab_track_events",
                          "DROP SEQUENCE IF EXISTS collab_track_event_sequence"):
            conn.execute(statement)
        PostgresTrackStore.ensure_schema(conn)
    yield TEST_PG, PostgresTrackStore(pool)
    pool.close()


def test_a_run_whose_owner_never_starts_again_is_ended_by_a_live_controller(tmp_path, track_spec):
    spec, track = track_spec
    base = _base(tmp_path, spec)
    owner = _start(base, "pod-abc")
    processes = [owner]
    try:
        intents.submit(track, OpDefinition("slow", (OpStep("a", "slow", "run", {"seconds": 120}),)), by=BY)
        intents.submit(track, OpDefinition("asked", (OpStep("a", "slow", "run", {"seconds": 120}),)), by=BY)
        _until(lambda: all("interaction_started" in _types(track, r) for r in ("slow", "asked")),
               "the owner never started the runs")
        assert id_held(spec, "pod-abc") is True
        intents.request_cancel(track, "asked", actor="alice")
        # The owner's pod is replaced: it never starts again under that id.
        os.killpg(owner.pid, signal.SIGKILL)
        owner.wait(10)
        _until(lambda: not id_held(spec, "pod-abc"), "the dead owner still holds its id")
        other = _start(base, "pod-xyz")
        processes.append(other)
        _until(lambda: intents.describe(track, "slow").state.ended and intents.describe(track, "asked").state.ended,
               "the live controller left the dead owner's runs as they were")
        assert intents.describe(track, "slow").state is RunState.INTERRUPTED
        [ended] = [e.payload for e in track.replay("slow") if e.event_type == "interrupted"]
        assert ended["taken_over_by"] == "pod-xyz" and ended["from_controller"] == "pod-abc"
        assert intents.describe(track, "asked").state is RunState.CANCELLED  # the cancel asked for was delivered
        # Every worker of the dead owner is recorded stopped; the launcher killed it with the owner.
        for run_id in ("slow", "asked"):
            assert [e.payload["reaped"] for e in track.replay(run_id) if e.event_type == "worker_stopped"] == [False]
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(10)


def test_a_run_whose_owner_is_alive_is_never_touched_by_another_controller(tmp_path):
    path = tmp_path / "track.sqlite"
    SqliteTrackStore.ensure_schema(path)
    track = SqliteTrackStore(path)
    held = hold_id(str(path), "pod-abc")  # the owner, alive: it holds its id
    blocked = InMemoryCogExecutor({"slow": lambda e, v, **f: time.sleep(60)})
    owner = RunController(LifecycleRunner(executor=blocked, track=track, controller="pod-abc"), poll_interval=0.01)
    intents.submit(track, OpDefinition("r", (OpStep("a", "slow", "run"),)), by=BY)
    owner.tick()
    _until(lambda: "interaction_started" in _types(track, "r"), "the owner never started the run")
    other = RunController(LifecycleRunner(executor=InMemoryCogExecutor({}), track=track, controller="pod-xyz"),
                          poll_interval=0.01, track_spec=str(path), owner_check_interval=0)
    for _ in range(5):
        other.tick()
    assert intents.describe(track, "r").state is RunState.RUNNING
    assert held()


def test_a_controller_without_a_track_spec_leaves_other_owners_runs_alone(tmp_path):
    track = InMemoryTrackStore()
    intents.submit(track, OpDefinition("r", (OpStep("a", "slow", "run"),)), by=BY)
    from collab_hub_execution.track import SCHEMA_VERSION, TrackEvent
    track.append(TrackEvent(run_id="r", event_type="run_picked_up", payload={"controller": "gone"},
                            schema=SCHEMA_VERSION))
    other = RunController(LifecycleRunner(executor=InMemoryCogExecutor({}), track=track, controller="pod-xyz"),
                          poll_interval=0.01)
    other.tick()
    assert intents.describe(track, "r").state is RunState.RUNNING


def test_a_host_starts_in_time_proportional_to_its_unfinished_runs_not_the_tracks_history():
    class Indexed(InMemoryTrackStore):
        run_ids_read = 0

        def run_ids(self):
            self.run_ids_read += 1
            return super().run_ids()

    track = Indexed()
    done = InMemoryCogExecutor({"echo": lambda e, v, **f: {"ok": True}})
    first = LifecycleRunner(executor=done, track=track)
    for n in range(20):
        assert first.submit(OpDefinition(f"done-{n}", (OpStep("a", "echo", "run"),))) is RunState.COMPLETED
    intents.submit(track, OpDefinition("left", (OpStep("a", "echo", "run"),)), by=BY)
    from collab_hub_execution.track import SCHEMA_VERSION, TrackEvent
    track.append(TrackEvent(run_id="left", event_type="run_picked_up", payload={}, schema=SCHEMA_VERSION))
    track.run_ids_read = 0
    second = LifecycleRunner(executor=done, track=track)
    assert second.start() == ("left",)
    assert track.run_ids_read == 0  # the index, not the history: every finished run went unread
