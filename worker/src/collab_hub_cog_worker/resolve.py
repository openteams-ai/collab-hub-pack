"""The Cog's side of resolution: select satisfiers from the inventory the hub offers.

The hub offers, the Cog selects (ADR-0001 D3). At install, the hub invokes
the worker's ``resolve`` entry point with ``{"requires": [...], "inventory":
[...]}``: the Cog's own ``requires`` as its card declares them, and one
descriptor per satisfier the hub offers (a served model with its endpoint,
model id, transport, locality, ``auth_ref`` and limits; an installed harness
Cog by its reference). The worker answers ``{"selections": {<capability>:
<satisfier id>}}``, and the hub records the binding and delivers it.

This is the selection a worker on the SDK makes unless the Cog registers a
``resolve`` of its own: for each requirement, the first satisfier that
provides the capability, runs where the requirement's locality allows, and,
when the requirement names the Cogs that may satisfy it, is one of them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .envelope import WorkerError

ANY = "any"


def _provides(descriptor: Mapping[str, Any]) -> set[str]:
    provides = descriptor.get("provides")
    names = {str(item) for item in provides} if isinstance(provides, list) else set()
    kind = descriptor.get("kind")
    if isinstance(kind, str):
        names.add(kind)
    return names


def satisfies(requirement: Mapping[str, Any], descriptor: Mapping[str, Any]) -> bool:
    """Whether one satisfier meets one requirement: the capability, the locality, and the named Cogs if any."""
    capability = str(requirement.get("capability") or "")
    if capability not in _provides(descriptor):
        return False
    locality = requirement.get("locality") or ANY
    if locality != ANY and descriptor.get("locality") not in (None, locality):
        return False
    named = requirement.get("satisfiers")
    if isinstance(named, list) and named:
        cog_id = descriptor.get("cog_id")
        if cog_id is not None and cog_id not in named:
            return False
    return True


def select(requires: Sequence[Mapping[str, Any]], inventory: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The default selection: ``{"selections": {capability: id}, "unsatisfied": [capability, ...]}``.

    A requirement that names the Cogs that may satisfy it prefers one of
    them; failing that, any satisfier of the capability with an admissible
    locality.
    """
    selections: dict[str, str] = {}
    unsatisfied: list[str] = []
    for requirement in requires:
        capability = str(requirement.get("capability") or "")
        if not capability:
            continue
        candidates = [d for d in inventory if isinstance(d, Mapping) and isinstance(d.get("id"), str)
                      and satisfies(requirement, d)]
        named = requirement.get("satisfiers") if isinstance(requirement.get("satisfiers"), list) else []
        preferred = [d for d in candidates if d.get("cog_id") in named] if named else []
        chosen = (preferred or candidates)[0] if (preferred or candidates) else None
        if chosen is None:
            unsatisfied.append(capability)
        else:
            selections[capability] = str(chosen["id"])
    return {"selections": selections, "unsatisfied": unsatisfied}


def resolve_input(value: Any) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """``(requires, inventory)`` from a ``resolve`` task's input; ``invalid-input`` for any other shape."""
    if not isinstance(value, Mapping):
        raise WorkerError("invalid-input", "resolve takes {requires: [...], inventory: [...]}")
    requires, inventory = value.get("requires") or [], value.get("inventory") or []
    if not isinstance(requires, list) or not isinstance(inventory, list) \
            or not all(isinstance(item, Mapping) for item in (*requires, *inventory)):
        raise WorkerError("invalid-input", "resolve takes {requires: [{capability, ...}], inventory: [{id, ...}]}")
    return list(requires), list(inventory)
