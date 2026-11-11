"""What the suites share for the keyed claim: an in-memory executor whose workers honour it as the SDK does.

A worker reserves its task's key on the claim store before it acts and
commits its envelope after; a replay of a committed key is answered with the
envelope, and a replay of a key reserved by a lost worker fails the step
``outcome-unknown``. ``stop_after`` makes a worker's host stop at a point of
one key's attempt, once: after ``reserved`` (nothing acted), after ``acted``
(the side effect happened, nothing was committed), or after ``committed`` (the
result was committed, and not yet recorded), raising ``stops_with``, which the
runner does not handle, as a dying process would not.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from itertools import count
from typing import Any

from collab_hub_execution import InMemoryClaimStore, InMemoryCogExecutor, ResultEnvelope
from collab_hub_execution.ops import _NO_SIGNAL


class HostStops(BaseException):
    """The host's process stops here: not an error the runner handles, as a failing worker is."""


class ClaimingExecutor(InMemoryCogExecutor):
    """An in-memory executor whose workers reserve, act and commit, like a worker on the SDK."""

    def __init__(self, handlers: Mapping[str, Callable[..., Any]], store: InMemoryClaimStore | None = None, *,
                 stop_after: Mapping[str, str] | None = None, stops_with: type[BaseException] = HostStops) -> None:
        super().__init__(dict(handlers))
        self.store = store or InMemoryClaimStore()
        self.acted: list[str] = []
        """The keys whose side effect happened, in order: what must never hold one key twice."""
        self.stop_after = dict(stop_after or {})
        self.stops_with = stops_with
        self._workers = count(1)

    def materialize(self, cog: str, run_id: str, instance: str = "") -> ClaimingWorker:
        self.materialized.append((run_id, cog))
        # Each materialization is a new worker with a token of its own, as a process or a pod is.
        return ClaimingWorker(self, cog, self.handlers[cog], run_id, f"worker-{next(self._workers)}")

    def _stop(self, key: str, after: str) -> None:
        if self.stop_after.get(key) == after:
            del self.stop_after[key]
            raise self.stops_with(f"the host stopped after {after} {key}")


class ClaimingWorker:
    def __init__(self, executor: ClaimingExecutor, cog: str, handler: Callable[..., Any], run_id: str,
                 identity: str) -> None:
        self.executor = executor
        self.cog = cog
        self.handler = handler
        self.run_id = run_id
        self.identity = identity

    def interact(self, entry_point: str, input: Any = None, idempotency_key: str | None = None, *,
                 signal: Any = _NO_SIGNAL, frames: Any = None) -> ResultEnvelope:
        store = self.executor.store
        claim, acts = store.reserve(self.run_id, idempotency_key, worker=self.identity)
        if not acts:
            if claim.state == "committed":
                return ResultEnvelope.parse(claim.envelope)
            return ResultEnvelope.failure("outcome-unknown", f"the key {idempotency_key} is {claim.state}: reserved "
                                                             f"by a worker that never committed its result")
        self.executor._stop(idempotency_key, "reserved")
        feedback = {} if signal is _NO_SIGNAL else {"signal": signal}
        if frames:
            feedback["frames"] = list(frames)
        result = self.handler(entry_point, input, **feedback)
        self.executor.acted.append(idempotency_key)
        self.executor._stop(idempotency_key, "acted")
        envelope = result if isinstance(result, ResultEnvelope) else (
            ResultEnvelope.parse(result) if isinstance(result, Mapping) and "envelope" in result
            else ResultEnvelope.success(result))
        store.commit(self.run_id, idempotency_key, worker=self.identity, envelope=envelope.to_dict())
        self.executor._stop(idempotency_key, "committed")
        return envelope
