"""The lifecycle runner: a Cog's lifecycle, written once, as step functions and the driver that runs them.

ADR-0002 D1 puts the lifecycle in one component that every durability backend
schedules. This is that component, in two parts.

The *step functions*, registered in ``STEP_FUNCTIONS``, are the units of work.
An attempt resolves its identity, materializes its worker, interacts and reads
the envelope, then tears the worker down whatever happened, since the worker is
one-shot. Only then does the step end: failed if it produced no result,
otherwise through its Gate, which completes it or escalates it. Each function
moves the state machines of ``states`` by asking them for a transition and
writing the records it returns; none assigns a state itself.

The *driver*, ``_advance`` with ``_run_attempt``, sequences them, and is
lifecycle logic too: it picks the run up and completes it, skips the steps
already completed, completes an approved escalation instead of running the step
again, checks and consumes the budget at step boundaries, and maps a failure to
the attempt's outcome and its unknown usage.

There is one driver and it is shared: every durability backend (``backends/``)
is handed each step function through ``DurabilityBackend.run_step`` and decides
only how it is scheduled and whether what it returned is checkpointed, never
what runs next. ``none``, the only backend built, calls them in process and
keeps nothing, so ``start()`` records a run a stopped host left unfinished as
``interrupted`` rather than resuming it (ADR-0002 D2).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from .backends import DurabilityBackend, select_backend
from .envelope import EnvelopeInvalid, ResultEnvelope
from .gates import DEFAULT_APPROVERS, Gate, GateOutcome, envelope_digest, escalation_id
from .lifecycle import BudgetExceeded, BudgetTracker, RunBudget
from .locations import select_executor
from .ops import (
    _NO_SIGNAL,
    CogExecutor,
    CogWorker,
    OpDefinition,
    OpStep,
    WorkflowEngine,
    _canonical_op,
    _deserialize_op,
    _serialize_op,
)
from .states import RUN, InvalidTransition, Run, RunState, Transition, Worker
from .track import PAYLOAD_INLINE_MAX_BYTES, SCHEMA_VERSION, TrackEvent, TrackStore, upgrade

_log = logging.getLogger(__name__)

# A failure's message on the Track is bounded, so a stack trace or a model's
# answer cannot turn the accountability record into a log.
MESSAGE_MAX_CHARS = 1024

def _bound(text: Any) -> str | None:
    """A failure's text as the Track keeps it: at most ``MESSAGE_MAX_CHARS`` characters."""
    return None if text is None else str(text)[:MESSAGE_MAX_CHARS]


def _render(payload: Any) -> str:
    """A step's payload as JSON, once: what the size check measures and a store keeps.

    The envelope contract says a payload is JSON. One that is not fails the step
    as an invalid envelope, durably, instead of failing later in whichever store
    happens to serialize it.
    """
    try:
        return json.dumps(payload)
    except (TypeError, ValueError) as exc:
        raise EnvelopeInvalid(f"the payload is not JSON: {exc}") from exc


def _payload_digest(payload: Any) -> str:
    """The sha256 of a payload's canonical JSON: sorted keys, compact separators.

    Canonical, not the bytes that were kept, so anyone can recompute it from the
    stored payload: Postgres keeps it as ``jsonb``, which does not keep key order.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _key_component(value: str) -> str:
    """Percent-escape the key delimiter so idempotency keys are injective.

    The key is ``{run_id}:{step}:{attempt}``; without escaping, ``run="a:b"
    step="c"`` and ``run="a" step="b:c"`` collide. Escaping ``:`` (and ``%``, so
    the escaping itself round-trips) keeps distinct (run, step) pairs distinct —
    the contract the exactly-once claim in #1 is built on — while leaving keys
    readable whenever the ids contain no ``:``.
    """
    return value.replace("%", "%25").replace(":", "%3A")


class UsageUnavailable(ValueError):
    """A configured budget cannot be accounted for, or usage is malformed."""


def _recorded(envelope: ResultEnvelope) -> dict[str, Any]:
    """The envelope fields the Track keeps beside a step's outcome, when present.

    ``problems`` are what a Gate decides on; ``binding`` is what makes the
    outcome auditable. Both are optional in the envelope, so the event only
    carries them when the worker reported them.
    """
    extra: dict[str, Any] = {}
    if envelope.problems:
        extra["problems"] = [{"check": p.check, "detail": p.detail, "severity": p.severity} for p in envelope.problems]
    if envelope.binding is not None:
        extra["binding"] = dict(envelope.binding)
    return extra


def _validate_usage(raw: Any, budget: RunBudget | None) -> dict[str, Any] | None:
    if raw is not None and not isinstance(raw, Mapping):
        raise UsageUnavailable("usage must be an object")
    usage = dict(raw) if raw is not None else {}
    for name in ("tokens", "cost"):
        if name not in usage:
            if budget is not None and getattr(budget, f"max_{name}") is not None:
                raise UsageUnavailable(f"missing {name} usage for configured budget")
            continue
        value = usage[name]
        valid = type(value) is int if name == "tokens" else type(value) in (int, float)
        if not valid or value < 0 or (type(value) is float and not math.isfinite(value)):
            raise UsageUnavailable(f"invalid {name} usage")
        if name == "cost":
            try:
                finite = math.isfinite(value)
            except OverflowError:
                finite = False
            if not finite:
                raise UsageUnavailable("invalid cost usage")
    return {key: usage[key] for key in ("tokens", "cost") if key in usage} if raw is not None else None


def _escalation(payload: Mapping[str, Any]) -> dict[str, Any]:
    """One escalation as a caller reads it, filled in for a Track written before Gates.

    An escalation recorded before Gates carries only the step and the reason: the
    Cog asked for the pause, so there is no id, no envelope and no approvers. It
    still answers to a decision — naming its id, ``None`` — and an approval re-runs
    the step, since no envelope was recorded to complete it with.
    """
    return {
        "escalation": payload.get("escalation"),
        "step": payload.get("step"),
        "reason": payload.get("reason"),
        "attempt": payload.get("attempt"),
        "envelope": payload.get("envelope"),
        "payload_ref": payload.get("payload_ref"),
        "usage": payload.get("usage"),
        "approvers": payload.get("approvers", list(DEFAULT_APPROVERS)),
        "gate": payload.get("gate", Gate().escalate),
    }


STEP_FUNCTIONS: dict[str, Callable[..., Any]] = {}
"""The runner's step functions, by name, in the order an attempt reaches them.

``resolve``, ``materialize``, ``interact``, ``read_envelope``, then ``teardown``
of the one-shot worker whatever happened; then the step ends: ``evaluate_gate``
and ``complete`` or ``escalate`` when it produced a result, ``fail`` when it did
not. Outside an attempt, ``complete_approved`` completes a step from an approved
escalation, and ``stop_for_budget`` stops a run at a boundary.

They are the units a durability backend schedules and checkpoints. The order
they run in, and the decisions between them, are the driver's (``_advance``),
which every backend reuses rather than reimplements; ``test_durability_backends.py``
holds every backend to being handed exactly these.
"""


def step_function(function: Callable[..., Any]) -> Callable[..., Any]:
    """Register one of the runner's step functions."""
    STEP_FUNCTIONS[function.__name__] = function
    return function


@dataclass
class Attempt:
    """One step attempt, as the step functions hand it on to each other."""

    step: OpStep
    number: int
    instance: str
    key: str
    worker: CogWorker | None = None
    cog_worker: Worker | None = None
    invoked: bool = False
    answered: bool = False
    accounted: bool = False
    result: Any = None
    rendered: str = ""
    usage: dict[str, Any] | None = None
    outcome: tuple[str, Any] = ("broken", "Unknown")
    failure_reason: str | None = None
    teardown_error: str | None = None
    started: bool = False
    """A ``worker_started`` was recorded for the worker, so its teardown records ``worker_stopped``."""
    released: bool = False
    """Whoever tears the worker down has claimed it: the teardown step, or ``cancel()`` from another
    thread. Set under the runner's lock, so the worker is torn down once."""
    cancel_teardown: threading.Event | None = None
    """Set by ``cancel()`` when it claimed the worker; done when its teardown returned."""
    cancel_teardown_error: str | None = None
    """What the executor raised when ``cancel()`` tore the worker down, so the teardown step tries again."""


class _Pass:
    """One advance of a run: the Track as it was read, and everything written since.

    Every write goes through ``append`` or ``record`` into ``history``, so a step
    reads what the ones before it wrote without reading the Track again.
    """

    def __init__(self, runner: LifecycleRunner, run_id: str, events: Sequence[TrackEvent],
                 advancing: _Advancing | None = None) -> None:
        self.runner = runner
        self.run_id = run_id
        self.history = list(events)
        self.advancing = advancing or _Advancing()

    def append(self, event_type: str, payload: Mapping[str, Any]) -> None:
        self.runner._append(self.run_id, event_type, payload, self.history)

    def record(self, transition: Transition[Any]) -> Any:
        return self.runner._record(self.run_id, transition, self.history)


class _Cancelled(Exception):
    """An attempt stopped because its run was cancelled; the driver ends the run ``cancelled``."""


class RunBusy(RuntimeError):
    """The run is claimed by another call in this host."""


class RunOwnedElsewhere(RuntimeError):
    """The run was picked up by another controller, which alone advances it, cancels it and decides on it."""


@dataclass
class _Advancing:
    """A run claimed by one call in this host, and whether someone asked to cancel it.

    ``accepting`` is true only while the driver advances the run: a cancel is
    taken then, and at no other time. It turns false when the driver writes the
    run's end, under ``changed`` — the same condition ``cancel()`` takes — so a
    cancel and a run's end never interleave. ``finished`` is set when the claim
    is released.

    ``_keep`` and ``_finish`` hold ``changed`` across ``backend.run_step`` for the
    step function that writes the result or the end. Under ``none`` that is a
    few Track writes, so a cancel waits briefly; a durable backend's
    ``run_step`` is where it checkpoints, which may be a round trip to its
    engine, and a ``cancel()`` of the run waits that long.
    """

    attempt: Attempt | None = None
    cancelled_by: str | None = None
    accepting: bool = False
    finished: bool = False
    changed: threading.Condition = field(default_factory=threading.Condition)


class LifecycleRunner(WorkflowEngine):
    """Runs Ops step by step through the step functions, recording every move on the Track.

    Experimental: interfaces may change. ``submit()``, ``decide()`` and ``retry()``
    run synchronously until the run completes, fails, waits at a Gate, or is
    cancelled. The ``backend`` setting (``none``, ``dbos`` or ``temporal``) chooses
    how the step functions are scheduled; only ``none`` is built, and it keeps
    nothing across a restart: ``start()`` records every run a stopped host left
    unfinished as ``interrupted``, and a person retries it.

    Every change of a run's or a worker's state is a transition of its machine
    (``states/``): the runner asks, and writes the records the machine returns.
    """

    def __init__(
        self,
        *,
        executor: CogExecutor | None = None,
        track: TrackStore,
        budget: RunBudget | None = None,
        max_revisions: int | None = None,
        payload_inline_max_bytes: int = PAYLOAD_INLINE_MAX_BYTES,
        backend: str = "none",
        location: str | None = None,
        location_settings: Mapping[str, Any] | None = None,
        controller: str | None = None,
    ) -> None:
        if (executor is None) == (location is None):
            raise ValueError("a runner takes a location, 'local' or 'remote', or an executor handed to it; not both")
        # The configuration value is the only switch; a location not built yet is refused here.
        self.executor: CogExecutor = executor if location is None else select_executor(
            location, **(location_settings or {}))
        self.track = track
        self.budget = budget
        self.max_revisions = max_revisions
        self.payload_inline_max_bytes = payload_inline_max_bytes
        # The configuration value is the only switch; a backend not built yet is refused here.
        self.backend: DurabilityBackend = select_backend(backend)
        self._lock = threading.Lock()
        self._advancing: dict[str, _Advancing] = {}
        self.controller = controller
        """The controller this runner advances runs for, named on each run it picks up; ``None`` for a host
        alone on its Track. Named, a runner shares its Track with other controllers: of several picking up
        one run, one does, and each takes on start and moves only the runs it picked up."""

    def _append(self, run_id: str, event_type: str, payload: Mapping[str, Any],
                into: list[TrackEvent] | None = None) -> TrackEvent:
        """The one write path: store one event, and keep any snapshot it belongs to current."""
        event = self.track.append(TrackEvent(run_id=run_id, event_type=event_type, payload=dict(payload),
                                             schema=SCHEMA_VERSION))
        if into is not None:
            into.append(event)
        return event

    def _result(self, run_id: str, key: str, payload: Any, rendered: str) -> dict[str, Any]:
        """The result as an event carries it: ``payload`` inline, or ``payload_ref`` above the threshold.

        A large result is kept under the attempt's idempotency key, so recovering
        an attempt rewrites the same row instead of leaving another one behind, and
        an escalated result approved later is completed from the row already kept.
        """
        if len(rendered.encode()) > self.payload_inline_max_bytes:
            self.track.put_payload(run_id, key, rendered)
            return {"payload_ref": key}
        return {"payload": payload}

    def _step_completed(self, step: OpStep, attempt: int, envelope: ResultEnvelope,
                        usage: Mapping[str, Any] | None, result: Mapping[str, Any],
                        escalation: str | None = None) -> dict[str, Any]:
        """A ``step_completed`` payload: what produced the result, and the result inline or by reference."""
        record: dict[str, Any] = {
            "step": step.name, "attempt": attempt, "cog": step.cog, "digest": step.digest, "usage": usage,
            "frames": [], **_recorded(envelope), **result,
        }
        if escalation is not None:
            record["escalation"] = escalation
        return record

    def _step_failed(self, step: OpStep, attempt: int, key: str, error: str, message: str | None,
                     envelope: ResultEnvelope | None = None) -> dict[str, Any]:
        """A ``step_failed`` payload: the attempt, its key, the worker, the code and a bounded message."""
        record: dict[str, Any] = {
            "step": step.name, "attempt": attempt, "key": key, "cog": step.cog, "digest": step.digest,
            "error": error, "message": _bound(message) or "",
        }
        if envelope is not None:
            record.update(_recorded(envelope))
        return record

    def _record(self, run_id: str, transition: Transition[Any], into: list[TrackEvent] | None = None) -> Any:
        """Write what a transition reports, and return the context after it.

        ``into`` collects the events written, so a caller holding a snapshot of the
        Track advances on what it read *and* wrote, without reading it again.
        """
        for record in transition.records:
            self._append(run_id, record.event_type, record.payload, into)
        return transition.after

    def _record_if_still(self, run_id: str, transition: Transition[Run], seen: Run,
                         into: list[TrackEvent] | None = None) -> Run | None:
        """Write a run's one-record transition only if, as it lands, the run is where ``seen`` was.

        Where is its state and the controller that has it. The check and the
        write are one step on the Track (``append_if``), so of two controllers
        moving one run from the same place, one does, and the other gets ``None``.
        """
        (record,) = transition.records
        event = TrackEvent(run_id=run_id, event_type=record.event_type, payload=dict(record.payload),
                           schema=SCHEMA_VERSION)

        def still(events: tuple[TrackEvent, ...]) -> bool:
            run = Run.replay(tuple(upgrade(e) for e in events))
            return run is not None and (run.state, run.controller) == (seen.state, seen.controller)

        stored = self.track.append_if(event, still)
        if stored is None:
            return None
        if into is not None:
            into.append(stored)
        return transition.after

    def _owns(self, run: Run) -> bool:
        """Whether this runner may move a run that has been picked up: one it, or a host before controllers
        named themselves, picked up. A runner with no name owns every run on its Track."""
        return self.controller is None or run.controller in (None, self.controller)

    def _refuse_elsewhere(self, run_id: str, run: Run) -> None:
        if not self._owns(run):
            raise RunOwnedElsewhere(f"run {run_id!r} is advanced by controller {run.controller!r}, not this one")

    def _read(self, run_id: str) -> tuple[TrackEvent, ...]:
        """The run's Track, read once and in schema v1 whatever version it was written in."""
        return tuple(upgrade(event) for event in self.track.replay(run_id))

    def observe(self, run_id: str) -> RunState | None:
        run = Run.replay(self._read(run_id))
        return None if run is None else run.state

    # The helpers below read one snapshot of the Track, taken once per call, so a
    # step costs no further reads of it.

    @staticmethod
    def _submitted_definition(run_id: str, events: tuple[TrackEvent, ...]) -> OpDefinition:
        for event in events:
            if event.event_type == "op_submitted":
                return _deserialize_op(event.payload["op"])
        raise LookupError(f"no submitted Op for run {run_id!r}")

    @staticmethod
    def _completed_steps(events: tuple[TrackEvent, ...]) -> set[str]:
        return {event.payload["step"] for event in events if event.event_type == "step_completed"}

    def budget_tracker(self, events: tuple[TrackEvent, ...]) -> BudgetTracker | None:
        """Reconstruct the run's budget from the Track so it survives restarts."""
        if self.budget is None:
            return None
        # New runs use op_submitted alone. Accept submitted for old Tracks.
        started_at = next(
            (e.occurred_at for e in events if e.event_type in ("submitted", "op_submitted")),
            None,
        )
        tracker = BudgetTracker(self.budget, started_at=started_at)
        accounted_steps = {e.payload.get("step") for e in events if e.event_type == "interaction_usage"}
        for event in events:
            if event.event_type == "interaction_usage" or (
                event.event_type == "step_completed" and event.payload.get("step") not in accounted_steps
            ):
                usage = _validate_usage(event.payload.get("usage"), self.budget) or {}
                tracker.tokens += int(usage.get("tokens", 0))
                tracker.cost += float(usage.get("cost", 0.0))
        return tracker

    @staticmethod
    def _retry_count(events: tuple[TrackEvent, ...]) -> int:
        # Only a retry that opens a new attempt moves the key on; one that
        # continues an interrupted attempt keeps it, so a committed claim answers.
        return sum(
            1 for e in events if e.event_type == "retry_requested" and e.payload.get("attempt", "new") == "new"
        )

    @staticmethod
    def _signal_for(events: tuple[TrackEvent, ...], step: str) -> Any:
        """The findings of the latest send back of a step, or ``_NO_SIGNAL``.

        Recovery re-reads the decision from the Track, so a crash after a send
        back was recorded re-runs the step with both its input and the findings.
        """
        value: Any = _NO_SIGNAL
        for e in events:
            if (e.event_type == "gate_decided" and e.payload.get("step") == step
                    and e.payload.get("outcome", "send_back") == "send_back"):
                value = e.payload.get("value")
        return value

    @staticmethod
    def _approved(events: tuple[TrackEvent, ...], step: str) -> Mapping[str, Any] | None:
        """The escalation of a step that was approved and is not yet completed, or ``None``.

        An approval completes the step with the envelope the approver saw, so
        a crash between recording the approval and completing the step completes
        it on recovery rather than running it again. An escalation recorded before
        Gates has no envelope to complete from, so its approval re-runs the step.
        """
        escalated: Mapping[str, Any] | None = None
        approved: Mapping[str, Any] | None = None
        for e in events:
            if e.payload.get("step") != step:
                continue
            if e.event_type == "gate_escalated":
                escalated, approved = e.payload, None
            elif e.event_type == "gate_decided":
                approved_now = e.payload.get("outcome") == "approve" and escalated is not None
                approved = escalated if approved_now and escalated.get("envelope") is not None else None
            elif e.event_type == "step_completed":
                approved = None
        return approved

    @staticmethod
    def _open_escalation(events: tuple[TrackEvent, ...], run: Run | None) -> dict[str, Any] | None:
        if run is None or run.state is not RunState.WAITING_AT_GATE:
            return None
        return _escalation(next(
            e.payload for e in reversed(events)
            if e.event_type == "gate_escalated" and e.payload.get("escalation") == run.open_escalation
        ))

    def open_escalation(self, run_id: str) -> Mapping[str, Any] | None:
        """What a run waiting at a Gate waits on — the escalation id, the envelope, who may decide — or ``None``."""
        events = self._read(run_id)
        return self._open_escalation(events, Run.replay(events))

    # --- the host ------------------------------------------------------------------------------

    def start(self) -> tuple[str, ...]:
        """What a host does when it starts: under ``none``, end every run left unfinished as ``interrupted``.

        ``none`` keeps nothing across a restart, so a run a stopped host was
        advancing — running, or waiting at a Gate — can never resume; it is
        recorded ``interrupted`` rather than left looking alive, and continues only
        when a person retries it (ADR-0002 D2). A run this host is advancing right
        now is left alone. Returns the runs it interrupted. A durable backend
        resumes its runs instead, which Phases 26 and 32 build.

        A runner named for a controller takes only the runs that controller
        picked up, and those a host picked up before controllers named
        themselves; one with no name takes every unfinished run on the Track.
        Each is written only if it is still where it was read, so two
        controllers starting at once interrupt it once. A worker of an
        interrupted run that is still alive — whose ``worker_started`` has no
        ``worker_stopped`` — is reaped through the executor, and its stop recorded.
        """
        if self.backend.durable:
            return ()
        interrupted = []
        for run_id in self.track.run_ids():
            # Claimed first, then read: a retry or a decision in this host claims its run
            # before it writes, so a run it is starting is never taken for one left behind.
            try:
                with self._claim(run_id):
                    events = self._read(run_id)
                    run = Run.replay(events)
                    if run is None or run.state not in (RunState.RUNNING, RunState.WAITING_AT_GATE):
                        continue
                    if not self._owns(run):
                        continue  # another controller's, which records it when it starts
                    if self._record_if_still(run_id, run.host_stopped(backend=self.backend.name), run) is None:
                        continue  # it moved as it was read: whoever moved it has it
                    interrupted.append(run_id)
                    self._reap(run_id, events)
            except RunBusy:
                continue
            except InvalidTransition as exc:
                # A Track the machine cannot replay is passed over, as a controller's pass passes over
                # it: one such run never keeps a host from starting, and nothing is written to it.
                _log.warning("run %s cannot be read, and is left as it is: %s", run_id, exc)
        return tuple(interrupted)

    def _reap(self, run_id: str, events: Sequence[TrackEvent]) -> None:
        """Stop each worker of the run that was started and never recorded stopped, and record its stop."""
        stopped = {(e.payload.get("step"), e.payload.get("attempt"), e.payload.get("instance"))
                   for e in events if e.event_type == "worker_stopped"}
        reap = getattr(self.executor, "reap", None)
        for event in events:
            if event.event_type != "worker_started":
                continue
            started = event.payload
            which = (started.get("step"), started.get("attempt"), started.get("instance"))
            if which in stopped:
                continue
            try:
                reaped = bool(reap(started, run_id)) if reap is not None else False
            except Exception:  # noqa: BLE001 - one worker that could not be reaped never stops a start
                _log.exception("reaping the worker of %s step %s failed", run_id, which[0])
                reaped = False
            # Recorded either way: the worker's run token expires here, alive or not.
            self._append(run_id, "worker_stopped", {"step": which[0], "attempt": which[1], "instance": which[2],
                                                    "reaped": reaped})

    def live_worker(self, run_id: str) -> CogWorker | None:
        """The worker this host has invoked for the run and not yet let go of; ``None`` otherwise.

        What a session Cog's turns are delivered to while its step is in flight.
        """
        with self._lock:
            advancing = self._advancing.get(run_id)
            attempt = advancing.attempt if advancing is not None else None
            if attempt is None or not attempt.invoked or attempt.released or attempt.worker is None:
                return None
            return attempt.worker

    def cancel(self, run_id: str, *, actor: str) -> RunState:
        """End a run ``cancelled``, recording who cancelled it, and tear its worker down.

        A run this host is advancing is cancelled at its next step boundary: the
        request is recorded against it here, its live worker is torn down now, and
        the call advancing it records ``cancelled`` and returns — so this returns
        the state the run is in until then. Any other run that can still be
        cancelled — submitted, running, or waiting at a Gate — is cancelled at once;
        an ended run cannot be, and raises ``InvalidTransition``.
        """
        if not actor:
            raise ValueError("a cancellation names its actor")
        while True:
            with self._lock:
                advancing = self._advancing.get(run_id)
            if advancing is not None:
                accepted, worker, attempt = False, None, None
                with advancing.changed:
                    # A claim that is not advancing the run yet, or has written its end, is
                    # short-lived: wait for it to start advancing, or to finish.
                    advancing.changed.wait_for(lambda: advancing.accepting or advancing.finished)
                    if advancing.accepting:
                        advancing.cancelled_by = advancing.cancelled_by or actor
                        accepted = True
                        with self._lock:
                            attempt = advancing.attempt
                            if attempt is not None and attempt.worker is not None and not attempt.released:
                                attempt.released, worker = True, attempt.worker
                                attempt.cancel_teardown = threading.Event()
                if accepted:
                    if worker is not None:
                        try:
                            self.executor.teardown(worker)
                        except Exception as exc:  # noqa: BLE001 - the teardown step tries again, and records it
                            attempt.cancel_teardown_error = type(exc).__name__
                        finally:
                            attempt.cancel_teardown.set()
                    return self.observe(run_id)
                continue  # the claim finished: the run is no longer advancing, so claim it here
            try:
                with self._claim(run_id):
                    run = Run.replay(self._read(run_id))
                    if run is None:
                        raise LookupError(f"no run {run_id!r} on the Track")
                    self._refuse_elsewhere(run_id, run)
                    cancelled = self._record_if_still(run_id, run.cancel(actor=actor), run)
                    if cancelled is not None:
                        return cancelled.state
                    # It moved as it was read — picked up by a controller, say: look at it again.
            except RunBusy:
                continue

    @contextmanager
    def _claim(self, run_id: str) -> Iterator[_Advancing]:
        """Claim a run for one call, before it reads or writes anything of the run.

        Every call that moves a run claims it first — submit, retry, decide, a
        host's start, and a cancel of a run nothing is advancing — so no two calls
        in this host write one run's transitions at once. ``RunBusy`` when another
        call holds it.
        """
        advancing = self._acquire(run_id)
        try:
            yield advancing
        finally:
            self._release(run_id, advancing)

    def _acquire(self, run_id: str) -> _Advancing:
        advancing = _Advancing()
        with self._lock:
            if run_id in self._advancing:
                raise RunBusy(f"run {run_id!r} is being moved by another call in this host")
            self._advancing[run_id] = advancing
        return advancing

    def _release(self, run_id: str, advancing: _Advancing) -> None:
        with self._lock:
            del self._advancing[run_id]
        with advancing.changed:
            advancing.accepting, advancing.finished = False, True
            advancing.changed.notify_all()

    @contextmanager
    def _claim_or_refuse(self, run_id: str) -> Iterator[_Advancing]:
        try:
            advancing = self._acquire(run_id)
        except RunBusy as exc:
            raise ValueError(f"run {run_id!r} is being advanced by another call in this host") from exc
        try:
            yield advancing
        finally:
            self._release(run_id, advancing)

    def _step(self, now: _Pass, name: str, *args: Any) -> Any:
        """Hand one step function to the backend, which runs it and decides what survives."""
        return self.backend.run_step(now.run_id, name, getattr(self, name), *args)

    def _cancelled(self, now: _Pass, run: Run) -> RunState:
        """End the run cancelled; a worker that could not be torn down is recorded beside it, not hidden."""
        attempt = now.advancing.attempt
        if attempt is not None and attempt.teardown_error is not None:
            own = self._step_failed(attempt.step, attempt.number, attempt.key, "TeardownFailed",
                                    f"{attempt.teardown_error}: the worker could not be torn down")
            now.append("step_failed", {**own, "teardown_error": attempt.teardown_error})
        return now.record(run.cancel(actor=now.advancing.cancelled_by)).state

    def _finish(self, now: _Pass, run: Run, end: Callable[[], RunState] | None = None) -> RunState:
        """Write the run's end, or ``cancelled`` if a cancel was taken first; no cancel is taken after.

        Holds the claim's condition, which ``cancel()`` takes to accept a request,
        so a cancel lands before the end or not at all.
        """
        advancing = now.advancing
        with advancing.changed:
            state = self._cancelled(now, run) if advancing.cancelled_by else end()
            advancing.accepting = False
            advancing.changed.notify_all()
            return state

    def _keep(self, now: _Pass, run: Run, write: Callable[[], Any]) -> RunState | None:
        """Write a step's result unless a cancel was taken first, in which case end the run cancelled."""
        advancing = now.advancing
        with advancing.changed:
            if advancing.cancelled_by:
                return self._finish(now, run)
            write()
            return None

    def submit(self, op: OpDefinition) -> RunState:
        names = [step.name for step in op.steps]
        if len(names) != len(set(names)):
            raise ValueError(f"Op {op.run_id!r} has duplicate step names: {names}")
        while True:
            try:
                advancing = self._acquire(op.run_id)
                break
            except RunBusy:
                # Another call in this host is moving it, and submitting again never resumes a
                # run. Once that call is advancing it — its submission written — or done, this
                # submission is checked against the recorded Op and answered with the status.
                state = self._submitted_elsewhere(op)
                if state is not None:
                    return state
        try:
            existing = self._read(op.run_id)
            written: list[TrackEvent] = []
            if not existing:
                self._record(op.run_id, Run.submit(op.run_id, _serialize_op(op)), into=written)
            else:
                if _canonical_op(self._submitted_definition(op.run_id, existing)) != _canonical_op(op):
                    raise ValueError(f"run {op.run_id!r} was submitted with a different Op")
                run = Run.replay(existing)
                if run is not None and run.state.ended:
                    # A finished run is immutable: re-submitting must not silently
                    # re-drive steps (and repeat side effects). Re-running a failed run
                    # is a deliberate act — call retry().
                    return run.state
                if run is not None and run.state is not RunState.SUBMITTED:
                    # Submitting again never resumes a run (ADR-0002 D2): one waiting at a
                    # Gate resumes through decide(), and one a stopped host left running
                    # is recorded `interrupted` by start() and continues only through
                    # retry(). Only a run submitted and never picked up is started here.
                    return run.state
            return self._advance(_Pass(self, op.run_id, (*existing, *written), advancing), op)
        finally:
            self._release(op.run_id, advancing)

    def _submitted_elsewhere(self, op: OpDefinition) -> RunState | None:
        """A resubmission of a run another call holds: its status, once there is a submission to check.

        Waits only until the holder is advancing the run or has let it go, never for
        the run itself. ``None`` when the holder let it go without submitting it, so
        the caller claims it again.
        """
        with self._lock:
            holder = self._advancing.get(op.run_id)
        if holder is not None:
            with holder.changed:
                holder.changed.wait_for(lambda: holder.accepting or holder.finished)
        existing = self._read(op.run_id)
        if not existing:
            return None
        if _canonical_op(self._submitted_definition(op.run_id, existing)) != _canonical_op(op):
            raise ValueError(f"run {op.run_id!r} was submitted with a different Op")
        return Run.replay(existing).state

    def retry(self, run_id: str) -> RunState:
        """Re-drive an unsuccessfully-ended run from its first incomplete step.

        Retry is for failed runs. Exhausted duration/token/cost budgets are not
        reset — the run machine allows it as a new budget epoch, which #4 builds —
        so start a new run instead. A completed run has no incomplete steps, so
        retrying it would only append a spurious `completed`; that is rejected
        (re-running finished work is a new Op, with its own run id). Unlike a
        crash-recovery resume (which reuses the same idempotency key so a durable
        worker can dedupe), a retry of a failed run records a ``retry_requested``
        marker that advances the per-step attempt, so each step gets a fresh key —
        the caller is asking for the work to run again.
        """
        with self._claim_or_refuse(run_id) as advancing:
            return self._retry(run_id, advancing)

    def _retry(self, run_id: str, advancing: _Advancing) -> RunState:
        events = self._read(run_id)
        run = Run.replay(events)
        state = None if run is None else run.state
        if run is None or not run.state.ended:
            raise ValueError(f"run {run_id!r} is not terminal (status={state}); nothing to retry")
        if state is RunState.BUDGET_EXCEEDED:
            # The run machine allows it as a new budget epoch; the engine does not until #4 builds epochs.
            raise ValueError(f"run {run_id!r} exhausted its budget; start a new run instead")
        if not RUN.accepts(state, "retry"):
            done = "completed" if state is RunState.COMPLETED else f"was {state.value}"
            raise ValueError(f"run {run_id!r} {done}; nothing to retry (start a new run instead)")
        op = self._submitted_definition(run_id, events)
        written: list[TrackEvent] = []
        if self._record_if_still(run_id, run.retry(controller=self.controller), run, written) is None:
            raise ValueError(f"run {run_id!r} moved while it was being retried; look at it again")
        return self._advance(_Pass(self, run_id, (*events, *written), advancing), op)

    # --- advancing a run ------------------------------------------------------------------------

    def _advance(self, now: _Pass, op: OpDefinition) -> RunState:
        """The driver: the order the step functions run in, and every decision between them.

        The run is claimed by the caller, and ``now`` holds the Track it read. A
        cancel is taken from here until the run's end is written, and every step
        result and the end itself are written through ``_keep`` and ``_finish``, so
        a cancel either lands before them or not at all.
        """
        with now.advancing.changed:
            now.advancing.accepting = True
            now.advancing.changed.notify_all()
        run = Run.replay(now.history)
        if run.state is RunState.SUBMITTED:
            # Picked up only if nobody picked it up, or cancelled it, as this lands: of several
            # controllers sharing the Track, one starts the run.
            picked = self._record_if_still(now.run_id, run.pickup(controller=self.controller), run, now.history)
            if picked is None:
                return self.observe(now.run_id)
            run = picked
        completed = self._completed_steps(now.history)
        retries = self._retry_count(now.history)
        spent_on: str | None = None
        try:
            tracker = self.budget_tracker(now.history)
        except UsageUnavailable as exc:
            reason = str(exc)
            return self._finish(now, run, lambda: now.record(run.fail(error="UsageUnavailable", reason=reason)).state)
        for step in op.steps:
            if step.name in completed:
                continue
            if now.advancing.cancelled_by:
                return self._finish(now, run)
            # The last step this call reaches names the budget stop, if one lands at the end.
            spent_on = step.name
            approved = self._approved(now.history, step.name)
            if approved is not None:
                # The approver saw this envelope, so it is the step's result; the step does not run again.
                stopped = self._keep(now, run, lambda: self._step(now, "complete_approved", now, step, approved))
                if stopped is not None:
                    return stopped
                continue
            if tracker is not None:
                try:
                    tracker.check()
                except BudgetExceeded as exc:
                    stop, name = exc, step.name
                    return self._finish(now, run, lambda: self._step(now, "stop_for_budget", now, run, name, stop))
            attempt = self._step(now, "resolve", now, op, step, run, retries)
            with self._lock:
                now.advancing.attempt = attempt
            self._run_attempt(now, op, attempt)
            if now.advancing.cancelled_by:
                # Cancelled while the attempt ran: its worker is torn down, and its result is not kept.
                return self._finish(now, run)
            if attempt.teardown_error is not None or attempt.outcome[0] != "ok":
                return self._finish(now, run, lambda: self._step(now, "fail", now, run, attempt))
            budget_stop = None
            if tracker is not None:
                usage = attempt.usage or {}
                try:
                    tracker.consume(tokens=usage.get("tokens", 0), cost=usage.get("cost", 0.0))
                except BudgetExceeded as exc:
                    budget_stop = exc
            envelope = attempt.outcome[1]
            verdict, why = self._step(now, "evaluate_gate", step, envelope)
            if verdict is GateOutcome.ESCALATE:
                # A budget stop never discards a result that was paid for: the escalation
                # is recorded, and the stop lands at the next boundary — the end of the
                # run included — by when no further work has been spent.
                return self._finish(now, run, lambda: self._step(now, "escalate", now, op, run, attempt, envelope, why))
            stopped = self._keep(now, run, lambda: self._step(now, "complete", now, op, attempt, envelope))
            if stopped is not None:
                return stopped
            if budget_stop is not None:
                stop = budget_stop
                return self._finish(now, run, lambda: self._step(now, "stop_for_budget", now, run, step.name, stop))
        if tracker is not None and spent_on is not None:
            # The end of a run is a boundary too: one that overspent on its last step
            # stops here instead of completing over its budget.
            try:
                tracker.check()
            except BudgetExceeded as exc:
                stop, last = exc, spent_on
                return self._finish(now, run, lambda: self._step(now, "stop_for_budget", now, run, last, stop))
        return self._finish(now, run, lambda: now.record(run.complete()).state)

    def _run_attempt(self, now: _Pass, op: OpDefinition, attempt: Attempt) -> None:
        """Materialize, interact and read the envelope, then tear the worker down whatever happened.

        All three are inside failure handling so any infra error becomes a durable
        `failed` event (never a non-terminal run); teardown is best-effort in `finally`.
        """
        step = attempt.step
        try:
            # A cancel taken before the worker exists, or before it is invoked, stops the attempt
            # there: no worker is brought up for a cancelled run, and none is asked to act.
            if now.advancing.cancelled_by:
                raise _Cancelled("the run was cancelled before its worker was materialized")
            self._step(now, "materialize", now, op, attempt)
            if now.advancing.cancelled_by:
                raise _Cancelled("the run was cancelled before its worker was invoked")
            self._step(now, "interact", now, attempt)
            self._step(now, "read_envelope", now, attempt)
        except UsageUnavailable as exc:
            # Persist unknown accounting so recovery/retry cannot forget it.
            now.append("interaction_usage", {"step": step.name, "attempt": attempt.number, "usage": None})
            attempt.failure_reason = _bound(exc)
            attempt.outcome = ("broken", "UsageUnavailable")
        except EnvelopeInvalid as exc:
            # Not the seam's envelope, so whatever the worker spent is unknown too,
            # unless it was a valid envelope but for its payload.
            if not attempt.accounted:
                now.append("interaction_usage", {"step": step.name, "attempt": attempt.number, "usage": None})
            attempt.failure_reason = _bound(exc)
            attempt.outcome = ("broken", "EnvelopeInvalid")
        except Exception as exc:  # noqa: BLE001 - any materialize/interact failure is durable-failed
            if attempt.invoked:
                # A failed request may have spent resources before failing.
                now.append("interaction_usage", {"step": step.name, "attempt": attempt.number, "usage": None})
            # The class names the failure; its message, bounded, says what happened.
            attempt.failure_reason = _bound(exc) or None
            attempt.outcome = ("broken", type(exc).__name__)
        finally:
            if attempt.worker is not None:
                attempt.teardown_error = self._step(now, "teardown", now, attempt)

    # --- the step functions ---------------------------------------------------------------------

    @step_function
    def resolve(self, now: _Pass, op: OpDefinition, step: OpStep, run: Run, retries: int) -> Attempt:
        """Name the attempt this step runs as, and record that it started.

        attempt = prior escalations (revisions) + explicit retries, NOT the
        step_started count: a crash before the outcome is recorded re-runs with
        the SAME instance/key (a durable worker can dedupe the replay), while an
        explicit retry() bumps the attempt so it re-runs under a fresh one.
        `instance` identifies the materialization (so distinct steps and attempts
        never reuse a still-terminating worker name); `key` is the same identity,
        run-scoped, handed to the worker for dedupe.
        """
        number = run.escalations.get(step.name, 0) + retries
        instance = f"{_key_component(step.name)}:{number}"
        attempt = Attempt(step=step, number=number, instance=instance, key=f"{_key_component(op.run_id)}:{instance}")
        now.append("step_started", {"step": step.name, "cog": step.cog, "digest": step.digest, "attempt": number})
        return attempt

    @step_function
    def materialize(self, now: _Pass, op: OpDefinition, attempt: Attempt) -> None:
        """Bring up the attempt's worker, and move its machine to ready."""
        step = attempt.step
        attempt.worker = self.executor.materialize(step.cog, op.run_id, attempt.instance)
        attempt.cog_worker = now.record(Worker.materialize(step.cog, step=step.name, digest=step.digest))
        token_digest = getattr(attempt.worker, "run_token_digest", None)
        if token_digest is not None:
            # Where the worker is, and the hash of its run token: the token itself is never recorded.
            now.append("worker_started", {
                "step": step.name, "attempt": attempt.number, "instance": attempt.instance,
                **getattr(attempt.worker, "details", {}), "run_token_sha256": token_digest})
            attempt.started = True
        attempt.cog_worker = now.record(attempt.cog_worker.ready())

    @step_function
    def interact(self, now: _Pass, attempt: Attempt) -> None:
        """Invoke the step's entry point with its input, and the findings of a send back if there was one."""
        step = attempt.step
        attempt.cog_worker = now.record(attempt.cog_worker.invoke(entry_point=step.entry_point, step=step.name))
        signal_value = self._signal_for(now.history, step.name)
        feedback = {} if signal_value is _NO_SIGNAL else {"signal": signal_value}
        attempt.invoked = True
        attempt.result = attempt.worker.interact(step.entry_point, step.input, idempotency_key=attempt.key,
                                                 **feedback)

    @step_function
    def read_envelope(self, now: _Pass, attempt: Attempt) -> None:
        """Check the answer is an envelope with a JSON payload, and account for what it spent."""
        result = attempt.result
        if not isinstance(result, ResultEnvelope):
            raise EnvelopeInvalid("interact() must return a ResultEnvelope")
        not_json = None
        if result.ok:
            try:
                attempt.rendered = _render(result.payload)
            except EnvelopeInvalid as exc:
                not_json = exc
        attempt.answered = not_json is None
        # ok with problems is not a failure: the step's Gate decides what
        # the problems mean. ok: false is one.
        attempt.outcome = ("ok", result) if result.ok else ("error", result)
        attempt.usage = _validate_usage(result.usage, self.budget)
        now.append("interaction_usage", {"step": attempt.step.name, "attempt": attempt.number, "usage": attempt.usage})
        attempt.accounted = True
        if not_json is not None:
            # The worker spent what it reported, so that is counted; its
            # answer is still not a valid envelope, and fails the step.
            raise not_json

    @step_function
    def teardown(self, now: _Pass, attempt: Attempt) -> str | None:
        """Tear a step's worker down, moving it through its machine; the executor's error if teardown failed.

        A worker that answered — with an envelope, ok or not — goes IDLE and
        is torn down as a one-shot, and a teardown that fails is its machine's
        `teardown_failed`. One that did not answer has failed: the executor reclaims
        what is left of it, and if that fails the run's `failed` record says so.
        ``TORN_DOWN`` is never recorded, so the runner does not move the worker there.
        A worker ``cancel()`` already tore down is not torn down again; its machine
        records why it stopped. A worker whose start was recorded has its stop
        recorded once it is gone, which is when its run token expires.
        """
        error = self._tear_down(now, attempt)
        if error is None and attempt.started:
            now.append("worker_stopped", {"step": attempt.step.name, "attempt": attempt.number,
                                          "instance": attempt.instance})
        return error

    def _tear_down(self, now: _Pass, attempt: Attempt) -> str | None:
        cog_worker = attempt.cog_worker
        with self._lock:
            by_cancel, attempt.released = attempt.released, True
        if by_cancel:
            if cog_worker is not None:
                now.record(cog_worker.fail(error="Cancelled"))
            attempt.cancel_teardown.wait()
            if attempt.cancel_teardown_error is None:
                return None
            try:  # cancel()'s teardown failed: try once more, and report it if this fails too
                self.executor.teardown(attempt.worker)
            except Exception as exc:  # noqa: BLE001 - never crash on cleanup
                return type(exc).__name__
            return None
        tearing_down = attempt.answered and cog_worker is not None
        if tearing_down:
            cog_worker = now.record(cog_worker.envelope_returned())
            cog_worker = now.record(cog_worker.tear_down(reason="one_shot"))
        elif cog_worker is not None:
            cog_worker = now.record(cog_worker.fail(error=str(attempt.outcome[1])))
        try:
            self.executor.teardown(attempt.worker)
        except Exception as exc:  # noqa: BLE001 - never crash on cleanup
            if tearing_down:
                now.record(cog_worker.fail(error=type(exc).__name__))
            return type(exc).__name__
        return None

    @step_function
    def evaluate_gate(self, step: OpStep, envelope: ResultEnvelope) -> tuple[GateOutcome, str | None]:
        """What the step's Gate decides about its envelope, and why."""
        return step.gate.evaluate(envelope)

    @step_function
    def complete(self, now: _Pass, op: OpDefinition, attempt: Attempt, envelope: ResultEnvelope) -> None:
        """Record the step's result: inline, or by reference above the threshold."""
        result = self._result(op.run_id, attempt.key, envelope.payload, attempt.rendered)
        now.append("step_completed", self._step_completed(attempt.step, attempt.number, envelope, attempt.usage,
                                                          result))

    @step_function
    def escalate(self, now: _Pass, op: OpDefinition, run: Run, attempt: Attempt, envelope: ResultEnvelope,
                 why: str | None) -> RunState:
        """Hold the run at the step's Gate, with the result a person decides on.

        A large result is kept once, by reference, like a completed one: the
        escalation holds the envelope without it, and its digest, so what a
        decision names still identifies the result.
        """
        step = attempt.step
        result = self._result(op.run_id, attempt.key, envelope.payload, attempt.rendered)
        shown = envelope.to_dict()
        if "payload_ref" in result:
            shown["payload"] = None
            shown["payload_digest"] = _payload_digest(envelope.payload)
        details = {
            "attempt": attempt.number, "envelope": shown, **result, "usage": attempt.usage,
            "approvers": list(step.gate.deciders), "gate": step.gate.escalate,
        }
        eid = escalation_id(op.run_id, step.name, attempt.number, envelope)
        run = now.record(run.escalate(step=step.name, reason=why, escalation=eid, details=details))
        return run.state

    @step_function
    def fail(self, now: _Pass, run: Run, attempt: Attempt) -> RunState:
        """Record a step that did not produce a result, and end the run ``failed``."""
        step, key, number = attempt.step, attempt.key, attempt.number
        kind, detail = attempt.outcome
        teardown_error = attempt.teardown_error
        if teardown_error is not None:
            # A worker we couldn't tear down may keep running/serving — that is
            # a leak, not success. Fail the run so it is visible; durable
            # cleanup-retry lands with the crash-safe engine backing (#1).
            # The step's own failure, when it had one, is what step_failed
            # records; the teardown that also failed is recorded beside it.
            if kind == "broken":
                own = self._step_failed(step, number, key, detail, attempt.failure_reason)
            elif kind == "error":
                own = self._step_failed(step, number, key, detail.error.code, detail.error.detail, detail)
            else:
                own = self._step_failed(step, number, key, "TeardownFailed",
                                        f"{teardown_error}: the worker could not be torn down")
            now.append("step_failed", {**own, "teardown_error": teardown_error})
            # A failed worker's own error is kept beside it, since its teardown is not a worker move.
            details = None if attempt.answered else {"worker_error": str(detail)}
            failed = run.fail(step=step.name, error="TeardownFailed", reason=_bound(teardown_error),
                              details=details)
            run = now.record(failed)
            return run.state
        if kind == "broken":
            now.append("step_failed", self._step_failed(step, number, key, detail, attempt.failure_reason))
            run = now.record(run.fail(step=step.name, error=detail, reason=_bound(attempt.failure_reason)))
            return run.state
        if kind != "error":
            # Only an attempt without a result ends here: one that broke, answered ok: false,
            # or whose worker could not be torn down. An ok answer goes through the Gate instead.
            raise ValueError(f"fail() is for an attempt without a result; step {step.name!r} answered {kind!r}")
        # The worker answered, and said no. The code is what a client acts
        # on, so it is the event's error, verbatim; the detail is the reason.
        now.append("step_failed", self._step_failed(step, number, key, detail.error.code, detail.error.detail,
                                                     detail))
        failed = run.fail(step=step.name, error=detail.error.code, reason=_bound(detail.error.detail),
                          details=_recorded(detail))
        run = now.record(failed)
        return run.state

    @step_function
    def complete_approved(self, now: _Pass, step: OpStep, approved: Mapping[str, Any]) -> None:
        """Complete a step whose escalated result was approved, from the envelope its approver saw."""
        envelope = ResultEnvelope.parse(approved["envelope"])
        kept = approved.get("payload_ref")
        result = {"payload_ref": kept} if kept else {"payload": envelope.payload}
        now.append("step_completed", self._step_completed(step, approved.get("attempt", 0), envelope,
                                                          approved.get("usage"), result, approved["escalation"]))

    @step_function
    def stop_for_budget(self, now: _Pass, run: Run, step: str, exc: BudgetExceeded) -> RunState:
        """Stop the run at a boundary its budget has passed; a duration stop keeps the Track's `timed_out` event."""
        run = now.record(run.exhaust_budget(dimension=exc.dimension, step=step, reason=str(exc)))
        return run.state

    def decide(
        self, run_id: str, *, escalation: str | None, actor: str, outcome: str,
        findings: Sequence[Any] | None = (),
    ) -> RunState:
        """Answer the escalation a run waits on: ``approve``, ``reject`` or ``send_back``.

        The decision names the escalation it answers; one naming an escalation
        that is no longer open raises ``StaleEscalation`` and changes nothing.
        Approve completes the step with the envelope the approver saw, reject
        ends the run ``REJECTED``, and send back re-runs the step with the
        findings as its signal, up to ``max_revisions`` revisions. The decision is
        recorded before the run advances, so a crash after it resumes from it.
        Who may decide is the run API's to check (#103); the engine records who did.
        """
        if not actor:
            raise ValueError("a decision names its actor")
        if findings is not None and (isinstance(findings, (str, bytes)) or not isinstance(findings, Sequence)):
            # One string, a mapping or a set is not a sequence of findings: listing it would
            # record its characters, its keys, or an arbitrary order.
            raise ValueError("findings are a sequence of findings, not one string, mapping or set")
        with self._claim_or_refuse(run_id) as advancing:
            return self._decide(run_id, advancing, escalation=escalation, actor=actor, outcome=outcome,
                                findings=findings)

    def _decide(self, run_id: str, advancing: _Advancing, *, escalation: str | None, actor: str, outcome: str,
                findings: Sequence[Any] | None) -> RunState:
        events = self._read(run_id)
        run = Run.replay(events)
        if run is None or run.state is not RunState.WAITING_AT_GATE:
            raise ValueError(f"run {run_id!r} is not waiting at a Gate")
        self._refuse_elsewhere(run_id, run)
        waiting_on = self._open_escalation(events, run)
        if outcome == "approve" and waiting_on is not None and waiting_on["envelope"] is None:
            # An escalation recorded before Gates holds no result, so there is nothing to
            # accept as it is; a send back asks for the work again, a reject ends the run.
            raise ValueError(f"run {run_id!r} escalated before Gates and has no recorded result to approve; "
                             f"send it back or reject it")
        op = self._submitted_definition(run_id, events)
        decided_on = None if waiting_on is None or waiting_on["envelope"] is None \
            else envelope_digest(waiting_on["envelope"])
        decision = run.decide(outcome=outcome, escalation=escalation, findings=list(findings or ()), actor=actor,
                              revise_limit=self.max_revisions, envelope_digest=decided_on)
        written: list[TrackEvent] = []
        run = self._record(run_id, decision, into=written)
        if run.state is not RunState.RUNNING:
            return run.state
        return self._advance(_Pass(self, run_id, (*events, *written), advancing), op)
