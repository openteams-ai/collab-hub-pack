"""Durability backends: how the runner's step functions are scheduled, and whether progress is checkpointed.

ADR-0002 D1. The lifecycle runner has one driver, and every backend shares it:
the driver hands each step function to the backend through
:meth:`DurabilityBackend.run_step`, and the backend decides only how that call is
scheduled and whether what it returned is checkpointed. A backend holds no
lifecycle logic: it never decides what runs next.

A backend is chosen by configuration, ``none``, ``dbos`` or ``temporal``, through
:func:`select_backend`, and callers never import one. ``none`` is implemented;
``dbos`` and ``temporal`` are refused when a runner starts until Phases 27 and 33
of the plan build them, so the configuration shape is fixed now.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, TypeVar

T = TypeVar("T")

DURABILITY_BACKENDS = ("none", "dbos", "temporal")
"""Every value the ``backend`` setting takes, in the order they are built."""

_NOT_YET = {"dbos": "Phase 27 (#104)", "temporal": "Phase 33 (#110)"}


class BackendNotImplemented(NotImplementedError):
    """A backend the configuration names that is not built yet."""


class DurabilityBackend(Protocol):
    """Where the runner's step functions run, and what survives a restart."""

    name: str
    durable: bool
    """Whether a run in flight survives its host stopping. ``none`` is not durable:
    a run it was advancing when its host stopped is recorded ``interrupted`` when a
    host starts again, and never resumed (ADR-0002 D2)."""

    def run_step(self, run_id: str, name: str, function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Run one step function of a run, named as ``STEP_FUNCTIONS`` names it, and return what it returned."""


def select_backend(name: str) -> DurabilityBackend:
    """The backend the configuration names; the only way a caller reaches one."""
    if name == "none":
        from .none import NoneBackend

        return NoneBackend()
    if name in _NOT_YET:
        raise BackendNotImplemented(f"the {name!r} durability backend is not implemented yet: it arrives with "
                                    f"{_NOT_YET[name]}; use 'none'")
    raise ValueError(f"unknown durability backend {name!r}; the backend is one of {', '.join(DURABILITY_BACKENDS)}")
