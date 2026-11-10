"""An Op and the seam a step crosses: what a run is made of, and what a Cog worker answers.

The types every part of execution shares — the lifecycle runner, the engine in
front of it, the executors behind it — so none of them imports another to reach
them.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from .envelope import ResultEnvelope
from .gates import Gate
from .states import RunState

# Distinguishes "no external signal" (a fresh submit/retry) from a signal whose
# value is genuinely None (a human resuming a Gate with an empty decision). None
# alone is overloaded, so a paused step could not be resumed with a real None.
_NO_SIGNAL = object()


@dataclass(frozen=True, slots=True)
class FrameRef:
    """A Frame a step is grounded in, as snapshotted when the run was submitted.

    The snapshot's text is kept by reference on the Track (``ref``), so a step
    retried later is given the Frame as it was at submit, not as edited since;
    ``digest`` is the sha256 of that text, recorded so the Track names what the
    step saw.
    """

    id: str
    name: str
    digest: str
    ref: str
    uri: str | None = None


@dataclass(frozen=True, slots=True)
class OpStep:
    """One interaction with a Cog entry point, and the Gate that decides on its result."""

    name: str
    cog: str
    entry_point: str
    input: Any = None
    digest: str | None = None
    gate: Gate = field(default_factory=Gate)
    frames: tuple[FrameRef, ...] = ()
    """The Frames the step is grounded in, placed beside its input in the task bundle."""


@dataclass(frozen=True, slots=True)
class OpDefinition:
    """A serializable, multi-step Op definition."""

    run_id: str
    steps: tuple[OpStep, ...]


class CogWorker(Protocol):
    def interact(
        self, entry_point: str, input: Any = None, idempotency_key: str | None = None,
        *, signal: Any = _NO_SIGNAL, frames: Sequence[Mapping[str, Any]] | None = None,
    ) -> ResultEnvelope:
        """Interact through a declared entry point.

        ``frames`` are the step's Frame snapshots, each ``{id, title, uri,
        digest, text}``, sent in the task bundle beside the input when the step
        has any; a worker places them in its prompt and never fetches a Frame.

        Return the result envelope (``envelope.py``): ``payload`` is the Cog's
        output and ``usage`` its accounting, never hidden inside the payload.
        A Cog cannot pause a run: its problems go to the step's Gate, which
        decides whether a person looks at the result.

        ``idempotency_key`` is stable per (run, step, attempt): a crash-recovery
        re-drives the same incomplete step with the *same* key, and an explicit
        retry or a send back at a Gate uses a *new* key. ``input`` always contains
        the step's original input; ``signal`` carries a send back's findings
        separately and is omitted until a step is sent back.
        A worker that persists results by key can turn the
        replay into a no-op — but that durability is the worker's to provide. The
        reference and Kubernetes workers here do NOT persist keys across pod
        replacement, so a replaced worker re-runs the side effect: execution is
        at-least-once across pod replacement. Crash-safe, exactly-once execution
        (a durable keyed claim) lands with the crash-safe engine backing tracked
        in #1.
        """


class CogExecutor(Protocol):
    def materialize(self, cog: str, run_id: str, instance: str = "") -> CogWorker:
        """Materialize one Cog worker for a run.

        ``instance`` uniquely and recovery-stably identifies this materialization
        within the run (the engine passes step name + attempt). An executor that
        names cluster resources should fold it in so distinct steps and successive
        attempts never reuse a name a prior teardown may still be terminating.
        """

    def teardown(self, worker: CogWorker) -> None:
        """Release a materialized worker."""


class WorkflowEngine(Protocol):
    """Experimental boundary used by callers, independent of how runs are scheduled.

    ``LifecycleRunner`` implements it; the durability backend behind it is chosen
    by configuration.
    """

    def submit(self, op: OpDefinition) -> RunState:
        """Start an Op; submitting one already started returns its state and never resumes it."""

    def open_escalation(self, run_id: str) -> Mapping[str, Any] | None:
        """What a run waiting at a Gate waits on, or ``None``: what ``decide`` answers."""

    def decide(
        self, run_id: str, *, escalation: str | None, actor: str, outcome: str,
        findings: Sequence[Any] | None = (),
    ) -> RunState:
        """Answer the escalation a run waits on: approve, reject or send back."""

    def retry(self, run_id: str) -> RunState:
        """Run an ended run again: an interrupted attempt continues under its key, a failed one starts anew."""

    def cancel(self, run_id: str, *, actor: str) -> RunState:
        """End a run ``cancelled``, recording who cancelled it."""

    def observe(self, run_id: str) -> RunState | None:
        """Return the run's state reconstructed from the Track; ``None`` if never submitted."""


class InMemoryCogExecutor(CogExecutor):
    """A fake executor for exercising orchestration without infrastructure.

    A handler returns a ``ResultEnvelope`` (or an envelope-shaped mapping, one
    with an ``envelope`` key) to report usage or problems. Any other value
    becomes the payload of a successful envelope with unknown usage, which
    cannot satisfy a configured spending limit.

    That last convenience is this fixture's alone: a worker answering over HTTP
    must send an envelope, and anything else — a ``{"pause": true}`` included —
    fails the step as ``EnvelopeInvalid``. Neither path lets a Cog pause a run.
    """

    def __init__(self, handlers: dict[str, Callable[..., Any]]) -> None:
        self.handlers = handlers
        self.materialized: list[tuple[str, str]] = []
        self.torn_down: list[str] = []

    def materialize(self, cog: str, run_id: str, instance: str = "") -> CogWorker:
        self.materialized.append((run_id, cog))
        return _Worker(cog, self.handlers[cog])

    def teardown(self, worker: CogWorker) -> None:
        self.torn_down.append(worker.cog)  # type: ignore[attr-defined]


class _Worker:
    def __init__(self, cog: str, handler: Callable[..., Any]) -> None:
        self.cog = cog
        self.handler = handler

    def interact(
        self, entry_point: str, input: Any = None, idempotency_key: str | None = None,
        *, signal: Any = _NO_SIGNAL, frames: Sequence[Mapping[str, Any]] | None = None,
    ) -> ResultEnvelope:
        feedback = {} if signal is _NO_SIGNAL else {"signal": signal}
        if frames:
            feedback["frames"] = list(frames)
        result = self.handler(entry_point, input, **feedback)
        if isinstance(result, ResultEnvelope):
            return result
        if isinstance(result, Mapping) and "envelope" in result:
            return ResultEnvelope.parse(result)
        return ResultEnvelope.success(result)


def _canonical_op(op: OpDefinition) -> str:
    """Compare definitions using the same JSON representation as durable storage."""
    return json.dumps(json.loads(json.dumps(_serialize_op(op))), sort_keys=True)


def _serialize_op(op: OpDefinition) -> dict[str, Any]:
    return {
        "run_id": op.run_id,
        "steps": [
            {
                "name": step.name,
                "cog": step.cog,
                "entry_point": step.entry_point,
                "input": step.input,
                "digest": step.digest,
                "gate": step.gate.to_dict(),
                **({"frames": [_frame_dict(frame) for frame in step.frames]} if step.frames else {}),
            }
            for step in op.steps
        ],
    }


def _frame_dict(frame: FrameRef) -> dict[str, Any]:
    return {"id": frame.id, "name": frame.name, "digest": frame.digest, "ref": frame.ref, "uri": frame.uri}


def _deserialize_op(value: dict[str, Any]) -> OpDefinition:
    return OpDefinition(
        run_id=value["run_id"],
        steps=tuple(
            OpStep(
                name=step["name"],
                cog=step["cog"],
                entry_point=step["entry_point"],
                input=step.get("input"),
                digest=step.get("digest"),
                gate=Gate.from_dict(step.get("gate")),
                frames=tuple(FrameRef(id=f["id"], name=f.get("name") or f["id"], digest=f.get("digest", ""),
                                      ref=f["ref"], uri=f.get("uri"))
                             for f in step.get("frames") or ()),
            )
            for step in value["steps"]
        ),
    )
