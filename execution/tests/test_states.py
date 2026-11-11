"""The four state machines: every (state, event) pair, every guard, and replay from a Track."""

from dataclasses import replace

import pytest

from collab_hub_execution import TrackEvent, derive_run_status
from collab_hub_execution.states import (
    MACHINES,
    RUN,
    CogInstall,
    InstallState,
    InvalidTransition,
    Run,
    RunState,
    StaleEscalation,
    StepAttempt,
    StepAttemptState,
    Worker,
    WorkerState,
)
from collab_hub_execution.states._machine import Transition, accepts

CONTEXTS = {
    "install": CogInstall(reference="registry.example/cogs/echo@sha256:abc"),
    "worker": Worker(cog="echo", step="s"),
    "step_attempt": StepAttempt(key="run:s:0"),
    "run": Run(run_id="r"),
}

WAITING = replace(CONTEXTS["run"], state=RunState.WAITING_AT_GATE, open_step="s", escalations={"s": 1})

# One event per declared transition, with arguments that reach its target.
SCENARIOS = [
    (CONTEXTS["install"], "fetch", {}, InstallState.FETCHED),
    (CONTEXTS["install"], "uninstall", {}, InstallState.UNINSTALLED),
    (replace(CONTEXTS["install"], state=InstallState.FETCHED), "admit_binding", {"binding": "b1"}, InstallState.BOUND),
    (replace(CONTEXTS["install"], state=InstallState.FETCHED), "refuse_binding", {"reason": "no model"},
     InstallState.FETCHED),
    (replace(CONTEXTS["install"], state=InstallState.FETCHED), "uninstall", {}, InstallState.UNINSTALLED),
    (replace(CONTEXTS["install"], state=InstallState.BOUND), "check_passed", {}, InstallState.INVOKABLE),
    (replace(CONTEXTS["install"], state=InstallState.BOUND), "check_failed", {"step": "health", "reason": "503"},
     InstallState.BOUND),
    (replace(CONTEXTS["install"], state=InstallState.BOUND), "uninstall", {}, InstallState.UNINSTALLED),
    (replace(CONTEXTS["install"], state=InstallState.INVOKABLE), "uninstall", {}, InstallState.UNINSTALLED),
    (CONTEXTS["worker"], "ready", {}, WorkerState.READY),
    (CONTEXTS["worker"], "fail", {"error": "Boom"}, WorkerState.WORKER_FAILED),
    (replace(CONTEXTS["worker"], state=WorkerState.READY), "invoke", {"entry_point": "run"}, WorkerState.INTERACTING),
    (replace(CONTEXTS["worker"], state=WorkerState.READY), "tear_down", {"reason": "cancel"},
     WorkerState.TEARING_DOWN),
    (replace(CONTEXTS["worker"], state=WorkerState.READY), "fail", {"error": "Boom"}, WorkerState.WORKER_FAILED),
    (replace(CONTEXTS["worker"], state=WorkerState.INTERACTING), "envelope_returned", {}, WorkerState.IDLE),
    (replace(CONTEXTS["worker"], state=WorkerState.INTERACTING), "tear_down", {"reason": "deadline"},
     WorkerState.TEARING_DOWN),
    (replace(CONTEXTS["worker"], state=WorkerState.INTERACTING), "fail", {"error": "Boom"},
     WorkerState.WORKER_FAILED),
    (replace(CONTEXTS["worker"], state=WorkerState.IDLE), "invoke", {"entry_point": "run", "step": "t"},
     WorkerState.INTERACTING),
    (replace(CONTEXTS["worker"], state=WorkerState.IDLE), "tear_down", {"reason": "one_shot"},
     WorkerState.TEARING_DOWN),
    (replace(CONTEXTS["worker"], state=WorkerState.IDLE), "fail", {"error": "Boom"}, WorkerState.WORKER_FAILED),
    (replace(CONTEXTS["worker"], state=WorkerState.TEARING_DOWN), "torn_down", {}, WorkerState.TORN_DOWN),
    (replace(CONTEXTS["worker"], state=WorkerState.TEARING_DOWN), "fail", {"error": "Boom"},
     WorkerState.WORKER_FAILED),
    (CONTEXTS["step_attempt"], "worker_lost", {}, StepAttemptState.INVOKED),
    (CONTEXTS["step_attempt"], "reserve", {}, StepAttemptState.RESERVED),
    (replace(CONTEXTS["step_attempt"], state=StepAttemptState.RESERVED), "commit", {}, StepAttemptState.COMMITTED),
    (replace(CONTEXTS["step_attempt"], state=StepAttemptState.RESERVED), "worker_lost", {},
     StepAttemptState.OUTCOME_UNKNOWN),
    (replace(CONTEXTS["step_attempt"], state=StepAttemptState.COMMITTED), "record", {}, StepAttemptState.RECORDED),
    (replace(CONTEXTS["step_attempt"], state=StepAttemptState.OUTCOME_UNKNOWN), "reconcile", {"actor": "ops"},
     StepAttemptState.RECORDED),
    (CONTEXTS["run"], "pickup", {}, RunState.RUNNING),
    (CONTEXTS["run"], "cancel", {"actor": "alice"}, RunState.CANCELLED),
    (replace(CONTEXTS["run"], state=RunState.RUNNING), "escalate", {"step": "s"}, RunState.WAITING_AT_GATE),
    (replace(CONTEXTS["run"], state=RunState.RUNNING), "complete", {}, RunState.COMPLETED),
    (replace(CONTEXTS["run"], state=RunState.RUNNING), "fail", {"error": "Boom"}, RunState.FAILED),
    (replace(CONTEXTS["run"], state=RunState.RUNNING), "exhaust_budget", {"dimension": "cost"},
     RunState.BUDGET_EXCEEDED),
    (replace(CONTEXTS["run"], state=RunState.RUNNING), "cancel", {"actor": "alice"}, RunState.CANCELLED),
    (replace(CONTEXTS["run"], state=RunState.RUNNING), "host_stopped", {"backend": "none"}, RunState.INTERRUPTED),
    (WAITING, "decide", {"outcome": "approve"}, RunState.RUNNING),
    (WAITING, "decide", {"outcome": "reject"}, RunState.REJECTED),
    (WAITING, "decide", {"outcome": "send_back", "revise_limit": 0}, RunState.FAILED),
    (WAITING, "cancel", {"actor": "alice"}, RunState.CANCELLED),
    (WAITING, "host_stopped", {"backend": "none"}, RunState.INTERRUPTED),
    (replace(CONTEXTS["run"], state=RunState.FAILED), "retry", {}, RunState.RUNNING),
    (replace(CONTEXTS["run"], state=RunState.BUDGET_EXCEEDED), "retry", {}, RunState.RUNNING),
    (replace(CONTEXTS["run"], state=RunState.INTERRUPTED), "retry", {}, RunState.RUNNING),
]


def _refused_pairs():
    for machine in MACHINES.values():
        for state in machine.states:
            for event in machine.events:
                if not machine.accepts(state, event):
                    yield pytest.param(machine.name, state, event, id=f"{machine.name}-{state.name}-{event}")


@pytest.mark.parametrize("machine,state,event", list(_refused_pairs()))
def test_every_undeclared_pair_is_refused_naming_state_and_event(machine, state, event):
    context = replace(CONTEXTS[machine], state=state)
    with pytest.raises(InvalidTransition) as refused:
        getattr(state, event)(context)
    assert refused.value.state is state and refused.value.event == event
    assert state.name in str(refused.value) and repr(event) in str(refused.value)


@pytest.mark.parametrize("context,event,arguments,target", SCENARIOS,
                         ids=[f"{c.state.name}-{e}-{t.name}" for c, e, _, t in SCENARIOS])
def test_each_declared_transition_reaches_its_target_without_touching_the_context(context, event, arguments, target):
    transition = getattr(context, event)(**arguments)
    assert transition.after.state is target
    assert transition.after is not context  # a new context; the old one is unchanged


def test_the_scenarios_exercise_every_declared_transition_and_nothing_else():
    exercised = {(c.state, e, t) for c, e, _, t in SCENARIOS}
    declared = {edge for machine in MACHINES.values() for edge in machine.edges}
    assert exercised == declared


def test_a_handler_returning_an_undeclared_state_is_a_bug_not_a_transition(monkeypatch):
    @accepts("RUNNING")
    def skip_to_completed(self, run, **_):
        return Transition(replace(run, state=RunState.COMPLETED))

    monkeypatch.setattr(type(RunState.SUBMITTED), "pickup", skip_to_completed)
    with pytest.raises(RuntimeError, match="not a declared transition"):
        Run(run_id="r").pickup()


def test_states_have_one_instance_a_lower_case_wire_value_and_a_readable_repr():
    assert RunState.WAITING_AT_GATE.value == "waiting_at_gate" == str(RunState.WAITING_AT_GATE)
    assert RUN["waiting_at_gate"] is RunState.WAITING_AT_GATE is RUN["WAITING_AT_GATE"]
    assert repr(WorkerState.IDLE) == "WorkerState.IDLE"
    assert [state.name for state in RUN.states if state.final] == ["COMPLETED", "REJECTED", "CANCELLED"]
    assert {state.name for state in RUN.states if state.ended} == {
        "COMPLETED", "FAILED", "REJECTED", "CANCELLED", "BUDGET_EXCEEDED", "INTERRUPTED"
    }


# --- guards --------------------------------------------------------------------------


def test_a_decision_must_name_the_open_escalation():
    waiting = Run(run_id="r").pickup().after.escalate(step="s", escalation="esc-2").after
    with pytest.raises(StaleEscalation, match="esc-1"):
        waiting.decide(outcome="approve", escalation="esc-1")
    assert waiting.decide(outcome="approve", escalation="esc-2").after.state is RunState.RUNNING


def test_revise_limit_n_allows_n_revisions_when_decisions_send_back():
    run = Run(run_id="r").pickup().after
    for revision in (1, 2):  # the first two send backs produce revisions 1 and 2
        run = run.escalate(step="s").after
        run = run.decide(outcome="send_back", revise_limit=2).after
        assert run.state is RunState.RUNNING, revision
    run = run.escalate(step="s").after
    stopped = run.decide(outcome="send_back", findings="fix c", revise_limit=2)  # would be revision 3
    assert stopped.after.state is RunState.FAILED
    [record] = stopped.records
    assert record.event_type == "failed" and record.payload["error"] == "revise_limit_exceeded"
    assert record.payload["revise_limit"] == 2
    # An approval is never limited.
    assert run.decide(outcome="approve", revise_limit=2).after.state is RunState.RUNNING


def test_an_escalation_records_what_the_gate_escalated_on_and_a_decision_who_made_it():
    running = replace(CONTEXTS["run"], state=RunState.RUNNING)
    details = {"attempt": 0, "envelope": {"envelope": 1, "ok": True}, "approvers": ["owner", "operator"]}
    escalated = running.escalate(step="s", reason="a problem with severity error", escalation="esc-1", details=details)
    assert escalated.records[0].payload == {"step": "s", "reason": "a problem with severity error", **details,
                                            "escalation": "esc-1"}
    decided = escalated.after.decide(outcome="approve", escalation="esc-1", actor="alice", findings=["fine"])
    assert decided.records[0].payload == {"step": "s", "outcome": "approve", "value": ["fine"],
                                          "escalation": "esc-1", "actor": "alice"}
    # The actor replays with the decision.
    track = _track("op_submitted", "run_picked_up", ("gate_escalated", {"step": "s", "escalation": "esc-1"}),
                   ("gate_decided", decided.records[0].payload))
    assert derive_run_status(track) is RunState.RUNNING


def test_a_decision_outcome_must_be_one_of_the_three():
    with pytest.raises(InvalidTransition, match="unknown outcome"):
        WAITING.decide(outcome="maybe")


def test_only_the_none_backend_interrupts_a_run():
    running = replace(CONTEXTS["run"], state=RunState.RUNNING)
    with pytest.raises(InvalidTransition, match="resumes the run"):
        running.host_stopped(backend="dbos")
    assert running.host_stopped(backend="none").after.state is RunState.INTERRUPTED


def test_retry_says_whether_the_attempt_and_the_budget_start_again():
    def retried(state):
        [record] = replace(CONTEXTS["run"], state=state).retry().records
        return record.payload

    assert retried(RunState.FAILED)["attempt"] == "new"
    assert retried(RunState.INTERRUPTED)["attempt"] == "same"
    assert retried(RunState.BUDGET_EXCEEDED) == {"from_status": "budget_exceeded", "attempt": "same",
                                                 "budget_epoch": "new"}


def test_a_budget_stop_names_its_dimension():
    running = replace(CONTEXTS["run"], state=RunState.RUNNING)
    assert running.exhaust_budget(dimension="duration").records[0].payload["dimension"] == "duration"
    assert running.exhaust_budget(dimension="tokens").records[0].payload["dimension"] == "tokens"
    with pytest.raises(InvalidTransition, match="unknown budget dimension"):
        running.exhaust_budget(dimension="gpu")


def test_cancelling_and_reconciling_name_who_did_it():
    with pytest.raises(InvalidTransition, match="actor"):
        Run(run_id="r").cancel(actor="")
    unknown = replace(CONTEXTS["step_attempt"], state=StepAttemptState.OUTCOME_UNKNOWN)
    with pytest.raises(InvalidTransition, match="who made it"):
        unknown.reconcile(actor="")


@pytest.mark.parametrize("state,reason", [
    (WorkerState.READY, "one_shot"),
    (WorkerState.READY, "deadline"),
    (WorkerState.INTERACTING, "idle_timeout"),
    (WorkerState.IDLE, "cancel"),
])
def test_a_worker_is_torn_down_only_for_a_reason_its_state_allows(state, reason):
    with pytest.raises(InvalidTransition, match="not a reason to tear down"):
        replace(CONTEXTS["worker"], state=state).tear_down(reason=reason)


def test_only_an_invokable_install_is_materialized():
    install = CogInstall(reference="registry.example/cogs/echo@sha256:abc")
    with pytest.raises(InvalidTransition, match="only an INVOKABLE Cog"):
        Worker.materialize("echo", install=install)
    install = install.fetch().after.admit_binding(binding="b1").after.check_passed().after
    assert Worker.materialize("echo", install=install).after.state is WorkerState.MATERIALIZED
    # A development package read from a directory has no install to check.
    assert Worker.materialize("echo").records[0].event_type == "materialized"


def test_an_outcome_unknown_attempt_leaves_only_through_reconciliation():
    attempt = StepAttempt(key="run:s:0").reserve().after.worker_lost().after
    assert attempt.state is StepAttemptState.OUTCOME_UNKNOWN
    for event in ("reserve", "commit", "record", "worker_lost"):
        with pytest.raises(InvalidTransition):
            getattr(attempt, event)()
    assert attempt.reconcile(actor="ops").after.state is StepAttemptState.RECORDED


# --- status from the Track -----------------------------------------------------------


def _track(*kinds_and_payloads, schema=1):
    events = []
    for item in kinds_and_payloads:
        kind, payload = item if isinstance(item, tuple) else (item, {})
        events.append(TrackEvent(run_id="r", event_type=kind, payload=payload, schema=schema))
    return events


def test_replay_yields_waiting_at_gate_and_interrupted():
    waiting = _track("op_submitted", "run_picked_up", "step_started",
                     ("gate_escalated", {"step": "s", "reason": "review"}))
    assert derive_run_status(waiting) is RunState.WAITING_AT_GATE
    run = Run.replay(waiting)
    assert run.open_step == "s" and run.escalations == {"s": 1}
    interrupted = [*waiting, *_track(("interrupted", {"backend": "none"}))]
    assert derive_run_status(interrupted) is RunState.INTERRUPTED
    resumed = [*interrupted, *_track(("retry_requested", {"from_status": "interrupted", "attempt": "same"}))]
    assert derive_run_status(resumed) is RunState.RUNNING


def test_replay_ignores_the_facts_of_steps_and_workers():
    facts = ["step_started", "materialized", "ready", "interaction_started", "interaction_usage", "idle",
             "teardown_started", "step_completed"]
    assert derive_run_status(_track("op_submitted", "run_picked_up", *facts)) is RunState.RUNNING
    assert derive_run_status(_track("op_submitted", "run_picked_up", *facts, "completed")) is RunState.COMPLETED


def _revise_stop(**failed):
    return _track(
        "op_submitted", "run_picked_up",
        ("gate_escalated", {"step": "s"}), ("gate_decided", {"step": "s", "value": "fix a"}),
        ("gate_escalated", {"step": "s"}),
        ("failed", {"step": "s", "error": "revise_limit_exceeded", "value": "fix b", **failed}),
    )


def test_replay_re_applies_a_revise_limit_stop_with_its_recorded_limit():
    assert derive_run_status(_revise_stop(revise_limit=1)) is RunState.FAILED
    # The same stop, recorded when the step escalated again rather than at a decision.
    at_escalation = _track("op_submitted", "run_picked_up", ("gate_escalated", {"step": "s"}), "gate_decided",
                           ("failed", {"step": "s", "error": "revise_limit_exceeded", "revise_limit": 1}))
    assert derive_run_status(at_escalation) is RunState.FAILED


@pytest.mark.parametrize("failed", [{"revise_limit": 99}, {}], ids=["limit-not-reached", "no-limit"])
def test_a_revise_limit_stop_the_recorded_limit_does_not_produce_is_refused(failed):
    with pytest.raises(InvalidTransition, match="records 'gate_decided'"):
        derive_run_status(_revise_stop(**failed))


def test_a_track_the_machine_could_not_have_written_is_refused():
    with pytest.raises(InvalidTransition):  # completed before its pickup
        derive_run_status(_track("op_submitted", "completed", "run_picked_up"))
    with pytest.raises(InvalidTransition, match="not an event of a run's Track"):
        derive_run_status(_track("op_submitted", "run_picked_up", "step_complete"))  # a typo, not a fact
    with pytest.raises(InvalidTransition):  # a decision with no escalation open
        derive_run_status(_track("op_submitted", "run_picked_up",
                                 ("gate_decided", {"step": "s", "outcome": "approve"})))
    with pytest.raises(InvalidTransition, match="attempt='same'"):  # a failed run retries as a new attempt
        derive_run_status(_track("op_submitted", "run_picked_up", ("failed", {"error": "Boom"}),
                                 ("retry_requested", {"from_status": "failed", "attempt": "same"})))
    with pytest.raises(InvalidTransition, match="already submitted"):
        derive_run_status(_track("op_submitted", "op_submitted"))
    with pytest.raises(InvalidTransition, match="no submission"):
        derive_run_status(_track("run_picked_up"))
    with pytest.raises(InvalidTransition):
        derive_run_status(_track("op_submitted", "run_picked_up", "completed", "retry_requested"))


@pytest.mark.parametrize("kinds,state", [
    (("op_submitted", "step_started", "materialized", "ready", "interaction_started", "idle", "teardown_started",
      "step_completed", "completed"), RunState.COMPLETED),
    (("op_submitted", "step_started", ("gate_escalated", {"step": "s"})), RunState.WAITING_AT_GATE),
    (("op_submitted", ("budget_exceeded", {"reason": "run duration budget exceeded", "dimension": "duration"})),
     RunState.BUDGET_EXCEEDED),
    (("op_submitted", "completed"), RunState.COMPLETED),  # an Op with no steps
    (("submitted", "step_started", ("failed", {"step": "s", "error": "Boom"})), RunState.FAILED),
], ids=["completed", "gate_escalated", "timed-out-before-a-step", "no-steps", "failed"])
def test_a_track_written_before_pickups_were_recorded_still_has_its_status(kinds, state):
    assert derive_run_status(_track(*kinds)) is state


def test_a_track_with_no_submission_has_no_status_and_submitted_is_the_older_name():
    assert derive_run_status([]) is None
    assert derive_run_status(_track("step_started")) is None
    assert derive_run_status(_track("submitted")) is RunState.SUBMITTED


def test_every_run_record_replays_to_the_state_its_transition_reached():
    # Live transitions and replay agree: fold each transition's records back through the machine.
    run = Run.submit("r", {"steps": []})
    records = list(run.records)
    live = run.after
    for step in (
        lambda r: r.pickup(),
        lambda r: r.escalate(step="s", escalation="e1"),
        lambda r: r.decide(outcome="send_back", escalation="e1", findings="again"),
        lambda r: r.exhaust_budget(dimension="tokens", step="s"),
        lambda r: r.retry(),
        lambda r: r.fail(error="Boom", step="s"),
        lambda r: r.retry(),
        lambda r: r.host_stopped(backend="none"),
        lambda r: r.retry(),
        lambda r: r.escalate(step="s", escalation="e2"),
        lambda r: r.decide(outcome="reject", escalation="e2"),
    ):
        transition = step(live)
        live = transition.after
        records.extend(transition.records)
        replayed = Run.replay(TrackEvent(run_id="r", event_type=r.event_type, payload=r.payload) for r in records)
        assert replayed.state is live.state
    assert live.state is RunState.REJECTED
