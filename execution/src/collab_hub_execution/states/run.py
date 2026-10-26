"""The run: where a run is, from submission to its end.

A run's status is this machine folded over its Track (:meth:`Run.replay`), so
the API, the controller and a local run host derive it the same way.

The records are Track event schema v1 (``docs/cog-execution/track.md``):
``gate_escalated`` for an escalation, ``gate_decided`` for a decision,
``budget_exceeded`` with a dimension for a budget stop. A Track written before
v1 is read through ``track.upgrade`` before it is replayed here.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from ._machine import Context, InvalidTransition, Machine, Record, State, Transition, accepts, move

DIMENSIONS = frozenset({"duration", "tokens", "cost"})
DECISIONS = frozenset({"approve", "send_back", "reject"})


def _owner(controller: str | None) -> dict[str, str]:
    """What a record that hands a run to a controller says of it: its name, when it has one."""
    return {} if controller is None else {"controller": controller}


class StaleEscalation(InvalidTransition):
    """A decision that names an escalation other than the open one."""


class RunState(State):
    """The run machine's interface: one method per event, each refused by default."""

    def pickup(self, run: Run, **_: Any) -> Transition[Run]:
        return self.refuse("pickup")

    def escalate(self, run: Run, **_: Any) -> Transition[Run]:
        return self.refuse("escalate")

    def decide(self, run: Run, **_: Any) -> Transition[Run]:
        return self.refuse("decide")

    def complete(self, run: Run, **_: Any) -> Transition[Run]:
        return self.refuse("complete")

    def fail(self, run: Run, **_: Any) -> Transition[Run]:
        return self.refuse("fail")

    def exhaust_budget(self, run: Run, **_: Any) -> Transition[Run]:
        return self.refuse("exhaust_budget")

    def cancel(self, run: Run, **_: Any) -> Transition[Run]:
        return self.refuse("cancel")

    def host_stopped(self, run: Run, **_: Any) -> Transition[Run]:
        return self.refuse("host_stopped")

    def retry(self, run: Run, **_: Any) -> Transition[Run]:
        return self.refuse("retry")

    @property
    def ended(self) -> bool:
        """True when the run is not advancing: it has finished, or waits for a retry."""
        return not isinstance(self, (Submitted, Running, WaitingAtGate))



def _cancel(state: RunState, run: Run, actor: str | None) -> Transition[Run]:
    if not actor:
        state.refuse("cancel", "a cancellation names its actor")
    return move(run, RunState.CANCELLED, Record("cancelled", {"actor": actor}), open_step=None, open_escalation=None)


def _host_stopped(state: RunState, run: Run, backend: str) -> Transition[Run]:
    # Only `none` cannot resume a run; under `dbos` or `temporal` a host stop
    # leaves the run where it was, and the engine resumes it.
    if backend != "none":
        state.refuse("host_stopped", f"the {backend!r} backend resumes the run; only 'none' interrupts it")
    return move(run, RunState.INTERRUPTED, Record("interrupted", {"backend": backend}))


def _failed(step: str | None, error: str, reason: str | None, details: Mapping[str, Any] | None) -> Record:
    payload: dict[str, Any] = {} if step is None else {"step": step}
    payload["error"] = error
    if reason is not None:
        payload["reason"] = reason
    return Record("failed", {**payload, **(details or {})})


class Submitted(RunState):
    name = "SUBMITTED"

    @accepts("RUNNING")
    def pickup(self, run: Run, *, controller: str | None = None, **_: Any) -> Transition[Run]:
        # The controller that picked the run up owns it from here: it alone advances it, and
        # under `none` the run ends `interrupted` with it.
        return move(run, RunState.RUNNING, Record("run_picked_up", _owner(controller)), controller=controller)

    @accepts("CANCELLED")
    def cancel(self, run: Run, *, actor: str | None = None, **_: Any) -> Transition[Run]:
        return _cancel(self, run, actor)


class Running(RunState):
    name = "RUNNING"

    @accepts("WAITING_AT_GATE")
    def escalate(
        self, run: Run, *, step: str, reason: str | None = None, escalation: str | None = None,
        details: Mapping[str, Any] | None = None, **_: Any,
    ) -> Transition[Run]:
        # `details` is what the Gate escalated on: the attempt, the envelope, who may decide.
        payload: dict[str, Any] = {"step": step, "reason": reason, **(details or {})}
        if escalation is not None:
            payload["escalation"] = escalation
        counts = dict(run.escalations)
        counts[step] = counts.get(step, 0) + 1
        return move(
            run, RunState.WAITING_AT_GATE, Record("gate_escalated", payload),
            open_step=step, open_escalation=escalation, escalations=counts,
        )

    @accepts("COMPLETED")
    def complete(self, run: Run, **_: Any) -> Transition[Run]:
        return move(run, RunState.COMPLETED, Record("completed", {}))

    @accepts("FAILED")
    def fail(
        self, run: Run, *, error: str, step: str | None = None, reason: str | None = None,
        details: Mapping[str, Any] | None = None, **_: Any,
    ) -> Transition[Run]:
        return move(run, RunState.FAILED, _failed(step, error, reason, details))

    @accepts("BUDGET_EXCEEDED")
    def exhaust_budget(
        self, run: Run, *, dimension: str, step: str | None = None, reason: str | None = None, **_: Any
    ) -> Transition[Run]:
        if dimension not in DIMENSIONS:
            self.refuse("exhaust_budget", f"unknown budget dimension {dimension!r}")
        payload: dict[str, Any] = {} if step is None else {"step": step}
        payload.update(reason=reason, dimension=dimension)
        return move(run, RunState.BUDGET_EXCEEDED, Record("budget_exceeded", payload))

    @accepts("CANCELLED")
    def cancel(self, run: Run, *, actor: str | None = None, **_: Any) -> Transition[Run]:
        return _cancel(self, run, actor)

    @accepts("INTERRUPTED")
    def host_stopped(self, run: Run, *, backend: str = "none", **_: Any) -> Transition[Run]:
        return _host_stopped(self, run, backend)


class WaitingAtGate(RunState):
    name = "WAITING_AT_GATE"

    @accepts("RUNNING", "REJECTED", "FAILED")
    def decide(
        self, run: Run, *, outcome: str, escalation: str | None = None, findings: Any = None,
        revise_limit: int | None = None, actor: str | None = None, envelope_digest: str | None = None,
        **_: Any,
    ) -> Transition[Run]:
        if outcome not in DECISIONS:
            self.refuse("decide", f"unknown outcome {outcome!r}")
        if escalation != run.open_escalation:
            raise StaleEscalation(
                self, "decide", f"it names escalation {escalation!r}, and the open one is {run.open_escalation!r}"
            )
        step = run.open_step
        closed = {"open_step": None, "open_escalation": None}
        # Every decision record names the escalation it answered, so replay answers the same one,
        # and who decided it.
        # A decision names the escalation it answered, who decided, and the result
        # they decided on, so a reader of decisions needs no join to identify it.
        answered = {} if escalation is None else {"escalation": escalation}
        if actor is not None:
            answered["actor"] = actor
        if envelope_digest is not None:
            answered["envelope_digest"] = envelope_digest
        if outcome == "reject":
            record = Record("gate_decided", {"step": step, "outcome": outcome, "value": findings, **answered})
            return move(run, RunState.REJECTED, record, **closed)
        if outcome == "send_back" and revise_limit is not None and run.escalations.get(step, 0) > revise_limit:
            # The step has escalated `escalations` times, so a send back now would produce
            # revision number `escalations`: past the limit, the run fails instead.
            details = {"revise_limit": revise_limit, "value": findings, **answered}
            return move(run, RunState.FAILED, _failed(step, "revise_limit_exceeded", None, details), **closed)
        record = Record("gate_decided", {"step": step, "outcome": outcome, "value": findings, **answered})
        return move(run, RunState.RUNNING, record, **closed)

    @accepts("CANCELLED")
    def cancel(self, run: Run, *, actor: str | None = None, **_: Any) -> Transition[Run]:
        return _cancel(self, run, actor)

    @accepts("INTERRUPTED")
    def host_stopped(self, run: Run, *, backend: str = "none", **_: Any) -> Transition[Run]:
        return _host_stopped(self, run, backend)


class Completed(RunState):
    name = "COMPLETED"


class Failed(RunState):
    name = "FAILED"

    @accepts("RUNNING")
    def retry(self, run: Run, *, controller: str | None = None, **_: Any) -> Transition[Run]:
        # A recorded failure runs again as a new attempt, under a new key.
        payload = {"from_status": self.value, "attempt": "new", **_owner(controller)}
        return move(run, RunState.RUNNING, Record("retry_requested", payload), controller=controller)


class Rejected(RunState):
    name = "REJECTED"


class Cancelled(RunState):
    name = "CANCELLED"


class BudgetExceeded(RunState):
    name = "BUDGET_EXCEEDED"

    @accepts("RUNNING")
    def retry(self, run: Run, *, controller: str | None = None, **_: Any) -> Transition[Run]:
        # The stop fell between steps, so no attempt was in flight; the budget starts again.
        payload = {"from_status": self.value, "attempt": "same", "budget_epoch": "new", **_owner(controller)}
        return move(run, RunState.RUNNING, Record("retry_requested", payload), controller=controller)


class Interrupted(RunState):
    name = "INTERRUPTED"

    @accepts("RUNNING")
    def retry(self, run: Run, *, controller: str | None = None, **_: Any) -> Transition[Run]:
        # The attempt in flight continues under its key, so a committed claim answers.
        payload = {"from_status": self.value, "attempt": "same", **_owner(controller)}
        return move(run, RunState.RUNNING, Record("retry_requested", payload), controller=controller)


RUN = Machine(
    "run",
    RunState,
    (Submitted, Running, WaitingAtGate, Completed, Failed, Rejected, Cancelled, BudgetExceeded, Interrupted),
    initial="SUBMITTED",
)


class TrackFact(Protocol):
    """What replay reads from a Track event."""

    event_type: str
    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Run(Context):
    """A run: its state, the Gate it waits at, and how often each step escalated."""

    run_id: str
    state: RunState = field(default_factory=lambda: RUN.initial)  # type: ignore[assignment]
    open_step: str | None = None
    open_escalation: str | None = None
    escalations: Mapping[str, int] = field(default_factory=dict)
    controller: str | None = None
    """The controller that picked the run up, or took its last retry: the one that advances it. ``None``
    for a run nobody has picked up, or one a host picked up before controllers named themselves."""

    @classmethod
    def submit(cls, run_id: str, op: Mapping[str, Any], *, by: Mapping[str, Any] | None = None,
               name: str | None = None) -> Transition[Run]:
        """A new run, and the record of its submission; ``by`` is who submitted it, when a client did, and
        ``name`` what they called it."""
        payload: dict[str, Any] = {"op": dict(op)}
        if by is not None:
            payload["submitted_by"] = dict(by)
        if name is not None:
            payload["name"] = name
        return Transition(cls(run_id=run_id), (Record("op_submitted", payload),))

    def pickup(self, *, controller: str | None = None) -> Transition[Run]:
        return self.dispatch("pickup", controller=controller)

    def escalate(
        self, *, step: str, reason: str | None = None, escalation: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> Transition[Run]:
        return self.dispatch("escalate", step=step, reason=reason, escalation=escalation, details=details)

    def decide(
        self, *, outcome: str, escalation: str | None = None, findings: Any = None, revise_limit: int | None = None,
        actor: str | None = None, envelope_digest: str | None = None,
    ) -> Transition[Run]:
        return self.dispatch(
            "decide", outcome=outcome, escalation=escalation, findings=findings, revise_limit=revise_limit,
            actor=actor, envelope_digest=envelope_digest,
        )

    def complete(self) -> Transition[Run]:
        return self.dispatch("complete")

    def fail(
        self, *, error: str, step: str | None = None, reason: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> Transition[Run]:
        return self.dispatch("fail", error=error, step=step, reason=reason, details=details)

    def exhaust_budget(self, *, dimension: str, step: str | None = None, reason: str | None = None) -> Transition[Run]:
        return self.dispatch("exhaust_budget", dimension=dimension, step=step, reason=reason)

    def cancel(self, *, actor: str) -> Transition[Run]:
        return self.dispatch("cancel", actor=actor)

    def host_stopped(self, *, backend: str) -> Transition[Run]:
        return self.dispatch("host_stopped", backend=backend)

    def retry(self, *, controller: str | None = None) -> Transition[Run]:
        return self.dispatch("retry", controller=controller)

    def apply(self, fact: TrackFact) -> Run:
        """The run after one recorded Track event.

        A fact about a step or a worker leaves the run where it is. A run event
        goes through the same handler as it did live, with the arguments the Track
        recorded, and must record what was recorded: the same event, with the same
        outcome, attempt, error and dimension. Anything else is refused.
        """
        if fact.event_type in _FACTS:
            return self
        replay = _REPLAY.get(fact.event_type)
        if replay is None:
            raise InvalidTransition(self.state, fact.event_type, "it is not an event of a run's Track")
        payload = fact.payload or {}
        transition = replay(self, payload)
        produced = transition.records[0] if transition.records else None
        if produced is None or produced.event_type != fact.event_type:
            written = produced.event_type if produced else "nothing"
            raise InvalidTransition(self.state, fact.event_type, f"replayed here, the machine records {written!r}")
        for key in _DECIDING:
            if key in payload and key in produced.payload and payload[key] != produced.payload[key]:
                raise InvalidTransition(
                    self.state, fact.event_type,
                    f"it records {key}={payload[key]!r}, and the machine records {produced.payload[key]!r}",
                )
        return transition.after

    @classmethod
    def replay(cls, facts: Iterable[TrackFact]) -> Run | None:
        """Fold a run's Track through the machine; ``None`` for a run never submitted.

        The facts are read in schema v1: a Track written before it goes through
        ``track.upgrade`` first, which ``derive_run_status`` does. A Track the
        machine could not have written fails with
        :class:`InvalidTransition` instead of becoming a status. A Track with no
        ``run_picked_up`` at all was written before pickups were recorded: its first
        step start, or its first run event, is read as the pickup.
        """
        facts = tuple(facts)
        before_pickups = not any(fact.event_type == "run_picked_up" for fact in facts)
        run: Run | None = None
        for fact in facts:
            if fact.event_type in _SUBMISSIONS:
                if run is not None:
                    raise InvalidTransition(run.state, fact.event_type, "the run was already submitted")
                run = cls(run_id=getattr(fact, "run_id", ""))
            elif run is None:
                if fact.event_type in _REPLAY:
                    raise InvalidTransition(RUN.initial, fact.event_type, "the Track has no submission before it")
            else:
                if before_pickups and run.state is RunState.SUBMITTED and fact.event_type in _PICKED_UP_BY:
                    run = run.pickup().after
                run = run.apply(fact)
        return run


def _replay_failed(run: Run, payload: Mapping[str, Any]) -> Transition[Run]:
    if run.state is RunState.WAITING_AT_GATE and payload.get("error") == "revise_limit_exceeded":
        return run.decide(
            outcome="send_back", escalation=payload.get("escalation"), findings=payload.get("value"),
            revise_limit=payload.get("revise_limit"), actor=payload.get("actor"),
            envelope_digest=payload.get("envelope_digest"),
        )
    return run.fail(error=payload.get("error", ""), step=payload.get("step"), reason=payload.get("reason"))


def _replay_decision(outcome: str | None) -> Callable[[Run, Mapping[str, Any]], Transition[Run]]:
    def replay(run: Run, payload: Mapping[str, Any]) -> Transition[Run]:
        # A decision recorded before Gates was a send back: it re-ran the step with its value.
        return run.decide(
            outcome=outcome or payload.get("outcome", "send_back"),
            escalation=payload.get("escalation"),
            findings=payload.get("value"),
            actor=payload.get("actor"),
            envelope_digest=payload.get("envelope_digest"),
        )

    return replay


# "submitted" is what older Tracks called the submission.
_SUBMISSIONS = frozenset({"op_submitted", "submitted"})

# Track events about a step or its worker: facts beside the run, not moves of it.
_FACTS = frozenset({
    "step_started", "materialized", "ready", "interaction_started", "interaction_usage",
    "idle", "teardown_started", "teardown_failed", "step_completed", "step_failed",
    "worker_started", "worker_stopped",
    # What a client asked for, written by the API for the controller to deliver; the run moves
    # when the controller acts on it, which `cancelled` records.
    "cancel_requested",
    # A turn of a session Cog: asked by a client through the API, answered through the
    # controller. Neither moves the run.
    "turn_requested", "turn_answered", "turn_failed",
    # A decision on a Gate, asked by a client for the controller to deliver; `gate_decided` records it
    # when the run's runner takes it, and `decision_refused` when it does not.
    "decision_requested", "decision_refused",
})

# The payload fields that decide where a replayed event leads; a record and its replay must agree on them.
_DECIDING = ("outcome", "attempt", "from_status", "error", "dimension")

# In a Track written before pickups were recorded, what showed the run had been picked up.
_PICKED_UP_BY = frozenset({
    "step_started", "gate_escalated", "gate_decided", "completed", "failed",
    "budget_exceeded", "interrupted", "retry_requested",
})

_REPLAY: dict[str, Callable[[Run, Mapping[str, Any]], Transition[Run]]] = {
    "run_picked_up": lambda run, payload: run.pickup(controller=payload.get("controller")),
    "gate_escalated": lambda run, payload: run.escalate(
        step=payload.get("step", ""), reason=payload.get("reason"), escalation=payload.get("escalation")
    ),
    "gate_decided": _replay_decision(None),
    "completed": lambda run, payload: run.complete(),
    "failed": _replay_failed,
    "budget_exceeded": lambda run, payload: run.exhaust_budget(
        # Older records did not say which spending limit stopped the run; the state is the same.
        dimension=payload.get("dimension", "tokens"), step=payload.get("step"), reason=payload.get("reason")
    ),
    "cancelled": lambda run, payload: run.cancel(actor=payload.get("actor", "")),
    "interrupted": lambda run, payload: run.host_stopped(backend=payload.get("backend", "none")),
    "retry_requested": lambda run, payload: run.retry(controller=payload.get("controller")),
}
