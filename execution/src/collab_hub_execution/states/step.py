"""A step attempt: one invocation under one idempotency key, and its keyed claim.

The claim store (``claims.py``) keeps these states; the Track records the attempt's
outcome as ``step_completed`` or ``step_failed``, which is ``RECORDED``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ._machine import Context, Machine, Record, State, Transition, accepts, move


class StepAttemptState(State):
    """The step attempt machine's interface: one method per event, each refused by default."""

    def worker_lost(self, attempt: StepAttempt, **_: Any) -> Transition[StepAttempt]:
        return self.refuse("worker_lost")

    def reserve(self, attempt: StepAttempt, **_: Any) -> Transition[StepAttempt]:
        return self.refuse("reserve")

    def commit(self, attempt: StepAttempt, **_: Any) -> Transition[StepAttempt]:
        return self.refuse("commit")

    def record(self, attempt: StepAttempt, **_: Any) -> Transition[StepAttempt]:
        return self.refuse("record")

    def reconcile(self, attempt: StepAttempt, **_: Any) -> Transition[StepAttempt]:
        return self.refuse("reconcile")



class Invoked(StepAttemptState):
    name = "INVOKED"

    @accepts("INVOKED")
    def worker_lost(self, attempt: StepAttempt, **_: Any) -> Transition[StepAttempt]:
        # Nothing acted before the reservation, so the same key is invoked again.
        return move(attempt, StepAttemptState.INVOKED, Record("step_reinvoked", {"key": attempt.key}))

    @accepts("RESERVED")
    def reserve(self, attempt: StepAttempt, **_: Any) -> Transition[StepAttempt]:
        return move(attempt, StepAttemptState.RESERVED, Record("claim_reserved", {"key": attempt.key}))


class Reserved(StepAttemptState):
    name = "RESERVED"

    @accepts("COMMITTED")
    def commit(self, attempt: StepAttempt, **_: Any) -> Transition[StepAttempt]:
        return move(attempt, StepAttemptState.COMMITTED, Record("claim_committed", {"key": attempt.key}))

    @accepts("OUTCOME_UNKNOWN")
    def worker_lost(self, attempt: StepAttempt, **_: Any) -> Transition[StepAttempt]:
        # The side effect may have happened: nothing may act again until someone reconciles.
        return move(attempt, StepAttemptState.OUTCOME_UNKNOWN, Record("outcome_unknown", {"key": attempt.key}))


class Committed(StepAttemptState):
    name = "COMMITTED"

    @accepts("RECORDED")
    def record(self, attempt: StepAttempt, **_: Any) -> Transition[StepAttempt]:
        return move(attempt, StepAttemptState.RECORDED)


class OutcomeUnknown(StepAttemptState):
    name = "OUTCOME_UNKNOWN"

    @accepts("RECORDED")
    def reconcile(self, attempt: StepAttempt, *, actor: str | None = None, **_: Any) -> Transition[StepAttempt]:
        if not actor:
            self.refuse("reconcile", "a reconciliation names who made it")
        record = Record("outcome_reconciled", {"key": attempt.key, "actor": actor})
        return move(attempt, StepAttemptState.RECORDED, record)


class Recorded(StepAttemptState):
    name = "RECORDED"


STEP_ATTEMPT = Machine(
    "step_attempt",
    StepAttemptState,
    (Invoked, Reserved, Committed, OutcomeUnknown, Recorded),
    initial="INVOKED",
)


@dataclass(frozen=True, slots=True)
class StepAttempt(Context):
    """One attempt of one step, under its idempotency key."""

    key: str
    state: StepAttemptState = field(default_factory=lambda: STEP_ATTEMPT.initial)  # type: ignore[assignment]

    def worker_lost(self) -> Transition[StepAttempt]:
        return self.dispatch("worker_lost")

    def reserve(self) -> Transition[StepAttempt]:
        return self.dispatch("reserve")

    def commit(self) -> Transition[StepAttempt]:
        return self.dispatch("commit")

    def record(self) -> Transition[StepAttempt]:
        return self.dispatch("record")

    def reconcile(self, *, actor: str) -> Transition[StepAttempt]:
        return self.dispatch("reconcile", actor=actor)
