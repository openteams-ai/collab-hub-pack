"""Agent locations: where a Cog worker runs, relative to the controller.

ADR-0002 D12. The lifecycle runner drives every worker through the same seam
(``POST /invoke``, ``GET /healthz``) and the same executor interface
(``materialize``, ``teardown``); the location decides only where the worker is
brought up. An executor holds no lifecycle logic: it never decides what runs
next, and it writes nothing to the Track.

A location is chosen by configuration, ``local`` or ``remote``, through
:func:`select_executor`, and callers never import an executor. ``local`` is
implemented: the worker is a process on the controller's host. ``remote``, the
worker as a workload on a cluster, is refused when a runner starts until Phase
21 of the plan puts it behind this switch, so the configuration shape is fixed
now.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

AGENT_LOCATIONS = ("local", "remote")
"""Every value the ``location`` setting takes, in the order they are built."""

_NOT_YET = {"remote": "Phase 22 (#6)"}


class LocationNotImplemented(NotImplementedError):
    """A location the configuration names that is not built yet."""


def select_executor(
    location: str,
    *,
    packages: Iterable[str | Path] = (),
    allow: Iterable[str] | None = None,
    work_dir: str | Path | None = None,
    **settings: Any,
) -> Any:
    """The executor of the location the configuration names; the only way a caller reaches one.

    ``local`` takes ``packages``, the directories Cog packages are found under,
    ``allow``, the names that may be run from them (every package when omitted),
    and ``work_dir``, where each run's worker output goes. Further settings are
    the executor's own (``environment``, ``pixi``, ``deliver``,
    ``ready_timeout``).
    """
    if location == "local":
        from .local import LocalProcessCogExecutor
        from .packages import DirectoryPackageSource

        if work_dir is None:
            raise ValueError("the 'local' location needs a work_dir: where each run's worker output goes")
        return LocalProcessCogExecutor(source=DirectoryPackageSource(packages, allow), work_dir=work_dir, **settings)
    if location in _NOT_YET:
        raise LocationNotImplemented(f"the {location!r} agent location is not implemented yet: it arrives with "
                                     f"{_NOT_YET[location]}; use 'local'")
    raise ValueError(f"unknown agent location {location!r}; the location is one of {', '.join(AGENT_LOCATIONS)}")


def location_of(executor: Any) -> str | None:
    """The location an executor is of, when it is one ``select_executor`` returned."""
    return getattr(executor, "location", None)


__all__ = ["AGENT_LOCATIONS", "LocationNotImplemented", "location_of", "select_executor"]
