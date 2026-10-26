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


OVERFLOW_PHRASES = ("context_length", "context length", "context window", "maximum context", "prompt is too long",
                    "input is too long", "input too long", "too many tokens", "exceeds the model's maximum")
NOT_OVERFLOW_PHRASES = ("rate limit", "rate_limit", "ratelimit", "per min", "per minute", "per day", "quota")


def looks_like_overflow(message: str) -> bool:
    """Whether a model's or harness's error says the context did not fit: a phrase about the context, and
    not a rate limit, which also talks of tokens and limits and asks for a retry rather than a smaller input."""
    lowered = message.lower()
    if any(phrase in lowered for phrase in NOT_OVERFLOW_PHRASES):
        return False
    return any(phrase in lowered for phrase in OVERFLOW_PHRASES)
