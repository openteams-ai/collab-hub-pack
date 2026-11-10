"""Reading and writing which connectors are switched off.

The table holds one bit per connector and absence means enabled, so a
deployment that never opens the admin panel behaves exactly as it did before
this existed. See :mod:`.connector_state` for how the bit is enforced.
"""

from __future__ import annotations

from .audit import (
    AUDIT_ACTION_CONNECTOR_DISABLE,
    AUDIT_ACTION_CONNECTOR_ENABLE,
    audited,
)
from .auth import AuthContext

__all__ = ["ConnectorStateUnavailableError", "PostgresConnectorStore"]


class ConnectorStateUnavailableError(RuntimeError):
    """This deployment has no database, so nothing can be switched."""


class PostgresConnectorStore:
    """Connector switches, with their audit rows."""

    configured = True

    def __init__(self, db) -> None:
        self._db = db

    def disabled(self) -> set[str]:
        """Every connector currently switched off.

        Read on the request path, uncached, for the same reason membership is:
        an administrator switching a connector off expects it to be off on the
        next request, not whenever a cache happens to expire. The table holds
        one row per connector, so this is a handful of rows by construction.
        """

        import psycopg

        try:
            with self._db.connection() as conn:
                rows = conn.execute("SELECT connector FROM collab_connector_state WHERE enabled = false").fetchall()
        except psycopg.errors.UndefinedTable:
            # A database that predates this migration has switched nothing off.
            # Failing closed here would disable every connector on a deployment
            # whose only fault is being one migration behind.
            return set()
        return {row["connector"] for row in rows}

    def set_enabled(self, actor: AuthContext, *, connector: str, enabled: bool) -> None:
        action = AUDIT_ACTION_CONNECTOR_ENABLE if enabled else AUDIT_ACTION_CONNECTOR_DISABLE
        with audited(
            self._db,
            actor,
            action,
            target_type="connector",
            target_id=connector,
            target_label=connector,
            org_id=None,
            detail={"enabled": enabled},
        ) as event:
            event.conn.execute(
                """
                INSERT INTO collab_connector_state (connector, enabled, updated_by)
                VALUES (%s, %s, %s)
                ON CONFLICT (connector) DO UPDATE
                   SET enabled = EXCLUDED.enabled,
                       updated_by = EXCLUDED.updated_by,
                       updated_at = now()
                """,
                (connector, enabled, actor.user),
            )
