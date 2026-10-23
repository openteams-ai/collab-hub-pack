"""Response models of the Cog catalog read API (issue #85).

The card is served **as stored**: an open JSON object, never re-validated.
The store keeps the bundle reader's output verbatim (ADR-0001 D9: the card
is the Cog's own declarations, not a hub-invented schema), and the reader
passes publisher values through as it found them -- a profile may declare
``id: 42``, and the card says ``42``. A typed response model would turn one
such card into a failed response for every page it appears on, so the types
live in the OpenAPI description (:data:`CARD_SCHEMA`) and nowhere else.

The rest wrap a card with what only the catalog knows -- where the artifact
lives and when it was seen -- because identity is the digest, and the card
itself does not carry one.

Two things are subtracted from that on the wire, both declared here:

- A list item carries a **trimmed** card: the stored card minus
  :data:`LIST_CARD_OMITTED_KEYS`, the verbatim documents. The per-version
  routes serve the full card, and ``cog.md`` the body.
- An **anonymous** caller (one a ``public`` catalog rule admitted) sees no
  ``source_id`` and no reader diagnostics (:data:`ANONYMOUS_CARD_OMITTED_KEYS`).
  Each model names what it drops in its ``ANONYMOUS_EXCLUDE``, a
  ``model_dump(exclude=...)`` spec the router applies in one place.
"""

from __future__ import annotations

from dataclasses import fields
from datetime import datetime
from typing import Annotated, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema

from .bundle import CogCard
from .catalog import CogArtifact
from .registry import reference as build_reference

_CARD_KEY_NOTES = {
    "card": "Card format version (1).",
    "manifest": "Bundle-relative path of the manifest COG.md names.",
    "id": "The Cog id, usually `<publisher>/<name>`: a search key, not identity.",
    "version": "The declared version.",
    "kind": "`complete`, `model`, `harness`, `context`, `prog`, ...",
    "provides": "What the Cog provides (usually a list of strings).",
    "requires": "Required capabilities (usually a list of `{capability, locality, satisfiers}`).",
    "io": "Declared io types (usually `{accepts: [...], produces: [...]}`).",
    "entry_points": "Declared entry points (usually a list of objects).",
    "ops": "Entry points by audience (usually `{usage: [...], lifecycle: [...]}`).",
    "name": "The frontmatter name.",
    "description": "The frontmatter description.",
    "publisher": "The frontmatter publisher.",
    "frontmatter": "The COG.md frontmatter, parsed.",
    "frontmatter_raw": "The COG.md frontmatter, verbatim.",
    "profile": "The whole profile the manifest declares, as structured data.",
    "profile_raw": "The profile file, verbatim.",
    "body": "The Markdown body of COG.md, after its frontmatter.",
    "errors": "Every problem the reader found (usually a list of strings). Omitted for anonymous callers.",
    "warnings": "Non-fatal findings (usually a list of strings). Omitted for anonymous callers.",
}

LIST_CARD_OMITTED_KEYS: tuple[str, ...] = ("body", "profile_raw", "frontmatter_raw")
"""Card keys a ``GET /v1/cogs`` list item leaves out: the verbatim documents.

The per-version routes serve them (``cog.md`` serves the body). Keeping them
off list pages spares every page up to three documents per item, and adding
a key back later is compatible where removing one would not be.
"""

ANONYMOUS_CARD_OMITTED_KEYS: tuple[str, ...] = ("errors", "warnings")
"""Card keys an anonymous caller never sees: the reader's diagnostics."""

SOURCE_ID_DESCRIPTION = "The registry source this location was indexed from. Omitted for anonymous callers."

BACKING_REFERENCE_DESCRIPTION = (
    "Where the artifact is stored: `<backing registry host>/<repository>@<digest>`. Present only when the Hub "
    "serves pulls itself, and only for platform operators; absent (not null) otherwise."
)

_ANONYMOUS_CARD_EXCLUDE = dict.fromkeys(ANONYMOUS_CARD_OMITTED_KEYS, True)
_DIAGNOSTICS = ", ".join(f"`{key}`" for key in ANONYMOUS_CARD_OMITTED_KEYS)

CARD_SCHEMA: dict[str, Any] = {
    "title": "CogCard",
    "type": "object",
    "description": (
        "A Cog's catalog card: the bundle reader's output as captured at index time, served verbatim. "
        "The keys below are the ones the reader emits, in its order; their values are the Cog's own "
        "declarations and are not validated here, so a client must tolerate unexpected shapes. "
        f"Keys a newer reader adds appear too. The reader diagnostics ({_DIAGNOSTICS}) are omitted "
        "for anonymous callers."
    ),
    "properties": {field.name: {"description": _CARD_KEY_NOTES.get(field.name, "")} for field in fields(CogCard)},
    "additionalProperties": True,
}
"""The OpenAPI schema of the card: every key the reader emits, documented, none type-enforced."""

_LIST_OMITTED = ", ".join(f"`{key}`" for key in LIST_CARD_OMITTED_KEYS)

LIST_CARD_SCHEMA: dict[str, Any] = {
    "title": "CogListCard",
    "type": "object",
    "description": (
        f"A trimmed catalog card: the stored card without {_LIST_OMITTED}. "
        "`GET /v1/cogs/{cog_id}` and `GET /v1/cogs/{cog_id}/versions/{digest}` serve the full card, "
        "and `.../cog.md` the body. Otherwise the same open object as `CogCard`: values are not validated "
        f"here, keys a newer reader adds appear too, and the reader diagnostics ({_DIAGNOSTICS}) are "
        "omitted for anonymous callers."
    ),
    "properties": {key: value for key, value in CARD_SCHEMA["properties"].items() if key not in LIST_CARD_OMITTED_KEYS},
    "additionalProperties": True,
}
"""The OpenAPI schema of a list item's card: :data:`CARD_SCHEMA` minus :data:`LIST_CARD_OMITTED_KEYS`."""

CardDocument = Annotated[dict[str, Any], WithJsonSchema(CARD_SCHEMA)]
ListCardDocument = Annotated[dict[str, Any], WithJsonSchema(LIST_CARD_SCHEMA)]


def client_reference(row: CogArtifact, registry_host: str | None) -> str:
    """The reference a client pulls: through the Hub when it serves pulls, else the backing registry.

    ``registry_host`` is the Hub's own registry host when ``cogs.serve.enabled``
    (issue #179) and ``None`` otherwise. The repository path and the digest
    are the same either way; only the host a client talks to changes.
    """

    if registry_host is None:
        return row.reference
    return build_reference(registry_host, row.repository, row.digest)


def list_card(card: dict[str, Any] | None) -> dict[str, Any]:
    """``card`` without :data:`LIST_CARD_OMITTED_KEYS`."""

    return {key: value for key, value in (card or {}).items() if key not in LIST_CARD_OMITTED_KEYS}


def _not_required(*names: str):
    # The field is always set on the model, but the router drops it for
    # anonymous callers, so the published schema must not promise it.
    # Every model this decorates keeps other required fields, so the list is
    # never left empty.
    def edit(schema: dict[str, Any]) -> None:
        schema["required"] = [name for name in schema.get("required", ()) if name not in names]

    return edit


class CogVersion(BaseModel):
    """One indexed location of one version: a digest in one repository of one source."""

    model_config = ConfigDict(json_schema_extra=_not_required("source_id"))
    ANONYMOUS_EXCLUDE: ClassVar[dict[str, Any]] = {"source_id": True}

    digest: str = Field(description="`sha256:<64 hex>`: the artifact's identity.")
    version: str | None = None
    source_id: str = Field(description=SOURCE_ID_DESCRIPTION)
    repository: str
    reference: str = Field(
        description=(
            "The pinned install reference `<host>/<repository>@<digest>`. The host is the Hub's own registry "
            "host when the Hub serves pulls, else the backing registry's."
        )
    )
    tags: list[str] = Field(default_factory=list)
    pushed_at: datetime | None = None
    indexed_at: datetime | None = None
    removed_at: datetime | None = Field(
        default=None, description="Set once the artifact is gone from its registry; the row stays readable."
    )

    @classmethod
    def of(cls, row: CogArtifact, registry_host: str | None = None) -> CogVersion:
        return cls(
            digest=row.digest,
            version=row.version,
            source_id=row.source_id,
            repository=row.repository,
            reference=client_reference(row, registry_host),
            tags=list(row.tags),
            pushed_at=row.pushed_at,
            indexed_at=row.indexed_at,
            removed_at=row.removed_at,
        )


class CogEntry(CogVersion):
    """One version's location plus its full card."""

    ANONYMOUS_EXCLUDE: ClassVar[dict[str, Any]] = {"source_id": True, "card": _ANONYMOUS_CARD_EXCLUDE}

    cog_id: str
    card: CardDocument

    @classmethod
    def of(cls, row: CogArtifact, registry_host: str | None = None) -> CogEntry:
        return cls(**CogVersion.of(row, registry_host).model_dump(), cog_id=row.cog_id or "", card=row.card or {})


class CogListEntry(CogVersion):
    """One list item: a current version's location plus its **trimmed** card (not a `CogEntry`)."""

    ANONYMOUS_EXCLUDE: ClassVar[dict[str, Any]] = CogEntry.ANONYMOUS_EXCLUDE

    cog_id: str
    card: ListCardDocument

    @classmethod
    def of(cls, row: CogArtifact, registry_host: str | None = None) -> CogListEntry:
        return cls(
            **CogVersion.of(row, registry_host).model_dump(), cog_id=row.cog_id or "", card=list_card(row.card)
        )


class CogListPage(BaseModel):
    """One page of current Cogs, ordered by `cog_id`."""

    ANONYMOUS_EXCLUDE: ClassVar[dict[str, Any]] = {"items": {"__all__": CogListEntry.ANONYMOUS_EXCLUDE}}

    items: list[CogListEntry]
    limit: int
    offset: int
    next_offset: int | None = Field(description="Pass as `offset` for the next page; null on the last page.")


class CogDetail(CogEntry):
    """A Cog's current version (the newest present one) plus every indexed version, newest first."""

    ANONYMOUS_EXCLUDE: ClassVar[dict[str, Any]] = {
        **CogEntry.ANONYMOUS_EXCLUDE,
        "versions": {"__all__": CogVersion.ANONYMOUS_EXCLUDE},
    }

    versions: list[CogVersion] = Field(description="Every indexed location of every version, removed ones included.")


class CogLocation(BaseModel):
    """Another place the same digest was indexed."""

    model_config = ConfigDict(json_schema_extra=_not_required("source_id"))
    ANONYMOUS_EXCLUDE: ClassVar[dict[str, Any]] = {"source_id": True}

    reference: str
    backing_reference: str | None = Field(default=None, description=BACKING_REFERENCE_DESCRIPTION)
    source_id: str = Field(description=SOURCE_ID_DESCRIPTION)
    repository: str
    pushed_at: datetime | None = None
    removed_at: datetime | None = None

    @classmethod
    def of(cls, row: CogArtifact, registry_host: str | None = None, *, backing: bool = False) -> CogLocation:
        return cls(
            reference=client_reference(row, registry_host),
            backing_reference=row.reference if backing else None,
            source_id=row.source_id,
            repository=row.repository,
            pushed_at=row.pushed_at,
            removed_at=row.removed_at,
        )


class CogReference(BaseModel):
    """What a client hands to `nebi import`: the pinned reference of one digest."""

    model_config = ConfigDict(json_schema_extra=_not_required("source_id"))
    ANONYMOUS_EXCLUDE: ClassVar[dict[str, Any]] = {
        "source_id": True,
        "locations": {"__all__": CogLocation.ANONYMOUS_EXCLUDE},
    }
    BACKING_EXCLUDE: ClassVar[dict[str, Any]] = {
        "backing_reference": True,
        "locations": {"__all__": {"backing_reference": True}},
    }
    """Dropped, on top of the caller's own cut, for everyone the backing location is not for."""

    reference: str = Field(description="`<host>/<repository>@<digest>` of the preferred location.")
    backing_reference: str | None = Field(default=None, description=BACKING_REFERENCE_DESCRIPTION)
    source_id: str = Field(description=SOURCE_ID_DESCRIPTION)
    repository: str
    digest: str
    present: bool = Field(description="False when every location of this digest has been removed from its registry.")
    locations: list[CogLocation] = Field(description="The digest's other indexed locations, preferred first.")


class RegistryCredentialRequest(BaseModel):
    """What to exchange the Hub session for. `pull` is the only scope there is."""

    model_config = ConfigDict(extra="forbid")

    scope: Literal["pull"] = "pull"


class RegistryCredentialResponse(BaseModel):
    """A registry credential, shown once: the Hub keeps only a digest of `secret`."""

    id: str = Field(description="Names the credential for `DELETE /v1/cogs/registry-credentials/{id}`.")
    registry: str = Field(description="`host[:port]` of the Hub's registry: what a client logs in to and pulls from.")
    username: str
    secret: str = Field(description="The password. Not retrievable again.")
    scope: Literal["pull"]
    expires_at: datetime = Field(
        description="RFC 3339, UTC. The credential, and every token minted from it, stops here."
    )


class CatalogV1Repository(BaseModel):
    namespace: str = Field(description="The first path segment of the repository.")
    name: str = Field(description="The rest of the repository path.")
    description: str = ""


class CatalogV1(BaseModel):
    """Transitional: the static `catalog.v1.json` shape, generated from the index."""

    schemaVersion: Literal[1] = 1
    repositories: list[CatalogV1Repository]


class CogErrorBody(BaseModel):
    code: str
    message: str


class CogErrorResponse(BaseModel):
    """The API error envelope."""

    error: CogErrorBody
