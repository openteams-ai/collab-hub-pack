"""Model binding: the inventory the hub offers, the record of what a Cog's ``resolve`` selected, its delivery.

The hub offers, the Cog selects, the hub records and delivers (ADR-0001 D3).
The *inventory* is generated from the hub's ``models:`` block, one satisfier
per served model, plus the harness Cogs installed on the hub; the installed
Cog's ``resolve`` entry point is handed that inventory and answers with its
selections; the hub turns them into a *binding record*, keeps it with the
install, and delivers it to every worker of that Cog as a mounted file plus
its environment. Secrets stay references: a satisfier names its key by
``auth_ref``, the record carries that name, and only the worker's own
environment ever holds the value, resolved by the controller as the worker
starts. ``refuse_literal_secrets`` holds that line.

Before ``resolve`` sees the inventory it passes through a filter, the plug
point for sensitivity (#10): ``no_filter`` is the only one built.

The ``models:`` block also still binds a Cog by name (``[cogs]``), for the
packages a development controller runs straight from a directory; an
installed Cog is bound by its own ``resolve`` instead.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class BindingResolutionError(LookupError):
    """Raised when a declared Cog dependency cannot be resolved safely."""


@dataclass(frozen=True, slots=True)
class ModelCog:
    """A model provider's public connection metadata.

    ``auth_ref`` identifies a secret or credential binding managed by the
    runtime. It is never a token or password.
    """

    name: str
    endpoint: str
    model_identifier: str
    auth_ref: str
    transport: str = "http"
    provides: frozenset[str] = field(default_factory=lambda: frozenset({"model"}))
    provider: str = "openai-compatible"
    """How the model is spoken to: ``openai-compatible`` at ``endpoint``, or ``anthropic``, Claude through the
    Anthropic API, for which ``endpoint`` may be empty."""
    context_window: int | None = None
    """The most tokens the model takes in one call, input and output together, when known."""
    max_output_tokens: int | None = None
    """The most tokens the model writes in one call, when known."""
    locality: str = "external"
    """Where the model runs and where data goes when it is used: ``in-cluster``, ``on-prem`` or ``external``."""


# --- the hub's models: block --------------------------------------------------------------------
#
# Until a Cog's own `resolve` binds it to a model (Phase 25), the run controller is configured with
# the models the hub offers and which Cog talks to which, and delivers each model to the workers of
# its Cogs, and to no other. Phase 25's inventory is generated from the same block.

PROVIDERS = ("openai-compatible", "anthropic")
LOCALITIES = ("in-cluster", "on-prem", "external")
_MODEL_KEYS = {"provider", "endpoint", "model", "auth_ref", "context_window", "max_output_tokens", "locality"}


@dataclass(frozen=True, slots=True)
class ModelsBlock:
    """The models the hub offers, by name, and the model each Cog is bound to."""

    models: Mapping[str, ModelCog]
    cogs: Mapping[str, str]

    @classmethod
    def load(cls, path: str | Path) -> ModelsBlock:
        """Read a ``models:`` block from TOML: ``[models.NAME]`` tables, and ``[cogs]`` naming each Cog's model.

        Anything it does not know is refused here, when the controller starts, not
        when a worker first needs it.
        """
        try:
            document = tomllib.loads(Path(path).read_text())
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise BindingResolutionError(f"the models block {str(path)!r} cannot be read: {exc}") from exc
        return cls.parse(document, where=str(path))

    @classmethod
    def parse(cls, document: Mapping[str, object], *, where: str = "the models block") -> ModelsBlock:
        unknown = set(document) - {"models", "cogs"}
        if unknown:
            raise BindingResolutionError(f"{where}: unknown tables {sorted(unknown)}; it has [models.*] and [cogs]")
        for table in ("models", "cogs"):
            if not isinstance(document.get(table, {}), Mapping):
                raise BindingResolutionError(f"{where}: [{table}] is a table")
        models: dict[str, ModelCog] = {}
        for name, spec in dict(document.get("models", {})).items():
            if not isinstance(spec, Mapping):
                raise BindingResolutionError(f"{where}: [models.{name}] is a table")
            extra = set(spec) - _MODEL_KEYS
            if extra:
                raise BindingResolutionError(f"{where}: [models.{name}] has unknown keys {sorted(extra)}")
            provider = str(spec.get("provider", "openai-compatible"))
            if provider not in PROVIDERS:
                raise BindingResolutionError(f"{where}: [models.{name}] provider is one of {', '.join(PROVIDERS)}")
            if provider == "openai-compatible" and not spec.get("endpoint"):
                raise BindingResolutionError(f"{where}: [models.{name}] needs an endpoint")
            if not spec.get("model") and provider == "openai-compatible":
                raise BindingResolutionError(f"{where}: [models.{name}] needs a model")
            for limit in ("context_window", "max_output_tokens"):
                value = spec.get(limit)
                if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 1):
                    raise BindingResolutionError(f"{where}: [models.{name}] {limit} is a positive integer")
            locality = str(spec.get("locality", "external"))
            if locality not in LOCALITIES:
                raise BindingResolutionError(f"{where}: [models.{name}] locality is one of {', '.join(LOCALITIES)}")
            auth_ref = str(spec.get("auth_ref", ""))
            if auth_ref and not AUTH_REF.match(auth_ref):
                raise BindingResolutionError(f"{where}: [models.{name}] auth_ref {auth_ref!r} is env:NAME, the only "
                                             "kind yet; a key itself never goes here")
            models[name] = ModelCog(
                name=name, endpoint=str(spec.get("endpoint", "")), model_identifier=str(spec.get("model", "")),
                auth_ref=auth_ref, provider=provider,
                context_window=spec.get("context_window"), max_output_tokens=spec.get("max_output_tokens"),
                locality=locality)
        cogs = dict(document.get("cogs", {}))
        for cog, model in cogs.items():
            if not isinstance(model, str):
                raise BindingResolutionError(f"{where}: [cogs] {cog} names a model, as a string")
        for cog, model in cogs.items():
            if model not in models:
                raise BindingResolutionError(f"{where}: Cog {cog!r} is bound to {model!r}, which [models] does not "
                                             f"name; it names {', '.join(sorted(models)) or 'none'}")
        return cls(models=models, cogs=cogs)

    def secret(self, model: ModelCog, environ: Mapping[str, str]) -> str:
        """The key ``auth_ref`` names: ``env:NAME``, read from the controller's environment; ``""`` for none."""
        ref = model.auth_ref
        if not ref:
            return ""
        scheme, _, name = ref.partition(":")
        if scheme != "env" or not name:
            raise BindingResolutionError(f"model {model.name!r}: auth_ref {ref!r} is env:NAME, the only kind yet")
        if not environ.get(name):
            raise BindingResolutionError(f"model {model.name!r}: auth_ref {ref!r} names a variable that is not set")
        return environ[name]

    def check(self, environ: Mapping[str, str]) -> None:
        """Refuse, at start, a model whose key cannot be found, or an Anthropic one with none."""
        for model in self.models.values():
            key = self.secret(model, environ)
            if model.provider == "anthropic" and not key:
                raise BindingResolutionError(f"model {model.name!r}: the anthropic provider needs an auth_ref")

    def delivery(self, environ: Mapping[str, str] | None = None) -> Callable[[str, str, str], dict[str, str]]:
        """What the local executor delivers: each Cog its model's connection, as ``COLLAB_MODEL_*`` variables.

        The key is read when a worker starts, so a rotated secret reaches the next
        worker; it enters that worker's environment only, never the Track.
        """
        environ = os.environ if environ is None else environ

        def deliver(cog: str, run_id: str, instance: str) -> dict[str, str]:
            name = self.cogs.get(cog)
            if name is None:
                return {}
            model = self.models[name]
            given = {"COLLAB_MODEL_PROVIDER": model.provider, "COLLAB_MODEL_BASE_URL": model.endpoint,
                     "COLLAB_MODEL_NAME": model.model_identifier,
                     "COLLAB_MODEL_API_KEY": self.secret(model, environ)}
            if model.context_window is not None:
                given["COLLAB_MODEL_CONTEXT_WINDOW"] = str(model.context_window)
            if model.max_output_tokens is not None:
                given["COLLAB_MODEL_MAX_OUTPUT_TOKENS"] = str(model.max_output_tokens)
            return {key: value for key, value in given.items() if value}

        return deliver


# --- the inventory, and the plug point in front of it --------------------------------------------

AUTH_REF = re.compile(r"^env:[A-Za-z_][A-Za-z0-9_]*$")
"""The only kind of credential reference yet: the name of a variable in the controller's environment."""
MODEL_KEY_ENV = "COLLAB_MODEL_API_KEY"  # noqa: S105 - the variable's name, not a secret
"""Where a worker finds the key its binding's ``auth_ref`` names: the one secret delivered, by environment."""
BINDING_FILE = "binding.json"
"""The binding as delivered to a worker: the file ``COLLAB_BINDING_FILE`` names."""
BINDING_FILE_ENV = "COLLAB_BINDING_FILE"


@dataclass(frozen=True, slots=True)
class Satisfier:
    """One entry of the inventory: something the hub offers that a Cog's ``requires`` may be satisfied by.

    A model satisfier is a served model, described by its endpoint, model
    id, transport, locality, ``auth_ref`` and limits; a harness satisfier is
    a harness Cog installed on the hub, by its pinned reference. Neither ever
    carries a credential's value.
    """

    id: str
    kind: str
    """``model`` or ``harness``."""
    provides: tuple[str, ...]
    locality: str = "external"
    provider: str | None = None
    endpoint: str | None = None
    model: str | None = None
    transport: str | None = None
    auth_ref: str | None = None
    context_window: int | None = None
    max_output_tokens: int | None = None
    reference: str | None = None
    digest: str | None = None
    cog_id: str | None = None
    install: str | None = None

    def descriptor(self) -> dict[str, Any]:
        """The satisfier as a Cog's ``resolve`` sees it, and as the binding record keeps it: no value absent."""
        return {key: value for key, value in (
            ("id", self.id), ("kind", self.kind), ("provides", list(self.provides)), ("locality", self.locality),
            ("provider", self.provider), ("endpoint", self.endpoint), ("model", self.model),
            ("transport", self.transport), ("auth_ref", self.auth_ref), ("context_window", self.context_window),
            ("max_output_tokens", self.max_output_tokens), ("reference", self.reference), ("digest", self.digest),
            ("cog_id", self.cog_id), ("install", self.install),
        ) if value is not None}

    @classmethod
    def from_descriptor(cls, value: Mapping[str, Any]) -> Satisfier:
        known = {f for f in cls.__dataclass_fields__}
        fields = {key: value[key] for key in known if key in value}
        fields["provides"] = tuple(fields.get("provides") or ())
        return cls(**fields)


def model_satisfier(model: ModelCog) -> Satisfier:
    return Satisfier(id=f"model:{model.name}", kind="model", provides=("model", f"model-endpoint/{model.provider}"),
                     locality=model.locality, provider=model.provider, endpoint=model.endpoint or None,
                     model=model.model_identifier or None, transport=model.transport, auth_ref=model.auth_ref or None,
                     context_window=model.context_window, max_output_tokens=model.max_output_tokens)


def harness_satisfier(install_id: str, reference: str, digest: str, cog_id: str | None,
                      card: Mapping[str, Any] | None = None) -> Satisfier:
    provides = ["harness"]
    for item in (card or {}).get("provides") or ():
        if isinstance(item, str) and item not in provides:
            provides.append(item)
    locality = (card or {}).get("locality")
    return Satisfier(id=f"harness:{install_id}", kind="harness", provides=tuple(provides),
                     locality=locality if isinstance(locality, str) else "in-cluster", reference=reference,
                     digest=digest, cog_id=cog_id, install=install_id)


def is_harness(card: Mapping[str, Any] | None) -> bool:
    """Whether a card describes a harness Cog: its kind says so, or it provides ``harness``."""
    card = card or {}
    provides = card.get("provides") or ()
    return card.get("kind") == "harness" or (isinstance(provides, list) and "harness" in provides)


def inventory(models: ModelsBlock | None, harnesses: Iterable[Satisfier] = ()) -> tuple[Satisfier, ...]:
    """The satisfiers the hub offers: one per served model in the ``models:`` block, and each installed harness."""
    offered = [model_satisfier(model) for model in (models.models.values() if models is not None else ())]
    offered.extend(harnesses)
    return tuple(offered)


InventoryFilter = Callable[[Sequence[Satisfier], Mapping[str, Any]], Sequence[Satisfier]]
"""What stands between the inventory and a Cog's ``resolve``: given the satisfiers and the install being bound
(its reference and card), the satisfiers that Cog may see. The plug point for sensitivity (#10): a rating
filter goes here, and nothing else in resolution changes."""


def no_filter(satisfiers: Sequence[Satisfier], context: Mapping[str, Any]) -> Sequence[Satisfier]:
    """The only filter built: every Cog sees the whole inventory."""
    return satisfiers


def inventory_digest(satisfiers: Sequence[Satisfier]) -> str:
    canonical = json.dumps([s.descriptor() for s in satisfiers], sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


# --- the binding record ----------------------------------------------------------------------------

_SECRET_KEYS = re.compile(r"(api[_-]?key|password|passwd|secret|token|authorization|credential)s?$", re.IGNORECASE)


def refuse_literal_secrets(document: Any, *, where: str) -> None:
    """Refuse a manifest or a binding that carries a credential's value rather than a reference to one.

    A key that names a secret (``api_key``, ``password``, ``token``, ...)
    with a non-empty string value is refused, and so is an ``auth_ref`` that
    is not ``env:NAME``. ``*_sha256`` and ``*_ref`` keys are references, not
    values.
    """
    def walk(value: Any, path: str) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                name = str(key)
                if name == "auth_ref":
                    if item not in (None, "") and not (isinstance(item, str) and AUTH_REF.match(item)):
                        raise BindingResolutionError(f"{where}: {path}{name} is {item!r}; a credential is named "
                                                     "by reference, env:NAME, never by its value")
                elif (_SECRET_KEYS.search(name) and not name.endswith(("_ref", "_sha256", "_digest"))
                      and isinstance(item, str) and item.strip()):
                    raise BindingResolutionError(f"{where}: {path}{name} carries a literal secret; credentials "
                                                 "travel by reference only")
                walk(item, f"{path}{name}.")
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                walk(item, f"{path}{index}.")

    walk(document, "")


@dataclass(frozen=True, slots=True)
class BindingRecord:
    """What resolution produced for one installed Cog: the satisfier it talks to, and the harness it runs on.

    The record is what the Track and every envelope name by ``id``, and what
    is delivered to each worker of the Cog. It carries references, never a
    credential's value.
    """

    id: str
    cog: str
    """The pinned reference of the Cog bound."""
    model: Satisfier | None = None
    harness: Satisfier | None = None
    resolved_by: str = "cog"
    """``cog``: the Cog's own ``resolve`` selected; ``none``: the Cog requires nothing."""
    inventory_digest: str | None = None
    selections: Mapping[str, str] = field(default_factory=dict)
    """What ``resolve`` answered: each required capability and the satisfier id it chose."""

    @classmethod
    def none(cls, cog: str) -> BindingRecord:
        """The binding of a Cog that requires nothing: an id, and nothing to deliver."""
        return cls(id=binding_id(cog, {}, None), cog=cog, resolved_by="none")

    @classmethod
    def of(cls, cog: str, selections: Mapping[str, str], offered: Sequence[Satisfier], *,
           resolved_by: str = "cog") -> BindingRecord:
        """The record of a Cog's selections over the inventory it was offered; a selection the inventory does not
        hold is refused, as is more than one model or harness."""
        by_id = {s.id: s for s in offered}
        model = harness = None
        for capability, chosen in selections.items():
            satisfier = by_id.get(chosen) if isinstance(chosen, str) else None
            if satisfier is None:
                raise BindingResolutionError(f"resolve selected {chosen!r} for {capability!r}, which the inventory "
                                             "it was offered does not hold")
            if satisfier.kind == "model":
                if model is not None and model.id != satisfier.id:
                    raise BindingResolutionError("resolve selected two models; a binding names one")
                model = satisfier
            elif satisfier.kind == "harness":
                if harness is not None and harness.id != satisfier.id:
                    raise BindingResolutionError("resolve selected two harnesses; a binding names one")
                harness = satisfier
        digest = inventory_digest(offered)
        return cls(id=binding_id(cog, selections, digest), cog=cog, model=model, harness=harness,
                   resolved_by=resolved_by, inventory_digest=digest, selections=dict(selections))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "cog": self.cog, "resolved_by": self.resolved_by, "inventory_digest": self.inventory_digest,
            "selections": dict(self.selections),
            "model": self.model.descriptor() if self.model is not None else None,
            "harness": self.harness.descriptor() if self.harness is not None else None,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> BindingRecord:
        model, harness = value.get("model"), value.get("harness")
        return cls(id=str(value["id"]), cog=str(value.get("cog") or ""),
                   model=Satisfier.from_descriptor(model) if isinstance(model, Mapping) else None,
                   harness=Satisfier.from_descriptor(harness) if isinstance(harness, Mapping) else None,
                   resolved_by=str(value.get("resolved_by") or "cog"), inventory_digest=value.get("inventory_digest"),
                   selections=dict(value.get("selections") or {}))

    def file(self) -> dict[str, Any]:
        """The binding as a worker reads it: the format the worker SDK's loader takes, the key by reference."""
        document: dict[str, Any] = {"id": self.id, "cog": self.cog, "resolved_by": self.resolved_by}
        if self.model is not None:
            document.update({
                "provider": self.model.provider or "openai-compatible", "endpoint": self.model.endpoint,
                "model": self.model.model, "transport": self.model.transport, "locality": self.model.locality,
                "context_window": self.model.context_window, "max_output_tokens": self.model.max_output_tokens,
                "satisfier": self.model.id,
            })
            if self.model.auth_ref:
                document["auth_ref"] = f"env:{MODEL_KEY_ENV}"
        if self.harness is not None:
            document["harness"] = {"satisfier": self.harness.id, "reference": self.harness.reference,
                                   "digest": self.harness.digest, "cog_id": self.harness.cog_id}
        return document

    def delivery(self, environ: Mapping[str, str] | None = None) -> Delivered:
        """What a worker bound by this record is given: the binding file, and the key its model names, by
        environment. The key is read as the worker starts, from the controller's environment, and goes nowhere
        else."""
        environ = os.environ if environ is None else environ
        env: dict[str, str] = {}
        if self.model is not None and self.model.auth_ref:
            name = self.model.auth_ref[4:]
            if not environ.get(name):
                raise BindingResolutionError(f"binding {self.id}: auth_ref {self.model.auth_ref!r} names a variable "
                                             "that is not set")
            env[MODEL_KEY_ENV] = environ[name]
        return Delivered(env=env, files={BINDING_FILE: json.dumps(self.file(), sort_keys=True)})


def binding_id(cog: str, selections: Mapping[str, str], inventory_digest: str | None) -> str:
    canonical = json.dumps({"cog": cog, "selections": dict(selections), "inventory": inventory_digest},
                           sort_keys=True, separators=(",", ":"))
    return "bnd-" + hashlib.sha256(canonical.encode()).hexdigest()[:16]


# --- what the executor delivers to a worker --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Delivered:
    """What the binding delivers to one worker: variables for its environment, and files to mount beside it.

    An executor puts the files where the worker can read them and names the
    binding file in ``COLLAB_BINDING_FILE``; the variables enter that
    worker's environment only.
    """

    env: Mapping[str, str] = field(default_factory=dict)
    files: Mapping[str, str] = field(default_factory=dict)


def as_delivered(value: Any) -> Delivered:
    """A delivery as an executor takes it: a ``Delivered``, or a mapping of variables alone."""
    if isinstance(value, Delivered):
        return value
    if value is None:
        return Delivered()
    if isinstance(value, Mapping):
        return Delivered(env=dict(value))
    raise TypeError(f"a delivery is a Delivered or a mapping of variables, not {type(value).__name__}")
