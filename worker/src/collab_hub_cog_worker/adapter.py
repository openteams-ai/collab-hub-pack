"""The harness adapter: the one interface the SDK drives a harness through.

An adapter wraps one kind of harness — an ACP agent over stdio, a direct call
to an OpenAI-compatible model — and nothing of the seam: the server hands it a
``Context`` to start with, a ``Prompt`` per interaction, and the cancellation
``Signal``; it answers with an ``Interaction`` or raises ``WorkerError`` with
one of the envelope's codes. Writing a Cog for a new harness is writing one of
these.
"""

from __future__ import annotations

from typing import Protocol

from .task import Context, Interaction, Prompt, Signal


class HarnessAdapter(Protocol):
    def start(self, context: Context) -> None:
        """Bring the harness up for this worker: started once, before the first interaction."""

    def interact(self, prompt: Prompt, context: Context, signal: Signal) -> Interaction:
        """One prompt in, one answer out. Raises ``WorkerError`` for a failure, ``Cancelled`` when ``signal`` is set."""

    def cancel(self) -> None:
        """Interrupt the interaction in flight, if the harness can be interrupted."""

    def close(self) -> None:
        """Stop the harness and leave nothing behind. Closing twice is the same as once."""

    def alive(self) -> bool:
        """Whether the harness can still answer: a session whose harness is gone ends."""
