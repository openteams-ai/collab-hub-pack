"""The Cog on a hub: an install, once per digest, from the registry to invokable.

Install by digest (#105) moves through these states; only an ``INVOKABLE``
install is materialized (:meth:`CogInstall.require_invokable`). An install
asked for and removed before anything was fetched goes from ``PUBLISHED``
to ``UNINSTALLED`` directly: there is nothing on the hub to remove, and the
record of the ask ends honestly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ._machine import Context, InvalidTransition, Machine, Record, State, Transition, accepts, move


class InstallState(State):
    """The install machine's interface: one method per event, each refused by default."""

    def fetch(self, install: CogInstall, **_: Any) -> Transition[CogInstall]:
        return self.refuse("fetch")

    def admit_binding(self, install: CogInstall, **_: Any) -> Transition[CogInstall]:
        return self.refuse("admit_binding")

    def refuse_binding(self, install: CogInstall, **_: Any) -> Transition[CogInstall]:
        return self.refuse("refuse_binding")

    def check_passed(self, install: CogInstall, **_: Any) -> Transition[CogInstall]:
        return self.refuse("check_passed")

    def check_failed(self, install: CogInstall, **_: Any) -> Transition[CogInstall]:
        return self.refuse("check_failed")

    def uninstall(self, install: CogInstall, **_: Any) -> Transition[CogInstall]:
        return self.refuse("uninstall")



def _uninstall(install: CogInstall) -> Transition[CogInstall]:
    return move(install, InstallState.UNINSTALLED, Record("uninstalled", {"reference": install.reference}))


class Published(InstallState):
    name = "PUBLISHED"

    @accepts("FETCHED")
    def fetch(self, install: CogInstall, **_: Any) -> Transition[CogInstall]:
        return move(install, InstallState.FETCHED, Record("install_fetched", {"reference": install.reference}))

    @accepts("UNINSTALLED")
    def uninstall(self, install: CogInstall, **_: Any) -> Transition[CogInstall]:
        return _uninstall(install)


class Fetched(InstallState):
    name = "FETCHED"

    @accepts("BOUND")
    def admit_binding(self, install: CogInstall, *, binding: str, **_: Any) -> Transition[CogInstall]:
        record = Record("binding_admitted", {"reference": install.reference, "binding": binding})
        return move(install, InstallState.BOUND, record, binding=binding, failure=None)

    @accepts("FETCHED")
    def refuse_binding(self, install: CogInstall, *, reason: str, **_: Any) -> Transition[CogInstall]:
        record = Record("binding_refused", {"reference": install.reference, "reason": reason})
        return move(install, InstallState.FETCHED, record, failure=reason)

    @accepts("UNINSTALLED")
    def uninstall(self, install: CogInstall, **_: Any) -> Transition[CogInstall]:
        return _uninstall(install)


class Bound(InstallState):
    name = "BOUND"

    @accepts("INVOKABLE")
    def check_passed(self, install: CogInstall, **_: Any) -> Transition[CogInstall]:
        record = Record("check_passed", {"reference": install.reference, "binding": install.binding})
        return move(install, InstallState.INVOKABLE, record, failure=None)

    @accepts("BOUND")
    def check_failed(self, install: CogInstall, *, step: str, reason: str, **_: Any) -> Transition[CogInstall]:
        record = Record("check_failed", {"reference": install.reference, "step": step, "reason": reason})
        return move(install, InstallState.BOUND, record, failure=f"{step}: {reason}")

    @accepts("UNINSTALLED")
    def uninstall(self, install: CogInstall, **_: Any) -> Transition[CogInstall]:
        return _uninstall(install)


class Invokable(InstallState):
    name = "INVOKABLE"

    @accepts("UNINSTALLED")
    def uninstall(self, install: CogInstall, **_: Any) -> Transition[CogInstall]:
        return _uninstall(install)


class Uninstalled(InstallState):
    name = "UNINSTALLED"


INSTALL = Machine(
    "install",
    InstallState,
    (Published, Fetched, Bound, Invokable, Uninstalled),
    initial="PUBLISHED",
)


@dataclass(frozen=True, slots=True)
class CogInstall(Context):
    """A Cog pinned by digest on this hub, and what its install has reached."""

    reference: str
    state: InstallState = field(default_factory=lambda: INSTALL.initial)  # type: ignore[assignment]
    binding: str | None = None
    failure: str | None = None

    def fetch(self) -> Transition[CogInstall]:
        return self.dispatch("fetch")

    def admit_binding(self, *, binding: str) -> Transition[CogInstall]:
        return self.dispatch("admit_binding", binding=binding)

    def refuse_binding(self, *, reason: str) -> Transition[CogInstall]:
        return self.dispatch("refuse_binding", reason=reason)

    def check_passed(self) -> Transition[CogInstall]:
        return self.dispatch("check_passed")

    def check_failed(self, *, step: str, reason: str) -> Transition[CogInstall]:
        return self.dispatch("check_failed", step=step, reason=reason)

    def uninstall(self) -> Transition[CogInstall]:
        return self.dispatch("uninstall")

    def require_invokable(self) -> None:
        """Refuse to materialize a worker of a Cog that is not ``INVOKABLE``."""
        if self.state is not InstallState.INVOKABLE:
            raise InvalidTransition(self.state, "materialize", "only an INVOKABLE Cog is materialized")
