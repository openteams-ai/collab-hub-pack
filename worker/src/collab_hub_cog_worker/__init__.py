"""The Collab Hub Cog worker SDK: the worker side of the seam, once, and a harness adapter behind it.

A Cog built on it declares its entry points on a :class:`Worker`, names the
adapter that drives its harness, and calls ``serve()``. The hub speaks only the
seam and never imports this package.
"""

from .adapter import HarnessAdapter
from .adapters import AcpHarnessAdapter, OpenAICompatAdapter
from .binding import Binding
from .binding import load as load_binding
from .envelope import ERROR_CODES, Cancelled, Problem, WorkerError, envelope
from .seam import Harness, Worker
from .task import Context, Frame, Interaction, Prompt, Signal, Task, Usage

__all__ = [
    "ERROR_CODES", "AcpHarnessAdapter", "Binding", "Cancelled", "Context", "Frame", "Harness", "HarnessAdapter",
    "Interaction", "OpenAICompatAdapter", "Problem", "Prompt", "Signal", "Task", "Usage", "Worker", "WorkerError",
    "envelope", "load_binding",
]
