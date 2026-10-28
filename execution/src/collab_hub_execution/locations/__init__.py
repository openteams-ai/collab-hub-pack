"""Agent locations: where a Cog worker runs, relative to the controller.

ADR-0002 D12. The lifecycle runner drives every worker through the same seam
(``POST /invoke``, ``GET /healthz``) and the same executor interface
(``materialize``, ``teardown``); the location decides only where the worker is
brought up. An executor holds no lifecycle logic: it never decides what runs
next, and it writes nothing to the Track.

A location is chosen by configuration, ``local`` or ``remote``, through
:func:`select_executor`, and callers never import an executor. ``local`` is
the worker as a process on the controller's host; ``remote`` is the worker as
a Deployment and Service on the cluster the controller runs in, reached over
service DNS, which only the run controller's identity may create.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

AGENT_LOCATIONS = ("local", "remote")
"""Every value the ``location`` setting takes, in the order they were built."""

_NOT_YET: dict[str, str] = {}


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

    ``remote`` takes ``image``, the image a worker pod runs (the hub's own,
    which ships the Cog packages under ``packages_root``), ``namespace``, and
    ``allow_ingress_from``, the labels of the pods that may reach a worker's
    ``/invoke`` (the run controller's), or ``insecure_skip_network_policy``;
    ``packages`` and ``work_dir`` mean nothing to it, since a pod runs from
    the image and writes on volumes of its own. Its pods' ``resources``,
    ``security_context`` and the ``cache_size_limit`` and ``tmp_size_limit`` of
    their volumes have defaults a Pod Security ``restricted`` namespace admits.
    """
    if location == "local":
        from .local import LocalProcessCogExecutor
        from .packages import DirectoryPackageSource

        if work_dir is None:
            raise ValueError("the 'local' location needs a work_dir: where each run's worker output goes")
        settings.pop("image", None)
        return LocalProcessCogExecutor(source=DirectoryPackageSource(packages, allow), work_dir=work_dir, **settings)
    if location == "remote":
        from ..kubernetes import KubernetesCogExecutor

        image = settings.pop("image", None)
        if not image:
            raise ValueError("the 'remote' location needs an image: the one a worker pod runs, which ships the "
                             "Cog packages")
        for local_only in ("environment", "pixi", "grace"):
            settings.pop(local_only, None)
        return KubernetesCogExecutor(runner_image=image, **settings)
    if location in _NOT_YET:
        raise LocationNotImplemented(f"the {location!r} agent location is not implemented yet: it arrives with "
                                     f"{_NOT_YET[location]}; use 'local'")
    raise ValueError(f"unknown agent location {location!r}; the location is one of {', '.join(AGENT_LOCATIONS)}")


def location_of(executor: Any) -> str | None:
    """The location an executor is of, when it is one ``select_executor`` returned."""
    return getattr(executor, "location", None)


__all__ = ["AGENT_LOCATIONS", "LocationNotImplemented", "location_of", "select_executor"]
