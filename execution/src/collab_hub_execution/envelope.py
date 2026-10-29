"""The result envelope: what a Cog's usage entry point returns (version 1).

The shape is ``docs/cog-execution/result-envelope.md``. This module is the
hub's reading of it — the one place the seam's return value is parsed — so the
engine, the HTTP worker client and the in-memory executor all agree on what a
worker answered. Consumers detect the version through ``envelope`` and ignore
fields they do not know, as the document's versioning rule requires.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

ENVELOPE_VERSION = 1

# The error codes the envelope document defines. ``code`` is kept verbatim on
# the Track's ``failed`` event, so a client can act on it without parsing text.
ERROR_CODES = frozenset({
    "binding-invalid", "invalid-input", "model-unavailable", "model-call-failed", "model-response-malformed",
    "context-exhausted",
})

# Over HTTP, the status a worker uses for each code (result-envelope.md, "error").
STATUS_FOR_CODE: Mapping[str, int] = {
    "invalid-input": 422,
    "context-exhausted": 422,
    "model-call-failed": 502,
    "model-response-malformed": 502,
    "model-unavailable": 503,
    "binding-invalid": 503,
}

# ...and the code the client assumes when one of those statuses arrives without
# a parseable envelope body (a proxy's 502 page, a crashed worker's 503).
CODE_FOR_STATUS: Mapping[int, str] = {422: "invalid-input", 502: "model-call-failed", 503: "model-unavailable"}

SEVERITIES = ("error", "warn")


class EnvelopeInvalid(ValueError):
    """A worker's return is not a version-1 result envelope."""


@dataclass(frozen=True, slots=True)
class Problem:
    """One self-reported contract-check finding: input to Guards, never a verdict."""

    check: str
    detail: str
    severity: str = "error"

    def __post_init__(self) -> None:
        if not isinstance(self.check, str) or not self.check:
            raise EnvelopeInvalid("problem.check must be a non-empty string")
        if not isinstance(self.detail, str):
            raise EnvelopeInvalid("problem.detail must be a string")
        if self.severity not in SEVERITIES:
            raise EnvelopeInvalid(f"problem.severity must be one of {SEVERITIES}")


@dataclass(frozen=True, slots=True)
class EnvelopeError:
    """Why ``ok`` is false: one of ``ERROR_CODES`` and a human sentence.

    The code set is closed for version 1 — it is what a client acts on, so an
    invented code is an invalid envelope, not a new kind of failure.
    """

    code: str
    detail: str

    def __post_init__(self) -> None:
        if self.code not in ERROR_CODES:
            raise EnvelopeInvalid(f"error.code must be one of {sorted(ERROR_CODES)}, not {self.code!r}")
        if not isinstance(self.detail, str):
            raise EnvelopeInvalid("error.detail must be a string")


@dataclass(frozen=True, slots=True)
class ResultEnvelope:
    """An envelope the hub can act on. ``usage`` is kept as reported; the engine
    validates it against the run's budget, so a malformed report fails the run
    as unknown spending rather than being silently dropped here.

    The invariants are checked on construction, not only in ``parse``, so an
    envelope a worker builds in process obeys the same contract as one that
    arrived as JSON — and the engine can trust any ``ResultEnvelope`` it holds.
    """

    ok: bool
    payload: Any = None
    problems: tuple[Problem, ...] = ()
    error: EnvelopeError | None = None
    binding: Mapping[str, Any] | None = None
    usage: Any = None
    raw: str | None = None
    cog: Mapping[str, Any] | None = None
    task: str | None = None
    timing: Mapping[str, Any] | None = None
    envelope: int = field(default=ENVELOPE_VERSION)

    def __post_init__(self) -> None:
        if type(self.envelope) is not int or self.envelope != ENVELOPE_VERSION:
            raise EnvelopeInvalid(f"unsupported envelope version {self.envelope!r}; this hub reads {ENVELOPE_VERSION}")
        if type(self.ok) is not bool:
            raise EnvelopeInvalid("ok must be a boolean")
        if self.error is not None and not isinstance(self.error, EnvelopeError):
            raise EnvelopeInvalid("error must be null or an EnvelopeError")
        if not self.ok and self.error is None:
            raise EnvelopeInvalid("ok: false requires error {code, detail}")
        if self.ok and self.error is not None:
            raise EnvelopeInvalid("ok: true cannot carry an error")
        if isinstance(self.problems, list):
            object.__setattr__(self, "problems", tuple(self.problems))
        if not isinstance(self.problems, tuple) or not all(isinstance(p, Problem) for p in self.problems):
            raise EnvelopeInvalid("problems must be Problem entries")
        for name in ("binding", "cog", "timing"):
            _optional_mapping(getattr(self, name), name)
        for name in ("raw", "task"):
            _optional_str(getattr(self, name), name)

    @classmethod
    def success(
        cls,
        payload: Any = None,
        *,
        usage: Any = None,
        problems: tuple[Problem, ...] | list[Problem] = (),
        binding: Mapping[str, Any] | None = None,
        raw: str | None = None,
        cog: Mapping[str, Any] | None = None,
        task: str | None = None,
        timing: Mapping[str, Any] | None = None,
    ) -> ResultEnvelope:
        return cls(
            ok=True, payload=payload, problems=tuple(problems), binding=binding, usage=usage,
            raw=raw, cog=cog, task=task, timing=timing,
        )

    @classmethod
    def failure(
        cls,
        code: str,
        detail: str = "",
        *,
        usage: Any = None,
        problems: tuple[Problem, ...] | list[Problem] = (),
        binding: Mapping[str, Any] | None = None,
        raw: str | None = None,
        cog: Mapping[str, Any] | None = None,
        task: str | None = None,
        timing: Mapping[str, Any] | None = None,
    ) -> ResultEnvelope:
        return cls(
            ok=False, payload=None, problems=tuple(problems), error=EnvelopeError(code, detail), binding=binding,
            usage=usage, raw=raw, cog=cog, task=task, timing=timing,
        )

    @classmethod
    def parse(cls, data: Any) -> ResultEnvelope:
        """Read a version-1 envelope from decoded JSON, ignoring unknown fields.

        Raises ``EnvelopeInvalid`` for anything that is not an envelope: a
        missing or unsupported ``envelope`` version, a non-boolean ``ok``, an
        ``ok: false`` with no ``error`` (an unexplained failure) or an
        ``ok: true`` with one (a contradiction), an ``error`` without both
        ``code`` and ``detail`` or with a code outside ``ERROR_CODES``, and
        malformed ``problems``. ``usage`` is passed through for the engine's
        budget rules.
        """
        if not isinstance(data, Mapping):
            raise EnvelopeInvalid("a result envelope is a JSON object")
        if data.get("envelope") is None:
            raise EnvelopeInvalid("missing envelope version")
        return cls(
            ok=data.get("ok"),
            payload=data.get("payload"),
            problems=_parse_problems(data.get("problems")),
            error=_parse_error(data.get("error")),
            binding=data.get("binding"),
            usage=data.get("usage"),
            raw=data.get("raw"),
            cog=data.get("cog"),
            task=data.get("task"),
            timing=data.get("timing"),
            envelope=data.get("envelope"),
        )

    def to_dict(self) -> dict[str, Any]:
        """The JSON shape of ``result-envelope.md``, as a worker would send it."""
        return {
            "envelope": self.envelope,
            "cog": dict(self.cog) if self.cog is not None else None,
            "task": self.task,
            "ok": self.ok,
            "error": {"code": self.error.code, "detail": self.error.detail} if self.error is not None else None,
            "payload": self.payload,
            "raw": self.raw,
            "problems": [{"check": p.check, "detail": p.detail, "severity": p.severity} for p in self.problems],
            "binding": dict(self.binding) if self.binding is not None else None,
            "usage": self.usage,
            "timing": dict(self.timing) if self.timing is not None else None,
        }


def _parse_error(value: Any) -> EnvelopeError | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise EnvelopeInvalid("error must be null or {code, detail}")
    if "code" not in value or "detail" not in value:
        raise EnvelopeInvalid("error needs both code and detail")
    code = value["code"]
    if not isinstance(code, str):
        raise EnvelopeInvalid("error.code must be a string")
    return EnvelopeError(code, value["detail"])  # validates the code and the detail


def _parse_problems(value: Any) -> tuple[Problem, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise EnvelopeInvalid("problems must be a list")
    problems = []
    for item in value:
        if not isinstance(item, Mapping):
            raise EnvelopeInvalid("each problem is {check, detail, severity}")
        # Problem validates its own fields.
        problems.append(Problem(item.get("check"), item.get("detail", ""), item.get("severity", "error")))
    return tuple(problems)


def _optional_mapping(value: Any, name: str) -> Mapping[str, Any] | None:
    if value is not None and not isinstance(value, Mapping):
        raise EnvelopeInvalid(f"{name} must be null or an object")
    return value


def _optional_str(value: Any, name: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise EnvelopeInvalid(f"{name} must be null or a string")
    return value
