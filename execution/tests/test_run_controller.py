"""Intent on the Track, and the controller that acts on it (ADR-0002 D4).

The API's half is ``intents``: it records a submission or a request to cancel,
and reads a run back. The controller's half is ``RunController``: it starts
what was submitted and delivers what was asked. Neither calls the other.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from locations_support import gone, packages

from collab_hub_execution import (
    Gate,
    InMemoryCogExecutor,
    InMemoryTrackStore,
    LifecycleRunner,
    OneSubmissionPerRun,
    OpDefinition,
    OpStep,
    ResultEnvelope,
    RunState,
    SqliteTrackStore,
    intents,
)
from collab_hub_execution.controller import RunController
from collab_hub_execution.locations.local import TurnRefused
from collab_hub_execution.track import SCHEMA_VERSION, TrackEvent

BY = {"user": "alice", "org_id": "acme"}


def _controller(track, handlers):
    return RunController(LifecycleRunner(executor=InMemoryCogExecutor(handlers), track=track), poll_interval=0.01)


def _settle(controller, until=lambda: True):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        controller.tick()
        if until() and controller.idle():
            return
        time.sleep(0.01)
    raise AssertionError("the controller did not settle")


def _types(track, run_id):
    return [event.event_type for event in track.replay(run_id)]


def test_a_submission_is_only_intent_until_a_controller_picks_it_up():
    track = InMemoryTrackStore()
    op = OpDefinition("r", (OpStep("a", "echo", "run", 1), OpStep("b", "echo", "run", 2)))
    intents.submit(track, op, by=BY)
    assert _types(track, "r") == ["op_submitted"]
    view = intents.describe(track, "r")
    assert view.state is RunState.SUBMITTED and view.submitted_by == BY
    assert [(step.name, step.cog, step.state) for step in view.steps] == [("a", "echo", "pending"),
                                                                           ("b", "echo", "pending")]
    controller = _controller(track, {"echo": lambda entry, value: value})
    _settle(controller)
    view = intents.describe(track, "r")
    assert view.state is RunState.COMPLETED and [step.state for step in view.steps] == ["completed", "completed"]
    assert _types(track, "r")[:2] == ["op_submitted", "run_picked_up"]
    _settle(controller)  # an ended run is not picked up again
    assert _types(track, "r").count("run_picked_up") == 1


def test_a_run_id_is_submitted_once():
    track = InMemoryTrackStore()
    op = OpDefinition("r", (OpStep("a", "echo", "run"),))
    intents.submit(track, op, by=BY)
    with pytest.raises(OneSubmissionPerRun):
        intents.submit(track, op, by=BY)


def test_a_failed_step_is_told_with_its_error():
    track = InMemoryTrackStore()
    intents.submit(track, OpDefinition("r", (OpStep("a", "fails", "run"), OpStep("b", "fails", "run"))), by=BY)
    _settle(_controller(track, {"fails": lambda entry, value: ResultEnvelope.failure("model-call-failed", "no")}))
    view = intents.describe(track, "r")
    assert view.state is RunState.FAILED and view.error == "model-call-failed" and view.reason == "no"
    assert [(step.state, step.error) for step in view.steps] == [("failed", "model-call-failed"), ("pending", None)]


def test_a_request_to_cancel_is_delivered_to_the_run_in_flight():
    track, release = InMemoryTrackStore(), threading.Event()

    def slow(entry, value):
        release.wait(10)
        return value

    intents.submit(track, OpDefinition("r", (OpStep("wait", "slow", "run"), OpStep("never", "slow", "run"))), by=BY)
    controller = _controller(track, {"slow": slow})
    deadline = time.monotonic() + 10
    while "interaction_started" not in _types(track, "r"):
        assert time.monotonic() < deadline
        controller.tick()
        time.sleep(0.01)
    asked = intents.request_cancel(track, "r", actor="bob")
    assert asked.state is RunState.RUNNING and asked.cancel_requested_by == "bob"
    assert intents.request_cancel(track, "r", actor="carol").cancel_requested_by == "bob"  # recorded once
    controller.tick()
    release.set()
    _settle(controller)
    view = intents.describe(track, "r")
    assert view.state is RunState.CANCELLED
    assert [step.state for step in view.steps] == ["cancelled", "pending"]
    assert [e.payload for e in track.replay("r") if e.event_type == "cancelled"] == [{"actor": "bob"}]
    with pytest.raises(intents.RunEnded, match="has ended CANCELLED"):
        intents.request_cancel(track, "r", actor="bob")


def test_racing_cancel_requests_are_recorded_once_and_none_lands_on_a_run_that_ended():
    track = InMemoryTrackStore()
    intents.submit(track, OpDefinition("r", (OpStep("a", "echo", "run"),)), by=BY)
    start = threading.Barrier(8)

    def ask(n):
        start.wait()
        intents.request_cancel(track, "r", actor=f"actor-{n}")

    threads = [threading.Thread(target=ask, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert _types(track, "r").count("cancel_requested") == 1

    # A run that ended on its own: the request is refused, and nothing is written after its end.
    intents.submit(track, OpDefinition("done", (OpStep("a", "echo", "run"),)), by=BY)
    _settle(_controller(track, {"echo": lambda entry, value: value}))
    before = _types(track, "done")
    with pytest.raises(intents.RunEnded, match="has ended COMPLETED"):
        intents.request_cancel(track, "done", actor="bob")
    assert _types(track, "done") == before and before[-1] == "completed"


def test_a_cancel_request_a_run_outlived_does_not_cancel_its_retry():
    # Asked for, but the run failed on its own before the controller delivered it. Retried, the
    # run is a new attempt nobody asked to cancel.
    track, fail = InMemoryTrackStore(), [True]

    def flaky(entry, value):
        return ResultEnvelope.failure("model-call-failed", "no") if fail.pop() else ResultEnvelope.success(value)

    op = OpDefinition("r", (OpStep("a", "c", "run", 1),))
    intents.submit(track, op, by=BY)
    runner = LifecycleRunner(executor=InMemoryCogExecutor({"c": flaky}), track=track)
    # The request lands while the run is still advancing, and the run fails before it is delivered.
    real_materialize = runner.executor.materialize

    def materialize(cog, run_id, instance=""):
        intents.request_cancel(track, "r", actor="bob")
        return real_materialize(cog, run_id, instance)

    runner.executor.materialize = materialize
    assert runner.submit(op) is RunState.FAILED
    runner.executor.materialize = real_materialize
    assert intents.describe(track, "r").cancel_requested_by == "bob"
    fail.append(False)
    controller = RunController(runner, poll_interval=0.01)
    retried = threading.Thread(target=runner.retry, args=("r",))
    retried.start()
    retried.join()
    _settle(controller)
    view = intents.describe(track, "r")
    assert view.state is RunState.COMPLETED and view.cancel_requested_by is None
    assert "cancelled" not in _types(track, "r")


@pytest.mark.parametrize(("end", "state"), [("cancel", "cancelled"), ("reject", "rejected"),
                                             ("interrupt", "interrupted")])
def test_a_step_waiting_at_its_gate_when_the_run_ends_ends_with_it(end, state):
    track = InMemoryTrackStore()
    op = OpDefinition("r", (OpStep("draft", "echo", "run", gate=Gate(escalate="always")), OpStep("b", "echo", "run")))
    intents.submit(track, op, by=BY)
    controller = _controller(track, {"echo": lambda entry, value: value})
    _settle(controller)
    assert [step.state for step in intents.describe(track, "r").steps] == ["waiting_at_gate", "pending"]
    if end == "cancel":
        intents.request_cancel(track, "r", actor="bob")
        _settle(controller)
    elif end == "reject":
        runner = controller.runner
        runner.decide("r", escalation=runner.open_escalation("r")["escalation"], actor="alice", outcome="reject")
    else:
        assert _controller(track, {}).start() == ("r",)
    view = intents.describe(track, "r")
    assert view.status == state.upper()
    assert [step.state for step in view.steps] == [state, "pending"]


def test_a_run_cancelled_before_pickup_never_starts_and_an_unknown_run_is_not_cancelled():
    track = InMemoryTrackStore()
    intents.submit(track, OpDefinition("r", (OpStep("a", "echo", "run"),)), by=BY)
    intents.request_cancel(track, "r", actor="bob")
    controller = _controller(track, {"echo": lambda entry, value: value})
    _settle(controller)
    assert intents.describe(track, "r").state is RunState.CANCELLED
    assert controller.runner.executor.materialized == []
    with pytest.raises(LookupError):
        intents.request_cancel(track, "absent", actor="bob")
    assert intents.describe(track, "absent") is None


def test_a_starting_controller_interrupts_what_a_stopped_one_left_and_leaves_submissions_alone():
    track = InMemoryTrackStore()

    class Stops(BaseException):
        pass

    def stops(entry, value):
        raise Stops

    intents.submit(track, OpDefinition("left", (OpStep("a", "c", "run"),)), by=BY)
    with pytest.raises(Stops):
        LifecycleRunner(executor=InMemoryCogExecutor({"c": stops}), track=track).submit(
            OpDefinition("left", (OpStep("a", "c", "run"),)))
    intents.submit(track, OpDefinition("waiting", (OpStep("a", "c", "run"),)), by=BY)
    controller = _controller(track, {"c": lambda entry, value: value})
    assert controller.start() == ("left",)
    _settle(controller)
    assert intents.describe(track, "left").state is RunState.INTERRUPTED
    assert [step.state for step in intents.describe(track, "left").steps] == ["interrupted"]
    assert intents.describe(track, "waiting").state is RunState.COMPLETED
    assert [view.run_id for view in intents.list_runs(track)] == ["waiting", "left"]  # newest first


def test_one_runs_failure_does_not_stop_the_controller():
    track = InMemoryTrackStore()

    class Broken:
        def materialize(self, cog, run_id, instance=""):
            raise KeyboardInterrupt if run_id == "never" else RuntimeError("no worker today")

        def teardown(self, worker):
            pass

    intents.submit(track, OpDefinition("r", (OpStep("a", "c", "run"),)), by=BY)
    controller = RunController(LifecycleRunner(executor=Broken(), track=track), poll_interval=0.01)
    _settle(controller)
    view = intents.describe(track, "r")
    assert view.state is RunState.FAILED and view.error == "RuntimeError"


def test_the_controller_process_runs_a_submitted_package_and_holds_its_track(tmp_path):
    # The process itself: a submission written to a SQLite Track is run as a real worker, a second
    # controller under the same id refuses to start, and stopping it leaves no worker.
    track_path = tmp_path / "track.sqlite"
    SqliteTrackStore.ensure_schema(track_path)
    track = SqliteTrackStore(track_path)
    command = [sys.executable, "-m", "collab_hub_execution.controller", "--track", str(track_path),
               "--packages", str(packages(tmp_path)), "--work-dir", str(tmp_path / "runs"),
               "--poll-interval", "0.05", "--id", "one"]
    # The fake packages run under this interpreter here, so the executor is told not to use pixi.
    controller = subprocess.Popen([*command, "--environment", "host"], stderr=subprocess.PIPE, text=True)
    try:
        intents.submit(track, OpDefinition("r", (OpStep("a", "echo", "run", {"n": 1}),)), by=BY)
        deadline = time.monotonic() + 30
        while intents.describe(track, "r").state is not RunState.COMPLETED:
            assert time.monotonic() < deadline and controller.poll() is None, _types(track, "r")
            time.sleep(0.05)
        second = subprocess.run([*command, "--environment", "host"], capture_output=True, text=True, timeout=30)
        assert second.returncode == 1 and "a controller named 'one' is running" in second.stderr
        intents.submit(track, OpDefinition("slow", (OpStep("a", "slow", "run", {"seconds": 60}),)), by=BY)
        while "interaction_started" not in _types(track, "slow"):
            assert time.monotonic() < deadline and controller.poll() is None
            time.sleep(0.05)
        [worker] = [e.payload["pid"] for e in track.replay("slow") if e.event_type == "worker_started"]
    finally:
        controller.terminate()
        controller.wait(timeout=30)
    assert controller.returncode == 0
    assert gone(worker)


# --- turns: talking to a session Cog while its step runs -----------------------------------------

EXAMPLE_COGS = Path(__file__).resolve().parents[2] / "examples" / "cog-local" / "cogs"


def _local_controller(tmp_path, track):
    """A controller whose workers are real processes: the example's `hello` and the fake Cogs, under this Python."""
    root = packages(tmp_path)
    shutil.copytree(EXAMPLE_COGS / "hello", root / "hello", ignore=shutil.ignore_patterns(".pixi"))
    manifest = root / "hello" / "pixi.toml"
    manifest.write_text(re.sub(r'^serve = .*$', f'serve = "{sys.executable} serve.py"', manifest.read_text(),
                               flags=re.M))
    runner = LifecycleRunner(track=track, location="local", location_settings={
        "packages": [root], "work_dir": tmp_path / "runs", "environment": "host", "interaction_timeout": None})
    return RunController(runner, poll_interval=0.01, session_grace=0.5)


def _until(controller, check, what):
    deadline = time.monotonic() + 30
    while not check():
        assert time.monotonic() < deadline, what
        controller.tick()
        time.sleep(0.02)


def test_a_session_cog_answers_turns_in_order_and_ends_when_told(tmp_path):
    track = InMemoryTrackStore()
    controller = _local_controller(tmp_path, track)
    intents.submit(track, OpDefinition("r", (OpStep("chat", "hello", "session"),)), by=BY)
    # Asked before the worker is up: it waits, and is delivered once the session is open.
    first = intents.request_turn(track, "r", text="sum 1 2 3", actor="alice")
    assert first.state == "pending" and first.actor == "alice"
    second = intents.request_turn(track, "r", text="whoami", actor="alice")
    answered = lambda turn: intents.turns(track.replay("r"))[turn.turn].state == "answered"  # noqa: E731
    _until(controller, lambda: answered(first) and answered(second), "the turns were not answered")
    asked = intents.turns(track.replay("r"))
    assert asked[first.turn].answer == "1 + 2 + 3 = 6"
    assert "Run `r`" in asked[second.turn].answer
    # Every turn and its answer are on the Track, in the order they were asked.
    kinds = [event.event_type for event in track.replay("r") if event.event_type.startswith("turn_")]
    assert kinds == ["turn_requested", "turn_requested", "turn_answered", "turn_answered"]
    bye = intents.request_turn(track, "r", text="bye", actor="alice")
    _until(controller, lambda: intents.describe(track, "r").state is RunState.COMPLETED, "the session did not end")
    assert intents.turns(track.replay("r"))[bye.turn].answer.startswith("Bye!")
    view = intents.describe(track, "r")
    assert view.steps[0].output == {"turns": 3, "said": ["sum 1 2 3", "whoami", "bye"]}
    with pytest.raises(intents.RunEnded, match="has ended COMPLETED"):
        intents.request_turn(track, "r", text="hello", actor="alice")
    with pytest.raises(LookupError):
        intents.request_turn(track, "absent", text="hello", actor="alice")


def test_a_terminated_session_fails_the_turns_still_waiting(tmp_path):
    track = InMemoryTrackStore()
    controller = _local_controller(tmp_path, track)
    intents.submit(track, OpDefinition("r", (OpStep("chat", "hello", "session"),)), by=BY)
    _until(controller, lambda: "interaction_started" in _types(track, "r"), "the session never opened")
    intents.request_cancel(track, "r", actor="bob")
    late = intents.request_turn(track, "r", text="time", actor="alice")  # the run has not ended yet
    _until(controller, lambda: intents.describe(track, "r").state is RunState.CANCELLED, "the run was not cancelled")
    _settle(controller)
    turn = intents.turns(track.replay("r"))[late.turn]
    # Either it was never delivered, or it reached a worker already being torn down: failed both ways.
    assert turn.state == "failed"
    assert turn.error == "the run ended CANCELLED" or turn.error.startswith(
        ("TurnRefused", "ConnectError", "RemoteProtocolError", "ReadError")), turn.error


def test_a_cog_that_holds_no_session_fails_the_turn_and_its_run_goes_on(tmp_path):
    track = InMemoryTrackStore()
    controller = _local_controller(tmp_path, track)
    intents.submit(track, OpDefinition("r", (OpStep("wait", "slow", "run", {"seconds": 2}),)), by=BY)
    _until(controller, lambda: "interaction_started" in _types(track, "r"), "the step never started")
    turn = intents.request_turn(track, "r", text="hello", actor="alice")
    _until(controller, lambda: intents.turns(track.replay("r"))[turn.turn].state == "failed", "no failure recorded")
    assert "it holds no session" in intents.turns(track.replay("r"))[turn.turn].error
    _until(controller, lambda: intents.describe(track, "r").state is RunState.COMPLETED, "the run did not complete")


def test_a_worker_with_no_turns_at_all_is_said_so():
    track, release = InMemoryTrackStore(), threading.Event()
    intents.submit(track, OpDefinition("r", (OpStep("a", "c", "run"),)), by=BY)
    controller = _controller(track, {"c": lambda entry, value: release.wait(10) and value})
    _until(controller, lambda: "interaction_started" in _types(track, "r"), "the step never started")
    turn = intents.request_turn(track, "r", text="hello", actor="alice")
    _until(controller, lambda: intents.turns(track.replay("r"))[turn.turn].state == "failed", "no failure recorded")
    assert intents.turns(track.replay("r"))[turn.turn].error == "this Cog's worker takes no turns"
    release.set()
    _settle(controller)


def test_a_turn_is_bounded_and_answered_one_way():
    track = InMemoryTrackStore()
    intents.submit(track, OpDefinition("r", (OpStep("a", "c", "run"),)), by=BY)
    with pytest.raises(ValueError, match="at most"):
        intents.request_turn(track, "r", text="x" * (intents.MAX_TURN_TEXT + 1), actor="alice")
    with pytest.raises(ValueError, match="a text or failed with an error"):
        intents.answer_turn(track, "r", "t", text="a", error="b")


# --- review: unreadable runs, a session not open yet, reading only what is new ---------------


def test_one_run_that_cannot_be_read_hides_no_other_run():
    track = InMemoryTrackStore()
    # A Track the run machine could not have written: a decision with no Gate ever escalated.
    submission = {"op": {"run_id": "broken", "steps": []}}
    track.append(TrackEvent(run_id="broken", event_type="op_submitted", payload=submission, schema=SCHEMA_VERSION))
    track.append(TrackEvent(run_id="broken", event_type="gate_decided", payload={"outcome": "approve"},
                            schema=SCHEMA_VERSION))
    intents.submit(track, OpDefinition("good", (OpStep("a", "echo", "run", 1),)), by=BY)
    controller = _controller(track, {"echo": lambda entry, value: value})
    assert controller.start() == ()  # a host starts, passing over the run it cannot read
    _settle(controller)
    assert intents.describe(track, "good").state is RunState.COMPLETED  # picked up, despite the broken run
    assert [view.run_id for view in intents.list_runs(track)] == ["good"]  # listed, the broken one left out
    with pytest.raises(intents.RunUnreadable, match="broken"):
        intents.RunViews(track).view("broken")


class _OpensLate:
    """An executor whose worker answers "no session" to its first turns, as one still opening it does."""

    def __init__(self, refusals):
        self.refusals, self.release = refusals, threading.Event()

    def materialize(self, cog, run_id, instance=""):
        executor = self

        class Worker:
            def interact(self, entry_point, input=None, idempotency_key=None, **_):
                executor.release.wait(10)
                return ResultEnvelope.success({"ok": True})

            def turn(self, turn, text):
                if executor.refusals > 0:
                    executor.refusals -= 1
                    raise TurnRefused("the worker answered HTTP 404: it holds no session", status=404)
                return f"heard: {text}"

        return Worker()

    def teardown(self, worker):
        pass


def test_a_turn_that_reaches_a_worker_still_opening_its_session_waits_for_it():
    track, executor = InMemoryTrackStore(), _OpensLate(refusals=3)
    controller = RunController(LifecycleRunner(executor=executor, track=track), poll_interval=0.01, session_grace=10)
    intents.submit(track, OpDefinition("r", (OpStep("chat", "c", "session"),)), by=BY)
    turn = intents.request_turn(track, "r", text="hello", actor="alice")
    deadline = time.monotonic() + 10
    while intents.turns(track.replay("r"))[turn.turn].state == "pending":
        assert time.monotonic() < deadline
        controller.tick()
        time.sleep(0.01)
    assert intents.turns(track.replay("r"))[turn.turn].answer == "heard: hello" and executor.refusals == 0
    executor.release.set()
    _settle(controller)


def test_a_worker_that_never_opens_a_session_fails_the_turn_after_the_grace():
    track, executor = InMemoryTrackStore(), _OpensLate(refusals=10**6)
    controller = RunController(LifecycleRunner(executor=executor, track=track), poll_interval=0.01, session_grace=0.3)
    intents.submit(track, OpDefinition("r", (OpStep("chat", "c", "session"),)), by=BY)
    turn = intents.request_turn(track, "r", text="hello", actor="alice")
    deadline = time.monotonic() + 10
    while intents.turns(track.replay("r"))[turn.turn].state == "pending":
        assert time.monotonic() < deadline
        controller.tick()
        time.sleep(0.01)
    assert "holds no session" in intents.turns(track.replay("r"))[turn.turn].error
    executor.release.set()
    _settle(controller)


def test_a_run_waiting_at_a_gate_takes_no_turns():
    track = InMemoryTrackStore()
    intents.submit(track, OpDefinition("r", (OpStep("a", "echo", "run", gate=Gate(escalate="always")),)), by=BY)
    _settle(_controller(track, {"echo": lambda entry, value: value}))
    with pytest.raises(intents.RunEnded, match="waiting at a Gate"):
        intents.request_turn(track, "r", text="hello", actor="alice")


def test_run_views_read_only_what_each_track_gained():
    class Counting(InMemoryTrackStore):
        def __init__(self):
            super().__init__()
            self.read: list[int] = []

        def replay(self, run_id, *, after_sequence=0):
            events = super().replay(run_id, after_sequence=after_sequence)
            self.read.append(len(events))
            return events

    track = Counting()
    for n in range(3):
        intents.submit(track, OpDefinition(f"r{n}", (OpStep("a", "echo", "run"),)), by=BY)
    views = intents.RunViews(track)
    assert len(views.views()) == 3 and track.read == [1, 1, 1]
    track.read.clear()
    assert len(views.views(org_id="acme")) == 3 and track.read == [0, 0, 0]  # nothing new: nothing read
    intents.request_cancel(track, "r1", actor="bob")
    track.read.clear()
    assert views.view("r1").cancel_requested_by == "bob" and track.read == [1]  # only the new event
    assert views.views(org_id="other") == () and views.views(status="SUBMITTED")[0].run_id in ("r0", "r1", "r2")


def test_run_views_keep_nothing_for_unknown_ids_and_let_an_ended_runs_events_go():
    track = InMemoryTrackStore()
    views = intents.RunViews(track)
    for n in range(1000):  # ids nobody submitted, as any client can ask for
        assert views.view(f"run-{n:012d}") is None
    assert views._kept == {}
    intents.submit(track, OpDefinition("r", (OpStep("a", "c", "run"),)), by=BY)
    turn = intents.request_turn(track, "r", text="hello", actor="alice")
    intents.request_cancel(track, "r", actor="bob")
    _settle(_controller(track, {"c": lambda entry, value: value}))
    view = views.view("r")
    assert view.state is RunState.CANCELLED and views._kept["r"].events is None  # ended: events let go
    assert views.turns("r")[turn.turn].state == "failed"  # its turns are still told
    assert [event.event_type for event in views.events("r")][-1] == "cancelled"  # read again when asked
    views.forget("r")
    assert views._kept == {}
