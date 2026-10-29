"""Frame Group models and persistence backends.

A Frame Group bundles one or more Frames under its own owners and visibility.
Unlike a Frame it has **no document body**, so it is a pure relational record:
it does *not* use the blob ``FrameStore``. Groups are a required relational
feature with **no per-feature toggle** (like ``history.py``): ``InMemory`` for
tests/dev, ``Postgres`` whenever the shared ``frames.postgres`` URL is set, and
``UnavailableFrameGroupStore`` (every op raises → 503) when no DB is configured.

``all_published`` is a **derived** field: it is never stored on the group row.
The router computes it at read time by AND-ing the ``published`` flag of every
member Frame (proposal §5, PRD §2.3). The model carries it only so it can be
returned in API responses.

**Deferred (Spec 3 §3):** the reciprocal ``group_ids`` projection on Frame reads
is intentionally *not* implemented for A3. Membership lives only on the group
row (``frame_ids``); ``FrameMetadata.group_ids`` stays ``[]``. Populating it would
require a per-read group lookup plus a per-group all-members-published / access
computation on the hot Frame GET/list path, which is not worth the cost for A3.
The membership is fully queryable from the group side (``GET /frame-groups`` and
``GET /frames?group_id=`` already filters on the stored ``frame_ids``).
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .codec import FrameDecodeError, undecodable_frame_log
from .db import locked_schema_connection
from .models import (
    DESCRIPTION_MAX_LENGTH,
    FRAME_NAME_MAX_LENGTH,
    ID_PATTERN,
    Visibility,
    normalize_name,
    normalize_owners,
    validate_frame_id,
)
from .store import FrameNotFoundError, FrameStore, utc_now

FRAME_GROUP_SCHEMA_VERSION = 1


class FrameGroupNotFoundError(KeyError):
    """Raised when a requested Frame Group id is not present in the store."""

    pass


class FrameGroupStoreUnavailableError(RuntimeError):
    """Raised when the group store is used but no shared frames Postgres is set.

    The only off state for Groups: a required relational feature with no
    per-feature toggle, unavailable solely when no DB is configured (→ 503).
    """

    pass


def normalize_frame_ids(frame_ids: list[str], *, require_non_empty: bool = True) -> list[str]:
    """Validate, deduplicate, and order-preserve member Frame ids.

    ``frame_ids`` is never empty in storage; the store and router both enforce
    ``>=1`` so a group always bundles at least one Frame.
    """

    normalized = []
    seen = set()
    for frame_id in frame_ids:
        validate_frame_id(frame_id)
        if frame_id not in seen:
            normalized.append(frame_id)
            seen.add(frame_id)
    if require_non_empty and not normalized:
        raise ValueError("a Frame Group must contain at least one frame")
    return normalized


class FrameGroup(BaseModel):
    """A persisted bundle of Frames with its own owners and visibility."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = FRAME_GROUP_SCHEMA_VERSION
    id: str = Field(pattern=ID_PATTERN)
    org_id: str
    workspace_id: str
    name: str = Field(min_length=1, max_length=FRAME_NAME_MAX_LENGTH)
    description: str = Field(default="", max_length=DESCRIPTION_MAX_LENGTH)
    created_by: str
    owners: list[str] = Field(default_factory=list)
    visibility: Visibility = Visibility.private
    frame_ids: list[str] = Field(default_factory=list)
    # Derived at read time from member frames; never stored (see project_group).
    # all_published: every member frame is published.
    all_published: bool = False
    # effective_visibility: least-broad of the group's own visibility and every
    # member's visibility (a group is never more visible than its narrowest
    # member). Defaults to the most restrictive tier until projected.
    effective_visibility: Visibility = Visibility.private
    created_at: datetime
    updated_at: datetime

    @field_validator("name")
    @classmethod
    def validate_name(cls, name: str) -> str:
        return normalize_name(name)

    @field_validator("owners")
    @classmethod
    def validate_owners(cls, owners: list[str]) -> list[str]:
        return normalize_owners(owners)

    @field_validator("frame_ids")
    @classmethod
    def validate_frame_ids(cls, frame_ids: list[str]) -> list[str]:
        return normalize_frame_ids(frame_ids)


class FrameGroupCreate(BaseModel):
    """Request body for creating a Frame Group."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=FRAME_NAME_MAX_LENGTH)
    description: str = Field(default="", max_length=DESCRIPTION_MAX_LENGTH)
    visibility: Visibility = Visibility.private
    frame_ids: list[str] = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def validate_name(cls, name: str) -> str:
        return normalize_name(name)

    @field_validator("frame_ids")
    @classmethod
    def validate_frame_ids(cls, frame_ids: list[str]) -> list[str]:
        return normalize_frame_ids(frame_ids)


class FrameGroupUpdate(BaseModel):
    """Request body for replacing mutable Frame Group fields."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=FRAME_NAME_MAX_LENGTH)
    description: str = Field(default="", max_length=DESCRIPTION_MAX_LENGTH)
    visibility: Visibility = Visibility.private

    @field_validator("name")
    @classmethod
    def validate_name(cls, name: str) -> str:
        return normalize_name(name)


class FrameIdBody(BaseModel):
    """Request body for adding a single member Frame to a group."""

    model_config = ConfigDict(extra="forbid")

    frame_id: str = Field(pattern=ID_PATTERN)


BROADNESS = {Visibility.private: 0, Visibility.internal: 1, Visibility.public: 2}


def effective_group_visibility(
    group_visibility: Visibility,
    member_visibilities: list[Visibility],
) -> Visibility:
    """Least-broad of the group's own visibility and every member's visibility.

    A group is never more visible than its narrowest member — e.g. a ``public``
    group containing a ``private`` member is effectively ``private`` (Spec 3 §3).
    """

    return min([group_visibility, *member_visibilities], key=lambda v: BROADNESS[v])


def compute_derived(group: FrameGroup, store: FrameStore) -> tuple[bool, Visibility]:
    """Compute ``(all_published, effective_visibility)`` from member Frames.

    A missing member (for example a deleted Frame) counts as not published, so
    the group stays owner-only until membership is reconciled. Member visibility
    is collected from the members that still exist; when the group isn't fully
    published the readiness gate makes ``effective_visibility`` moot anyway.
    """

    all_published = True
    member_visibilities: list[Visibility] = []
    for frame_id in group.frame_ids:
        try:
            frame = store.get_frame(frame_id)
        except FrameNotFoundError:
            all_published = False
            continue
        except FrameDecodeError as exc:
            # A corrupt member is undecodable, not merely absent: count it as
            # not-published (like a missing member) so the group stays owner-only
            # rather than failing the whole projection.
            undecodable_frame_log.skipped(exc, context=f"group {group.id} projection")
            all_published = False
            continue
        if not frame.published:
            all_published = False
        member_visibilities.append(frame.visibility)
    return all_published, effective_group_visibility(group.visibility, member_visibilities)


def project_group(group: FrameGroup, store: FrameStore) -> FrameGroup:
    """Return a copy of the group with both derived fields populated."""

    all_published, effective_visibility = compute_derived(group, store)
    return group.model_copy(
        update={"all_published": all_published, "effective_visibility": effective_visibility}
    )


class FrameGroupStore(ABC):
    """Workspace-scoped persistence contract for Frame Groups.

    Groups own only relational membership and governance metadata; member Frame
    content lives in the ``FrameStore``. ``frame_ids`` and ``owners`` are never
    empty.
    """

    @abstractmethod
    def list_groups(self, org_id: str, workspace_id: str) -> list[FrameGroup]:
        """Return all groups scoped to one org/workspace."""

        raise NotImplementedError

    @abstractmethod
    def get_group(self, group_id: str) -> FrameGroup:
        """Return one group or raise ``FrameGroupNotFoundError``."""

        raise NotImplementedError

    @abstractmethod
    def find_groups_containing(self, frame_id: str) -> list[FrameGroup]:
        """Return every group referencing ``frame_id`` as a member, in ANY tenant.

        The sole caller is frame-DELETE reconciliation
        (``reconcile_groups_after_frame_delete``). Unlike ``list_groups``, this
        is deliberately unscoped: group-add accepts a readable-not-owned member
        (including cross-tenant ``public`` frames), so a Frame's membership can
        live in a group outside its own org/workspace. When the Frame is
        deleted, that stale id must be pruned from those cross-tenant
        memberships too, so the lookup cannot be tenant-scoped. (Access
        *narrowing* is non-destructive and never calls this — only deletion
        mutates membership.)
        """

        raise NotImplementedError

    @abstractmethod
    def create_group(
        self,
        org_id: str,
        workspace_id: str,
        created_by: str,
        owners: list[str],
        name: str,
        description: str,
        visibility: Visibility,
        frame_ids: list[str],
    ) -> FrameGroup:
        """Create a group with its creator, owners, and >=1 member Frame."""

        raise NotImplementedError

    @abstractmethod
    def update_group(
        self,
        group_id: str,
        name: str,
        description: str,
        visibility: Visibility,
    ) -> FrameGroup:
        """Overwrite mutable group fields, preserving owners and membership."""

        raise NotImplementedError

    @abstractmethod
    def delete_group(self, group_id: str) -> None:
        """Delete a group only; member Frames are never touched."""

        raise NotImplementedError

    @abstractmethod
    def set_owners(self, group_id: str, owners: list[str]) -> FrameGroup:
        """Replace the owner list (>=1) without touching membership."""

        raise NotImplementedError

    @abstractmethod
    def add_frame(self, group_id: str, frame_id: str) -> FrameGroup:
        """Add one member Frame, deduplicated and order-preserving."""

        raise NotImplementedError

    @abstractmethod
    def remove_frame(self, group_id: str, frame_id: str) -> FrameGroup:
        """Remove one member Frame; callers must keep membership non-empty."""

        raise NotImplementedError


class UnavailableFrameGroupStore(FrameGroupStore):
    """Store returned when no shared frames Postgres is configured.

    Every operation raises ``FrameGroupStoreUnavailableError`` so all group
    endpoints return 503. Frame-side callers that touch the group store as a
    best-effort side-task (frame-delete reconciliation, the ``group_id`` list
    filter) catch this so a missing groups DB never breaks frame operations.
    """

    _MESSAGE = "Frame Groups require a shared frames Postgres URL"

    def list_groups(self, org_id: str, workspace_id: str) -> list[FrameGroup]:
        raise FrameGroupStoreUnavailableError(self._MESSAGE)

    def get_group(self, group_id: str) -> FrameGroup:
        raise FrameGroupStoreUnavailableError(self._MESSAGE)

    def find_groups_containing(self, frame_id: str) -> list[FrameGroup]:
        raise FrameGroupStoreUnavailableError(self._MESSAGE)

    def create_group(
        self,
        org_id: str,
        workspace_id: str,
        created_by: str,
        owners: list[str],
        name: str,
        description: str,
        visibility: Visibility,
        frame_ids: list[str],
    ) -> FrameGroup:
        raise FrameGroupStoreUnavailableError(self._MESSAGE)

    def update_group(
        self,
        group_id: str,
        name: str,
        description: str,
        visibility: Visibility,
    ) -> FrameGroup:
        raise FrameGroupStoreUnavailableError(self._MESSAGE)

    def delete_group(self, group_id: str) -> None:
        raise FrameGroupStoreUnavailableError(self._MESSAGE)

    def set_owners(self, group_id: str, owners: list[str]) -> FrameGroup:
        raise FrameGroupStoreUnavailableError(self._MESSAGE)

    def add_frame(self, group_id: str, frame_id: str) -> FrameGroup:
        raise FrameGroupStoreUnavailableError(self._MESSAGE)

    def remove_frame(self, group_id: str, frame_id: str) -> FrameGroup:
        raise FrameGroupStoreUnavailableError(self._MESSAGE)


class InMemoryFrameGroupStore(FrameGroupStore):
    """Process-local group store for tests and narrow dev scenarios."""

    def __init__(self):
        self._lock = threading.Lock()
        self._items: dict[str, FrameGroup] = {}

    def list_groups(self, org_id: str, workspace_id: str) -> list[FrameGroup]:
        """Return scoped groups from process-local memory."""

        with self._lock:
            return [
                group.model_copy(deep=True)
                for group in self._items.values()
                if group.org_id == org_id and group.workspace_id == workspace_id
            ]

    def get_group(self, group_id: str) -> FrameGroup:
        """Return one group from process-local memory."""

        with self._lock:
            group = self._items.get(group_id)
            if group is None:
                raise FrameGroupNotFoundError(group_id)
            return group.model_copy(deep=True)

    def find_groups_containing(self, frame_id: str) -> list[FrameGroup]:
        """Return every group referencing ``frame_id``, across all tenants."""

        with self._lock:
            return [group.model_copy(deep=True) for group in self._items.values() if frame_id in group.frame_ids]

    def create_group(
        self,
        org_id: str,
        workspace_id: str,
        created_by: str,
        owners: list[str],
        name: str,
        description: str,
        visibility: Visibility,
        frame_ids: list[str],
    ) -> FrameGroup:
        """Create a group in process-local memory."""

        now = utc_now()
        group = FrameGroup(
            id=uuid4().hex,
            org_id=org_id,
            workspace_id=workspace_id,
            name=name,
            description=description,
            created_by=created_by,
            owners=owners,
            visibility=visibility,
            frame_ids=normalize_frame_ids(frame_ids),
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._items[group.id] = group
        return group.model_copy(deep=True)

    def update_group(
        self,
        group_id: str,
        name: str,
        description: str,
        visibility: Visibility,
    ) -> FrameGroup:
        """Replace mutable fields in process-local memory."""

        return self._mutate(
            group_id,
            {"name": name, "description": description, "visibility": visibility},
        )

    def delete_group(self, group_id: str) -> None:
        """Delete a group from process-local memory."""

        with self._lock:
            if group_id not in self._items:
                raise FrameGroupNotFoundError(group_id)
            del self._items[group_id]

    def set_owners(self, group_id: str, owners: list[str]) -> FrameGroup:
        """Replace the owner list in process-local memory."""

        return self._mutate(group_id, {"owners": normalize_owners(owners)})

    def add_frame(self, group_id: str, frame_id: str) -> FrameGroup:
        """Add a member Frame in process-local memory."""

        with self._lock:
            group = self._items.get(group_id)
            if group is None:
                raise FrameGroupNotFoundError(group_id)
            frame_ids = normalize_frame_ids([*group.frame_ids, frame_id])
            updated = group.model_copy(update={"frame_ids": frame_ids, "updated_at": utc_now()})
            self._items[group_id] = updated
            return updated.model_copy(deep=True)

    def remove_frame(self, group_id: str, frame_id: str) -> FrameGroup:
        """Remove a member Frame in process-local memory."""

        with self._lock:
            group = self._items.get(group_id)
            if group is None:
                raise FrameGroupNotFoundError(group_id)
            remaining = normalize_frame_ids([fid for fid in group.frame_ids if fid != frame_id])
            updated = group.model_copy(update={"frame_ids": remaining, "updated_at": utc_now()})
            self._items[group_id] = updated
            return updated.model_copy(deep=True)

    def _mutate(self, group_id: str, fields: dict) -> FrameGroup:
        with self._lock:
            group = self._items.get(group_id)
            if group is None:
                raise FrameGroupNotFoundError(group_id)
            updated = group.model_copy(update={**fields, "updated_at": utc_now()})
            self._items[group_id] = updated
            return updated.model_copy(deep=True)


class PostgresFrameGroupStore(FrameGroupStore):
    """Postgres-backed group store for production-style deployments.

    Mirrors ``PostgresActiveFrameStore``/``PostgresFrameHistoryStore``: the
    database lives outside the pack (for example in RDS); the pack only needs a
    pooled database handle (``db.PostgresDatabase``) and creates its table if
    it is missing.
    """

    def __init__(self, db, auto_migrate: bool = False):
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("PostgresFrameGroupStore requires psycopg") from exc

        self.db = db
        self.database_url = db.database_url
        self.psycopg = psycopg
        if auto_migrate:
            self._ensure_schema()

    def list_groups(self, org_id: str, workspace_id: str) -> list[FrameGroup]:
        """Return scoped groups from Postgres, newest first."""

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, org_id, workspace_id, name, description, created_by,
                       owners, visibility, frame_ids, created_at, updated_at
                FROM frames_server_groups
                WHERE org_id = %s AND workspace_id = %s
                ORDER BY created_at DESC, id DESC
                """,
                (org_id, workspace_id),
            ).fetchall()
        return [self._row_to_group(row) for row in rows]

    def get_group(self, group_id: str) -> FrameGroup:
        """Return one group from Postgres."""

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, org_id, workspace_id, name, description, created_by,
                       owners, visibility, frame_ids, created_at, updated_at
                FROM frames_server_groups
                WHERE id = %s
                """,
                (group_id,),
            ).fetchone()
        if row is None:
            raise FrameGroupNotFoundError(group_id)
        return self._row_to_group(row)

    def find_groups_containing(self, frame_id: str) -> list[FrameGroup]:
        """Return every group referencing ``frame_id`` in Postgres, across all tenants."""

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, org_id, workspace_id, name, description, created_by,
                       owners, visibility, frame_ids, created_at, updated_at
                FROM frames_server_groups
                WHERE frame_ids @> %s
                """,
                (self.psycopg.types.json.Jsonb([frame_id]),),
            ).fetchall()
        return [self._row_to_group(row) for row in rows]

    def create_group(
        self,
        org_id: str,
        workspace_id: str,
        created_by: str,
        owners: list[str],
        name: str,
        description: str,
        visibility: Visibility,
        frame_ids: list[str],
    ) -> FrameGroup:
        """Insert a group row into Postgres."""

        group_id = uuid4().hex
        owners = normalize_owners(owners)
        frame_ids = normalize_frame_ids(frame_ids)
        with self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO frames_server_groups (
                    id, org_id, workspace_id, name, description, created_by,
                    owners, visibility, frame_ids
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, org_id, workspace_id, name, description, created_by,
                          owners, visibility, frame_ids, created_at, updated_at
                """,
                (
                    group_id,
                    org_id,
                    workspace_id,
                    name,
                    description,
                    created_by,
                    self.psycopg.types.json.Jsonb(owners),
                    visibility.value,
                    self.psycopg.types.json.Jsonb(frame_ids),
                ),
            ).fetchone()
        return self._row_to_group(row)

    def update_group(
        self,
        group_id: str,
        name: str,
        description: str,
        visibility: Visibility,
    ) -> FrameGroup:
        """Update mutable group fields in Postgres."""

        with self._connect() as conn:
            row = conn.execute(
                """
                UPDATE frames_server_groups
                SET name = %s, description = %s, visibility = %s, updated_at = now()
                WHERE id = %s
                RETURNING id, org_id, workspace_id, name, description, created_by,
                          owners, visibility, frame_ids, created_at, updated_at
                """,
                (name, description, visibility.value, group_id),
            ).fetchone()
        if row is None:
            raise FrameGroupNotFoundError(group_id)
        return self._row_to_group(row)

    def delete_group(self, group_id: str) -> None:
        """Delete a group row from Postgres."""

        with self._connect() as conn:
            row = conn.execute(
                "DELETE FROM frames_server_groups WHERE id = %s RETURNING id",
                (group_id,),
            ).fetchone()
        if row is None:
            raise FrameGroupNotFoundError(group_id)

    def set_owners(self, group_id: str, owners: list[str]) -> FrameGroup:
        """Replace the owner list in Postgres."""

        owners = normalize_owners(owners)
        with self._connect() as conn:
            row = conn.execute(
                """
                UPDATE frames_server_groups
                SET owners = %s, updated_at = now()
                WHERE id = %s
                RETURNING id, org_id, workspace_id, name, description, created_by,
                          owners, visibility, frame_ids, created_at, updated_at
                """,
                (self.psycopg.types.json.Jsonb(owners), group_id),
            ).fetchone()
        if row is None:
            raise FrameGroupNotFoundError(group_id)
        return self._row_to_group(row)

    def add_frame(self, group_id: str, frame_id: str) -> FrameGroup:
        """Add a member Frame in Postgres."""

        group = self.get_group(group_id)
        frame_ids = normalize_frame_ids([*group.frame_ids, frame_id])
        return self._set_frame_ids(group_id, frame_ids)

    def remove_frame(self, group_id: str, frame_id: str) -> FrameGroup:
        """Remove a member Frame in Postgres."""

        group = self.get_group(group_id)
        remaining = normalize_frame_ids([fid for fid in group.frame_ids if fid != frame_id])
        return self._set_frame_ids(group_id, remaining)

    def _set_frame_ids(self, group_id: str, frame_ids: list[str]) -> FrameGroup:
        with self._connect() as conn:
            row = conn.execute(
                """
                UPDATE frames_server_groups
                SET frame_ids = %s, updated_at = now()
                WHERE id = %s
                RETURNING id, org_id, workspace_id, name, description, created_by,
                          owners, visibility, frame_ids, created_at, updated_at
                """,
                (self.psycopg.types.json.Jsonb(frame_ids), group_id),
            ).fetchone()
        if row is None:
            raise FrameGroupNotFoundError(group_id)
        return self._row_to_group(row)

    def _row_to_group(self, row: dict) -> FrameGroup:
        return FrameGroup(
            id=row["id"],
            org_id=row["org_id"],
            workspace_id=row["workspace_id"],
            name=row["name"],
            description=row["description"],
            created_by=row["created_by"],
            owners=list(row["owners"]),
            visibility=Visibility(row["visibility"]),
            frame_ids=list(row["frame_ids"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _ensure_schema(self) -> None:
        # Advisory-locked (issue #42): bare CREATE TABLE IF NOT EXISTS races
        # under concurrent replica startup.
        with locked_schema_connection(self.db) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS frames_server_groups (
                    id           text PRIMARY KEY,
                    org_id       text NOT NULL,
                    workspace_id text NOT NULL,
                    name         text NOT NULL,
                    description  text NOT NULL DEFAULT '',
                    created_by   text NOT NULL,
                    owners       jsonb NOT NULL,
                    visibility   text NOT NULL DEFAULT 'private',
                    frame_ids    jsonb NOT NULL,
                    created_at   timestamptz NOT NULL DEFAULT now(),
                    updated_at   timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS frames_server_groups_scope
                ON frames_server_groups (org_id, workspace_id)
                """
            )
            # GIN index backing find_groups_containing's `frame_ids @> [id]`
            # containment query (frame-delete reconciliation), which scans
            # across all tenants and so cannot use the (org, workspace) scope
            # index above.
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS frames_server_groups_frame_ids
                ON frames_server_groups USING gin (frame_ids)
                """
            )

    def _connect(self):
        # A transaction-scoped checkout from the shared pool — never a fresh
        # per-request psycopg.connect.
        return self.db.connection()
