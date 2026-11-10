"""The lifecycle conformance suite: what every durability backend must do to a run.

Every backend the configuration can build runs the same Ops to the same Track:
multi-step completion, a Gate escalating and each human decision, cancel, a
budget stop, and a retry as a new attempt. Durable backends (Phases 27 and 33)
join the parametrization when they are built.
"""

from __future__ import annotations

import threading

import pytest

from collab_hub_execution import (
    DURABILITY_BACKENDS,
    BackendNotImplemented,
    Gate,
    InMemoryCogExecutor,
    InMemoryTrackStore,
    InvalidTransition,
    LifecycleRunner,
    OpDefinition,
    OpStep,
    ResultEnvelope,
    RunBudget,
    RunState,
)
from collab_hub_execution.backends import select_backend


def _built() -> list[str]:
    built = []
    for name in DURABILITY_BACKENDS:
        try:
            select_backend(name)
        except BackendNotImplemented:
            continue
        built.append(name)
    return built


@pytest.fixture(params=_built())
def backend(request) -> str:
    return request.param


def _runner(backend, handlers, track=None, executor=None, **kwargs) -> LifecycleRunner:
    return LifecycleRunner(executor=executor or InMemoryCogExecutor(handlers), track=track or InMemoryTrackStore(),
                           backend=backend, **kwargs)


def _kinds(runner, run_id):
    return [event.event_type for event in runner.track.replay(run_id)]


# --- completion and the Track ---------------------------------------------------------------------


def test_a_multi_step_op_completes_and_the_track_says_how(backend):
    runner = _runner(backend, {"c": lambda entry, value: {"got": value}})
    op = OpDefinition("r", (OpStep("a", "c", "run", 1), OpStep("b", "c", "run", 2)))
    assert runner.submit(op) is RunState.COMPLETED
    one_step = ["step_started", "materialized", "ready", "interaction_started", "interaction_usage", "idle",
                "teardown_started", "step_completed"]
    assert _kinds(runner, "r") == ["op_submitted", "run_picked_up", *one_step, *one_step, "completed"]


def test_a_multi_step_op_with_a_gate_completes_after_its_approval(backend):
    runner = _runner(backend, {"c": lambda entry, value: value})
    op = OpDefinition("r", (OpStep("draft", "c", "run", 1, gate=Gate(escalate="always")),
                            OpStep("publish", "c", "run", 2)))
    assert runner.submit(op) is RunState.WAITING_AT_GATE
    escalation = runner.open_escalation("r")["escalation"]
    assert runner.decide("r", escalation=escalation, actor="alice", outcome="approve") is RunState.COMPLETED


@pytest.mark.parametrize(("outcome", "ends"), [("reject", RunState.REJECTED), ("send_back", RunState.WAITING_AT_GATE)])
def test_each_decision_on_an_escalation(backend, outcome, ends):
    runner = _runner(backend, {"c": lambda entry, value, **signal: value})
    runner.submit(OpDefinition("r", (OpStep("s", "c", "run", 1, gate=Gate(escalate="always")),)))
    escalation = runner.open_escalation("r")["escalation"]
    assert runner.decide("r", escalation=escalation, actor="alice", outcome=outcome,
                         findings=["cite it"] if outcome == "send_back" else ()) is ends


# --- a budget stop, and retry as a new attempt ----------------------------------------------------


def test_a_budget_stop(backend):
    spender = {"c": lambda entry, value: ResultEnvelope.success(value, usage={"tokens": 10})}
    runner = _runner(backend, spender, budget=RunBudget(max_tokens=5))
    assert runner.submit(OpDefinition("r", (OpStep("a", "c", "run"), OpStep("b", "c", "run")))) \
        is RunState.BUDGET_EXCEEDED
    assert _kinds(runner, "r").count("step_started") == 1


def test_a_failed_run_is_retried_as_a_new_attempt_with_a_new_key(backend):
    keys = []

    class Worker:
        cog = "c"

        def interact(self, entry_point, input=None, idempotency_key=None, **_):
            keys.append(idempotency_key)
            if len(keys) == 1:
                return ResultEnvelope.failure("model-call-failed", "try again")
            return ResultEnvelope.success("ok")

    class Executor:
        def materialize(self, cog, run_id, instance=""):
            return Worker()

        def teardown(self, worker):
            pass

    runner = _runner(backend, {}, executor=Executor())
    assert runner.submit(OpDefinition("r", (OpStep("s", "c", "run"),))) is RunState.FAILED
    assert runner.retry("r") is RunState.COMPLETED
    assert keys == ["r:s:0", "r:s:1"]


# --- cancel ---------------------------------------------------------------------------------------


def test_a_run_waiting_at_a_gate_is_cancelled_with_its_actor(backend):
    runner = _runner(backend, {"c": lambda entry, value: value})
    runner.submit(OpDefinition("r", (OpStep("s", "c", "run", gate=Gate(escalate="always")),)))
    assert runner.cancel("r", actor="bob") is RunState.CANCELLED
    [cancelled] = [e for e in runner.track.replay("r") if e.event_type == "cancelled"]
    assert cancelled.payload == {"actor": "bob"}
    assert runner.open_escalation("r") is None
    with pytest.raises(InvalidTransition):
        runner.cancel("r", actor="bob")  # an ended run stays ended


def test_cancel_names_its_actor_and_its_run(backend):
    runner = _runner(backend, {})
    with pytest.raises(ValueError, match="names its actor"):
        runner.cancel("r", actor="")
    with pytest.raises(LookupError):
        runner.cancel("nobody", actor="bob")


class _BlockingExecutor(InMemoryCogExecutor):
    """A worker that blocks inside its interaction until it is torn down, as a real one would be."""

    def __init__(self, handlers, answer_after_teardown=False):
        super().__init__(handlers)
        self.interacting = threading.Event()
        self.released = threading.Event()
        self.teardowns = 0
        self.answer_after_teardown = answer_after_teardown

    def blocking(self, entry, value):
        self.interacting.set()
        assert self.released.wait(5), "the worker was never torn down"
        if self.answer_after_teardown:
            return value
        raise ConnectionError("the worker went away")

    def teardown(self, worker):
        self.teardowns += 1
        self.released.set()


def _in_thread(runner, op):
    outcome = {}
    thread = threading.Thread(target=lambda: outcome.setdefault("state", runner.submit(op)))
    thread.start()
    return thread, outcome


def test_a_run_cancelled_mid_interaction_tears_its_worker_down_and_ends_cancelled(backend):
    executor = _BlockingExecutor({})
    executor.handlers["c"] = executor.blocking
    runner = _runner(backend, {}, executor=executor)
    thread, outcome = _in_thread(runner, OpDefinition("r", (OpStep("s", "c", "run"), OpStep("t", "c", "run"))))
    assert executor.interacting.wait(5)
    assert runner.cancel("r", actor="bob") is RunState.RUNNING  # the advancing call records the end
    thread.join(5)
    assert outcome["state"] is RunState.CANCELLED and runner.observe("r") is RunState.CANCELLED
    kinds = _kinds(runner, "r")
    assert kinds[-1] == "cancelled" and "step_failed" not in kinds and "step_completed" not in kinds
    assert kinds.count("step_started") == 1  # the second step never started
    assert executor.teardowns == 1  # torn down once, by the cancel
    # The worker never answered: its interaction's usage is unknown, and it never went idle.
    assert kinds[kinds.index("interaction_started") + 1:] == ["interaction_usage", "cancelled"]


def test_a_result_that_arrives_after_the_cancel_is_not_kept(backend):
    executor = _BlockingExecutor({}, answer_after_teardown=True)
    executor.handlers["c"] = executor.blocking
    runner = _runner(backend, {}, executor=executor)
    thread, outcome = _in_thread(runner, OpDefinition("r", (OpStep("s", "c", "run", 1),)))
    assert executor.interacting.wait(5)
    runner.cancel("r", actor="bob")
    thread.join(5)
    assert outcome["state"] is RunState.CANCELLED
    assert "step_completed" not in _kinds(runner, "r")
