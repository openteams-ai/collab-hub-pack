"""The hub-side resolver the tests use as a double: selects a model by declaration, the way a Cog's own
``resolve`` does on the SDK (``collab_hub_cog_worker.resolve``). The hub itself runs no resolver (ADR-0001 D3):
it offers the inventory, the installed Cog selects, and the hub records what it selected."""

from __future__ import annotations

from dataclasses import dataclass

from collab_hub_execution import BindingResolutionError, ModelCog


@dataclass(frozen=True, slots=True)
class CapabilityRequirement:
    capability: str
    provider: str | None = None


@dataclass(frozen=True, slots=True)
class ContextCog:
    name: str
    model: CapabilityRequirement


@dataclass(frozen=True, slots=True)
class ModelBinding:
    endpoint: str
    model_identifier: str
    auth_ref: str
    transport: str


class DeclaredCapabilityResolver:
    """Resolve model capabilities using only structured declarations: the test double of a Cog's ``resolve``."""

    def resolve_model(self, context: ContextCog, models: tuple[ModelCog, ...]) -> ModelBinding:
        requirement = context.model
        matching = [model for model in models if requirement.capability in model.provides]
        if requirement.provider is not None:
            matching = [model for model in matching if model.name == requirement.provider]
        if len(matching) != 1:
            detail = "no matching model Cog" if not matching else "multiple matching model Cogs"
            raise BindingResolutionError(f"{detail} for {context.name!r}")
        model = matching[0]
        return ModelBinding(endpoint=model.endpoint, model_identifier=model.model_identifier,
                            auth_ref=model.auth_ref, transport=model.transport)
