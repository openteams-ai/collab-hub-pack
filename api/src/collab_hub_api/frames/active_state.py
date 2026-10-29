"""Active Frame selection storage backends."""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass

from .db import locked_schema_connection


@dataclass(frozen=True)
class ActiveFrameUsage:
    """Tenant-wide active-Frame aggregates for usage reporting."""

    frames: int
    """The number of distinct Frame ids active for at least one user."""

    users: int
    """The number of users with at least one active Frame."""


class ActiveStateUnavailableError(RuntimeError):
    """Raised when active Frame state is requested but no backend is configured."""

    pass


class ActiveFrameStore(ABC):
    """Workspace-scoped active Frame selection contract.

    Active state is intentionally separate from Frame storage: the FrameStore
    owns content, while this store records which existing Frame ids a user has
    enabled in one org/workspace for deterministic MCP injection.
    """

    @abstractmethod
    def get_active_frame_ids(
        self,
        org_id: str,
        workspace_id: str,
        user: str,
    ) -> list[str]:
        """Return active Frame ids for a scoped user in persisted order."""

        raise NotImplementedError

    @abstractmethod
    def set_active_frame_ids(
        self,
        org_id: str,
        workspace_id: str,
        user: str,
        frame_ids: list[str],
    ) -> list[str]:
        """Replace a scoped user's active Frame ids and return the stored order."""

        raise NotImplementedError

    @abstractmethod
    def remove_frame_id(self, frame_id: str) -> None:
        """Remove a deleted Frame id from every user's active set."""

        raise NotImplementedError

    @abstractmethod
    def find_active_holders(self, frame_id: str) -> list[tuple[str, str, str]]:
        """Return ``(org_id, workspace_id, user)`` for every holder of a Frame id.

        The lookup is **global** (across all tenants), not scoped to the frame's
        own tenant: a ``public`` Frame can be activated by users in other
        tenants, whose active records live under their own ``(org, workspace)``.
        Scoping the query would silently miss those cross-tenant holders during
        reconciliation.
        """

        raise NotImplementedError

    @abstractmethod
    def remove_frame_id_for(
        self,
        org_id: str,
        workspace_id: str,
        user: str,
        frame_id: str,
    ) -> None:
        """Remove a Frame id from one scoped user's active set."""

        raise NotImplementedError

    @abstractmethod
    def count_active(self, org_id: str, workspace_id: str) -> ActiveFrameUsage:
        """Return tenant-wide active-Frame aggregates for usage reporting."""

        raise NotImplementedError


class DisabledActiveFrameStore(ActiveFrameStore):
    """Active-state backend used when the feature is not configured."""

    def get_active_frame_ids(
        self,
        org_id: str,
        workspace_id: str,
        user: str,
    ) -> list[str]:
        """Reject reads because active-state persistence is disabled."""

        raise ActiveStateUnavailableError("Active Frame state is not configured")

    def set_active_frame_ids(
        self,
        org_id: str,
        workspace_id: str,
        user: str,
        frame_ids: list[str],
    ) -> list[str]:
        """Reject writes because active-state persistence is disabled."""

        raise ActiveStateUnavailableError("Active Frame state is not configured")

    def remove_frame_id(self, frame_id: str) -> None:
        """No-op when active-state persistence is disabled."""

        return None

    def find_active_holders(self, frame_id: str) -> list[tuple[str, str, str]]:
        """Return no holders when active-state persistence is disabled."""

        return []

    def remove_frame_id_for(
        self,
        org_id: str,
        workspace_id: str,
        user: str,
        frame_id: str,
    ) -> None:
        """No-op when active-state persistence is disabled."""

        return None

    def count_active(self, org_id: str, workspace_id: str) -> ActiveFrameUsage:
        """Reject aggregation because active-state persistence is disabled."""

        raise ActiveStateUnavailableError("Active Frame state is not configured")


class InMemoryActiveFrameStore(ActiveFrameStore):
    """Process-local active state store for tests and narrow dev scenarios."""

    def __init__(self):
        self._lock = threading.Lock()
        self._items: dict[tuple[str, str, str], list[str]] = {}

    def get_active_frame_ids(
        self,
        org_id: str,
        workspace_id: str,
        user: str,
    ) -> list[str]:
        """Return active Frame ids from process-local memory."""

        with self._lock:
            return list(self._items.get((org_id, workspace_id, user), []))

    def set_active_frame_ids(
        self,
        org_id: str,
        workspace_id: str,
        user: str,
        frame_ids: list[str],
    ) -> list[str]:
        """Replace process-local active Frame ids for one scoped user."""

        with self._lock:
            self._items[(org_id, workspace_id, user)] = list(frame_ids)
            return list(frame_ids)

    def remove_frame_id(self, frame_id: str) -> None:
        """Remove a deleted Frame id from every process-local active set."""

        with self._lock:
            for key, frame_ids in self._items.items():
                self._items[key] = [item for item in frame_ids if item != frame_id]

    def find_active_holders(self, frame_id: str) -> list[tuple[str, str, str]]:
        """Return all process-local holders of a Frame id, across every tenant."""

        with self._lock:
            return [
                (item_org, item_workspace, user)
                for (item_org, item_workspace, user), frame_ids in self._items.items()
                if frame_id in frame_ids
            ]

    def remove_frame_id_for(
        self,
        org_id: str,
        workspace_id: str,
        user: str,
        frame_id: str,
    ) -> None:
        """Remove a Frame id from one process-local scoped user's active set."""

        with self._lock:
            key = (org_id, workspace_id, user)
            frame_ids = self._items.get(key)
            if frame_ids is not None:
                self._items[key] = [item for item in frame_ids if item != frame_id]

    def count_active(self, org_id: str, workspace_id: str) -> ActiveFrameUsage:
        """Aggregate active Frames across the tenant from process-local memory."""

        frames: set[str] = set()
        users = 0
        with self._lock:
            for (item_org, item_workspace, _user), frame_ids in self._items.items():
                if item_org != org_id or item_workspace != workspace_id or not frame_ids:
                    continue
                users += 1
                frames.update(frame_ids)
        return ActiveFrameUsage(frames=len(frames), users=users)


class PostgresActiveFrameStore(ActiveFrameStore):
    """Postgres-backed active state for production-style deployments.

    The database is expected to live outside the pack in production, for example
    in RDS. The pack only needs a pooled database handle (``db.PostgresDatabase``)
    and creates its small table if it is missing.
    """

    def __init__(self, db, auto_migrate: bool = False):
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("PostgresActiveFrameStore requires psycopg") from exc

        self.db = db
        self.database_url = db.database_url
        self.psycopg = psycopg
        if auto_migrate:
            self._ensure_schema()

    def get_active_frame_ids(
        self,
        org_id: str,
        workspace_id: str,
        user: str,
    ) -> list[str]:
        """Return active Frame ids from Postgres for one scoped user."""

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT frame_ids
                FROM frames_server_active_frames
                WHERE org_id = %s AND workspace_id = %s AND user_id = %s
                """,
                (org_id, workspace_id, user),
            ).fetchone()
        if row is None:
            return []
        return list(row["frame_ids"])

    def set_active_frame_ids(
        self,
        org_id: str,
        workspace_id: str,
        user: str,
        frame_ids: list[str],
    ) -> list[str]:
        """Upsert active Frame ids in Postgres for one scoped user."""

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO frames_server_active_frames (
                    org_id, workspace_id, user_id, frame_ids, updated_at
                )
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (org_id, workspace_id, user_id)
                DO UPDATE SET frame_ids = EXCLUDED.frame_ids, updated_at = now()
                """,
                (org_id, workspace_id, user, self.psycopg.types.json.Jsonb(frame_ids)),
            )
        return list(frame_ids)

    def remove_frame_id(self, frame_id: str) -> None:
        """Remove a deleted Frame id from every Postgres active set."""

        with self._connect() as conn:
            conn.execute(
                """
                UPDATE frames_server_active_frames
                SET frame_ids = COALESCE(
                    (
                        SELECT jsonb_agg(value)
                        FROM jsonb_array_elements_text(frame_ids) AS value
                        WHERE value <> %s
                    ),
                    '[]'::jsonb
                ),
                updated_at = now()
                WHERE frame_ids ? %s
                """,
                (frame_id, frame_id),
            )

    def find_active_holders(self, frame_id: str) -> list[tuple[str, str, str]]:
        """Return all holders of a Frame id via a global jsonb containment query.

        Intentionally unscoped by tenant so cross-tenant holders of a ``public``
        Frame are found during reconciliation.
        """

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT org_id, workspace_id, user_id
                FROM frames_server_active_frames
                WHERE frame_ids @> %s
                """,
                (self.psycopg.types.json.Jsonb([frame_id]),),
            ).fetchall()
        return [(row["org_id"], row["workspace_id"], row["user_id"]) for row in rows]

    def remove_frame_id_for(
        self,
        org_id: str,
        workspace_id: str,
        user: str,
        frame_id: str,
    ) -> None:
        """Remove a Frame id from one scoped user's Postgres active set."""

        with self._connect() as conn:
            conn.execute(
                """
                UPDATE frames_server_active_frames
                SET frame_ids = COALESCE(
                    (
                        SELECT jsonb_agg(value)
                        FROM jsonb_array_elements_text(frame_ids) AS value
                        WHERE value <> %s
                    ),
                    '[]'::jsonb
                ),
                updated_at = now()
                WHERE org_id = %s
                  AND workspace_id = %s
                  AND user_id = %s
                  AND frame_ids ? %s
                """,
                (frame_id, org_id, workspace_id, user, frame_id),
            )

    def count_active(self, org_id: str, workspace_id: str) -> ActiveFrameUsage:
        """Aggregate active Frames across the tenant in Postgres."""

        # The lateral join expands each row's frame_ids array; rows with an
        # empty array produce no members, so they never count as active users.
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    count(DISTINCT member.value) AS frames,
                    count(DISTINCT holder.user_id) AS users
                FROM frames_server_active_frames AS holder
                CROSS JOIN LATERAL jsonb_array_elements_text(holder.frame_ids) AS member(value)
                WHERE holder.org_id = %s AND holder.workspace_id = %s
                """,
                (org_id, workspace_id),
            ).fetchone()
        return ActiveFrameUsage(frames=row["frames"], users=row["users"])

    def _ensure_schema(self) -> None:
        # Advisory-locked (issue #42): the CREATE and the ALTER/constraint
        # migration below all race under concurrent replica startup, exactly
        # like a bare CREATE TABLE IF NOT EXISTS does.
        with locked_schema_connection(self.db) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS frames_server_active_frames (
                    org_id text NOT NULL DEFAULT 'dev-org',
                    workspace_id text NOT NULL DEFAULT 'default',
                    user_id text NOT NULL,
                    frame_ids jsonb NOT NULL,
                    updated_at timestamptz NOT NULL DEFAULT now(),
                    PRIMARY KEY (org_id, workspace_id, user_id)
                )
                """
            )
            conn.execute(
                """
                ALTER TABLE frames_server_active_frames
                ADD COLUMN IF NOT EXISTS org_id text NOT NULL DEFAULT 'dev-org'
                """
            )
            conn.execute(
                """
                ALTER TABLE frames_server_active_frames
                ADD COLUMN IF NOT EXISTS workspace_id text NOT NULL DEFAULT 'default'
                """
            )
            conn.execute(
                """
                DO $$
                BEGIN
                    IF EXISTS (
                        SELECT 1
                        FROM pg_constraint
                        WHERE conname = 'frames_server_active_frames_pkey'
                    ) THEN
                        ALTER TABLE frames_server_active_frames
                        DROP CONSTRAINT frames_server_active_frames_pkey;
                    END IF;

                    IF NOT EXISTS (
                        SELECT 1
                        FROM pg_constraint
                        WHERE conname = 'frames_server_active_frames_pkey'
                    ) THEN
                        ALTER TABLE frames_server_active_frames
                        ADD CONSTRAINT frames_server_active_frames_pkey
                        PRIMARY KEY (org_id, workspace_id, user_id);
                    END IF;
                END $$;
                """
            )

    def _connect(self):
        # A transaction-scoped checkout from the shared pool — never a fresh
        # per-request psycopg.connect.
        return self.db.connection()
