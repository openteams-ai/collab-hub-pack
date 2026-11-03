"""The durability conformance suite: what survives a host that stops.

A durable backend (``dbos``, Phase 27; ``temporal``, Phase 33) resumes a run
killed mid-step without resubmission, resumes one paused at a Gate there, and
never lets two replicas run one step; those cases join this suite with the
backends. ``none`` is not durable, and this suite holds it to that contract
instead: a run its stopped host left unfinished is recorded ``interrupted`` when a
host starts, never resumed, and continues only when someone retries it.
"""

from __future__ import annotations

import threading

import pytest
from claims_support import ClaimingExecutor, HostStops

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


class _Stops(BaseException):
    """The host's process stops here: not an error the runner handles, as a failing worker is."""


def _runner(track, executor, backend="none"):
    return LifecycleRunner(executor=executor, track=track, backend=backend)


class _Executor:
    def __init__(self, stops_once_on_call: int | None = None):
        self.calls: list[str] = []
        self.stops_once_on_call = stops_once_on_call

    def materialize(self, cog, run_id, instance=""):
        executor = self

        class Worker:
            def interact(self, entry_point, input=None, idempotency_key=None, **_):
                executor.calls.append(idempotency_key)
                if executor.stops_once_on_call == len(executor.calls):
                    executor.stops_once_on_call = None
                    raise _Stops("the host stopped mid-step")
                return ResultEnvelope.success(input)

        return Worker()

    def teardown(self, worker):
        pass


def _stopped_mid_step(track, executor, op):
    with pytest.raises(_Stops):
        _runner(track, executor).submit(op)


def test_a_run_left_running_by_a_stopped_host_is_interrupted_when_a_host_starts():
    track, executor = InMemoryTrackStore(), _Executor(stops_once_on_call=1)
    op = OpDefinition("r", (OpStep("s", "c", "run", 1),))
    _stopped_mid_step(track, executor, op)
    assert _runner(track, executor).observe("r") is RunState.RUNNING  # nothing says otherwise yet
    host = _runner(track, executor)
    assert host.start() == ("r",)
    assert host.observe("r") is RunState.INTERRUPTED
    [interrupted] = [e for e in track.replay("r") if e.event_type == "interrupted"]
    assert interrupted.payload == {"backend": "none"}


def test_an_interrupted_run_is_never_resumed_by_submitting_or_starting_again():
    track, executor = InMemoryTrackStore(), _Executor(stops_once_on_call=1)
    op = OpDefinition("r", (OpStep("s", "c", "run", 1),))
    _stopped_mid_step(track, executor, op)
    host = _runner(track, executor)
    host.start()
    assert host.submit(op) is RunState.INTERRUPTED
    assert host.start() == ()  # starting again interrupts nothing more
    assert executor.calls == ["r:s:0"]  # the worker was never invoked again


def test_submitting_a_run_a_stopped_host_left_running_does_not_resume_it():
    # Before any host has started, the Track still says running: submitting again is not a way to resume.
    track, executor = InMemoryTrackStore(), _Executor(stops_once_on_call=1)
    op = OpDefinition("r", (OpStep("s", "c", "run", 1),))
    _stopped_mid_step(track, executor, op)
    assert _runner(track, executor).submit(op) is RunState.RUNNING
    assert executor.calls == ["r:s:0"]


def test_retrying_an_interrupted_run_continues_the_attempt_in_flight_under_its_key():
    track, executor = InMemoryTrackStore(), _Executor(stops_once_on_call=2)
    op = OpDefinition("r", (OpStep("a", "c", "run", 1), OpStep("b", "c", "run", 2)))
    _stopped_mid_step(track, executor, op)  # step a completed, step b was in flight
    host = _runner(track, executor)
    host.start()
    assert host.retry("r") is RunState.COMPLETED
    assert executor.calls == ["r:a:0", "r:b:0", "r:b:0"]  # b again, under the same key; a not again
    [retry] = [e for e in track.replay("r") if e.event_type == "retry_requested"]
    assert retry.payload == {"from_status": "interrupted", "attempt": "same"}


def test_a_run_submitted_before_schema_v1_and_left_running_is_interrupted_too():
    from collab_hub_execution import TrackEvent

    track = InMemoryTrackStore()
    for kind, payload in (("submitted", {}), ("step_started", {"step": "s"})):
        track.append(TrackEvent(run_id="old", event_type=kind, payload=payload, schema=0))
    host = _runner(track, _Executor())
    assert host.start() == ("old",)
    assert host.observe("old") is RunState.INTERRUPTED


def test_a_run_waiting_at_a_gate_when_its_host_stopped_is_interrupted_too():
    # A pause cannot survive a restart on none (decision 3); the run is not left waiting on nothing.
    track = InMemoryTrackStore()
    executor = InMemoryCogExecutor({"c": lambda entry, value: value})
    _runner(track, executor).submit(OpDefinition("r", (OpStep("s", "c", "run", gate=Gate(escalate="always")),)))
    host = _runner(track, executor)
    assert host.start() == ("r",)
    assert host.observe("r") is RunState.INTERRUPTED and host.open_escalation("r") is None


def test_starting_leaves_ended_runs_and_runs_never_picked_up_alone():
    track = InMemoryTrackStore()
    executor = InMemoryCogExecutor({"c": lambda entry, value: value})
    host = _runner(track, executor)
    host.submit(OpDefinition("done", (OpStep("s", "c", "run"),)))
    from collab_hub_execution import Run, TrackEvent
    from collab_hub_execution.orchestration import _serialize_op

    # Submitted and never picked up: nothing was in flight, so there is nothing to interrupt.
    pending = OpDefinition("pending", (OpStep("s", "c", "run"),))
    track.append(TrackEvent(run_id="pending", event_type="op_submitted", payload={"op": _serialize_op(pending)},
                            schema=1))
    assert host.start() == ()
    assert Run.replay(track.replay("pending")).state is RunState.SUBMITTED
    assert host.submit(pending) is RunState.COMPLETED  # never started, so submitting starts it


def test_starting_leaves_a_run_this_host_is_advancing_alone():
    track = InMemoryTrackStore()
    entered, release = threading.Event(), threading.Event()

    def slow(entry, value):
        entered.set()
        assert release.wait(5)
        return value

    host = _runner(track, InMemoryCogExecutor({"c": slow}))
    outcome = {}
    thread = threading.Thread(target=lambda: outcome.setdefault(
        "state", host.submit(OpDefinition("r", (OpStep("s", "c", "run"),)))))
    thread.start()
    assert entered.wait(5)
    assert host.start() == ()  # it is advancing here, not left behind
    release.set()
    thread.join(5)
    assert outcome["state"] is RunState.COMPLETED


# --- the keyed claim: a worker replaced around its side effect ---------------------------------------


def _failed_step(track, run_id):
    [failed] = [e.payload for e in track.replay(run_id) if e.event_type == "step_failed"]
    return failed


def test_a_worker_replaced_after_reserving_before_committing_leaves_the_step_outcome_unknown():
    # The side effect happened, and the worker was lost before it committed its result. The retry's worker
    # finds the key reserved by a worker that is gone: it acts on nothing, and the step ends outcome-unknown.
    executor = ClaimingExecutor({"c": lambda entry, value: {"sent": value}}, stop_after={"r:s:0": "acted"})
    track = InMemoryTrackStore()
    op = OpDefinition("r", (OpStep("s", "c", "run", 1),))
    with pytest.raises(HostStops):
        _runner(track, executor).submit(op)
    host = _runner(track, executor)
    assert host.start() == ("r",)
    assert host.retry("r") is RunState.FAILED
    assert executor.acted == ["r:s:0"]  # once; nothing acted twice
    failed = _failed_step(track, "r")
    assert failed["error"] == "outcome-unknown" and failed["step"] == "s" and failed["key"] == "r:s:0"
    assert executor.store.get("r", "r:s:0").state == "outcome_unknown"
    # An ordinary retry is refused: only a person settles what the lost worker did.
    with pytest.raises(ValueError, match="ended outcome-unknown"):
        host.retry("r")
    with pytest.raises(intents.NotRetryable, match="reconciled by a person"):
        intents.request_retry(track, "r", actor="bob")
    assert host.observe("r") is RunState.FAILED and executor.acted == ["r:s:0"]


def test_a_reconciled_outcome_lets_the_step_run_again_as_a_new_attempt():
    executor = ClaimingExecutor({"c": lambda entry, value: {"sent": value}}, stop_after={"r:s:0": "acted"})
    track = InMemoryTrackStore()
    with pytest.raises(HostStops):
        _runner(track, executor).submit(OpDefinition("r", (OpStep("s", "c", "run", 1),)))
    host = _runner(track, executor)
    host.start()
    assert host.retry("r") is RunState.FAILED
    with pytest.raises(intents.NotRetryable, match="nothing to reconcile"):
        intents.reconcile_outcome(track, "r", step="other", actor="ops")
    executor.store.reconcile("r", "r:s:0", actor="ops")  # the claim, settled by a person
    view = intents.reconcile_outcome(track, "r", step="s", actor="ops", note="the email was sent once")
    assert view.state is RunState.FAILED and intents.outcome_unknown_step(track.replay("r")) is None
    with pytest.raises(intents.NotRetryable):
        intents.reconcile_outcome(track, "r", step="s", actor="ops")  # once
    assert intents.request_retry(track, "r", actor="bob").retry_asked_by == "bob"
    assert host.retry("r", asked=True) is RunState.COMPLETED
    assert executor.acted == ["r:s:0", "r:s:1"]  # a new attempt, under a new key, as a recorded failure retries
    [reconciled] = [e.payload for e in track.replay("r") if e.event_type == "outcome_reconciled"]
    assert reconciled == {"step": "s", "actor": "ops", "note": "the email was sent once"}


def test_a_worker_replaced_after_committing_before_the_step_was_recorded_answers_with_its_envelope():
    executor = ClaimingExecutor({"c": lambda entry, value: ResultEnvelope.success({"sent": value},
                                                                                  usage={"tokens": 7})},
                                stop_after={"r:s:0": "committed"})
    track = InMemoryTrackStore()
    with pytest.raises(HostStops):
        _runner(track, executor).submit(OpDefinition("r", (OpStep("s", "c", "run", 1),)))
    host = _runner(track, executor)
    host.start()
    assert host.retry("r") is RunState.COMPLETED
    assert executor.acted == ["r:s:0"]
    [completed] = [e.payload for e in track.replay("r") if e.event_type == "step_completed"]
    assert completed["payload"] == {"sent": 1} and completed["usage"] == {"tokens": 7}
    # The stopped host recorded nothing of its interaction; the one answered from the claim is accounted once,
    # with what the committed envelope reported.
    usages = [e.payload["usage"] for e in track.replay("r") if e.event_type == "interaction_usage"]
    assert usages == [{"tokens": 7}]


@pytest.mark.parametrize("backend", [])  # dbos (Phase 27) and temporal (Phase 33) join here
def test_a_durable_backend_resumes_a_run_killed_mid_step_without_resubmission(backend):
    raise AssertionError("a durable backend's case runs only once the backend is built")
