"""What a client asks of a run, written to the Track, and what the Track says of it.

The process that accepts runs is not the one that advances them (ADR-0002 D4).
The API writes intent here — a submission, a request to cancel, a turn — and reads a
run's status from the Track; it constructs no executor and never calls the
controller. The controller (``controller.py``) finds the intent on the Track
and acts on it.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from .ops import OpDefinition, _deserialize_op, _serialize_op
from .states import InvalidTransition, Run, RunState
from .states.run import DECISIONS
from .track import SCHEMA_VERSION, TrackEvent, TrackStore, upgrade

CANCEL_REQUESTED = "cancel_requested"
_log = logging.getLogger(__name__)


class RunEnded(InvalidTransition):
    """A request that needs a run still advancing, made of one that has ended."""


def _append(track: TrackStore, run_id: str, event_type: str, payload: Mapping[str, Any]) -> None:
    track.append(TrackEvent(run_id=run_id, event_type=event_type, payload=dict(payload), schema=SCHEMA_VERSION))


def submit(track: TrackStore, op: OpDefinition, *, by: Mapping[str, Any], name: str | None = None) -> None:
    """Record that ``by`` submitted an Op. Nothing runs here: a controller picks the run up.

    A run id submitted twice is refused by the Track (``OneSubmissionPerRun``).
    """
    for record in Run.submit(op.run_id, _serialize_op(op), by=by, name=name).records:
        _append(track, op.run_id, record.event_type, record.payload)


def cancel_request(events: Iterable[TrackEvent]) -> str | None:
    """Who asked for the run to be cancelled, as it stands now; ``None`` when nobody has.

    A request belongs to the attempt it was made of: one that a run outlived,
    because it ended on its own first and was then retried, does not cancel the
    retry.
    """
    actor = None
    for event in events:
        if event.event_type == CANCEL_REQUESTED:
            actor = actor or event.payload.get("actor")
        elif event.event_type == "retry_requested":
            actor = None
    return actor


def request_cancel(track: TrackStore, run_id: str, *, actor: str) -> RunView:
    """Record that ``actor`` asked for a run to be cancelled; the controller tears it down and ends it.

    ``LookupError`` for a run never submitted, :class:`RunEnded` for one that
    has ended. Asking twice records the request once. The request is appended
    only if, as it lands, the run has not ended and holds no request yet: the
    check and the append are one step on the Track, so a run that ends, or a
    second request that arrives, in between is seen.
    """
    def still_wanted(events: tuple[TrackEvent, ...]) -> bool:
        events = tuple(upgrade(event) for event in events)
        run = Run.replay(events)
        return run is not None and not run.state.ended and cancel_request(events) is None

    request = TrackEvent(run_id=run_id, event_type=CANCEL_REQUESTED, payload={"actor": actor}, schema=SCHEMA_VERSION)
    recorded = track.append_if(request, still_wanted)
    view = describe(track, run_id)
    if view is None:
        raise LookupError(f"no run {run_id!r} on the Track")
    if recorded is None and view.state.ended:
        raise RunEnded(view.state, CANCEL_REQUESTED, f"the run has ended {view.status}")
    return view  # recorded now, or a request already stood


@dataclass(frozen=True, slots=True)
class StepView:
    name: str
    cog: str
    state: str
    """``pending``, ``running``, ``completed``, ``failed`` or ``waiting_at_gate``."""
    attempt: int | None = None
    error: str | None = None
    output: Any = None
    """What the step's Cog answered with, when the step completed and the Track holds it inline."""
    output_ref: str | None = None
    """Where a completed step's output is kept, when it was too large for the Track to hold inline."""


@dataclass(frozen=True, slots=True)
class RunView:
    """A run as its Track tells it: the status, each step, who submitted it, and when."""

    run_id: str
    state: RunState
    op: OpDefinition
    steps: tuple[StepView, ...]
    submitted_by: Mapping[str, Any]
    submitted_at: datetime
    updated_at: datetime
    cancel_requested_by: str | None = None
    error: str | None = None
    reason: str | None = None
    name: str | None = None
    """What the client called the run when it submitted it, if anything: a label, never an id."""
    controller: str | None = None
    """The controller that picked the run up, which alone advances it; ``None`` until one does."""
    decision: Mapping[str, Any] | None = None
    """A decision on the open Gate that a client asked for and the controller has not delivered yet."""

    @property
    def status(self) -> str:
        return self.state.name


def describe(track: TrackStore, run_id: str) -> RunView | None:
    """The run's view, replayed from its Track; ``None`` for a run never submitted."""
    return _view(run_id, tuple(upgrade(event) for event in track.replay(run_id)))


def _view(run_id: str, events: tuple[TrackEvent, ...]) -> RunView | None:
    """A run's view from its events, already in schema v1; ``None`` for a run never submitted."""
    run = Run.replay(events)
    if run is None:
        return None
    submission = next(event for event in events if event.event_type == "op_submitted")
    op = _deserialize_op(submission.payload["op"])
    steps: dict[str, dict[str, Any]] = {step.name: {"state": "pending"} for step in op.steps}
    error = reason = None
    cancel_requested_by = cancel_request(events)
    open_steps: set[str] = set()  # started, and neither completed nor failed: running, or waiting at a Gate
    for event in events:
        payload = event.payload
        step = steps.get(payload.get("step")) if isinstance(payload.get("step"), str) else None
        if event.event_type in ("failed", "budget_exceeded"):
            error, reason = payload.get("error") or event.event_type, payload.get("reason")
        elif event.event_type == "retry_requested":
            error = reason = None
        if step is None:
            continue
        if event.event_type == "step_started":
            step.update(state="running", attempt=payload.get("attempt"), error=None)
            open_steps.add(payload["step"])
        elif event.event_type == "gate_escalated":
            step.update(state="waiting_at_gate")
            open_steps.add(payload["step"])
        elif event.event_type == "step_completed":
            step.update(state="completed", output=payload.get("payload"), output_ref=payload.get("payload_ref"))
            open_steps.discard(payload["step"])
        elif event.event_type == "step_failed":
            step.update(state="failed", error=payload.get("error"))
            open_steps.discard(payload["step"])
    if run.state.ended:
        # A step that was running, or waiting at its Gate, when its run ended did not finish: it
        # ended with the run, cancelled, rejected or interrupted as the run was.
        for name in open_steps:
            steps[name]["state"] = run.state.value
    return RunView(
        run_id=run_id, state=run.state, op=op,
        steps=tuple(StepView(name=step.name, cog=step.cog, **steps[step.name]) for step in op.steps),
        submitted_by=dict(submission.payload.get("submitted_by") or {}), name=submission.payload.get("name"),
        submitted_at=submission.occurred_at, updated_at=events[-1].occurred_at,
        cancel_requested_by=cancel_requested_by, error=error, reason=reason, controller=run.controller,
        decision=pending_decision(events) if run.state is RunState.WAITING_AT_GATE else None,
    )


def list_runs(track: TrackStore) -> tuple[RunView, ...]:
    """Every run on the Track that can be read, newest submission first."""
    return RunViews(track).views()


class RunUnreadable(ValueError):
    """A run whose Track the run machine cannot replay, or whose submission cannot be read."""


@dataclass
class _Kept:
    events: list[TrackEvent] | None
    """``None`` once the run has ended: its view and turns are kept, and its events let go."""
    last_sequence: int = 0
    built_at: int = -1
    view: RunView | None = None
    turns: dict[str, TurnView] | None = None
    unreadable: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    """Held while this one run is read and rebuilt: reads of different runs never wait on each other."""


_NEVER_SUBMITTED = _Kept(events=[])
KEPT_RUNS = 10_000
"""How many runs a :class:`RunViews` keeps at most; the least recently read are let go, and read again
in full should they be asked for."""


class RunViews:
    """Run views kept current by reading only what each run's Track gained since the last read.

    Reading a run asks its Track for the events after the last one already
    read, so a run that has not moved costs one empty query, and a view is
    rebuilt only when its run has new events. A run whose Track cannot be
    replayed is set aside, logged once, and left out of :meth:`views`: one
    unreadable run never hides every other. Safe to share between threads.

    Each run is read under a lock of its own, so one run's read never waits for
    another's, and the store's query runs outside the lock that guards which
    runs are kept. What is kept stays bounded: a run id with no events leaves
    nothing behind, a run that has ended keeps its view and its turns but lets
    its events go, and past ``kept`` runs the least recently read are let go,
    to be read again in full should they be asked for. :meth:`forget` drops a
    run altogether.
    """

    def __init__(self, track: TrackStore, *, kept: int = KEPT_RUNS) -> None:
        self.track = track
        self._lock = threading.Lock()
        self._kept: OrderedDict[str, _Kept] = OrderedDict()
        self._most = kept

    def _entry(self, run_id: str) -> tuple[_Kept, bool]:
        """The run's entry, made fresh when it has none; and whether it was."""
        with self._lock:
            kept = self._kept.get(run_id)
            if kept is not None:
                self._kept.move_to_end(run_id)
                return kept, False
            kept = self._kept[run_id] = _Kept(events=[])
            while len(self._kept) > self._most:
                self._kept.popitem(last=False)
            return kept, True

    def _refresh(self, run_id: str) -> _Kept:
        kept, new = self._entry(run_id)
        with kept.lock:
            if new or kept.built_at == -1:
                fresh = self.track.replay(run_id)
                if not fresh:
                    with self._lock:
                        if self._kept.get(run_id) is kept:
                            del self._kept[run_id]  # nothing to keep for an id nobody submitted
                    return _NEVER_SUBMITTED
            else:
                fresh = self.track.replay(run_id, after_sequence=kept.last_sequence)
                if fresh and kept.events is None:
                    # An ended run moved again: read it whole once more.
                    kept.events, fresh = [], self.track.replay(run_id)
            if fresh:
                kept.events.extend(upgrade(event) for event in fresh)
                kept.last_sequence = max(event.sequence or 0 for event in fresh)
            if kept.built_at != kept.last_sequence:
                kept.built_at, kept.turns = kept.last_sequence, None
                try:
                    kept.view, kept.unreadable = _view(run_id, tuple(kept.events)), None
                except (InvalidTransition, LookupError, KeyError, TypeError, ValueError) as exc:
                    kept.view, kept.unreadable = None, f"{type(exc).__name__}: {exc}"
                    _log.warning("run %s cannot be read, and is left out: %s", run_id, kept.unreadable)
                if kept.view is not None and kept.view.state.ended:
                    kept.turns, kept.events = turns(kept.events), None
            return kept

    def forget(self, run_id: str) -> None:
        """Keep nothing more of a run, as a controller does once the run has ended."""
        with self._lock:
            self._kept.pop(run_id, None)

    def events(self, run_id: str) -> tuple[TrackEvent, ...]:
        kept = self._refresh(run_id)
        if kept.events is None:
            return tuple(upgrade(event) for event in self.track.replay(run_id))
        return tuple(kept.events)

    def after(self, run_id: str, sequence: int) -> tuple[tuple[TrackEvent, ...], RunView | None]:
        """The run's events after ``sequence`` and its view, from one read: the view is of those events.

        So a run that reads as ended has its last event among them, or before
        ``sequence``: nothing can land between the two. ``RunUnreadable`` for a
        run that cannot be read.
        """
        kept = self._refresh(run_id)
        if kept.unreadable is not None:
            raise RunUnreadable(f"run {run_id!r} cannot be read: {kept.unreadable}")
        with kept.lock:
            view, kept_events, last = kept.view, kept.events, kept.last_sequence
        if kept_events is None:
            # Ended, its events let go: they are read from the store, up to what the view was built on.
            tail = tuple(upgrade(event) for event in self.track.replay(run_id, after_sequence=sequence)
                         if (event.sequence or 0) <= last)
        else:
            tail = tuple(event for event in kept_events if (event.sequence or 0) > sequence)
        return tail, view

    def view(self, run_id: str) -> RunView | None:
        """The run's view; ``None`` for a run never submitted. :class:`RunUnreadable` for one that cannot be read."""
        kept = self._refresh(run_id)
        if kept.unreadable is not None:
            raise RunUnreadable(f"run {run_id!r} cannot be read: {kept.unreadable}")
        return kept.view

    def turns(self, run_id: str) -> dict[str, TurnView]:
        kept = self._refresh(run_id)
        with kept.lock:
            if kept.turns is None:
                kept.turns = turns(kept.events or [])
            return kept.turns

    def views(self, *, org_id: str | None = None, status: str | None = None) -> tuple[RunView, ...]:
        """The runs that can be read, newest submission first; an organization's, in a status, when given."""
        found = []
        for run_id in self.track.run_ids():
            kept = self._refresh(run_id)
            view = kept.view
            if view is None:
                continue
            if org_id is not None and view.submitted_by.get("org_id") != org_id:
                continue
            if status is not None and view.status != status:
                continue
            found.append(view)
        return tuple(sorted(found, key=lambda view: view.submitted_at, reverse=True))


# --- turns: talking to a Cog while its step runs ------------------------------------------------
#
# A Cog whose entry point holds a session (``hello``'s ``session``) answers turns while its step
# is in flight. A client asks for a turn here; the controller delivers it to the run's live
# worker and records the answer. Every turn and its answer are on the Track, like the rest of
# the run.

TURN_REQUESTED, TURN_ANSWERED, TURN_FAILED = "turn_requested", "turn_answered", "turn_failed"
MAX_TURN_TEXT = 64 * 1024


@dataclass(frozen=True, slots=True)
class TurnView:
    turn: str
    text: str
    actor: str | None
    state: str
    """``pending`` until the worker answers, then ``answered`` or ``failed``."""
    answer: str | None = None
    error: str | None = None


def turns(events: Iterable[TrackEvent]) -> dict[str, TurnView]:
    """Every turn of a run, in the order it was asked, as its Track tells it."""
    events = tuple(upgrade(event) for event in events)
    asked: dict[str, TurnView] = {}
    for event in events:
        payload = event.payload
        turn = payload.get("turn")
        if event.event_type == TURN_REQUESTED:
            asked[turn] = TurnView(turn=turn, text=payload.get("text", ""), actor=payload.get("actor"),
                                   state="pending")
        elif event.event_type == TURN_ANSWERED and turn in asked:
            asked[turn] = replace(asked[turn], state="answered", answer=payload.get("text"))
        elif event.event_type == TURN_FAILED and turn in asked:
            asked[turn] = replace(asked[turn], state="failed", error=payload.get("error"))
    run = Run.replay(events)
    if run is not None and run.state.ended:
        # A turn still waiting when its run ended will never be answered.
        for turn, view in asked.items():
            if view.state == "pending":
                asked[turn] = replace(view, state="failed", error=f"the run ended {run.state.name}")
    return asked


def request_turn(track: TrackStore, run_id: str, *, text: str, actor: str) -> TurnView:
    """Ask a running Cog something. The controller delivers it to the run's worker, in order.

    ``LookupError`` for a run never submitted, :class:`RunEnded` for one that
    has ended. The request is written only if, as it lands, the run has not
    ended.
    """
    if len(text.encode()) > MAX_TURN_TEXT:
        raise ValueError(f"a turn's text is at most {MAX_TURN_TEXT} bytes")
    turn = uuid.uuid4().hex[:12]

    def advancing(events: tuple[TrackEvent, ...]) -> bool:
        run = Run.replay(tuple(upgrade(event) for event in events))
        # A run waiting at a Gate has no session to answer until it is decided, which is not offered yet.
        return run is not None and not run.state.ended and run.state is not RunState.WAITING_AT_GATE

    request = TrackEvent(run_id=run_id, event_type=TURN_REQUESTED,
                         payload={"turn": turn, "text": text, "actor": actor}, schema=SCHEMA_VERSION)
    recorded = track.append_if(request, advancing)
    if recorded is None:
        view = describe(track, run_id)
        if view is None:
            raise LookupError(f"no run {run_id!r} on the Track")
        if view.state is RunState.WAITING_AT_GATE:
            raise RunEnded(view.state, TURN_REQUESTED, "the run is waiting at a Gate, and takes no turns there")
        raise RunEnded(view.state, TURN_REQUESTED, f"the run has ended {view.status}")
    return turns(track.replay(run_id))[turn]


def answer_turn(track: TrackStore, run_id: str, turn: str, *, text: str | None = None,
                error: str | None = None) -> None:
    """The controller's record of what became of a turn: the worker's answer, or why there is none."""
    if (text is None) == (error is None):
        raise ValueError("a turn is answered with a text or failed with an error")
    if text is not None:
        _append(track, run_id, TURN_ANSWERED, {"turn": turn, "text": text})
    else:
        _append(track, run_id, TURN_FAILED, {"turn": turn, "error": error})


# --- decisions: a person answering a Gate ---------------------------------------------------------
#
# A run waiting at a Gate waits on one escalation. A client asks for a decision on it here; the
# controller that owns the run delivers it to its runner, which records `gate_decided` and advances
# the run, or the controller records why it could not (`decision_refused`).

DECISION_REQUESTED, DECISION_REFUSED = "decision_requested", "decision_refused"


class StaleDecision(ValueError):
    """A decision on an escalation the run no longer waits on, or while another one is still pending."""


def pending_decision(events: Iterable[TrackEvent]) -> dict[str, Any] | None:
    """The decision asked for on the run's open escalation and not yet delivered; ``None`` when there is none.

    A decision is delivered when its escalation is decided, refused, or ends the
    run (a send back past the revision limit fails it).
    """
    pending = None
    for event in events:
        if event.event_type == DECISION_REQUESTED:
            pending = dict(event.payload)
        elif event.event_type in ("gate_decided", DECISION_REFUSED, "failed") and pending is not None \
                and event.payload.get("escalation") == pending.get("escalation"):
            pending = None
    return pending


def request_decision(track: TrackStore, run_id: str, *, escalation: str, outcome: str, actor: str,
                     findings: Sequence[Any] = ()) -> RunView:
    """Record that ``actor`` decided on the escalation a run waits at: ``approve``, ``send_back`` or ``reject``.

    The controller that owns the run delivers it. ``LookupError`` for a run
    never submitted, :class:`RunEnded` for one that has ended,
    :class:`StaleDecision` for one not waiting on that escalation, or already
    holding a decision on it. The check and the append are one step on the
    Track, so of two people deciding at once, one decision is recorded.
    """
    if outcome not in DECISIONS:
        raise ValueError(f"a decision is one of {', '.join(sorted(DECISIONS))}, not {outcome!r}")
    if not actor:
        raise ValueError("a decision names its actor")
    if isinstance(findings, (str, bytes)) or not isinstance(findings, Sequence):
        # The runner's own rule, checked before the intent is recorded: one string, a mapping or a set
        # would be recorded as its characters, its keys, or an arbitrary order.
        raise ValueError("findings are a sequence of findings, not one string, mapping or set")

    def open_on_it(events: tuple[TrackEvent, ...]) -> bool:
        events = tuple(upgrade(event) for event in events)
        run = Run.replay(events)
        return (run is not None and run.state is RunState.WAITING_AT_GATE and run.open_escalation == escalation
                and pending_decision(events) is None)

    request = TrackEvent(run_id=run_id, event_type=DECISION_REQUESTED, schema=SCHEMA_VERSION, payload={
        "escalation": escalation, "outcome": outcome, "actor": actor, "findings": list(findings)})
    recorded = track.append_if(request, open_on_it)
    view = describe(track, run_id)
    if view is None:
        raise LookupError(f"no run {run_id!r} on the Track")
    if recorded is None:
        if view.state.ended:
            raise RunEnded(view.state, DECISION_REQUESTED, f"the run has ended {view.status}")
        if view.decision is not None:
            raise StaleDecision(f"a decision on run {run_id!r} is already waiting to be delivered")
        raise StaleDecision(f"run {run_id!r} is {view.status}, and does not wait on escalation {escalation!r}")
    return view


def refuse_decision(track: TrackStore, run_id: str, *, escalation: str, error: str) -> None:
    """The controller's record of a decision its runner did not take, and why."""
    _append(track, run_id, DECISION_REFUSED, {"escalation": escalation, "error": error[:1024]})
