"""The Track's three DDL copies agree: the hub's migrations, the Postgres store, and SQLite.

On the hub the Track tables come from the API's ``collab_`` migration registry;
``PostgresTrackStore._SCHEMA`` creates them for the standalone package, and
``SqliteTrackStore._SCHEMA`` is the same tables in SQLite's dialect. A released
migration is frozen by its checksum, so a change is a new migration plus the
same statements appended to ``_SCHEMA`` — and these tests fail if either half
is forgotten. The registry is read from the API's source, since this package
never imports the API.
"""

import ast
import re
import sqlite3
from pathlib import Path

from collab_hub_execution import PostgresClaimStore, PostgresTrackStore, SqliteClaimStore, SqliteTrackStore

REGISTRY = Path(__file__).resolve().parents[2] / "api" / "src" / "collab_hub_api" / "frames" / "collab_schema.py"


def _normalize(sql: str) -> str:
    return " ".join(sql.split())


def _registry_track_statements(prefix: str = "collab_track") -> list[str]:
    """Every statement of every migration that touches a table named by ``prefix``, in version order."""
    tree = ast.parse(REGISTRY.read_text())
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "COLLAB_SCHEMA_MIGRATIONS":
            migrations = ast.literal_eval(node.value)
            break
    else:  # pragma: no cover - the registry moved
        raise AssertionError(f"COLLAB_SCHEMA_MIGRATIONS not found in {REGISTRY}")
    return [
        _normalize(statement)
        for _, statements in sorted(migrations)
        for statement in statements
        if prefix in statement
    ]


def _postgres_columns(statements) -> dict[str, list[str]]:
    """Table name -> column names, from CREATE TABLE and ADD COLUMN statements."""
    tables: dict[str, list[str]] = {}
    for statement in map(_normalize, statements):
        created = re.match(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*)\)$", statement)
        if created:
            # Split on the commas between columns, not those inside a table constraint's parentheses.
            columns = [part.strip().split()[0] for part in re.split(r",(?![^()]*\))", created.group(2))]
            tables[created.group(1)] = [c for c in columns if c.upper() not in {"PRIMARY", "UNIQUE", "CHECK"}]
        added = re.match(r"ALTER TABLE (\w+) ADD COLUMN IF NOT EXISTS (\w+)", statement)
        if added and added.group(2) not in tables.get(added.group(1), []):
            tables.setdefault(added.group(1), []).append(added.group(2))
    return tables


def test_the_postgres_store_creates_exactly_what_the_hub_s_migrations_create():
    assert [_normalize(s) for s in PostgresTrackStore._SCHEMA] == _registry_track_statements()


def _sqlite_columns(path, like: str) -> dict[str, list[str]]:
    connection = sqlite3.connect(path)
    try:
        return {
            table: [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
            for (table,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE ?", (like,)
            )
        }
    finally:
        connection.close()


def test_the_sqlite_store_has_the_same_tables_and_columns(tmp_path):
    path = tmp_path / "track.sqlite"
    SqliteTrackStore.ensure_schema(path)
    assert _sqlite_columns(path, "collab_track%") == _postgres_columns(PostgresTrackStore._SCHEMA)


def test_the_claim_stores_create_exactly_what_the_hub_s_migrations_create(tmp_path):
    # The keyed claim's table (migration 19), held equal across the same three copies.
    assert [_normalize(s) for s in PostgresClaimStore._SCHEMA] == _registry_track_statements("collab_claims")
    path = tmp_path / "track.sqlite"
    SqliteClaimStore.ensure_schema(path)
    assert _sqlite_columns(path, "collab_claims") == _postgres_columns(PostgresClaimStore._SCHEMA)
