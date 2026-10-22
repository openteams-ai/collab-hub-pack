"""Tests for the identity rewrite (issue #68).

The properties worth pinning are the refusals, the boundaries, and the merges:
everything this tool does rests on an unverified mapping, so what it *declines*
to do is the safety story. The regressions in the "boundaries" and "manifest
truth" sections each reproduce a defect found by review at 65cffed3 — a symlink
followed out of the store, a manifest asserting a write that failed, and a
sweep rewriting ordinary content whose value happened to match.
"""

from __future__ import annotations

import json
import os

import pytest

from collab_hub_api.identity_inventory.readonly import UnsafePathError
from collab_hub_api.identity_rewrite import (
    CARRIER_COVERAGE,
    COMMITTED,
    FAILED,
    PENDING,
    Manifest,
    PlanRefusedError,
    build_plan,
    load_inventory,
    rewrite_json_document,
    write_private_file,
)
from collab_hub_api.identity_rewrite.writers import (
    EXCLUDED_CARRIERS,
    JSON_COLUMNS,
    merge_active_frames,
    merge_task_devices,
    merge_task_state,
    merge_usage_users,
    rewrite_local_sidecars,
    rewrite_s3_sidecars,
    rewrite_text_columns,
)

MAP = {"alice@example.com": "sub-alice"}


def report(**overrides) -> dict:
    payload = {
        "generated_at": "2026-08-15T00:00:00+00:00",
        "verdict": "needs_confirmation",
        "coverage_gaps": [],
        "orphans": [],
        "carriers": [{"id": "frame.owners"}],
        "principals": [
            {
                "principal": "alice@example.com",
                "status": "matched_email",
                "confidence": "unverified",
                "sub": "sub-alice",
                "occurrences": 3,
                "carriers": ["frame.owners"],
                "grants_access": True,
                "padded": False,
                "reassignment_suspected": False,
            }
        ],
    }
    payload.update(overrides)
    return payload


def write_report(tmp_path, payload) -> str:
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def sidecar_at(directory, **fields):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "metadata.json"
    path.write_text(json.dumps(fields), encoding="utf-8")
    return path


# --- refusals -----------------------------------------------------------------


def test_redacted_report_is_refused(tmp_path):
    """A --redact report describes real findings with fake values."""

    payload = report(
        principals=[{"principal": "principal:abc123def456", "status": "matched_email", "sub": "principal:999888"}]
    )
    with pytest.raises(PlanRefusedError, match="--redact"):
        load_inventory(write_report(tmp_path, payload))


def test_a_file_that_is_not_a_report_is_refused(tmp_path):
    path = tmp_path / "notes.json"
    path.write_text(json.dumps({"hello": "world"}), encoding="utf-8")
    with pytest.raises(PlanRefusedError, match="does not look like"):
        load_inventory(str(path))


@pytest.mark.parametrize(
    ("field", "value", "klass"),
    [
        ("status", "ambiguous", "ambiguous"),
        ("reassignment_suspected", True, "reassignment_suspected"),
        ("padded", True, "padded"),
    ],
)
def test_dangerous_classes_stop_the_run_until_acknowledged(field, value, klass):
    entry = dict(report()["principals"][0])
    entry[field] = value
    payload = report(principals=[entry])

    with pytest.raises(PlanRefusedError, match=klass):
        build_plan(payload)

    plan = build_plan(payload, acknowledge={klass})
    assert plan.mappings == ()
    assert any(item.principal == "alice@example.com" for item in plan.skipped)


def test_blocking_orphan_stops_the_run_until_acknowledged():
    payload = report(orphans=[{"kind": "no_live_owner", "blocking": True, "entity_id": "frame-1"}])
    with pytest.raises(PlanRefusedError, match="blocking_orphan"):
        build_plan(payload)
    assert build_plan(payload, acknowledge={"blocking_orphan"}).mappings


def test_unknown_acknowledgement_is_refused():
    with pytest.raises(PlanRefusedError, match="Unknown acknowledgement"):
        build_plan(report(), acknowledge={"whatever"})


def test_one_principal_mapping_to_two_subjects_is_refused_with_no_waiver():
    """The table is keyed on the principal, so one mapping would silently win."""

    payload = report(
        principals=[
            {"principal": "alice@example.com", "status": "matched_email", "sub": "sub-one"},
            {"principal": "alice@example.com", "status": "matched_username", "sub": "sub-two"},
        ]
    )
    with pytest.raises(PlanRefusedError, match="more than one subject"):
        build_plan(payload)
    # Not acknowledgeable: a broken report is not a finding to wave through.
    for klass in ("ambiguous", "reassignment_suspected", "padded", "blocking_orphan"):
        with pytest.raises(PlanRefusedError, match="more than one subject"):
            build_plan(payload, acknowledge={klass})


def test_unmapped_and_already_sub_are_left_alone():
    payload = report(
        principals=[
            {"principal": "ghost@example.com", "status": "unmapped", "sub": None},
            {"principal": "sub-bob", "status": "already_sub", "sub": "sub-bob"},
        ]
    )
    plan = build_plan(payload)
    assert plan.by_principal == {}
    assert {item.status for item in plan.skipped} == {"unmapped", "already_sub"}


def test_coverage_gaps_are_carried_into_the_plan_as_a_note():
    plan = build_plan(report(coverage_gaps=["nexus_task_devices: table does not exist"]))
    assert any("not a complete scan" in note for note in plan.notes)


def test_two_principals_collapsing_on_one_subject_is_reported():
    payload = report(
        principals=[
            {"principal": "alice@example.com", "status": "matched_email", "sub": "sub-alice"},
            {"principal": "alice", "status": "matched_username", "sub": "sub-alice"},
        ]
    )
    plan = build_plan(payload)
    assert plan.collapses == {"sub-alice": ["alice", "alice@example.com"]}
    assert any("merged" in note for note in plan.notes)


# --- substitution scope (review finding 3) ------------------------------------


def test_substring_matches_are_never_touched():
    document = {"created_by": "alice@example.com", "description": "ask alice@example.com about this"}
    updated, changes, declined = rewrite_json_document(document, MAP)
    assert updated["created_by"] == "sub-alice"
    assert updated["description"] == "ask alice@example.com about this"
    assert declined == []


@pytest.mark.parametrize("field", ["description", "name", "some_future_field", "title"])
def test_whole_value_match_outside_an_identity_field_is_reported_not_rewritten(field):
    """Exact match is not enough: the key has to be one identities live under."""

    document = {"owners": ["alice@example.com"], field: "alice@example.com"}
    updated, changes, declined = rewrite_json_document(document, MAP)

    assert updated["owners"] == ["sub-alice"], "the identity field is still migrated"
    assert updated[field] == "alice@example.com", "ordinary content must survive untouched"
    assert declined == [(f"${'.'}{field}", "alice@example.com")]
    assert [path for path, _, _ in changes] == ["$.owners[0]"]


def test_an_operator_can_allow_one_reviewed_path():
    document = {"legacy_author": "alice@example.com"}
    updated, changes, declined = rewrite_json_document(
        document, MAP, allow_paths=frozenset({"$.legacy_author"})
    )
    assert updated["legacy_author"] == "sub-alice"
    assert declined == []
    assert len(changes) == 1


def test_identity_keys_nested_in_lists_are_still_rewritten():
    document = {"suggestions": [{"submitted_by": "alice@example.com", "description": "alice@example.com"}]}
    updated, _changes, declined = rewrite_json_document(document, MAP)
    assert updated["suggestions"][0]["submitted_by"] == "sub-alice"
    assert updated["suggestions"][0]["description"] == "alice@example.com"
    assert len(declined) == 1


def test_unexpected_paths_reach_the_manifest(tmp_path):
    sidecar_at(tmp_path / "f1", owners=["alice@example.com"], description="alice@example.com")
    manifest = Manifest()
    rewrite_local_sidecars(tmp_path, MAP, manifest, apply=True)
    assert [item.path for item in manifest.unexpected_paths] == ["$.description"]
    assert json.loads((tmp_path / "f1" / "metadata.json").read_text())["description"] == "alice@example.com"


def test_unknown_fields_and_legacy_shapes_survive(tmp_path):
    """The sidecar is swept as a raw document, never through the model."""

    path = sidecar_at(
        tmp_path / "f1",
        id="f1",
        owner="alice@example.com",
        tags=["NOT A VALID TAG"],
        visibility="internal",
        readers=["carol@example.com"],
    )
    manifest = Manifest()
    rewrite_local_sidecars(tmp_path, MAP, manifest, apply=True)

    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["owner"] == "sub-alice"
    assert written["tags"] == ["NOT A VALID TAG"]
    assert written["readers"] == ["carol@example.com"]
    assert len(manifest.changes) == 1


def test_local_sidecars_are_found_below_an_org_directory(tmp_path):
    """#46 adds a directory level; a one-level walk would report a clean pass."""

    path = sidecar_at(tmp_path / "frames" / "nebari" / "f1", created_by="alice@example.com")
    rewrite_local_sidecars(tmp_path, MAP, Manifest(), apply=True)
    assert json.loads(path.read_text(encoding="utf-8"))["created_by"] == "sub-alice"


def test_dry_run_writes_nothing_but_records_everything(tmp_path):
    path = sidecar_at(tmp_path / "f1", created_by="alice@example.com")
    original = path.read_text(encoding="utf-8")
    manifest = Manifest()
    rewrite_local_sidecars(tmp_path, MAP, manifest, apply=False)
    assert path.read_text(encoding="utf-8") == original
    assert len(manifest.changes) == 1
    assert manifest.committed == []


# --- host boundaries (review finding 1) ---------------------------------------


def test_a_symlinked_sidecar_is_refused_not_followed(tmp_path):
    """A planted link otherwise rewrites a file outside the configured store."""

    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"created_by": "alice@example.com"}), encoding="utf-8")
    root = tmp_path / "frames"
    (root / "f1").mkdir(parents=True)
    os.symlink(outside, root / "f1" / "metadata.json")

    manifest = Manifest()
    rewrite_local_sidecars(root, MAP, manifest, apply=True)

    assert json.loads(outside.read_text(encoding="utf-8"))["created_by"] == "alice@example.com"
    assert manifest.changes == []
    assert any("not a regular file" in note for note in manifest.skipped_carriers)


def test_a_sidecar_write_is_atomic_and_preserves_mode(tmp_path):
    path = sidecar_at(tmp_path / "f1", created_by="alice@example.com")
    os.chmod(path, 0o640)
    rewrite_local_sidecars(tmp_path, MAP, Manifest(), apply=True)

    assert json.loads(path.read_text(encoding="utf-8"))["created_by"] == "sub-alice"
    assert oct(path.stat().st_mode & 0o777) == "0o640", "an unrelated mode change is a surprise"
    assert list(path.parent.glob(".*rewrite*")) == [], "no temporary file left behind"


def test_the_manifest_is_private_even_when_the_file_already_exists(tmp_path):
    """os.open applies its mode only on creation, so a 0644 file stayed 0644."""

    target = tmp_path / "manifest.json"
    target.write_text("stale", encoding="utf-8")
    os.chmod(target, 0o644)

    write_private_file(target, '{"applied": true}\n')

    assert oct(target.stat().st_mode & 0o777) == "0o600"
    assert json.loads(target.read_text(encoding="utf-8")) == {"applied": True}


def test_the_manifest_refuses_to_write_through_a_symlink(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me", encoding="utf-8")
    link = tmp_path / "manifest.json"
    os.symlink(victim, link)

    with pytest.raises(UnsafePathError):
        write_private_file(link, "clobber")
    assert victim.read_text(encoding="utf-8") == "keep me"


# --- manifest truth (review finding 2) ----------------------------------------


class FailingS3:
    """An object store that lists and reads, then fails the write."""

    def __init__(self, key, document):
        self._key = key
        self._document = document
        self.put_calls = 0

    def get_paginator(self, _name):
        outer = self

        class _P:
            def paginate(self, **_kwargs):
                return [{"Contents": [{"Key": outer._key}]}]

        return _P()

    def get_object(self, **_kwargs):
        class _B:
            def read(self_inner):
                return json.dumps(outer._document).encode("utf-8")

        outer = self
        return {"Body": _B()}

    def put_object(self, **_kwargs):
        self.put_calls += 1
        raise RuntimeError("object store said no")


def test_a_failed_object_write_is_not_recorded_as_applied():
    s3 = FailingS3("frames/f1/metadata.json", {"created_by": "alice@example.com"})
    manifest = Manifest(applied=True)

    with pytest.raises(RuntimeError, match="object store said no"):
        rewrite_s3_sidecars(s3, "bucket", "frames", MAP, manifest, apply=True)

    assert s3.put_calls == 1
    assert manifest.committed == [], "the reversal record must not claim a write that failed"
    assert [item.state for item in manifest.changes] == [FAILED]
    assert manifest.errors and "object store said no" in manifest.errors[0]
    payload = json.loads(manifest.to_json())
    assert payload["committed_changes"] == 0
    assert payload["total_changes"] == 1


def test_a_failed_local_write_is_not_recorded_as_applied(tmp_path, monkeypatch):
    sidecar_at(tmp_path / "f1", created_by="alice@example.com")
    monkeypatch.setattr(
        "collab_hub_api.identity_rewrite.writers._replace_atomically",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("disk full")),
    )
    manifest = Manifest(applied=True)
    with pytest.raises(OSError, match="disk full"):
        rewrite_local_sidecars(tmp_path, MAP, manifest, apply=True)
    assert manifest.committed == []
    assert [item.state for item in manifest.changes] == [FAILED]


# --- Postgres carriers --------------------------------------------------------


def _row(user_id: str, **fields) -> dict:
    return {"org_id": "nebari", "workspace_id": "default", "user_id": user_id, **fields}


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class FakeConn:
    """Enough of a psycopg connection to pin the SQL each carrier issues."""

    def __init__(self, tables: dict[str, list[dict]], key="user_id"):
        self.tables = tables
        self.key = key
        self.executed: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        flat = " ".join(sql.split())
        self.executed.append((flat, params))
        if "to_regclass" in flat:
            return FakeCursor([{"reg": params[0] if params[0] in self.tables else None}])
        for table, rows in self.tables.items():
            if table not in flat or not flat.startswith("SELECT"):
                continue
            cols = _where_columns(flat)
            matched = [row for row in rows if all(row.get(c) == v for c, v in zip(cols, params))]
            return FakeCursor(matched)
        return FakeCursor([])


def _where_columns(sql: str) -> list[str]:
    tail = sql.split("WHERE", 1)[1] if "WHERE" in sql else ""
    return [part.strip().split()[0] for part in tail.split("AND") if "=" in part]


def test_text_carriers_name_the_rows_they_changed_not_a_count():
    conn = FakeConn({"frames_server_history": [{"id": "h1", "actor": "alice@example.com"},
                                               {"id": "h2", "actor": "alice@example.com"}]})
    manifest = Manifest(applied=True)
    rewrite_text_columns(conn, MAP, manifest, apply=True)

    entities = [item.entity for item in manifest.changes if item.carrier == "history.actor"]
    assert entities == ["frames_server_history:h1", "frames_server_history:h2"]
    assert all(item.state == PENDING for item in manifest.changes), "database changes await the commit"
    manifest.promote_pending()
    assert all(item.state == COMMITTED for item in manifest.committed)


def test_active_frames_merge_unions_frame_ids():
    conn = FakeConn(
        {
            "frames_server_active_frames": [
                _row("alice@example.com", frame_ids=["f1", "f2"], updated_at=1),
                _row("sub-alice", frame_ids=["f2", "f3"], updated_at=2),
            ]
        }
    )
    manifest = Manifest(applied=True)
    merge_active_frames(conn, MAP, manifest, apply=True)

    merged = next(params[0] for sql, params in conn.executed if sql.startswith("UPDATE") and "frame_ids" in sql)
    assert sorted(json.loads(merged)) == ["f1", "f2", "f3"]
    assert any(sql.startswith("DELETE") for sql, _ in conn.executed)
    assert any("unioned" in note for note in manifest.merges)
    manifest.promote_pending()
    assert manifest.committed


def test_active_frames_without_collision_is_a_plain_rename():
    conn = FakeConn({"frames_server_active_frames": [_row("alice@example.com", frame_ids=["f1"], updated_at=1)]})
    manifest = Manifest(applied=True)
    merge_active_frames(conn, MAP, manifest, apply=True)
    assert not any(sql.startswith("DELETE") for sql, _ in conn.executed)
    assert manifest.merges == []


def test_usage_users_merge_widens_the_seen_window():
    conn = FakeConn(
        {
            "frames_server_usage_users": [
                _row("alice@example.com", email="a@x", first_seen=1, last_seen=5),
                _row("sub-alice", email=None, first_seen=3, last_seen=4),
            ]
        }
    )
    merge_usage_users(conn, MAP, Manifest(applied=True), apply=True)
    assert any("LEAST" in sql and "GREATEST" in sql for sql, _ in conn.executed)
    assert any(sql.startswith("DELETE") for sql, _ in conn.executed)


def test_task_state_merge_keeps_the_newer_payload():
    """The legacy row is newer, so its payload wins and the row is dropped."""

    conn = FakeConn(
        {
            "nexus_task_state": [
                {"org_id": "nebari", "workspace_id": "default", "owner_id": "alice@example.com",
                 "payload": {"a": 1}, "updated_at": 9},
                {"org_id": "nebari", "workspace_id": "default", "owner_id": "sub-alice",
                 "payload": {"a": 0}, "updated_at": 2},
            ]
        }
    )
    manifest = Manifest(applied=True)
    merge_task_state(conn, MAP, manifest, apply=True)

    payload_update = [params for sql, params in conn.executed if sql.startswith("UPDATE") and "payload" in sql]
    assert payload_update, "the newer legacy payload must be carried onto the subject"
    assert json.loads(payload_update[0][0]) == {"a": 1}
    assert any(sql.startswith("DELETE") for sql, _ in conn.executed)
    assert any("kept the legacy" in note for note in manifest.merges)


def test_task_state_merge_keeps_the_existing_subject_when_it_is_newer():
    conn = FakeConn(
        {
            "nexus_task_state": [
                {"org_id": "nebari", "workspace_id": "default", "owner_id": "alice@example.com",
                 "payload": {"a": 1}, "updated_at": 1},
                {"org_id": "nebari", "workspace_id": "default", "owner_id": "sub-alice",
                 "payload": {"a": 0}, "updated_at": 7},
            ]
        }
    )
    manifest = Manifest(applied=True)
    merge_task_state(conn, MAP, manifest, apply=True)
    assert not any(sql.startswith("UPDATE") and "payload" in sql for sql, _ in conn.executed)
    assert any("existing subject" in note for note in manifest.merges)


def test_usage_users_email_column_is_never_rewritten():
    assert "usage_users.email" in EXCLUDED_CARRIERS


def test_missing_table_is_recorded_not_fatal():
    manifest = Manifest()
    rewrite_text_columns(FakeConn({}), MAP, manifest, apply=True)
    assert any("does not exist" in note for note in manifest.skipped_carriers)


# --- manifest and idempotence -------------------------------------------------


def test_manifest_records_before_and_after_for_reversal(tmp_path):
    sidecar_at(tmp_path / "f1", created_by="alice@example.com")
    manifest = Manifest(applied=True)
    rewrite_local_sidecars(tmp_path, MAP, manifest, apply=True)

    payload = json.loads(manifest.to_json())
    assert payload["applied"] is True
    assert payload["committed_changes"] == 1
    change = payload["changes"][0]
    assert (change["before"], change["after"], change["state"]) == ("alice@example.com", "sub-alice", COMMITTED)
    assert change["location"] == "$.created_by"


def test_rerunning_a_completed_rewrite_is_a_no_op(tmp_path):
    """Idempotence is why there is no progress ledger."""

    sidecar_at(tmp_path / "f1", created_by="alice@example.com")
    first, second = Manifest(applied=True), Manifest(applied=True)
    rewrite_local_sidecars(tmp_path, MAP, first, apply=True)
    rewrite_local_sidecars(tmp_path, MAP, second, apply=True)
    assert len(first.committed) == 1
    assert second.changes == []


# --- carrier parity (re-review finding 1) -------------------------------------


def test_every_inventoried_carrier_has_a_declared_writer():
    """The two tools must not drift.

    A carrier the inventory learns to *find* and this tool does not know how to
    *write* is a silent half-migration: a task row whose primary key moved while
    the identity inside its payload stayed legacy. Adding a carrier upstream
    should break this test, not the migration.
    """

    from collab_hub_api.identity_inventory.scan import CARRIERS

    inventoried = {carrier.id for carrier in CARRIERS}
    missing = sorted(inventoried - set(CARRIER_COVERAGE))
    assert missing == [], f"carriers with no declared writer: {missing}"
    stale = sorted(set(CARRIER_COVERAGE) - inventoried)
    assert stale == [], f"declared coverage for carriers the inventory no longer reports: {stale}"


def test_the_task_payload_carriers_are_actually_wired():
    """Declaring coverage is not the same as having a writer."""

    wired = {carrier for carrier, *_rest in JSON_COLUMNS}
    assert {"task_state.payload", "task_devices.payload"} <= wired


def test_task_device_rows_move_and_collide_on_the_same_device():
    conn = FakeConn(
        {
            "nexus_task_devices": [
                _row("alice@example.com", device_id="d1", payload={"u": "alice@example.com"},
                     last_seen_at=9, expires_at=99),
                _row("sub-alice", device_id="d1", payload={"u": "sub-alice"}, last_seen_at=2, expires_at=20),
            ]
        }
    )
    manifest = Manifest(applied=True)
    merge_task_devices(conn, MAP, manifest, apply=True)

    assert any(sql.startswith("UPDATE") and "payload" in sql for sql, _ in conn.executed)
    assert any(sql.startswith("DELETE") for sql, _ in conn.executed)
    assert any("kept the legacy" in note for note in manifest.merges)
    staged = [item for item in manifest.changes if item.carrier == "task_devices.user_id"]
    assert staged and staged[0].state == PENDING, "a database change is pending until the transaction commits"
    assert staged[0].before_image["kept"] == "legacy"


def test_task_device_rows_without_collision_are_a_plain_rename():
    conn = FakeConn(
        {"nexus_task_devices": [_row("alice@example.com", device_id="d1", payload={}, last_seen_at=1, expires_at=9)]}
    )
    manifest = Manifest(applied=True)
    merge_task_devices(conn, MAP, manifest, apply=True)
    assert not any(sql.startswith("DELETE") for sql, _ in conn.executed)
    assert manifest.merges == []


# --- manifest as a recovery record (re-review finding 2) ----------------------


def test_database_changes_stay_pending_until_the_transaction_commits():
    conn = FakeConn({"frames_server_history": [{"id": "h1", "actor": "alice@example.com"}]})
    manifest = Manifest(applied=True)
    rewrite_text_columns(conn, MAP, manifest, apply=True)

    assert [item.state for item in manifest.changes] == [PENDING]
    assert manifest.committed == [], "a statement running is not a commit"

    manifest.promote_pending()
    assert [item.state for item in manifest.changes] == [COMMITTED]


def test_a_rollback_demotes_staged_changes_to_failed():
    """Reproduces transaction_rolled_back=true with committed_changes=1."""

    conn = FakeConn({"frames_server_history": [{"id": "h1", "actor": "alice@example.com"}]})
    manifest = Manifest(applied=True)
    rewrite_text_columns(conn, MAP, manifest, apply=True)

    manifest.fail_pending("transaction rolled back: RuntimeError: later carrier blew up")

    payload = json.loads(manifest.to_json())
    assert payload["committed_changes"] == 0
    assert payload["transaction_rolled_back"] is True
    assert [item.state for item in manifest.changes] == [FAILED]


def test_file_writes_commit_on_their_own_not_via_the_transaction(tmp_path):
    """A sidecar write is its own commit, so it must not sit pending forever."""

    sidecar_at(tmp_path / "f1", created_by="alice@example.com")
    manifest = Manifest(applied=True)
    rewrite_local_sidecars(tmp_path, MAP, manifest, apply=True)
    assert [item.state for item in manifest.changes] == [COMMITTED]


def test_collision_merges_retain_a_before_image():
    conn = FakeConn(
        {
            "frames_server_active_frames": [
                _row("alice@example.com", frame_ids=["f1", "f2"], updated_at=1),
                _row("sub-alice", frame_ids=["f2", "f3"], updated_at=2),
            ]
        }
    )
    manifest = Manifest(applied=True)
    merge_active_frames(conn, MAP, manifest, apply=True)

    image = manifest.changes[0].before_image
    assert image == {"legacy_frame_ids": ["f1", "f2"], "subject_frame_ids": ["f2", "f3"]}
    assert json.loads(manifest.to_json())["changes"][0]["before_image"] == image


def test_task_state_before_image_keeps_the_overwritten_subject_payload():
    """The legacy row is newer, so the subject's payload is destroyed by the merge."""

    conn = FakeConn(
        {
            "nexus_task_state": [
                {"org_id": "nebari", "workspace_id": "default", "owner_id": "alice@example.com",
                 "payload": {"legacy": True}, "updated_at": 9},
                {"org_id": "nebari", "workspace_id": "default", "owner_id": "sub-alice",
                 "payload": {"subject": True}, "updated_at": 2},
            ]
        }
    )
    manifest = Manifest(applied=True)
    merge_task_state(conn, MAP, manifest, apply=True)

    image = manifest.changes[0].before_image
    assert image["kept"] == "legacy"
    assert image["subject"]["payload"] == {"subject": True}, "the overwritten payload must survive in the record"
    assert image["legacy"]["payload"] == {"legacy": True}


def test_task_device_before_image_keeps_the_overwritten_subject_registration():
    conn = FakeConn(
        {
            "nexus_task_devices": [
                _row("alice@example.com", device_id="d1", payload={"legacy": True},
                     last_seen_at=9, expires_at=99),
                _row("sub-alice", device_id="d1", payload={"subject": True},
                     last_seen_at=2, expires_at=20),
            ]
        }
    )
    manifest = Manifest(applied=True)
    merge_task_devices(conn, MAP, manifest, apply=True)

    image = manifest.changes[0].before_image
    assert image["kept"] == "legacy"
    assert image["subject"]["payload"] == {"subject": True}
    assert image["subject"]["expires_at"] == 20, "the overwritten expiry must survive too"


def test_usage_merge_before_image_keeps_both_sides_timestamps():
    """The merge widens both bounds, so both rows' originals are needed."""

    conn = FakeConn(
        {
            "frames_server_usage_users": [
                _row("alice@example.com", email="a@x", first_seen=1, last_seen=5),
                _row("sub-alice", email=None, first_seen=3, last_seen=4),
            ]
        }
    )
    manifest = Manifest(applied=True)
    merge_usage_users(conn, MAP, manifest, apply=True)

    image = manifest.changes[0].before_image
    assert image["legacy"] == {"email": "a@x", "first_seen": 1, "last_seen": 5}
    assert image["subject"] == {"email": None, "first_seen": 3, "last_seen": 4}


def test_usage_merge_before_image_keeps_the_timestamps_it_widened():
    conn = FakeConn(
        {
            "frames_server_usage_users": [
                _row("alice@example.com", email="a@x", first_seen=1, last_seen=5),
                _row("sub-alice", email=None, first_seen=3, last_seen=4),
            ]
        }
    )
    manifest = Manifest(applied=True)
    merge_usage_users(conn, MAP, manifest, apply=True)
    image = manifest.changes[0].before_image
    assert image["legacy"] == {"email": "a@x", "first_seen": 1, "last_seen": 5}


def test_task_state_before_image_keeps_the_discarded_payload():
    conn = FakeConn(
        {
            "nexus_task_state": [
                {"org_id": "nebari", "workspace_id": "default", "owner_id": "alice@example.com",
                 "payload": {"a": 1}, "updated_at": 1},
                {"org_id": "nebari", "workspace_id": "default", "owner_id": "sub-alice",
                 "payload": {"a": 0}, "updated_at": 7},
            ]
        }
    )
    manifest = Manifest(applied=True)
    merge_task_state(conn, MAP, manifest, apply=True)
    image = manifest.changes[0].before_image
    assert image["legacy"]["payload"] == {"a": 1}
    assert image["kept"] == "subject"


def test_an_unsafe_manifest_destination_is_refused_before_any_data_write(tmp_path, monkeypatch):
    """Reproduces: the sidecar changed, then UnsafePathError, and no manifest."""

    from collab_hub_api.identity_rewrite import cli

    root = tmp_path / "frames"
    sidecar = sidecar_at(root / "f1", created_by="alice@example.com")
    original = sidecar.read_text(encoding="utf-8")

    victim = tmp_path / "victim.txt"
    victim.write_text("keep me", encoding="utf-8")
    link = tmp_path / "manifest.json"
    os.symlink(victim, link)

    inventory = write_report(tmp_path, report())
    monkeypatch.setenv("COLLAB_HUB_API__STORAGE__FRAMES_PATH", str(root))

    code = cli.main(
        ["--inventory", inventory, "--frames-backend", "local", "--skip-postgres", "--apply",
         "--manifest", str(link)]
    )

    assert code == 1
    assert sidecar.read_text(encoding="utf-8") == original, "nothing may be written before the record can be"
    assert victim.read_text(encoding="utf-8") == "keep me"


# --- the contract the manifest does not claim (re-review 3) -------------------


def test_an_abrupt_termination_can_leave_a_committed_write_unrecorded(tmp_path, monkeypatch):
    """Reproduces the reviewer's probe, and pins it as a *documented* limit.

    The sidecar write commits on return; the manifest is written at termination.
    Killing the run in between leaves the store changed and the reserved manifest
    still empty. That is why the manifest is documented as a diagnostic rather
    than the authoritative rollback, and why the runbook takes a snapshot first.
    """

    from collab_hub_api.identity_rewrite import cli, writers

    root = tmp_path / "frames"
    sidecar = sidecar_at(root / "f1", created_by="alice@example.com")
    manifest_path = tmp_path / "manifest.json"
    inventory = write_report(tmp_path, report())
    monkeypatch.setenv("COLLAB_HUB_API__STORAGE__FRAMES_PATH", str(root))

    real_commit = writers.Manifest.commit

    def commit_then_die(self, pending):
        real_commit(self, pending)
        raise KeyboardInterrupt("operator pressed ctrl-c")

    monkeypatch.setattr(writers.Manifest, "commit", commit_then_die)

    with pytest.raises(KeyboardInterrupt):
        cli.main(
            ["--inventory", inventory, "--frames-backend", "local", "--skip-postgres", "--apply",
             "--manifest", str(manifest_path)]
        )

    # The store changed...
    assert json.loads(sidecar.read_text(encoding="utf-8"))["created_by"] == "sub-alice"
    # ...and the reservation written at startup is all the manifest holds.
    reserved = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert reserved["committed_changes"] == 0
    assert reserved["changes"] == []


def test_the_documented_contract_says_the_manifest_is_not_the_rollback():
    """The claim in the source has to match the one in the docs and the runbook.

    Pinned as a test because the previous revision's defect was exactly a
    mismatch: the PR comment called it a diagnostic while the module still
    called it the reversal artifact.
    """

    import collab_hub_api.identity_rewrite as package
    from collab_hub_api.identity_rewrite import cli as cli_module

    for doc in (package.__doc__, cli_module.__doc__):
        assert doc is not None
        lowered = " ".join(doc.split()).lower()
        assert "not the authoritative rollback" in lowered
        assert "backup/restore" in lowered or "snapshot" in lowered


# --- identity_root: columns that ARE the identity list -------------------------


def test_a_bare_array_column_is_rewritten_when_it_is_the_identity_list():
    """frames_server_groups.owners stores ["alice@x"], not {"owners": [...]}.

    Its elements sit at $[0] with no key above them, so a rule that decides by
    key name declines them — which migrates a group's created_by while leaving
    its owners legacy, i.e. a group its owners can no longer manage. Found by the
    first dry run against real data, on 4 groups.
    """

    document = ["alice@example.com", "bob"]
    updated, changes, declined = rewrite_json_document(document, MAP, identity_root=True)

    assert updated == ["sub-alice", "bob"]
    assert [path for path, _, _ in changes] == ["$[0]"]
    assert declined == []


def test_a_bare_array_is_declined_without_identity_root():
    """The default must stay strict: a payload array is not an ACL list."""

    updated, changes, declined = rewrite_json_document(["alice@example.com"], MAP)
    assert updated == ["alice@example.com"]
    assert changes == []
    assert declined == [("$[0]", "alice@example.com")]


def test_identity_root_does_not_make_every_nested_key_an_identity():
    """Only the root list is implied; nested keys are still judged on their name."""

    document = [{"submitted_by": "alice@example.com", "description": "alice@example.com"}]
    updated, changes, declined = rewrite_json_document(document, MAP, identity_root=True)

    assert updated[0]["submitted_by"] == "sub-alice"
    assert updated[0]["description"] == "alice@example.com"
    assert [p for p, _ in declined] == ["$[0].description"]


def test_only_the_acl_list_column_is_declared_an_identity_root():
    """payload/detail columns must stay key-gated, which the last review established."""

    roots = {carrier for carrier, _t, _c, _k, is_root in JSON_COLUMNS if is_root}
    assert roots == {"group.owners"}


def test_group_owners_column_substitutes_a_bare_array_end_to_end():
    conn = FakeConn({"frames_server_groups": [{"id": "g1", "owners": ["alice@example.com"]}]})
    manifest = Manifest(applied=True)
    from collab_hub_api.identity_rewrite.writers import rewrite_json_columns

    rewrite_json_columns(conn, MAP, manifest, apply=True)

    owners_changes = [c for c in manifest.changes if c.carrier == "group.owners"]
    assert owners_changes, "the owners array must be migrated, not reported as unexpected"
    assert owners_changes[0].location == "$[0]"
    assert manifest.unexpected_paths == []
    written = next(params[0] for sql, params in conn.executed if sql.startswith("UPDATE") and "owners" in sql)
    assert json.loads(written) == ["sub-alice"]
