"""The model's limits, applied before a harness is asked: an input that cannot fit is ``context-exhausted``.

A harness owns its context within a turn (how it compacts, what it keeps); the
SDK only refuses what could never fit the bound model, so the step fails with a
code a Gate or a person can act on, rather than with whatever the model says.
The estimate is coarse on purpose: four characters a token, the usual rule of
thumb, applied to the prompt and its Frames.
"""

from __future__ import annotations

from .binding import Binding
from .envelope import WorkerError
from .task import Prompt

CHARACTERS_PER_TOKEN = 4


def estimate(text: str) -> int:
    """A rough token count of ``text``."""
    return (len(text) + CHARACTERS_PER_TOKEN - 1) // CHARACTERS_PER_TOKEN


def check_fits(prompt: Prompt, binding: Binding) -> int:
    """The prompt's estimated tokens; ``WorkerError("context-exhausted")`` when they cannot fit the binding."""
    tokens = estimate(prompt.text) + sum(estimate(frame.text) for frame in prompt.frames)
    if binding.context_window is not None:
        headroom = binding.max_output_tokens or 0
        if tokens + headroom > binding.context_window:
            raise WorkerError(
                "context-exhausted",
                f"the input is about {tokens} tokens with {len(prompt.frames)} frame(s), and the bound model "
                f"{binding.model or ''} has a context window of {binding.context_window}"
                + (f" with {headroom} reserved for its answer" if headroom else ""))
    return tokens


def looks_like_overflow(message: str) -> bool:
    """Whether a model's or harness's error says the context did not fit."""
    lowered = message.lower()
    return "context_length" in lowered or "context length" in lowered or "context window" in lowered \
        or ("token" in lowered
            and any(word in lowered for word in ("exceed", "too long", "too many", "limit", "maximum")))
