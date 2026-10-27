"""Run pickup and ownership: several controllers on one Track (Phase 11, ADR-0002 D4).

A controller picks a run up with a record that lands only if nobody picked it
up first, so of several controllers one starts each run. The run is then that
controller's: it alone advances it, delivers its cancel, its turns and the
decision on its Gate, and when it is killed, the run ends ``interrupted`` the
next time it starts, its workers reaped.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter

import pytest
from locations_support import alive, gone, packages

from collab_hub_execution import (
    Gate,
    InMemoryCogExecutor,
    InMemoryTrackStore,
    LifecycleRunner,
    OpDefinition,
    OpStep,
    RunState,
    SqliteTrackStore,
    intents,
)
from collab_hub_execution.controller import RunController
from collab_hub_execution.runner import RunOwnedElsewhere
from collab_hub_execution.track import SCHEMA_VERSION, TrackEvent

BY = {"user": "alice", "org_id": "acme"}
TEST_PG = os.environ.get("TEST_POSTGRES_URL")


def _named(track, name, handlers, **runner):
    return RunController(LifecycleRunner(executor=InMemoryCogExecutor(handlers), track=track, controller=name,
                                         **runner), poll_interval=0.005)


def _types(track, run_id):
    return [event.event_type for event in track.replay(run_id)]


def _settle(*controllers, until, within=20.0):
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        for controller in controllers:
            controller.tick()
        if until() and all(controller.idle() for controller in controllers):
            return
        time.sleep(0.005)
    raise AssertionError("the controllers did not settle")


# --- pickup ------------------------------------------------------------------------------------


def test_of_two_controllers_passing_over_one_track_at_once_one_starts_each_run():
    track = InMemoryTrackStore()
    started = Counter()
    lock = threading.Lock()

    def echo(entry, value):
        with lock:
            started[value["run"]] += 1
        return value

    one, two = _named(track, "one", {"echo": echo}), _named(track, "two", {"echo": echo})
    runs = [f"r{n}" for n in range(40)]
    stop = threading.Event()
    passes = [threading.Thread(target=controller.run, args=(stop,)) for controller in (one, two)]
    for thread in passes:
        thread.start()
    try:
        for run_id in runs:
            intents.submit(track, OpDefinition(run_id, (OpStep("a", "echo", "run", {"run": run_id}),)), by=BY)
        deadline = time.monotonic() + 30
        while not all(intents.describe(track, run_id).state is RunState.COMPLETED for run_id in runs):
            assert time.monotonic() < deadline, "not every run completed"
            time.sleep(0.01)
    finally:
        stop.set()
        for thread in passes:
            thread.join(10)
    assert all(_types(track, run_id).count("run_picked_up") == 1 for run_id in runs)
    assert started == Counter({run_id: 1 for run_id in runs})  # each worker invoked once, by one controller
    owners = Counter(intents.describe(track, run_id).controller for run_id in runs)
    assert set(owners) <= {"one", "two"} and sum(owners.values()) == len(runs)


def _cancel_unless_ended(track, run_id):
    try:
        intents.request_cancel(track, run_id, actor="bob")
    except intents.RunEnded:
        pass  # it completed first: the other way this race may go


def test_a_run_cancelled_as_it_is_picked_up_either_never_starts_or_is_cancelled_by_its_owner():
    for _ in range(20):
        track = InMemoryTrackStore()
        intents.submit(track, OpDefinition("r", (OpStep("a", "echo", "run"),)), by=BY)
        one, two = _named(track, "one", {"echo": lambda e, v: v}), _named(track, "two", {"echo": lambda e, v: v})
        canceller = threading.Thread(target=_cancel_unless_ended, args=(track, "r"))
        canceller.start()
        _settle(one, two, until=lambda: intents.describe(track, "r").state.ended)
        canceller.join()
        kinds = _types(track, "r")
        assert kinds.count("run_picked_up") <= 1 and kinds.count("cancelled") + kinds.count("completed") == 1
        assert intents.describe(track, "r").state in (RunState.CANCELLED, RunState.COMPLETED)


# --- ownership ---------------------------------------------------------------------------------


def _left_running(track, run_id, controller):
    """A run a controller picked up and was stopped while it ran: its Track ends at the pickup."""
    intents.submit(track, OpDefinition(run_id, (OpStep("a", "echo", "run"),)), by=BY)
    track.append(TrackEvent(run_id=run_id, event_type="run_picked_up", payload={"controller": controller},
                            schema=SCHEMA_VERSION))


def test_a_starting_controller_interrupts_only_the_runs_it_picked_up():
    track = InMemoryTrackStore()
    _left_running(track, "mine", "one")
    _left_running(track, "theirs", "two")
    assert _named(track, "one", {}).start() == ("mine",)
    assert intents.describe(track, "theirs").state is RunState.RUNNING  # two records it when two starts
    assert _named(track, "two", {}).start() == ("theirs",)
    assert intents.describe(track, "mine").state is RunState.INTERRUPTED


def test_a_run_another_controller_owns_is_neither_cancelled_nor_decided_here():
    track = InMemoryTrackStore()
    _left_running(track, "r", "two")
    runner = _named(track, "one", {}).runner
    with pytest.raises(RunOwnedElsewhere, match="'two'"):
        runner.cancel("r", actor="bob")
    intents.request_cancel(track, "r", actor="bob")
    one = _named(track, "one", {})
    one.tick()
    assert one.idle() and intents.describe(track, "r").state is RunState.RUNNING  # one leaves it to two


def test_two_controllers_starting_at_once_interrupt_an_unowned_run_once():
    # A run picked up before controllers named themselves belongs to whichever starts first.
    for _ in range(20):
        track = InMemoryTrackStore()
        intents.submit(track, OpDefinition("r", (OpStep("a", "echo", "run"),)), by=BY)
        track.append(TrackEvent(run_id="r", event_type="run_picked_up", payload={}, schema=SCHEMA_VERSION))
        results = []
        starts = [threading.Thread(target=lambda name=name: results.append(_named(track, name, {}).start()))
                  for name in ("one", "two")]
        for thread in starts:
            thread.start()
        for thread in starts:
            thread.join()
        assert sorted(results) == [(), ("r",)]
        assert _types(track, "r").count("interrupted") == 1


# --- decisions through the Track ---------------------------------------------------------------


def _waiting_at_gate(track, controller):
    op = OpDefinition("r", (OpStep("draft", "echo", "run", {"text": "x"}, gate=Gate(escalate="always")),))
    intents.submit(track, op, by=BY)
    _settle(controller, until=lambda: intents.describe(track, "r").state is RunState.WAITING_AT_GATE)
    return controller.runner.open_escalation("r")["escalation"]


def test_a_decision_asked_through_the_track_is_delivered_by_the_runs_controller():
    track = InMemoryTrackStore()
    one = _named(track, "one", {"echo": lambda e, v: v})
    escalation = _waiting_at_gate(track, one)
    view = intents.request_decision(track, "r", escalation=escalation, outcome="approve", actor="alice")
    assert view.decision["outcome"] == "approve" and view.state is RunState.WAITING_AT_GATE
    two = _named(track, "two", {"echo": lambda e, v: v})
    two.tick()
    assert two.idle() and intents.describe(track, "r").state is RunState.WAITING_AT_GATE  # not two's run
    _settle(one, until=lambda: intents.describe(track, "r").state is RunState.COMPLETED)
    decided = [e.payload for e in track.replay("r") if e.event_type == "gate_decided"]
    assert [(d["escalation"], d["actor"], d["outcome"]) for d in decided] == [(escalation, "alice", "approve")]
    assert intents.describe(track, "r").decision is None


def test_a_decision_on_an_escalation_the_run_does_not_wait_on_is_refused_when_asked():
    track = InMemoryTrackStore()
    one = _named(track, "one", {"echo": lambda e, v: v})
    escalation = _waiting_at_gate(track, one)
    with pytest.raises(intents.StaleDecision, match="does not wait on escalation 'esc-other'"):
        intents.request_decision(track, "r", escalation="esc-other", outcome="approve", actor="alice")
    with pytest.raises(ValueError, match="one of"):
        intents.request_decision(track, "r", escalation=escalation, outcome="maybe", actor="alice")
    intents.request_decision(track, "r", escalation=escalation, outcome="reject", actor="alice")
    with pytest.raises(intents.StaleDecision, match="already waiting"):
        intents.request_decision(track, "r", escalation=escalation, outcome="approve", actor="bob")
    _settle(one, until=lambda: intents.describe(track, "r").state.ended)
    assert intents.describe(track, "r").state is RunState.REJECTED
    with pytest.raises(intents.RunEnded):
        intents.request_decision(track, "r", escalation=escalation, outcome="approve", actor="bob")


def test_a_send_back_beyond_the_revision_limit_fails_the_run_as_the_machine_says():
    track = InMemoryTrackStore()
    one = _named(track, "one", {"echo": lambda e, v: v}, max_revisions=0)
    escalation = _waiting_at_gate(track, one)
    intents.request_decision(track, "r", escalation=escalation, outcome="send_back", actor="alice",
                             findings=["shorter"])
    _settle(one, until=lambda: intents.describe(track, "r").state.ended)
    view = intents.describe(track, "r")
    assert view.state is RunState.FAILED and view.error == "revise_limit_exceeded" and view.decision is None


def test_a_decision_the_runner_does_not_take_is_recorded_refused_and_another_can_be_asked(monkeypatch):
    from collab_hub_execution.states import StaleEscalation

    track = InMemoryTrackStore()
    one = _named(track, "one", {"echo": lambda e, v: v})
    escalation = _waiting_at_gate(track, one)
    refuse = monkeypatch.setattr
    refuse(one.runner, "decide", lambda *a, **k: (_ for _ in ()).throw(
        StaleEscalation(RunState.WAITING_AT_GATE, "decide", "not this escalation")))
    intents.request_decision(track, "r", escalation=escalation, outcome="approve", actor="alice")
    _settle(one, until=lambda: "decision_refused" in _types(track, "r"))
    [refused] = [e.payload for e in track.replay("r") if e.event_type == "decision_refused"]
    assert refused["escalation"] == escalation and "not this escalation" in refused["error"]
    view = intents.describe(track, "r")
    assert view.state is RunState.WAITING_AT_GATE and view.decision is None
    monkeypatch.undo()
    intents.request_decision(track, "r", escalation=escalation, outcome="approve", actor="bob")
    _settle(one, until=lambda: intents.describe(track, "r").state is RunState.COMPLETED)


def test_a_decision_left_undelivered_by_a_restart_is_not_pending_against_the_retrys_escalation():
    # The owner restarts between `decision_requested` and delivery, and the run is retried: the old
    # decision names an escalation the run no longer waits on, so it is not pending, nothing refuses
    # it, and a decision on the new escalation is accepted at once.
    track = InMemoryTrackStore()
    one = _named(track, "one", {"echo": lambda e, v: v})
    first = _waiting_at_gate(track, one)
    intents.request_decision(track, "r", escalation=first, outcome="approve", actor="alice")
    again = _named(track, "one", {"echo": lambda e, v: v})  # the same id, started again: no delivery
    assert again.start() == ("r",) and intents.describe(track, "r").state is RunState.INTERRUPTED
    assert intents.describe(track, "r").decision is None
    again.runner.retry("r")
    _settle(again, until=lambda: intents.describe(track, "r").state is RunState.WAITING_AT_GATE)
    second = again.runner.open_escalation("r")["escalation"]
    assert second != first and intents.describe(track, "r").decision is None
    view = intents.request_decision(track, "r", escalation=second, outcome="approve", actor="bob")
    assert view.decision["escalation"] == second
    _settle(again, until=lambda: intents.describe(track, "r").state is RunState.COMPLETED)
    assert "decision_refused" not in _types(track, "r")
    decided = [e.payload["escalation"] for e in track.replay("r") if e.event_type == "gate_decided"]
    assert decided == [second]


def test_a_numeric_variable_that_is_not_a_number_is_a_usage_error(monkeypatch, tmp_path, capsys):
    from collab_hub_execution.controller import parse_args

    monkeypatch.setenv("COLLAB_CONTROLLER_TRACK", str(tmp_path / "t.sqlite"))
    monkeypatch.setenv("COLLAB_CONTROLLER_WORK_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("COLLAB_CONTROLLER_PACKAGES", "/a")
    for name in ("POLL_INTERVAL", "INTERACTION_TIMEOUT", "HEALTH_PORT", "WORKER_START_TIMEOUT", "WAIT"):
        monkeypatch.setenv(f"COLLAB_CONTROLLER_{name}", "soon")
        with pytest.raises(SystemExit) as stopped:
            parse_args([])
        assert stopped.value.code == 2
        assert f"COLLAB_CONTROLLER_{name} is not a number: 'soon'" in capsys.readouterr().err
        monkeypatch.delenv(f"COLLAB_CONTROLLER_{name}")
    assert parse_args([]).health_port == 0


# --- the reaper ----------------------------------------------------------------------------------


def _sleeper(run_id):
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                            env={**os.environ, "COLLAB_RUN_ID": run_id}, start_new_session=True)


def test_a_starting_controller_reaps_the_live_workers_of_the_runs_it_interrupts(tmp_path):
    track = InMemoryTrackStore()
    runner = LifecycleRunner(track=track, location="local", controller="one", location_settings={
        "packages": [packages(tmp_path)], "work_dir": tmp_path / "runs", "environment": "host", "grace": 0.5})
    survivor, stranger = _sleeper("left"), _sleeper("someone-else")
    try:
        for run_id, process in (("left", survivor), ("reused", stranger)):
            _left_running(track, run_id, "one")
            # `reused` recorded a pid the system has since given to a process of another run.
            track.append(TrackEvent(run_id=run_id, event_type="worker_started", schema=SCHEMA_VERSION, payload={
                "step": "a", "attempt": 1, "instance": "a:1", "location": "local", "pid": process.pid,
                "pgid": process.pid, "run_token_sha256": "0" * 64}))
        assert sorted(runner.start()) == ["left", "reused"]
        assert gone(survivor.pid)
        assert alive(stranger.pid)  # not this run's worker: left alone
        stops = {run_id: [e.payload for e in track.replay(run_id) if e.event_type == "worker_stopped"]
                 for run_id in ("left", "reused")}
        assert stops["left"][0]["reaped"] is True and stops["reused"][0]["reaped"] is False
        assert runner.start() == ()  # nothing is reaped twice
    finally:
        for process in (survivor, stranger):
            if process.poll() is None:
                process.kill()
            process.wait(10)


# --- the process: ids, health, kill, and replicas -----------------------------------------------


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _get(port, path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=2) as response:  # noqa: S310
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())
    except OSError:
        return None, None


class _Controllers:
    """Controller processes on one Track, each started with an id and a health port."""

    def __init__(self, tmp_path, track_spec):
        self.base = [sys.executable, "-m", "collab_hub_execution.controller", "--track", track_spec,
                     "--packages", str(packages(tmp_path)), "--work-dir", str(tmp_path / "runs"),
                     "--poll-interval", "0.02", "--environment", "host"]
        self.processes: list[subprocess.Popen] = []

    def start(self, name):
        port = _free_port()
        process = subprocess.Popen([*self.base, "--id", name, "--health-port", str(port)],
                                   stderr=subprocess.DEVNULL, start_new_session=True)
        self.processes.append(process)
        deadline = time.monotonic() + 30
        while _get(port, "/readyz")[0] != 200:
            assert process.poll() is None and time.monotonic() < deadline, f"controller {name} never got ready"
            time.sleep(0.05)
        return process, port

    def close(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
            process.wait(30)


def _until(check, what, within=30.0):
    deadline = time.monotonic() + within
    while not check():
        assert time.monotonic() < deadline, what
        time.sleep(0.05)


def test_a_killed_controllers_run_is_ended_by_a_live_controller_once_its_id_is_free(tmp_path):
    track_path = tmp_path / "track.sqlite"
    SqliteTrackStore.ensure_schema(track_path)
    track = SqliteTrackStore(track_path)
    controllers = _Controllers(tmp_path, str(track_path))
    try:
        one, port = controllers.start("one")
        status, health = _get(port, "/healthz")
        assert status == 200 and health["controller"] == "one" and health["ready"]
        assert _get(port, "/nope")[0] == 404
        intents.submit(track, OpDefinition("slow", (OpStep("a", "slow", "run", {"seconds": 60}),)), by=BY)
        _until(lambda: "interaction_started" in _types(track, "slow"), "the slow step never started")
        [worker] = [e.payload["pid"] for e in track.replay("slow") if e.event_type == "worker_started"]
        os.kill(one.pid, signal.SIGKILL)
        one.wait(10)
        assert gone(worker)  # its launcher let go of it when the controller died
        assert intents.describe(track, "slow").state is RunState.RUNNING  # nobody has said otherwise yet
        two, _ = controllers.start("two")
        intents.submit(track, OpDefinition("echo", (OpStep("a", "echo", "run", {"n": 1}),)), by=BY)
        _until(lambda: intents.describe(track, "echo").state is RunState.COMPLETED, "two never ran echo")
        # One's id is free, so two ends the run one left: it was picked up by a controller that is gone.
        _until(lambda: intents.describe(track, "slow").state is RunState.INTERRUPTED, "two never took it over")
        [ended] = [e.payload for e in track.replay("slow") if e.event_type == "interrupted"]
        assert ended["taken_over_by"] == "two" and ended["from_controller"] == "one"
        assert [e.payload.get("reaped") for e in track.replay("slow") if e.event_type == "worker_stopped"] == [False]
        controllers.start("one")  # back under its id, it finds nothing of its own left unfinished
        intents.submit(track, OpDefinition("again", (OpStep("a", "echo", "run", {"n": 2}),)), by=BY)
        _until(lambda: intents.describe(track, "again").state is RunState.COMPLETED, "the runs stopped")
        assert _types(track, "slow").count("interrupted") == 1
    finally:
        controllers.close()


def _replicas_never_share_a_run(tmp_path, track, spec):
    controllers = _Controllers(tmp_path, spec)
    runs = [f"r{n}" for n in range(12)]
    try:
        controllers.start("one")
        controllers.start("two")
        for run_id in runs:
            intents.submit(track, OpDefinition(run_id, (OpStep("a", "echo", "run", {"run": run_id}),)), by=BY)
        _until(lambda: all(intents.describe(track, r).state is RunState.COMPLETED for r in runs),
               "not every run completed", within=60)
    finally:
        controllers.close()
    for run_id in runs:
        kinds = _types(track, run_id)
        assert kinds.count("run_picked_up") == 1 and kinds.count("worker_started") == 1, (run_id, kinds)


def test_two_controller_processes_on_one_sqlite_track_never_both_start_a_run(tmp_path):
    track_path = tmp_path / "track.sqlite"
    SqliteTrackStore.ensure_schema(track_path)
    _replicas_never_share_a_run(tmp_path, SqliteTrackStore(track_path), str(track_path))


@pytest.mark.skipif(not TEST_PG, reason="set TEST_POSTGRES_URL to run the Postgres controller tests")
def test_two_controller_processes_on_one_postgres_track_never_both_start_a_run(tmp_path):
    from psycopg_pool import ConnectionPool

    from collab_hub_execution import PostgresTrackStore

    pool = ConnectionPool(TEST_PG, min_size=1, max_size=4, open=True)
    try:
        with pool.connection() as connection:
            PostgresTrackStore.ensure_schema(connection)
            connection.execute("TRUNCATE collab_track_events, collab_track_payloads")
        _replicas_never_share_a_run(tmp_path, PostgresTrackStore(pool), TEST_PG)
    finally:
        pool.close()


def test_a_second_controller_under_a_taken_id_is_refused(tmp_path):
    track_path = tmp_path / "track.sqlite"
    controllers = _Controllers(tmp_path, str(track_path))
    try:
        controllers.start("one")
        second = subprocess.run([*controllers.base, "--id", "one"], capture_output=True, text=True, timeout=30)
        assert second.returncode == 1 and "a controller named 'one' is running" in second.stderr
    finally:
        controllers.close()


# --- configuration -----------------------------------------------------------------------------


def test_the_id_is_held_for_as_long_as_its_check_is_kept(tmp_path):
    import gc

    from collab_hub_execution.controller import IdTaken, hold_id

    track = str(tmp_path / "track.sqlite")
    still_held = hold_id(track, "make-op")
    with pytest.raises(IdTaken):
        hold_id(track, "make-op")
    assert still_held()
    del still_held  # dropping the check lets the lock go: callers keep it, as `make op` now does
    gc.collect()
    assert hold_id(track, "make-op")()


def test_every_option_reads_its_variable_and_the_command_line_wins(monkeypatch, tmp_path):
    from collab_hub_execution.controller import parse_args

    for name, value in {"TRACK": str(tmp_path / "t.sqlite"), "WORK_DIR": str(tmp_path / "runs"),
                        "PACKAGES": os.pathsep.join(["/a", "/b"]), "ALLOW": "echo,slow", "ENVIRONMENT": "host",
                        "POLL_INTERVAL": "0.5", "INTERACTION_TIMEOUT": "0", "HEALTH_PORT": "8770",
                        "WORKER_START_TIMEOUT": "900",
                        "ID": "one"}.items():
        monkeypatch.setenv(f"COLLAB_CONTROLLER_{name}", value)
    args = parse_args([])
    assert (args.packages, args.allow, args.environment, args.poll_interval, args.interaction_timeout,
            args.health_port, args.id, args.worker_start_timeout) == (
        ["/a", "/b"], ["echo", "slow"], "host", 0.5, 0.0, 8770, "one", 900.0)
    args = parse_args(["--packages", "/c", "--environment", "pixi"])
    assert args.packages == ["/c"] and args.environment == "pixi"
    monkeypatch.setenv("COLLAB_CONTROLLER_ENVIRONMENT", "docker")
    with pytest.raises(SystemExit):
        parse_args([])  # a variable's value is checked as the option's would be


def test_a_decision_with_findings_that_are_not_a_sequence_is_refused_before_it_is_recorded():
    track = InMemoryTrackStore()
    one = _named(track, "one", {"echo": lambda e, v: v})
    escalation = _waiting_at_gate(track, one)
    for findings in ("shorter", {"tone": "warmer"}, {"a", "b"}, b"x"):
        with pytest.raises(ValueError, match="sequence of findings"):
            intents.request_decision(track, "r", escalation=escalation, outcome="send_back", actor="alice",
                                     findings=findings)
    assert "decision_requested" not in _types(track, "r")
    intents.request_decision(track, "r", escalation=escalation, outcome="send_back", actor="alice",
                             findings=("shorter",))


@pytest.mark.skipif(not TEST_PG, reason="set TEST_POSTGRES_URL to run the Postgres controller tests")
def test_a_postgres_with_part_of_a_track_is_refused_naming_what_is_missing():
    import psycopg

    from collab_hub_execution.controller import open_track

    with psycopg.connect(TEST_PG, autocommit=True) as admin:
        admin.execute("DROP DATABASE IF EXISTS track_partial")
        admin.execute("CREATE DATABASE track_partial")
    partial = TEST_PG.rsplit("/", 1)[0] + "/track_partial"
    try:
        with psycopg.connect(partial, autocommit=True) as connection:
            connection.execute("CREATE TABLE collab_track_events (sequence bigint)")
        with pytest.raises(SystemExit, match="missing collab_track_payloads"):
            open_track(partial)
    finally:
        with psycopg.connect(TEST_PG, autocommit=True) as admin:
            admin.execute("DROP DATABASE IF EXISTS track_partial WITH (FORCE)")


def test_a_controller_told_to_wait_takes_its_id_over_once_the_one_holding_it_stops(tmp_path):
    # A rollout: the new pod's controller starts while the old one still holds the id, and waits for it
    # rather than exiting; once the old one stops, it takes the id and its runs over.
    track_path = tmp_path / "track.sqlite"
    SqliteTrackStore.ensure_schema(track_path)
    track = SqliteTrackStore(track_path)
    controllers = _Controllers(tmp_path, str(track_path))
    try:
        old, _ = controllers.start("hub")
        intents.submit(track, OpDefinition("slow", (OpStep("a", "slow", "run", {"seconds": 60}),)), by=BY)
        _until(lambda: "interaction_started" in _types(track, "slow"), "the slow step never started")
        port = _free_port()
        new = subprocess.Popen([*controllers.base, "--id", "hub", "--wait", "60", "--health-port", str(port)],
                               stderr=subprocess.DEVNULL, start_new_session=True)
        controllers.processes.append(new)
        time.sleep(2)
        # Waiting, not refused: alive, so a liveness probe leaves it be, and not ready, saying for what.
        assert new.poll() is None and _get(port, "/healthz")[0] == 200
        status, waiting = _get(port, "/readyz")
        assert status == 503 and "holds the id 'hub'" in waiting["error"]
        old.terminate()
        old.wait(30)
        _until(lambda: _get(port, "/readyz")[0] == 200, "the waiting controller never took the id", within=60)
        _until(lambda: intents.describe(track, "slow").state is RunState.INTERRUPTED,
               "the new controller did not take the old one's run")
    finally:
        controllers.close()


def test_a_controller_told_to_wait_still_refuses_a_taken_id_once_its_wait_is_over(tmp_path):
    track_path = tmp_path / "track.sqlite"
    controllers = _Controllers(tmp_path, str(track_path))
    try:
        controllers.start("hub")
        started = time.monotonic()
        second = subprocess.run([*controllers.base, "--id", "hub", "--wait", "2"], capture_output=True, text=True,
                                timeout=60)
        assert second.returncode == 1 and "a controller named 'hub' is running" in second.stderr
        assert time.monotonic() - started >= 2
    finally:
        controllers.close()


def test_a_track_url_of_another_kind_is_refused_not_taken_for_a_file(tmp_path, monkeypatch):
    from collab_hub_execution.controller import open_track

    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match="postgresql://"):
        open_track("mysql://hub/runs")
    assert list(tmp_path.iterdir()) == []  # no file named after the URL


def test_a_misconfigured_track_stops_the_controller_at_once_even_told_to_wait(tmp_path):
    # Only a Track that may be ready soon is waited for; a wrong scheme never will be.
    started = time.monotonic()
    refused = subprocess.run([sys.executable, "-m", "collab_hub_execution.controller", "--track", "mysql://hub/runs",
                              "--packages", str(tmp_path), "--work-dir", str(tmp_path / "runs"), "--wait", "60"],
                             capture_output=True, text=True, timeout=60)
    assert refused.returncode != 0 and "postgresql://" in refused.stderr
    assert time.monotonic() - started < 30


@pytest.mark.skipif(not TEST_PG, reason="set TEST_POSTGRES_URL to run the Postgres controller tests")
def test_a_database_that_does_not_answer_is_waited_for_like_missing_tables():
    from collab_hub_execution.controller import TrackNotReady, open_track

    with pytest.raises(TrackNotReady, match="does not answer"):
        open_track("postgresql://nobody:x@127.0.0.1:1/none")
