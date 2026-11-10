"""The installer: what the run controller does with an install asked for, and with a removal.

An install moves ``PUBLISHED`` to ``FETCHED`` to ``BOUND`` to ``INVOKABLE``,
in that order, because a Cog's ``check`` may probe what its binding gives
it (a context Cog's ``check`` that asks its model for health cannot pass
before resolution has chosen that model):

1. **fetch** reads the artifact at its pinned digest through the registry:
   the manifest verified against the digest, the lock named, the ``serve``
   task found. A development install names a package of the directory
   source instead, and fetch resolves it there.
2. **bind** offers the hub's inventory to the Cog's own ``resolve`` entry
   point, on a worker materialized without a binding, and records what it
   selected as the binding record; a Cog that requires nothing is bound to
   nothing. The inventory passes through the filter first (#10's plug
   point). A selection the inventory does not hold, a requirement left
   unsatisfied, a credential the controller cannot find, or a literal secret
   anywhere refuses the binding, and the install stays ``FETCHED``.
3. **check** materializes a worker with that binding delivered, through the
   same executor a run uses, and invokes the Cog's ``check`` entry point
   when its card declares one; readiness alone is the check otherwise. A
   failing check leaves the install ``BOUND`` and says which step failed:
   ``materialize`` when the worker never came up, ``check`` when it answered
   no.

A removal tears down what the install's workers left and records
``uninstalled``. Every move is a transition of the install machine
(``states.install``), recorded with its events in the install store; the
API reads the state there and never calls the executor.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from . import artifacts
from .binding import (
    BindingRecord,
    BindingResolutionError,
    Delivered,
    InventoryFilter,
    Satisfier,
    harness_satisfier,
    is_harness,
    no_filter,
    refuse_literal_secrets,
)
from .envelope import ResultEnvelope
from .installs import InstallRow, InstallStore
from .locations.packages import DirectoryPackageSource, PackageNotFound, PackageRefused
from .states import InstallState, InvalidTransition

_log = logging.getLogger(__name__)

RESOLVE, CHECK = "resolve", "check"
MESSAGE_MAX_CHARS = 1024


def _bound(text: Any) -> str:
    return str(text)[:MESSAGE_MAX_CHARS]


def _lifecycle_ops(card: Mapping[str, Any]) -> set[str]:
    ops = card.get("ops") if isinstance(card.get("ops"), Mapping) else {}
    lifecycle = ops.get("lifecycle") if isinstance(ops, Mapping) else None
    declared = {str(op) for op in lifecycle} if isinstance(lifecycle, list) else set()
    for entry in card.get("entry_points") or ():
        if isinstance(entry, Mapping) and entry.get("audience") == "lifecycle" and isinstance(entry.get("task"), str):
            declared.add(entry["task"])
    return declared


def _requires(card: Mapping[str, Any]) -> list[dict[str, Any]]:
    requires = card.get("requires")
    if not isinstance(requires, list):
        return []
    return [dict(item) for item in requires if isinstance(item, Mapping) and item.get("capability")]


class Installer:
    """Takes each install asked for and moves it through fetch, bind and check; takes each removal."""

    def __init__(
        self,
        store: InstallStore,
        executor: Any,
        *,
        controller: str,
        inventory: Callable[[str | None], Sequence[Satisfier]],
        registry: artifacts.RegistryClient | None = None,
        packages: DirectoryPackageSource | None = None,
        environ: Mapping[str, str] | None = None,
        inventory_filter: InventoryFilter = no_filter,
        cache: Path | str | None = None,
    ) -> None:
        self.store = store
        self.executor = executor
        self.controller = controller
        self.inventory = inventory
        """The satisfiers the hub offers an organization's installs: its models, and its installed harnesses."""
        self.registry = registry
        self.packages = packages
        self.environ = environ
        self.filter = inventory_filter
        self.cache = Path(cache) if cache is not None else None
        """The artifact cache this controller can reach, where an uninstall evicts a package; ``None`` when it
        cannot reach one (the workers' own volumes)."""
        self._deliveries: dict[str, Delivered] = {}
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}

    # --- what the controller's delivery asks ---------------------------------------------------

    def delivered(self, run_id: str) -> Delivered | None:
        """What a worker materialized for an install (``install:<id>``) is given: its binding under check, nothing
        while it resolves."""
        with self._lock:
            return self._deliveries.get(run_id)

    # --- the controller's pass ----------------------------------------------------------------

    def tick(self) -> None:
        """One pass: take each removal asked for, and claim and start each install nobody has started."""
        for row in self.store.pending():
            if self._busy(row.id):
                continue
            if row.uninstall_requested_by is not None:
                self._spawn(row.id, self.uninstall, row.id)
            elif self.store.claim(row.id, self.controller):
                self._spawn(row.id, self.install, row.id)

    def _busy(self, install_id: str) -> bool:
        thread = self._threads.get(install_id)
        return thread is not None and thread.is_alive()

    def _spawn(self, install_id: str, target, *args) -> None:
        thread = threading.Thread(target=target, args=args, name=f"install-{install_id}", daemon=True)
        self._threads[install_id] = thread
        thread.start()

    def idle(self) -> bool:
        return not any(thread.is_alive() for thread in self._threads.values())

    # --- installing --------------------------------------------------------------------------

    def install(self, install_id: str) -> InstallRow | None:
        """Fetch, bind and check one install; the row after, with what failed recorded, or ``None`` when it was gone."""
        row = self.store.get(install_id)
        if row is None:
            return None
        try:
            row = self._fetch(row)
            if row is None:
                return self.store.get(install_id)  # stayed published, with the failed fetch recorded
            row = self._bind(row)
            if row.machine().state is not InstallState.BOUND:
                return row
            return self._check(row)
        except Exception:  # noqa: BLE001 - one install's trouble never stops the controller
            _log.exception("installing %s (%s) failed", install_id, row.reference)
            return self.store.get(install_id)

    def _fetch(self, row: InstallRow) -> InstallRow | None:
        install = row.machine()
        if install.state is not InstallState.PUBLISHED:
            return row  # fetched already by this controller before it stopped: carry on from where it is
        try:
            lock_digest = self._fetch_artifact(row)
        except (artifacts.PullError, PackageRefused, PackageNotFound, BindingResolutionError) as exc:
            # Nothing of the Cog reached the hub: the install stays PUBLISHED, and says why, so a retry is a
            # new ask rather than a stuck row.
            _log.warning("fetching %s failed: %s", row.reference, exc)
            self.store.record(row.id, _Stay(install), failure=f"fetch: {_bound(exc)}", failed_step="fetch")
            return None
        _log.info("install %s: fetched %s (lock %s)", row.id, row.reference, lock_digest[:19] if lock_digest else "-")
        return self.store.record(row.id, install.fetch(), lock_digest=lock_digest, failure=None, failed_step=None)

    def _fetch_artifact(self, row: InstallRow) -> str | None:
        if artifacts.is_reference(row.reference):
            if self.registry is None:
                raise artifacts.PullError("this controller reaches no registry: it was started without one")
            fetched = artifacts.fetch(row.reference, self.registry)
            refuse_literal_secrets(_toml(fetched.files.get(artifacts.MANIFEST)), where=f"{row.reference} pixi.toml")
            return fetched.lock_digest
        if self.packages is None:
            raise PackageRefused(f"{row.reference!r} is not a pinned reference, and this controller runs no "
                                 "package directory")
        package = self.packages.resolve(row.reference)
        return package.digest

    def _bind(self, row: InstallRow) -> InstallRow:
        install = row.machine()
        card = dict(row.card or {})
        requires = _requires(card)
        offered = tuple(self.filter(tuple(self.inventory(row.org_id)),
                                    {"reference": row.reference, "card": card, "install": row.id}))
        try:
            record = self._resolve(row, requires, offered)
            refuse_literal_secrets(record.to_dict(), where=f"the binding of {row.reference}")
            delivered = record.delivery(self.environ)  # the key its model names is found now, not at the check
        except BindingResolutionError as exc:
            _log.warning("binding %s refused: %s", row.reference, exc)
            return self.store.record(row.id, install.refuse_binding(reason=_bound(exc)),
                                     failure=f"bind: {_bound(exc)}", failed_step="bind")
        with self._lock:
            self._deliveries[row.run_id] = delivered
        _log.info("install %s: bound %s as %s", row.id, row.reference, record.id)
        return self.store.record(row.id, install.admit_binding(binding=record.id), binding_id=record.id,
                                 binding=record.to_dict(), failure=None, failed_step=None)

    def _resolve(self, row: InstallRow, requires: list[dict[str, Any]],
                 offered: tuple[Satisfier, ...]) -> BindingRecord:
        if not requires:
            return BindingRecord.none(row.reference)
        with self._lock:
            self._deliveries.pop(row.run_id, None)  # the resolving worker is given no binding
        worker = self.executor.materialize(row.reference, row.run_id, RESOLVE)
        try:
            envelope = worker.interact(RESOLVE, {"requires": requires,
                                                 "inventory": [s.descriptor() for s in offered]})
        except Exception as exc:  # noqa: BLE001 - the worker's refusal is the binding's refusal
            raise BindingResolutionError(f"resolve could not be asked: {type(exc).__name__}: {exc}") from exc
        finally:
            self.executor.teardown(worker)
        if not isinstance(envelope, ResultEnvelope):
            raise BindingResolutionError("resolve answered no envelope")
        if not envelope.ok:
            error = envelope.error
            raise BindingResolutionError(f"resolve failed: {error.code if error else 'error'}: "
                                         f"{error.detail if error else ''}")
        payload = envelope.payload if isinstance(envelope.payload, Mapping) else {}
        selections = payload.get("selections")
        if not isinstance(selections, Mapping):
            raise BindingResolutionError("resolve answered no selections: {capability: satisfier id}")
        missing = [str(r["capability"]) for r in requires if not selections.get(str(r["capability"]))]
        if missing:
            raise BindingResolutionError(f"resolve selected nothing for {', '.join(missing)}: the inventory offers "
                                         f"{', '.join(s.id for s in offered) or 'nothing'}")
        return BindingRecord.of(row.reference, {str(k): v for k, v in selections.items()}, offered)

    def _check(self, row: InstallRow) -> InstallRow:
        install = row.machine()
        try:
            worker = self.executor.materialize(row.reference, row.run_id, CHECK)
        except Exception as exc:  # noqa: BLE001 - a worker that never came up fails the check at materialize
            reason = f"{type(exc).__name__}: {exc}"
            _log.warning("check of %s failed at materialize: %s", row.reference, reason)
            return self.store.record(row.id, install.check_failed(step="materialize", reason=_bound(reason)),
                                     failure=f"materialize: {_bound(reason)}", failed_step="materialize")
        try:
            if CHECK in _lifecycle_ops(dict(row.card or {})):
                envelope = worker.interact(CHECK, {})
                if not isinstance(envelope, ResultEnvelope) or not envelope.ok:
                    error = getattr(envelope, "error", None)
                    reason = f"{error.code}: {error.detail}" if error else "check answered no envelope"
                    _log.warning("check of %s failed: %s", row.reference, reason)
                    return self.store.record(row.id, install.check_failed(step=CHECK, reason=_bound(reason)),
                                             failure=f"check: {_bound(reason)}", failed_step=CHECK)
        except Exception as exc:  # noqa: BLE001 - the worker's refusal is the check's failure
            reason = f"{type(exc).__name__}: {exc}"
            return self.store.record(row.id, install.check_failed(step=CHECK, reason=_bound(reason)),
                                     failure=f"check: {_bound(reason)}", failed_step=CHECK)
        finally:
            try:
                self.executor.teardown(worker)
            except Exception:  # noqa: BLE001 - a teardown that fails is logged; the check's verdict stands
                _log.exception("tearing down the check worker of %s failed", row.reference)
            with self._lock:
                self._deliveries.pop(row.run_id, None)
        _log.info("install %s: %s is invokable", row.id, row.reference)
        return self.store.record(row.id, install.check_passed(), failure=None, failed_step=None)

    # --- removing -----------------------------------------------------------------------------

    def uninstall(self, install_id: str) -> InstallRow | None:
        """Remove every runtime resource the install created, and record it uninstalled."""
        row = self.store.get(install_id)
        if row is None or not row.live:
            return row
        sweep = getattr(self.executor, "sweep", None)
        if sweep is not None:
            try:
                sweep(run_id=row.run_id)
            except Exception:  # noqa: BLE001 - what could not be swept is logged, and the removal goes on
                _log.exception("sweeping the workers of install %s failed", install_id)
        if self.cache is not None and artifacts.is_reference(row.reference):
            # The package and the environment pixi built beside it go when the last install of their lock does;
            # another install of the same lock (the same reference, or another artifact with the same
            # environment) keeps them.
            others = [r for r in self.store.list(include_uninstalled=False)
                      if r.id != row.id and r.lock_digest == row.lock_digest]
            if not others:
                try:
                    artifacts.evict(row.reference, self.cache)
                except Exception:  # noqa: BLE001
                    _log.exception("evicting %s from the cache failed", row.reference)
        with self._lock:
            self._deliveries.pop(row.run_id, None)
        try:
            transition = row.machine().uninstall()
        except InvalidTransition as exc:
            _log.warning("install %s cannot be uninstalled from %s: %s", install_id, row.state, exc)
            return row
        _log.info("install %s: %s uninstalled", install_id, row.reference)
        return self.store.record(install_id, transition)


class _Stay:
    """A non-move: nothing to record, the state as it was. What a failed fetch writes with its reason."""

    def __init__(self, install) -> None:
        self.after = install
        self.records = ()


def _toml(text: bytes | None) -> Any:
    if not text:
        return {}
    import tomllib

    try:
        return tomllib.loads(text.decode())
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return {}


def harnesses_installed(store: InstallStore, org_id: str | None) -> list[Satisfier]:
    """The harness Cogs an organization has installed and invokable: what its context Cogs may resolve to."""
    return [harness_satisfier(row.id, row.reference, row.digest, row.cog_id, row.card)
            for row in store.list(org_id=org_id, state="invokable") if is_harness(row.card)]


__all__ = ["CHECK", "RESOLVE", "Installer", "harnesses_installed"]
