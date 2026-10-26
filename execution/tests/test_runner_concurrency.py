"""Calls that move one run from two threads of one host: claims, cancel, and the run's end.

Every call that moves a run claims it before it reads or writes, and a cancel is
taken only while the driver advances the run, under the same condition the
driver holds to write a step's result or the run's end. Each test holds one
thread just before a Track write, and moves the run from another one there.
"""

from __future__ import annotations

import threading

import pytest

from collab_hub_execution import (
    InMemoryCogExecutor,
    InMemoryTrackStore,
    InvalidTransition,
    LifecycleRunner,
    OpDefinition,
    OpStep,
    ResultEnvelope,
    RunState,
)


class _PausingTrack(InMemoryTrackStore):
    """Holds the first write of ``pause_on`` until ``go`` is set: just before it, or just after with ``after``."""

    def __init__(self) -> None:
        super().__init__()
        self.pause_on: str | None = None
        self.after = False
        self.reached, self.go = threading.Event(), threading.Event()

        self._conditional = threading.local()

    def _hold(self):
        self.reached.set()
        assert self.go.wait(5), "never released"

    def append(self, event):
        if getattr(self._conditional, "on", False):
            return super().append(event)  # held around append_if instead, outside the store's lock
        hold = event.event_type == self.pause_on and not self.reached.is_set()
        if hold and not self.after:
            self._hold()
        stored = super().append(event)
        if hold and self.after:
            self._hold()
        return stored

    def append_if(self, event, condition):
        hold = event.event_type == self.pause_on and not self.reached.is_set()
        if hold and not self.after:
            self._hold()
        self._conditional.on = True
        try:
            stored = super().append_if(event, condition)
        finally:
            self._conditional.on = False
        if hold and self.after:
            self._hold()
        return stored


def _thread(target):
    result = {}

    def run():
        try:
            result["value"] = target()
        except BaseException as exc:  # noqa: BLE001 - handed to the test
            result["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    return thread, result


def _kinds(track, run_id):
    return [event.event_type for event in track.replay(run_id)]


def test_a_host_starting_leaves_alone_a_run_a_retry_is_about_to_advance():
    track = _PausingTrack()
    answers = iter([ResultEnvelope.failure("model-call-failed", "once"), ResultEnvelope.success("ok")])
    runner = LifecycleRunner(executor=InMemoryCogExecutor({"c": lambda entry, value: next(answers)}), track=track)
    assert runner.submit(OpDefinition("r", (OpStep("s", "c", "run"),))) is RunState.FAILED
    track.pause_on, track.after = "retry_requested", True  # the run reads RUNNING, and has not advanced yet
    thread, retried = _thread(lambda: runner.retry("r"))
    assert track.reached.wait(5)
    assert runner.start() == ()  # the retry holds the run: it was not left behind
    track.go.set()
    thread.join(5)
    assert retried["value"] is RunState.COMPLETED
    assert "interrupted" not in _kinds(track, "r")


def _held_until(release: threading.Event):
    """A step that does not finish before the test says so, so a run cannot end ahead of its cancel."""

    def handler(entry, value):
        assert release.wait(5)
        return value

    return handler


def test_a_cancel_during_submission_ends_the_run_cancelled():
    track, release = _PausingTrack(), threading.Event()
    track.pause_on = "op_submitted"
    runner = LifecycleRunner(executor=InMemoryCogExecutor({"c": _held_until(release)}), track=track)
    submitting, submitted = _thread(lambda: runner.submit(OpDefinition("r", (OpStep("s", "c", "run"),))))
    assert track.reached.wait(5)
    cancelling, cancelled = _thread(lambda: runner.cancel("r", actor="bob"))
    cancelling.join(0.3)
    assert cancelling.is_alive()  # waits for the run to be advancing, rather than cancel what is not there
    track.go.set()
    cancelling.join(5)
    release.set()
    submitting.join(5)
    assert "error" not in submitted and "error" not in cancelled, (submitted, cancelled)
    assert submitted["value"] is RunState.CANCELLED
    assert _kinds(track, "r")[-1] == "cancelled" and "step_completed" not in _kinds(track, "r")


def test_a_second_submission_while_the_first_is_being_written_is_checked_and_answered():
    track, release = _PausingTrack(), threading.Event()
    track.pause_on = "op_submitted"
    runner = LifecycleRunner(executor=InMemoryCogExecutor({"c": _held_until(release)}), track=track)
    op = OpDefinition("r", (OpStep("s", "c", "run"),))
    first, submitted = _thread(lambda: runner.submit(op))
    assert track.reached.wait(5)
    same, again = _thread(lambda: runner.submit(op))
    other, different = _thread(lambda: runner.submit(OpDefinition("r", (OpStep("different", "c", "run"),))))
    same.join(0.3)
    assert same.is_alive() and other.is_alive()  # nothing is recorded yet, so there is nothing to answer with
    track.go.set()
    same.join(5)
    other.join(5)
    # A status once the submission is written, not None: submitted, or picked up already.
    assert again["value"] in (RunState.SUBMITTED, RunState.RUNNING)
    assert isinstance(different.get("error"), ValueError) and "different Op" in str(different["error"])
    release.set()
    first.join(5)
    assert submitted["value"] is RunState.COMPLETED
    assert _kinds(track, "r").count("step_started") == 1  # the second submission did not run it again


def test_a_second_submission_is_claimed_again_when_the_first_gave_the_run_up_unsubmitted():
    # The first call claimed the run and let it go without writing it (it raised): the second submits it.
    runner = LifecycleRunner(executor=InMemoryCogExecutor({"c": lambda entry, value: value}),
                             track=InMemoryTrackStore())
    holder = runner._acquire("r")
    op = OpDefinition("r", (OpStep("s", "c", "run"),))
    second, submitted = _thread(lambda: runner.submit(op))
    second.join(0.3)
    assert second.is_alive()
    runner._release("r", holder)
    second.join(5)
    assert submitted["value"] is RunState.COMPLETED


def test_a_cancel_arriving_as_a_step_completes_lands_after_that_result_and_ends_the_run():
    track, release = _PausingTrack(), threading.Event()
    track.pause_on = "step_completed"
    handlers = {"quick": lambda entry, value: value, "held": _held_until(release)}
    runner = LifecycleRunner(executor=InMemoryCogExecutor(handlers), track=track)
    op = OpDefinition("r", (OpStep("a", "quick", "run"), OpStep("b", "held", "run")))
    submitting, submitted = _thread(lambda: runner.submit(op))
    assert track.reached.wait(5)
    cancelling, cancelled = _thread(lambda: runner.cancel("r", actor="bob"))
    cancelling.join(0.3)
    assert cancelling.is_alive()  # the step's result is being written: the cancel lands after it, not in it
    track.go.set()
    cancelling.join(5)
    release.set()
    submitting.join(5)
    assert submitted["value"] is RunState.CANCELLED
    kinds = _kinds(track, "r")
    assert kinds.count("step_completed") == 1 and kinds[-1] == "cancelled"  # a's result kept, b's never


def test_a_cancel_arriving_as_the_run_ends_is_refused_and_the_end_stands():
    track = _PausingTrack()
    track.pause_on = "completed"
    runner = LifecycleRunner(executor=InMemoryCogExecutor({"c": lambda entry, value: value}), track=track)
    submitting, submitted = _thread(lambda: runner.submit(OpDefinition("r", (OpStep("s", "c", "run"),))))
    assert track.reached.wait(5)
    cancelling, cancelled = _thread(lambda: runner.cancel("r", actor="bob"))
    cancelling.join(0.3)
    assert cancelling.is_alive()
    track.go.set()
    submitting.join(5)
    cancelling.join(5)
    assert submitted["value"] is RunState.COMPLETED
    assert isinstance(cancelled.get("error"), InvalidTransition)  # an ended run is not cancelled
    assert _kinds(track, "r")[-1] == "completed"


class _SlowToMaterialize(InMemoryCogExecutor):
    def __init__(self, handlers):
        super().__init__(handlers)
        self.materializing, self.go = threading.Event(), threading.Event()
        self.teardowns = 0

    def materialize(self, cog, run_id, instance=""):
        self.materializing.set()
        assert self.go.wait(5)
        return super().materialize(cog, run_id, instance)

    def teardown(self, worker):
        self.teardowns += 1


def test_a_worker_brought_up_after_a_cancel_is_never_invoked_and_is_torn_down_once():
    invoked = []
    executor = _SlowToMaterialize({"c": lambda entry, value: invoked.append(value) or value})
    runner = LifecycleRunner(executor=executor, track=InMemoryTrackStore())
    submitting, submitted = _thread(lambda: runner.submit(OpDefinition("r", (OpStep("s", "c", "run"),))))
    assert executor.materializing.wait(5)
    assert runner.cancel("r", actor="bob") is RunState.RUNNING  # no worker yet, so nothing for cancel to tear down
    executor.go.set()
    submitting.join(5)
    assert submitted["value"] is RunState.CANCELLED
    assert invoked == [] and executor.teardowns == 1


class _HardToTearDown(InMemoryCogExecutor):
    """A worker that blocks until torn down; the teardown fails ``fails`` times before it works."""

    def __init__(self, fails: int):
        super().__init__({})
        self.handlers["c"] = self.blocking
        self.fails = fails
        self.interacting, self.released = threading.Event(), threading.Event()
        self.teardowns = 0

    def blocking(self, entry, value):
        self.interacting.set()
        assert self.released.wait(5)
        raise ConnectionError("the worker went away")

    def teardown(self, worker):
        self.teardowns += 1
        self.released.set()
        if self.teardowns <= self.fails:
            raise ConnectionError("delete failed")


@pytest.mark.parametrize(("fails", "recorded"), [(1, False), (2, True)])
def test_a_teardown_that_fails_during_a_cancel_is_tried_again_and_recorded_if_it_fails_again(fails, recorded):
    executor = _HardToTearDown(fails)
    track = InMemoryTrackStore()
    runner = LifecycleRunner(executor=executor, track=track)
    submitting, submitted = _thread(lambda: runner.submit(OpDefinition("r", (OpStep("s", "c", "run"),))))
    assert executor.interacting.wait(5)
    runner.cancel("r", actor="bob")
    submitting.join(5)
    assert submitted["value"] is RunState.CANCELLED and executor.teardowns == 2
    failed = [e.payload for e in track.replay("r") if e.event_type == "step_failed"]
    if recorded:  # the leaked worker is on the Track, beside the cancel, not hidden by it
        assert failed == [{**failed[0], "error": "TeardownFailed", "teardown_error": "ConnectionError"}]
    else:
        assert failed == []
