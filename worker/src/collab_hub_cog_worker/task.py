"""What crosses the seam into a worker, and what an adapter answers with."""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .binding import Binding
from .envelope import WorkerError


@dataclass(frozen=True, slots=True)
class Frame:
    """One Frame snapshot a step carries: organizational context the harness is given, never fetched."""

    id: str
    text: str
    title: str | None = None
    uri: str | None = None
    digest: str | None = None

    @classmethod
    def parse(cls, value: Any) -> Frame:
        if not isinstance(value, Mapping) or not isinstance(value.get("id"), str) \
                or not isinstance(value.get("text"), str):
            raise WorkerError("invalid-input", "each frame is {id, text, title?, uri?, digest?}")
        return cls(id=value["id"], text=value["text"], title=value.get("title"), uri=value.get("uri"),
                   digest=value.get("digest"))

    @property
    def name(self) -> str:
        return self.title or self.id

    @property
    def location(self) -> str:
        return self.uri or f"frame:{self.id}"


@dataclass(frozen=True, slots=True)
class Task:
    """One ``/invoke``: the entry point asked for, its input, and the Frames the step carries."""

    entry_point: str
    input: Any = None
    idempotency_key: str | None = None
    frames: tuple[Frame, ...] = ()
    signal: Any = None
    """What the hub sent with the task (a Gate's send-back findings), passed through to the entry point."""


@dataclass(frozen=True, slots=True)
class Prompt:
    """One interaction with the harness: text, and the Frames to place beside it."""

    text: str
    frames: tuple[Frame, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise WorkerError("invalid-input", "a prompt is text")


@dataclass(frozen=True, slots=True)
class Interaction:
    """What a harness answered: its text, the verbatim exchange for ``raw``, its usage, and how it stopped."""

    text: str
    raw: str | None = None
    usage: Mapping[str, Any] | None = None
    stop: str | None = None


@dataclass(frozen=True, slots=True)
class Context:
    """What a harness is started with: the binding, which Cog and run it serves, and where it may write."""

    binding: Binding
    cog: str = ""
    run_id: str = ""
    work_dir: str | None = None


class Signal:
    """Cancellation, as an adapter sees it: set when the run is terminated or the session ends."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def set(self) -> None:
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)


@dataclass
class Usage:
    """Tokens and cost across a worker's interactions, summed for the envelope."""

    tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float | None = None
    interactions: int = 0
    seen: list[Mapping[str, Any]] = field(default_factory=list)

    def add(self, usage: Mapping[str, Any] | None) -> None:
        self.interactions += 1
        if not usage:
            return
        self.seen.append(dict(usage))
        for name in ("tokens", "prompt_tokens", "completion_tokens"):
            value = usage.get(name)
            if isinstance(value, int) and not isinstance(value, bool):
                setattr(self, name, getattr(self, name) + value)
        cost = usage.get("cost")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            self.cost = (self.cost or 0.0) + float(cost)

    def report(self) -> dict[str, Any] | None:
        """The envelope's ``usage``: absent when no interaction reported any."""
        if not self.seen:
            return None
        report: dict[str, Any] = {"tokens": self.tokens, "prompt_tokens": self.prompt_tokens,
                                  "completion_tokens": self.completion_tokens, "interactions": self.interactions}
        if self.cost is not None:
            report["cost"] = round(self.cost, 6)
        return report


def frames_from(value: Any) -> tuple[Frame, ...]:
    if value is None:
        return ()
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes, Mapping)):
        raise WorkerError("invalid-input", "frames are a list of {id, text, ...}")
    return tuple(Frame.parse(item) for item in value)
