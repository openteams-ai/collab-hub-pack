"""The binding: the model a worker was bound to, and its limits, as the hub delivered them.

The run controller delivers a Cog's model as ``COLLAB_MODEL_*`` variables in the
worker's environment (the hub's ``models:`` block), or as a file named by
``COLLAB_BINDING_FILE`` in the format the Cog's own ``resolve`` later fills in.
A worker reads either; its adapter gets one ``Binding``. The key stays in the
worker: ``record()`` is the part an envelope carries, and it never holds it.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .envelope import WorkerError

OPENAI_COMPATIBLE = "openai-compatible"

FILE_ENV = "COLLAB_BINDING_FILE"
VARIABLES = {
    "provider": "COLLAB_MODEL_PROVIDER", "base_url": "COLLAB_MODEL_BASE_URL", "model": "COLLAB_MODEL_NAME",
    "api_key": "COLLAB_MODEL_API_KEY", "context_window": "COLLAB_MODEL_CONTEXT_WINDOW",
    "max_output_tokens": "COLLAB_MODEL_MAX_OUTPUT_TOKENS",
}


@dataclass(frozen=True, slots=True)
class Binding:
    """What a worker was bound to. ``api_key`` is the one secret, and ``record()`` leaves it out."""

    provider: str = OPENAI_COMPATIBLE
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    context_window: int | None = None
    """The model's context window in tokens, when the hub knows it: what a harness reserves headroom from."""
    max_output_tokens: int | None = None
    resolved_from: str = "environment"

    @property
    def bound(self) -> bool:
        """Whether a model was delivered at all."""
        return bool(self.base_url or self.model or self.provider != OPENAI_COMPATIBLE)

    def record(self) -> dict[str, object]:
        """The binding as an envelope carries it: identity and limits, no secret."""
        return {"provider": self.provider, "endpoint": self.base_url, "model": self.model,
                "context_window": self.context_window, "max_output_tokens": self.max_output_tokens,
                "resolved_from": self.resolved_from}


def load(environ: Mapping[str, str] | None = None) -> Binding:
    """The binding the hub delivered: the file ``COLLAB_BINDING_FILE`` names, or the ``COLLAB_MODEL_*`` variables.

    ``WorkerError("binding-invalid")`` for a file that cannot be read, a limit
    that is not a whole number, or an ``auth_ref`` naming a variable that is
    not set.
    """
    environ = os.environ if environ is None else environ
    path = environ.get(FILE_ENV)
    if path:
        return _from_file(Path(path), environ)
    given = {field: environ.get(variable) for field, variable in VARIABLES.items()}
    return Binding(
        provider=given["provider"] or OPENAI_COMPATIBLE, base_url=given["base_url"] or None,
        model=given["model"] or None, api_key=given["api_key"] or None,
        context_window=_limit(given["context_window"], VARIABLES["context_window"]),
        max_output_tokens=_limit(given["max_output_tokens"], VARIABLES["max_output_tokens"]),
    )


def _from_file(path: Path, environ: Mapping[str, str]) -> Binding:
    try:
        document = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise WorkerError("binding-invalid", f"the binding file {path} cannot be read: {exc}") from None
    if not isinstance(document, Mapping):
        raise WorkerError("binding-invalid", f"the binding file {path} is not a JSON object")
    key = document.get("api_key")
    auth_ref = document.get("auth_ref")
    if key is None and isinstance(auth_ref, str):
        # ``env:NAME``: the key is in the worker's environment, never in the file.
        if not auth_ref.startswith("env:") or not environ.get(auth_ref[4:]):
            raise WorkerError("binding-invalid", f"auth_ref {auth_ref!r} names no variable that is set")
        key = environ[auth_ref[4:]]
    return Binding(
        provider=str(document.get("provider") or OPENAI_COMPATIBLE),
        base_url=document.get("endpoint") or document.get("base_url") or None,
        model=document.get("model") or None, api_key=key or None,
        context_window=_limit(document.get("context_window"), "context_window"),
        max_output_tokens=_limit(document.get("max_output_tokens"), "max_output_tokens"),
        resolved_from=f"file:{path}",
    )


def _limit(value: object, name: str) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool) or (isinstance(value, str) and not value.strip().isdigit()) \
            or (not isinstance(value, (int, str))) or int(value) <= 0:
        raise WorkerError("binding-invalid", f"{name} is a positive whole number of tokens, not {value!r}")
    return int(value)
