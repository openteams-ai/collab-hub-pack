"""Capability resolution and model bindings for Cogs.

The resolver selects declared providers and projects a model Cog into the
connection configuration a context Cog needs. Secrets remain references; the
resolver never loads or embeds their values.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


class BindingResolutionError(LookupError):
    """Raised when a declared Cog dependency cannot be resolved safely."""


@dataclass(frozen=True, slots=True)
class CapabilityRequirement:
    """A structured capability requirement declared by a Cog."""

    capability: str
    provider: str | None = None


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


@dataclass(frozen=True, slots=True)
class ContextCog:
    """A context Cog that points to a model Cog rather than embedding one."""

    name: str
    model: CapabilityRequirement


@dataclass(frozen=True, slots=True)
class ModelBinding:
    """The non-secret model configuration delivered to a worker."""

    endpoint: str
    model_identifier: str
    auth_ref: str
    transport: str


class CapabilityResolver(Protocol):
    def resolve_model(self, context: ContextCog, models: tuple[ModelCog, ...]) -> ModelBinding:
        """Resolve a context Cog's model requirement into a worker binding."""


class DeclaredCapabilityResolver(CapabilityResolver):
    """Resolve model capabilities using only structured declarations."""

    def resolve_model(self, context: ContextCog, models: tuple[ModelCog, ...]) -> ModelBinding:
        requirement = context.model
        matching = [model for model in models if requirement.capability in model.provides]
        if requirement.provider is not None:
            matching = [model for model in matching if model.name == requirement.provider]
        if len(matching) != 1:
            detail = "no matching model Cog" if not matching else "multiple matching model Cogs"
            raise BindingResolutionError(f"{detail} for {context.name!r}")
        model = matching[0]
        return ModelBinding(
            endpoint=model.endpoint,
            model_identifier=model.model_identifier,
            auth_ref=model.auth_ref,
            transport=model.transport,
        )


# --- the hub's models: block --------------------------------------------------------------------
#
# Until a Cog's own `resolve` binds it to a model (Phase 24), the run controller is configured with
# the models the hub offers and which Cog talks to which, and delivers each model to the workers of
# its Cogs, and to no other. Phase 24's inventory is generated from the same block.

PROVIDERS = ("openai-compatible", "anthropic")
_MODEL_KEYS = {"provider", "endpoint", "model", "auth_ref", "context_window", "max_output_tokens"}


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
            models[name] = ModelCog(
                name=name, endpoint=str(spec.get("endpoint", "")), model_identifier=str(spec.get("model", "")),
                auth_ref=str(spec.get("auth_ref", "")), provider=provider,
                context_window=spec.get("context_window"), max_output_tokens=spec.get("max_output_tokens"))
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
