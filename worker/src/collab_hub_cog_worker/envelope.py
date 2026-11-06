"""The result envelope, as a worker writes it (version 1; ``docs/cog-execution/result-envelope.md``).

The hub reads the envelope in ``collab_hub_execution.envelope``; this is the
writing side, kept to the same document, so a worker built on the SDK answers
``/invoke`` with what the hub parses. ``WorkerError`` is how an adapter or an
entry point fails a step with one of the document's codes.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

ENVELOPE_VERSION = 1

ERROR_CODES = frozenset({
    "binding-invalid", "invalid-input", "model-unavailable", "model-call-failed", "model-response-malformed",
    "context-exhausted",
})

# Over HTTP, the status a worker answers each code with.
STATUS_FOR_CODE: Mapping[str, int] = {
    "invalid-input": 422,
    "context-exhausted": 422,
    "model-call-failed": 502,
    "model-response-malformed": 502,
    "model-unavailable": 503,
    "binding-invalid": 503,
}

SEVERITIES = ("error", "warn")


class WorkerError(Exception):
    """A step failed with one of the envelope's error codes: what ``/invoke`` answers ``ok: false`` with."""

    def __init__(self, code: str, detail: str = "") -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"error.code is one of {sorted(ERROR_CODES)}, not {code!r}")
        super().__init__(detail)
        self.code = code
        self.detail = detail


class Cancelled(Exception):  # noqa: N818 - an outcome, not an error
    """The interaction was cancelled: the run was terminated, or the session ended, while it ran."""


@dataclass(frozen=True, slots=True)
class Problem:
    """One self-reported contract-check finding: input to Guards, never a verdict."""

    check: str
    detail: str
    severity: str = "error"

    def __post_init__(self) -> None:
        if not self.check or self.severity not in SEVERITIES:
            raise ValueError("a problem has a check and a severity of error or warn")

    def to_dict(self) -> dict[str, str]:
        return {"check": self.check, "detail": self.detail, "severity": self.severity}


def envelope(cog: str, version: str, task: str | None, *, payload: Any = None, error: WorkerError | None = None,
             raw: str | None = None, problems: Iterable[Problem] = (), binding: Mapping[str, Any] | None = None,
             usage: Mapping[str, Any] | None = None, timing: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """A version-1 envelope as JSON: ``ok`` is whether there is an error."""
    return {
        "envelope": ENVELOPE_VERSION,
        "cog": {"id": cog, "version": version},
        "task": task,
        "ok": error is None,
        "error": None if error is None else {"code": error.code, "detail": error.detail[:2000]},
        "payload": payload if error is None else None,
        "raw": raw,
        "problems": [problem.to_dict() for problem in problems],
        "binding": dict(binding) if binding is not None else None,
        "usage": dict(usage) if usage is not None else None,
        "timing": dict(timing) if timing is not None else None,
    }


def status_for(error: WorkerError | None) -> int:
    """The HTTP status an envelope is answered with: 200 for an answer, the code's own status for a failure."""
    return 200 if error is None else STATUS_FOR_CODE[error.code]


class Stopwatch:
    """Wall-clock latency of one interaction, for the envelope's ``timing``."""

    def __init__(self) -> None:
        self.started = time.monotonic()

    def timing(self) -> dict[str, float]:
        return {"latency_s": round(time.monotonic() - self.started, 3)}
