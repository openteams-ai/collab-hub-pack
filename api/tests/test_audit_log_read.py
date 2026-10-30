"""Reading the audit log.

The log has been written to since the operator foundation landed, and nothing
could read it except a database client -- so the record existed but could not
actually be used to reconstruct a change. This is the read path.

Paging is keyset, not offset: the table is append-only and read newest-first,
so "everything below this id" is both stable under concurrent writes and an
index seek rather than a count-and-skip.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

pytest.importorskip("psycopg")

from fake_audit_db import FakeAuditDatabase  # noqa: E402

from collab_hub_api.frames.audit_log import (  # noqa: E402
    MAX_PAGE_SIZE,
    AuditLogUnavailableError,
    PostgresAuditLog,
    UnavailableAuditLog,
)

AT = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)


def row(event_id: int, **overrides) -> dict:
    return {
        "id": event_id,
        "at": AT,
        "actor": "u-1",
        "actor_label": "alice@example.com",
        "action": "platform_role.grant",
        "target_type": "user",
        "target_id": "u-2",
        "target_label": "bob@example.com",
        "org_id": None,
        "detail": {"origin": "oidc"},
        **overrides,
    }


def test_the_newest_entries_come_back_first_with_a_cursor_for_the_next_page():
    db = FakeAuditDatabase(rows=[[row(9), row(8), row(7)]])
    log = PostgresAuditLog(db)

    page = log.list_events(limit=2)

    assert [entry.id for entry in page.entries] == [9, 8]
    assert page.next_before_id == 8


def test_the_last_page_offers_no_cursor():
    """Exactly `limit` rows means there is nothing below them."""

    db = FakeAuditDatabase(rows=[[row(2), row(1)]])
    log = PostgresAuditLog(db)

    page = log.list_events(limit=2)

    assert [entry.id for entry in page.entries] == [2, 1]
    assert page.next_before_id is None


def test_a_cursor_and_filters_reach_the_query_rather_than_python():
    """Filtering after the fetch would page over the wrong rows entirely."""

    db = FakeAuditDatabase(rows=[[row(5)]])
    log = PostgresAuditLog(db)

    log.list_events(limit=10, before_id=6, actor="u-1", action="platform_role.grant")

    (conn,) = db.connections
    sql, params = conn.statements[0]
    assert "id < %s" in sql and "actor = %s" in sql and "action = %s" in sql
    assert "ORDER BY id DESC" in sql
    assert params == (6, "u-1", "platform_role.grant", 11)


def test_the_page_size_is_capped_by_the_store_not_by_its_caller():
    db = FakeAuditDatabase(rows=[[]])
    log = PostgresAuditLog(db)

    log.list_events(limit=100_000)

    (conn,) = db.connections
    _sql, params = conn.statements[0]
    assert params == (MAX_PAGE_SIZE + 1,)


def test_a_deployment_without_a_database_says_so_rather_than_answering_empty():
    """ "Nothing was recorded" and "the record is unreachable" are opposites."""

    with pytest.raises(AuditLogUnavailableError):
        UnavailableAuditLog().list_events(limit=10)
