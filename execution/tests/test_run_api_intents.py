"""What the run API's later routes ask of the execution package: Frames on a step, retries, the open escalation.

The API records intent on the Track and reads the run from it; a controller
over the same Track acts on it. These tests drive that pair in process.
"""

from __future__ import annotations

import json
import time

import pytest

from collab_hub_execution import (
    Gate,
    InMemoryCogExecutor,
    InMemoryTrackStore,
    LifecycleRunner,
    OpDefinition,
    OpStep,
    ResultEnvelope,
    RunState,
    intents,
)
from collab_hub_execution.controller import RunController
from collab_hub_execution.ops import FrameRef, _deserialize_op, _serialize_op

BY = {"user": "alice", "org_id": "acme"}


def _controller(track, handlers, name="one"):
    return RunController(LifecycleRunner(executor=InMemoryCogExecutor(handlers), track=track, controller=name),
                         poll_interval=0.005)


def _settle(controller, until, within=20.0):
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        controller.tick()
        if until() and controller.idle():
            return
        time.sleep(0.005)
    raise AssertionError("the controller did not settle")


def _types(track, run_id):
    return [event.event_type for event in track.replay(run_id)]


# --- Frames on a step -------------------------------------------------------------------------------


def _snapshot(track, run_id, frame_id, text):
    ref = f"frame:{run_id}:{frame_id}"
    track.put_payload(run_id, ref, json.dumps({"id": frame_id, "text": text}))
    return FrameRef(id=frame_id, name=f"Frame {frame_id}", digest=f"sha256-{frame_id}", ref=ref)


def test_a_steps_frames_reach_the_worker_in_the_task_bundle_from_the_snapshot_submit_kept():
    track = InMemoryTrackStore()
    seen = {}

    def grounded(entry_point, value, **feedback):
        seen.update(feedback)
        return ResultEnvelope.success({"ok": True})

    frames = (_snapshot(track, "r", "f1", "Write plainly."), _snapshot(track, "r", "f2", "A Cog is a worker."))
    op = OpDefinition("r", (OpStep("draft", "writer", "ask", {"prompt": "hi"}, frames=frames),))
    intents.submit(track, op, by=BY)
    controller = _controller(track, {"writer": grounded})
    _settle(controller, until=lambda: intents.describe(track, "r").state is RunState.COMPLETED)
    assert seen["frames"] == [
        {"id": "f1", "title": "Frame f1", "uri": "frame:f1", "digest": "sha256-f1", "text": "Write plainly."},
        {"id": "f2", "title": "Frame f2", "uri": "frame:f2", "digest": "sha256-f2", "text": "A Cog is a worker."},
    ]
    # The Track names what the step saw, and the submitted Op carries the references, not the text.
    [completed] = [e.payload for e in track.replay("r") if e.event_type == "step_completed"]
    assert completed["frames"] == [{"id": "f1", "digest": "sha256-f1"}, {"id": "f2", "digest": "sha256-f2"}]
    [submitted] = [e.payload for e in track.replay("r") if e.event_type == "op_submitted"]
    assert submitted["op"]["steps"][0]["frames"][0] == {"id": "f1", "name": "Frame f1", "digest": "sha256-f1",
                                                        "ref": "frame:r:f1", "uri": None}
    assert _deserialize_op(_serialize_op(op)) == op


def test_a_step_without_frames_sends_none_and_a_missing_snapshot_fails_the_step():
    track = InMemoryTrackStore()
    seen = {}

    def plain(entry_point, value, **feedback):
        seen.update(feedback)
        return ResultEnvelope.success({})

    intents.submit(track, OpDefinition("plain", (OpStep("a", "echo", "run"),)), by=BY)
    controller = _controller(track, {"echo": plain})
    _settle(controller, until=lambda: intents.describe(track, "plain").state is RunState.COMPLETED)
    assert "frames" not in seen
    lost = FrameRef(id="gone", name="gone", digest="x", ref="frame:lost:gone")
    intents.submit(track, OpDefinition("lost", (OpStep("a", "echo", "run", frames=(lost,)),)), by=BY)
    _settle(controller, until=lambda: intents.describe(track, "lost").state.ended)
    view = intents.describe(track, "lost")
    assert view.state is RunState.FAILED and view.error == "RuntimeError" and "snapshot of Frame 'gone'" in view.reason


# --- retries ----------------------------------------------------------------------------------------


def _failing_then_ok():
    calls = {"n": 0}

    def flaky(entry_point, value, **feedback):
        calls["n"] += 1
        if calls["n"] == 1:
            return ResultEnvelope.failure("model-call-failed", "first time")
        return ResultEnvelope.success({"try": calls["n"]})

    return flaky, calls


def test_a_retry_asked_through_the_track_is_taken_by_a_controller_and_the_run_runs_again():
    track = InMemoryTrackStore()
    flaky, calls = _failing_then_ok()
    intents.submit(track, OpDefinition("r", (OpStep("a", "flaky", "run"),)), by=BY)
    one = _controller(track, {"flaky": flaky})
    _settle(one, until=lambda: intents.describe(track, "r").state is RunState.FAILED)
    one.tick()  # the ended run is read no more
    view = intents.request_retry(track, "r", actor="alice")
    assert view.retry_asked_by == "alice" and view.state is RunState.FAILED
    assert [row.run_id for row in track.runs(retry_asked=True)] == ["r"]
    with pytest.raises(intents.NotRetryable, match="already asked for by alice"):
        intents.request_retry(track, "r", actor="bob")
    _settle(one, until=lambda: intents.describe(track, "r").state is RunState.COMPLETED)
    assert calls["n"] == 2 and intents.describe(track, "r").retry_asked_by is None
    assert "retry_requested" in _types(track, "r") and track.runs(retry_asked=True) == ()
    # Picked up again under the controller that took it, under a new attempt.
    [retried] = [e.payload for e in track.replay("r") if e.event_type == "retry_requested"]
    assert retried["controller"] == "one" and retried["attempt"] == "new"


def test_a_retry_is_refused_where_the_machine_does_not_retry_from_or_the_run_has_not_ended():
    track = InMemoryTrackStore()
    intents.submit(track, OpDefinition("r", (OpStep("a", "echo", "run"),)), by=BY)
    with pytest.raises(intents.NotRetryable, match="has not ended"):
        intents.request_retry(track, "r", actor="alice")
    one = _controller(track, {"echo": lambda e, v, **f: {"ok": True}})
    _settle(one, until=lambda: intents.describe(track, "r").state is RunState.COMPLETED)
    with pytest.raises(intents.NotRetryable, match="nothing to retry"):
        intents.request_retry(track, "r", actor="alice")
    with pytest.raises(LookupError):
        intents.request_retry(track, "nope", actor="alice")
    assert "retry_asked" not in _types(track, "r")


def test_a_retry_the_runner_will_not_take_is_recorded_refused_and_the_run_is_read_no_more(monkeypatch):
    track = InMemoryTrackStore()
    flaky, _ = _failing_then_ok()
    intents.submit(track, OpDefinition("r", (OpStep("a", "flaky", "run"),)), by=BY)
    one = _controller(track, {"flaky": flaky})
    _settle(one, until=lambda: intents.describe(track, "r").state is RunState.FAILED)
    monkeypatch.setattr(one.runner, "retry", lambda run_id, **_: (_ for _ in ()).throw(ValueError("not today")))
    intents.request_retry(track, "r", actor="alice")
    _settle(one, until=lambda: "retry_refused" in _types(track, "r"))
    view = intents.describe(track, "r")
    assert view.state is RunState.FAILED and view.retry_asked_by is None
    [refused] = [e.payload for e in track.replay("r") if e.event_type == "retry_refused"]
    assert refused["error"] == "not today"
    monkeypatch.undo()
    intents.request_retry(track, "r", actor="bob")  # asked again, it is taken
    _settle(one, until=lambda: intents.describe(track, "r").state is RunState.COMPLETED)


# --- the open escalation ---------------------------------------------------------------------------


def test_the_view_names_the_escalation_a_waiting_run_needs_decided_and_who_may_decide_it():
    track = InMemoryTrackStore()
    gate = Gate(escalate="always", approvers=("reviewer",))
    intents.submit(track, OpDefinition("r", (OpStep("a", "echo", "run", gate=gate),)), by=BY)
    one = _controller(track, {"echo": lambda e, v, **f: {"ok": True}})
    _settle(one, until=lambda: intents.describe(track, "r").state is RunState.WAITING_AT_GATE)
    view = intents.describe(track, "r")
    assert view.escalation == one.runner.open_escalation("r")["escalation"] and view.approvers == ("reviewer",)
    intents.request_decision(track, "r", escalation=view.escalation, outcome="approve", actor="alice")
    _settle(one, until=lambda: intents.describe(track, "r").state is RunState.COMPLETED)
    assert intents.describe(track, "r").escalation is None


def test_two_controllers_on_one_retry_ask_take_it_once_and_refuse_nothing():
    """The ask is the condition of the retry: of two controllers that see it, one records `retry_requested`; the
    other finds nothing left to take, and writes no `retry_refused` over a retry that is under way."""
    track = InMemoryTrackStore()
    flaky, _ = _failing_then_ok()
    intents.submit(track, OpDefinition("r", (OpStep("a", "flaky", "run"),)), by=BY)
    one = _controller(track, {"flaky": flaky}, name="one")
    two = _controller(track, {"flaky": flaky}, name="two")
    _settle(one, until=lambda: intents.describe(track, "r").state is RunState.FAILED)
    intents.request_retry(track, "r", actor="alice")
    for _ in range(3):
        one.tick()
        two.tick()
    types = _types(track, "r")
    assert types.count("retry_requested") == 1 and "retry_refused" not in types
    _settle(one, until=lambda: intents.describe(track, "r").state is RunState.COMPLETED)
    _settle(two, until=lambda: True)
    assert "retry_refused" not in _types(track, "r")
    # A controller that saw the ask and only gets to it after the run completed finds nothing to refuse,
    # whichever check it reaches first; and a refusal written by any path lands only while the ask stands.
    two._retry("r", "alice")
    assert intents.refuse_retry(track, "r", error="late") is False
    assert "retry_refused" not in _types(track, "r")
