"""Tests for the read-only identity inventory (issue #65).

There is no internal-hub data and no Keycloak here, so the fixtures encode the
cases that decide whether the report can be trusted: a principal that maps, one
that does not, one that maps to *two* people, one whose account was created
after the record it would be mapped into (a reassigned address), a padded
string the service would never match, a frame nobody can reach afterwards, a
frame that only looks like one, identity strings buried in jsonb, and an
account that exists but owns nothing.

The verdict itself is tested as hard as the findings are: a scan that skipped a
source or hit an unreadable record must never report "clear", and a mapping
that rests on a mutable claim must never clear an entity on its own.

The read-only guarantees are tested as guarantees — the botocore hook refuses a
write operation, the SQL guard refuses multi-statement and CTE-wrapped writes,
and a clean interpreter importing the package does not pull in a single module
that can write.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from collab_hub_api.identity_inventory import (
    VERDICT_BLOCKED,
    VERDICT_CLEAR,
    VERDICT_INCOMPLETE,
    VERDICT_NEEDS_CONFIRMATION,
    DirectoryIndex,
    DirectoryUser,
    MappingConfidence,
    ResolutionStatus,
    analyze,
    render_json,
    render_markdown,
)
from collab_hub_api.identity_inventory.analysis import (
    ORPHAN_DISABLED,
    ORPHAN_NO_OWNERS,
    ORPHAN_REASSIGNED,
    ORPHAN_UNMAPPED,
    ORPHAN_UNVERIFIED_ONLY,
)
from collab_hub_api.identity_inventory.directory import load_directory_from_json
from collab_hub_api.identity_inventory.readonly import (
    ReadOnlyLocalFrames,
    ReadOnlyViolationError,
    UnsafePathError,
    _require_select,
    redact_database_url,
    reject_write_operations,
)
from collab_hub_api.identity_inventory.report import pseudonym
from collab_hub_api.identity_inventory.scan import (
    Occurrence,
    ScanResult,
    SourceCoverage,
    scan_frame_sidecar,
    scan_local_frames,
    walk_json_identities,
)

# --- fixtures: the hard cases ------------------------------------------------

LONG_AGO = datetime(2020, 1, 1, tzinfo=UTC)
RECORD_WRITTEN = datetime(2024, 6, 1, tzinfo=UTC)
AFTER_THE_RECORD = datetime(2025, 1, 1, tzinfo=UTC)

ALICE = DirectoryUser(
    sub="11111111-1111-1111-1111-111111111111",
    username="alice",
    email="alice@example.com",
    created_at=LONG_AGO,
)
BOB = DirectoryUser(
    sub="22222222-2222-2222-2222-222222222222",
    username="bob",
    email="bob@example.com",
    created_at=LONG_AGO,
)
# The collision: Carol's *username* is the string that is Dave's *email*.
CAROL = DirectoryUser(
    sub="33333333-3333-3333-3333-333333333333",
    username="dave@example.com",
    email="carol@example.com",
    created_at=LONG_AGO,
)
DAVE = DirectoryUser(
    sub="44444444-4444-4444-4444-444444444444",
    username="dave",
    email="dave@example.com",
    created_at=LONG_AGO,
)
RETIRED = DirectoryUser(
    sub="55555555-5555-5555-5555-555555555555",
    username="retired",
    email="retired@example.com",
    enabled=False,
    created_at=LONG_AGO,
)
NEVER_SEEN = DirectoryUser(
    sub="66666666-6666-6666-6666-666666666666",
    username="ghost",
    email="ghost@example.com",
    created_at=LONG_AGO,
)
# The dangerous one: this account was created *after* the records that name
# "reused@example.com" were written, so it inherited a released address.
NEWCOMER = DirectoryUser(
    sub="77777777-7777-7777-7777-777777777777",
    username="newcomer",
    email="reused@example.com",
    created_at=AFTER_THE_RECORD,
)


@pytest.fixture
def index() -> DirectoryIndex:
    return DirectoryIndex([ALICE, BOB, CAROL, DAVE, RETIRED, NEVER_SEEN, NEWCOMER])


def sidecar(
    frame_id: str,
    *,
    owners: list[str] | None = None,
    created_by: str = "",
    readers: list[str] | None = None,
    suggestions: list[dict] | None = None,
    name: str = "Frame",
    written_at: datetime = RECORD_WRITTEN,
    extra: dict | None = None,
) -> dict:
    owner_list = [] if owners is None else owners
    payload = {
        "schema_version": 1,
        "id": frame_id,
        "org_id": "org",
        "workspace_id": "default",
        "name": name,
        "created_by": created_by or (owner_list[0] if owner_list else ""),
        "owners": owner_list,
        "readers": readers or [],
        "suggestions": suggestions or [],
        "created_at": written_at.isoformat(),
        "updated_at": written_at.isoformat(),
    }
    payload.update(extra or {})
    return payload


def owned_by_a_certain_subject(frame_id: str = "certain") -> dict:
    """A frame owned by a stored *subject* — the only thing that clears an entity."""

    return sidecar(frame_id, owners=[ALICE.sub])


def scan_of(frames: list[dict], *, complete: bool = True) -> ScanResult:
    result = ScanResult()
    for metadata in frames:
        scan_frame_sidecar(
            result,
            metadata,
            source="fixture",
            origin=f"fixture://{metadata['id']}",
            fallback_frame_id=metadata["id"],
        )
    if complete:
        # Coverage gates the verdict, so a fixture that wants to exercise the
        # findings has to claim a complete scan explicitly.
        result.coverage.append(SourceCoverage("fixture", True, f"{len(frames)} sidecars"))
    return result


# --- mapping ------------------------------------------------------------------


def test_email_shaped_principal_maps_to_its_subject(index: DirectoryIndex) -> None:
    resolution = index.resolve("alice@example.com")
    assert resolution.status is ResolutionStatus.matched_email
    assert resolution.sub == ALICE.sub
    assert resolution.mapped and resolution.live


def test_username_principal_maps_to_its_subject(index: DirectoryIndex) -> None:
    assert index.resolve("bob").sub == BOB.sub
    assert index.resolve("bob").status is ResolutionStatus.matched_username


def test_email_shaped_principal_that_matches_nobody_is_unmapped(index: DirectoryIndex) -> None:
    resolution = index.resolve("contractor@partner.example")
    assert resolution.status is ResolutionStatus.unmapped
    assert resolution.sub is None


def test_username_colliding_with_another_users_email_is_ambiguous(index: DirectoryIndex) -> None:
    # "dave@example.com" is Dave's email *and* Carol's username. Two different
    # people are equally good readings, so the mapping refuses.
    resolution = index.resolve("dave@example.com")
    assert resolution.status is ResolutionStatus.ambiguous
    assert {user.sub for user in resolution.candidates} == {CAROL.sub, DAVE.sub}
    assert resolution.sub is None


def test_a_stored_subject_is_recognised_and_needs_no_rewrite(index: DirectoryIndex) -> None:
    assert index.resolve(ALICE.sub).status is ResolutionStatus.already_sub


def test_matching_is_case_insensitive(index: DirectoryIndex) -> None:
    assert index.resolve("ALICE@Example.COM").sub == ALICE.sub


def test_blank_principal_is_reported_not_mapped(index: DirectoryIndex) -> None:
    assert index.resolve("   ").status is ResolutionStatus.empty


def test_duplicate_directory_emails_are_surfaced() -> None:
    twin = DirectoryUser(sub="99999999", username="twin", email="alice@example.com")
    duplicated = DirectoryIndex([ALICE, twin])
    assert duplicated.duplicate_emails == ["alice@example.com"]
    assert duplicated.resolve("alice@example.com").status is ResolutionStatus.ambiguous


# --- orphan check --------------------------------------------------------------


def test_frame_with_no_mapping_owner_is_an_orphan(index: DirectoryIndex) -> None:
    scan = scan_of([sidecar("f1", owners=["gone@example.com", "also-gone@example.com"])])
    analysis = analyze(scan, index)
    assert [item.kind for item in analysis.orphans] == [ORPHAN_UNMAPPED]
    assert analysis.orphans[0].entity_id == "f1"
    assert analysis.blocking_orphans
    assert analysis.verdict == VERDICT_BLOCKED
    assert not analysis.clear_to_proceed


def test_frame_with_one_certain_owner_is_not_an_orphan(index: DirectoryIndex) -> None:
    # The surviving owner is stored as a *subject*: the only mapping that can
    # clear an entity by itself.
    scan = scan_of([sidecar("f2", owners=["gone@example.com", ALICE.sub])])
    analysis = analyze(scan, index)
    assert analysis.orphans == []
    assert analysis.verdict == VERDICT_CLEAR
    assert analysis.clear_to_proceed


def test_frame_whose_only_mapping_owner_is_unverified_needs_confirmation(index: DirectoryIndex) -> None:
    """BLOCKER 5(b): a unique email match is a proposal, not a clearance.

    ``alice@example.com`` resolves to exactly one live account today. That is
    not evidence the address always meant her, so the frame is neither clear
    nor orphaned — a person has to confirm it.
    """

    scan = scan_of([sidecar("f2b", owners=["gone@example.com", "alice@example.com"])])
    analysis = analyze(scan, index)
    assert [item.kind for item in analysis.orphans] == [ORPHAN_UNVERIFIED_ONLY]
    assert not analysis.blocking_orphans
    assert analysis.verdict == VERDICT_NEEDS_CONFIRMATION
    assert not analysis.clear_to_proceed


def test_owner_account_created_after_the_record_is_not_a_mapping(index: DirectoryIndex) -> None:
    """BLOCKER 5(c): the reassignment fingerprint discards the mapping.

    ``reused@example.com`` matches exactly one account — but that account was
    created after the frame was last written, so it cannot be the person the
    owner list named. Migrating on it would hand a stranger someone else's
    frame.
    """

    scan = scan_of([sidecar("f2c", owners=["reused@example.com"])])
    analysis = analyze(scan, index)
    assert [item.kind for item in analysis.orphans] == [ORPHAN_REASSIGNED]
    assert analysis.blocking_orphans
    assert analysis.verdict == VERDICT_BLOCKED
    suspect = analysis.reassignment_suspects[0]
    assert suspect.principal == "reused@example.com"
    assert suspect.confidence is MappingConfidence.none
    assert not suspect.usable_mapping
    report = render_markdown(analysis)
    assert "Suspected address reassignment" in report
    assert "ACCOUNT NEWER THAN RECORD" in report


def test_an_older_account_keeping_its_address_is_not_flagged(index: DirectoryIndex) -> None:
    scan = scan_of([sidecar("f2d", owners=["alice@example.com"])])
    analysis = analyze(scan, index)
    assert analysis.reassignment_suspects == []
    assert [item.kind for item in analysis.orphans] == [ORPHAN_UNVERIFIED_ONLY]


def test_frame_owned_only_by_a_disabled_account_is_its_own_severity(index: DirectoryIndex) -> None:
    scan = scan_of([sidecar("f3", owners=["retired@example.com"])])
    analysis = analyze(scan, index)
    assert [item.kind for item in analysis.orphans] == [ORPHAN_DISABLED]
    # Not blocking: the migration would carry it correctly. Still non-clear,
    # because nobody enabled can manage the frame.
    assert not analysis.blocking_orphans
    assert analysis.verdict == VERDICT_NEEDS_CONFIRMATION


def test_frame_with_no_owners_at_all_is_reported(index: DirectoryIndex) -> None:
    scan = scan_of([sidecar("f4", owners=[], created_by="alice@example.com")])
    analysis = analyze(scan, index)
    assert [item.kind for item in analysis.orphans] == [ORPHAN_NO_OWNERS]


def test_legacy_owner_scalar_is_promoted_only_when_the_owners_key_is_absent(index: DirectoryIndex) -> None:
    """BLOCKER 3: match ``frames.codec.normalize_metadata`` exactly.

    The service promotes the scalar only when there is no ``owners`` key at
    all. Reading it more generously would clear a frame the service already
    treats as ownerless.
    """

    legacy = {"id": "f5", "org_id": "org", "workspace_id": "default", "name": "Legacy", "owner": ALICE.sub}
    analysis = analyze(scan_of([legacy]), index)
    assert analysis.orphans == []
    assert any(item.carrier == "frame.legacy_owner" for item in analysis.scan.occurrences)


def test_empty_owners_list_beside_a_legacy_owner_is_ownerless(index: DirectoryIndex) -> None:
    contradictory = {
        "id": "f5b",
        "org_id": "org",
        "workspace_id": "default",
        "name": "Contradictory",
        "owners": [],
        "owner": ALICE.sub,
    }
    analysis = analyze(scan_of([contradictory]), index)
    assert [item.kind for item in analysis.orphans] == [ORPHAN_NO_OWNERS]
    # The divergence between the two readings is itself reported.
    assert analysis.scan.data_notes and "ownerless" in analysis.scan.data_notes[0]
    assert analysis.verdict == VERDICT_BLOCKED
    assert "Stored-data oddities" in render_markdown(analysis)


def test_group_with_no_mapping_owner_is_an_orphan(index: DirectoryIndex) -> None:
    from collab_hub_api.identity_inventory.scan import GroupRecord

    scan = ScanResult()
    scan.groups.append(
        GroupRecord(
            entity_type="group",
            entity_id="g1",
            org_id="org",
            workspace_id="default",
            name="Team pack",
            owners=["departed@example.com"],
            created_by="departed@example.com",
            origin="frames_server_groups",
        )
    )
    analysis = analyze(scan, index)
    assert [(item.entity_type, item.kind) for item in analysis.orphans] == [("group", ORPHAN_UNMAPPED)]


def test_task_owner_document_takes_part_in_the_orphan_check(index: DirectoryIndex) -> None:
    """BLOCKER 4: a scanned ACL carrier must be able to affect the verdict.

    Task endpoints scope every read and write to ``(org, workspace, auth.user)``,
    so an unmapped task owner is a user whose whole task list disappears —
    even on a deployment whose Frames data is spotless.
    """

    from collab_hub_api.identity_inventory.scan import OwnedRecord

    scan = scan_of([owned_by_a_certain_subject()])
    scan.task_owners.append(
        OwnedRecord(
            entity_type="task_state",
            entity_id="org/default/departed@example.com",
            org_id="org",
            workspace_id="default",
            name="scheduled tasks, runs, and notifications",
            owners=["departed@example.com"],
            created_by="departed@example.com",
            origin="nexus_task_state",
        )
    )
    analysis = analyze(scan, index)
    assert [(item.entity_type, item.kind) for item in analysis.orphans] == [("task_state", ORPHAN_UNMAPPED)]
    assert analysis.verdict == VERDICT_BLOCKED


def test_ambiguity_alone_blocks_the_migration(index: DirectoryIndex) -> None:
    scan = scan_of([sidecar("f6", owners=[ALICE.sub], readers=["dave@example.com"])])
    analysis = analyze(scan, index)
    assert analysis.orphans == []
    assert len(analysis.ambiguous) == 1
    assert analysis.verdict == VERDICT_BLOCKED
    assert not analysis.clear_to_proceed


# --- unmapped principals are preserved ----------------------------------------


def test_unmapped_readers_are_reported_and_never_altered(index: DirectoryIndex) -> None:
    frame = sidecar("f7", owners=[ALICE.sub], readers=["contractor@partner.example"])
    analysis = analyze(scan_of([frame]), index)
    unmapped = [item.principal for item in analysis.unmapped]
    assert unmapped == ["contractor@partner.example"]
    # The scanned record still holds the exact string; nothing rewrote it.
    assert frame["readers"] == ["contractor@partner.example"]
    assert analysis.scan.frames[0].readers == ["contractor@partner.example"]
    report = render_markdown(analysis)
    assert "LEFT IN PLACE" in report
    assert "contractor@partner.example" in report


def test_unmapped_principal_is_flagged_when_it_grants_access(index: DirectoryIndex) -> None:
    frame = sidecar(
        "f8",
        owners=[ALICE.sub],
        readers=["reader@partner.example"],
        suggestions=[{"id": "s1", "frame_id": "f8", "submitted_by": "visitor@partner.example", "body": "x"}],
    )
    analysis = analyze(scan_of([frame]), index)
    by_principal = {item.principal: item for item in analysis.principals}
    assert by_principal["reader@partner.example"].in_acl_carrier
    assert by_principal["visitor@partner.example"].in_acl_carrier


# --- history detail jsonb ------------------------------------------------------


def test_identity_keys_in_detail_jsonb_are_found_by_key() -> None:
    detail = {"added": ["new-owner@example.com"], "removed": ["old-owner@example.com"], "name": "Not an identity"}
    found = {value: by_key for _path, value, by_key in walk_json_identities(detail)}
    assert found["new-owner@example.com"] is True
    assert found["old-owner@example.com"] is True
    assert found["Not an identity"] is False


def test_identity_buried_under_an_unknown_key_is_found_by_value(index: DirectoryIndex) -> None:
    from collab_hub_api.identity_inventory.scan import _scan_jsonb

    scan = scan_of([sidecar("f9", owners=[ALICE.sub], readers=["alice@example.com"])])
    _scan_jsonb(
        scan,
        {"note": {"nested": ["alice@example.com", "just a string"]}},
        carrier="history.detail",
        entity_type="history",
        entity_id="h1",
        location_prefix="detail (event=updated)",
        org_id="org",
        workspace_id="default",
    )
    analysis = analyze(scan, index)
    history_hits = [item for item in analysis.scan.occurrences if item.carrier == "history.detail"]
    assert [item.principal for item in history_hits] == ["alice@example.com"]
    assert "just a string" not in {item.principal for item in analysis.scan.occurrences}


def test_deferred_json_value_limit_bounds_every_tracking_map(monkeypatch) -> None:
    """The distinct-value cap must bound memory, not only retained samples."""

    from collab_hub_api.identity_inventory import scan as scan_module

    monkeypatch.setattr(scan_module, "DEFERRED_VALUE_LIMIT", 3)
    scan = ScanResult()
    for index in range(20):
        value = f"value-{index}"
        scan.defer(
            value,
            Occurrence("history.detail", "postgres", "history", str(index), "detail", value),
        )

    assert len(scan.deferred_strings) == 3
    assert len(scan.deferred_counts) == 3
    assert scan.errors == [
        "jsonb scan: more than 3 distinct candidate strings; by-value matches beyond that point were not tracked"
    ]


def test_sampled_occurrence_count_includes_structural_and_omitted_hits(index: DirectoryIndex) -> None:
    """Sampling must not make the report undercount structural occurrences."""

    principal = ALICE.sub
    scan = ScanResult()
    for item in range(10):
        scan.add(Occurrence("history.actor", "postgres", "history", f"actor-{item}", "actor", principal))
    for item in range(20):
        scan.defer(
            principal,
            Occurrence("history.detail", "postgres", "history", f"detail-{item}", "detail", principal),
        )
    scan.coverage.append(SourceCoverage("fixture", True, "complete"))

    summary = analyze(scan, index).principals[0]
    assert len(summary.occurrences) == 10 + 5  # structural hits plus retained samples
    assert summary.sampled_total == 30
    assert summary.count == 30


def test_history_actor_is_scanned_from_a_fake_read_only_session(index: DirectoryIndex) -> None:
    from collab_hub_api.identity_inventory.scan import scan_history

    scan = ScanResult()
    scan_history(
        scan,
        FakeDatabase(
            {
                "frames_server_history": [
                    {
                        "id": "h1",
                        "org_id": "org",
                        "workspace_id": "default",
                        "entity_type": "frame",
                        "entity_id": "f1",
                        "event": "owners_changed",
                        "actor": "alice@example.com",
                        "detail": {"added": ["bob@example.com"], "removed": []},
                    }
                ]
            }
        ),
    )
    carriers = {item.carrier for item in scan.occurrences}
    assert carriers == {"history.actor", "history.detail"}
    assert {item.principal for item in scan.occurrences} == {"alice@example.com", "bob@example.com"}


def test_missing_table_is_coverage_not_a_crash() -> None:
    from collab_hub_api.identity_inventory.scan import scan_postgres

    scan = ScanResult()
    scan_postgres(scan, FakeDatabase({}))
    assert scan.occurrences == []
    assert all(not item.scanned for item in scan.coverage)
    assert any("does not exist" in item.detail for item in scan.coverage)


# --- directory coverage --------------------------------------------------------


def test_account_present_in_keycloak_but_absent_from_every_store(index: DirectoryIndex) -> None:
    analysis = analyze(scan_of([sidecar("f10", owners=["alice@example.com"])]), index)
    # Alice was seen through a (merely unverified) mapping; the ghost was not.
    unseen = {user.username for user in analysis.unseen_directory_users}
    assert "ghost" in unseen
    assert "alice" not in unseen


def test_directory_loads_from_a_keycloak_style_export() -> None:
    load = load_directory_from_json(
        [
            {"id": "abc", "username": "alice", "email": "alice@example.com", "enabled": True},
            {"username": "no-id-so-skipped"},
        ]
    )
    assert len(load.index) == 1
    assert load.index.resolve("alice").sub == "abc"


def test_directory_paging_stops_at_a_short_page() -> None:
    """A truncated directory turns real users into "unmapped" — so paging is tested."""

    from collab_hub_api.identity_inventory.directory import load_directory_from_keycloak

    class FakeClient:
        def __init__(self):
            self.calls: list[tuple[int, int]] = []

        def list_user_records_page(self, *, first: int, limit: int):
            self.calls.append((first, limit))
            everyone = [
                {"id": f"sub-{number}", "username": f"u{number}", "email": f"u{number}@example.com"}
                for number in range(5)
            ]
            return everyone[first : first + limit]

    client = FakeClient()
    load = load_directory_from_keycloak(client, page_size=2)
    assert len(load.index) == 5
    assert client.calls == [(0, 2), (2, 2), (4, 2)]
    assert load.index.resolve("u4@example.com").sub == "sub-4"


def test_directory_export_parses_keycloak_created_timestamps() -> None:
    load = load_directory_from_json(
        [
            {"id": "abc", "username": "alice", "email": "alice@example.com", "createdTimestamp": 1700000000000},
            {"id": "def", "username": "bob", "email": "bob@example.com"},
        ]
    )
    alice = load.index.resolve("alice").user
    assert alice is not None and alice.created_at == datetime.fromtimestamp(1700000000, tz=UTC)
    # A missing timestamp is missing evidence, and the report says so rather
    # than treating it as "no problem".
    assert load.index.accounts_without_created_at == 1
    assert any("could not run" in note for note in load.notes)


def test_reassignment_check_is_silent_without_a_timestamp() -> None:
    """No signal must never be reported as a clean signal."""

    no_timestamp = DirectoryUser(sub="s1", username="newcomer", email="reused@example.com")
    analysis = analyze(scan_of([sidecar("f17", owners=["reused@example.com"])]), DirectoryIndex([no_timestamp]))
    assert analysis.reassignment_suspects == []
    # Still not clear: the match is on a mutable claim.
    assert [item.kind for item in analysis.orphans] == [ORPHAN_UNVERIFIED_ONLY]
    assert analysis.verdict == VERDICT_NEEDS_CONFIRMATION


def test_reassignment_check_uses_the_earliest_record(index: DirectoryIndex) -> None:
    older = sidecar("f18", owners=["reused@example.com"], written_at=RECORD_WRITTEN)
    newer = sidecar("f19", owners=["reused@example.com"], written_at=AFTER_THE_RECORD + timedelta(days=30))
    analysis = analyze(scan_of([newer, older]), index)
    suspect = analysis.reassignment_suspects[0]
    assert suspect.earliest_record_written_at == RECORD_WRITTEN
    assert {item.kind for item in analysis.orphans} == {ORPHAN_REASSIGNED}


# --- local filesystem scan -----------------------------------------------------


def test_local_scan_reads_sidecars_without_creating_anything(tmp_path: Path, index: DirectoryIndex) -> None:
    root = tmp_path / "frames"
    (root / "aaa").mkdir(parents=True)
    (root / "aaa" / "metadata.json").write_text(json.dumps(sidecar("aaa", owners=[ALICE.sub])), encoding="utf-8")
    scan = ScanResult()
    scan_local_frames(scan, ReadOnlyLocalFrames(root))
    assert [frame.frame_id for frame in scan.frames] == ["aaa"]
    assert sorted(p.name for p in root.iterdir()) == ["aaa"]


def test_local_scan_reports_a_missing_root_instead_of_creating_it(tmp_path: Path) -> None:
    missing = tmp_path / "not-there"
    scan = ScanResult()
    scan_local_frames(scan, ReadOnlyLocalFrames(missing))
    assert not missing.exists()  # LocalFsFrameStore would have created it.
    assert scan.coverage[0].scanned is False


def test_unreadable_sidecar_is_an_error_not_a_silent_omission(tmp_path: Path) -> None:
    root = tmp_path / "frames"
    (root / "bad").mkdir(parents=True)
    (root / "bad" / "metadata.json").write_text("{not json", encoding="utf-8")
    scan = ScanResult()
    scan_local_frames(scan, ReadOnlyLocalFrames(root))
    assert scan.frames == []
    assert scan.errors and "could not read" in scan.errors[0]


def test_local_scan_refuses_a_symlinked_frame_directory(tmp_path: Path) -> None:
    root = tmp_path / "frames"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "metadata.json").write_text(json.dumps(sidecar("outside", owners=[ALICE.sub])), encoding="utf-8")
    (root / "linked-frame").symlink_to(outside, target_is_directory=True)

    scan = ScanResult()
    scan_local_frames(scan, ReadOnlyLocalFrames(root))

    assert scan.frames == []
    assert scan.occurrences == []
    assert scan.coverage == [
        SourceCoverage(
            "frames (local FS)",
            False,
            f"refusing symlink under frames root: {root / 'linked-frame'}",
        )
    ]
    assert scan.errors and "refusing symlink" in scan.errors[0]


def test_local_scan_refuses_a_symlinked_sidecar(tmp_path: Path) -> None:
    root = tmp_path / "frames"
    frame_dir = root / "frame"
    outside = tmp_path / "outside.json"
    frame_dir.mkdir(parents=True)
    outside.write_text(json.dumps(sidecar("outside", owners=[ALICE.sub])), encoding="utf-8")
    (frame_dir / "metadata.json").symlink_to(outside)

    scan = ScanResult()
    scan_local_frames(scan, ReadOnlyLocalFrames(root))

    assert scan.frames == []
    assert scan.occurrences == []
    assert not scan.coverage[0].scanned
    assert scan.errors and "refusing symlinked frame sidecar" in scan.errors[0]


# --- read-only enforcement -----------------------------------------------------


def test_sql_guard_refuses_anything_that_is_not_a_select() -> None:
    _require_select("SELECT 1")
    # Real statements this tool issues must survive the guard: `created_at` is
    # not CREATE and `updated_at` is not UPDATE.
    _require_select(
        "SELECT id, org_id, workspace_id, name, created_by, owners, created_at, updated_at "
        "FROM frames_server_groups ORDER BY id"
    )
    for statement in ("UPDATE frames_server_groups SET owners = '[]'", "DELETE FROM x", "INSERT INTO x VALUES (1)"):
        with pytest.raises(ReadOnlyViolationError):
            _require_select(statement)


def test_sql_guard_refuses_the_two_ways_a_prefix_check_is_walked_past() -> None:
    """A guard that can be stepped over must not be presented as a layer.

    The server-side read-only transaction is what actually stops these; the
    guard is defence in depth, and defence in depth that only catches the naive
    case invites being trusted as though it caught everything.
    """

    with pytest.raises(ReadOnlyViolationError):
        _require_select("SELECT 1; DELETE FROM frames_server_groups")
    with pytest.raises(ReadOnlyViolationError):
        _require_select("SELECT * FROM (WITH d AS (DELETE FROM frames_server_groups RETURNING *) SELECT * FROM d) q")
    # A single trailing semicolon is still one statement.
    _require_select("SELECT 1;")


def test_s3_hook_refuses_write_operations() -> None:
    class Model:
        def __init__(self, name: str):
            self.name = name

    reject_write_operations(model=Model("GetObject"))
    reject_write_operations(model=Model("ListObjectsV2"))
    for operation in ("PutObject", "DeleteObject", "DeleteObjects", "CopyObject", "PutBucketPolicy"):
        with pytest.raises(ReadOnlyViolationError):
            reject_write_operations(model=Model(operation))


def test_importing_the_package_pulls_in_no_module_that_can_write() -> None:
    # The strongest form of "it cannot write": in a clean interpreter, nothing
    # capable of writing is even loaded.
    banned = [
        "collab_hub_api.frames.store",
        "collab_hub_api.frames.groups",
        "collab_hub_api.frames.history",
        "collab_hub_api.frames.usage",
        "collab_hub_api.frames.active_state",
        "collab_hub_api.tasks.store",
        "collab_hub_api.config",
        "collab_hub_api.core",
    ]
    program = (
        "import sys, json;"
        "import collab_hub_api.identity_inventory as m;"
        "import collab_hub_api.identity_inventory.cli;"
        f"print(json.dumps([name for name in {banned!r} if name in sys.modules]))"
    )
    output = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(output.stdout.strip()) == []


def test_the_no_filesystem_writes_claim_is_backed_by_bytecode_settings() -> None:
    """BLOCKER 7: Python's own bytecode caching is a filesystem write.

    The import-graph test cannot see this, and neither can any assertion inside
    the process — by the time this package's code runs, the caches would already
    be written. So the guard is the image setting and the documented command,
    and this test is what keeps either from being quietly dropped.
    """

    repo = Path(__file__).resolve().parents[2]
    dockerfile = (repo / "api" / "Dockerfile").read_text(encoding="utf-8")
    assert "ENV PYTHONDONTWRITEBYTECODE=1" in dockerfile

    operations = (repo / "docs" / "frames-operations.md").read_text(encoding="utf-8")
    assert "python -B -m collab_hub_api.identity_inventory" in operations


def test_database_urls_are_redacted_before_they_are_printed() -> None:
    redacted = redact_database_url("postgresql://nexus:hunter2@db.internal:5432/frames")
    assert "hunter2" not in redacted
    assert "nexus" not in redacted
    assert redacted == "postgresql://db.internal:5432/frames"


# --- report --------------------------------------------------------------------


def test_report_names_the_orphan_and_states_the_verdict(index: DirectoryIndex) -> None:
    scan = scan_of(
        [
            sidecar("orphaned", owners=["gone@example.com"], name="Runbook"),
            sidecar("fine", owners=["alice@example.com"], name="Kept"),
        ]
    )
    report = render_markdown(analyze(scan, index))
    assert "Do not run the migration" in report
    assert "`orphaned`" in report
    assert "Runbook" in report
    assert "gone@example.com" in report
    assert "READ-ONLY" in report
    # Carrier provenance is visible, including the carriers the issue omitted.
    assert "usage_events.user_id" in report
    assert "issue #61 (absent from #65)" in report


def test_redaction_removes_principals_but_keeps_the_structure(index: DirectoryIndex) -> None:
    scan = scan_of([sidecar("f11", owners=["gone@example.com"], readers=["contractor@partner.example"])])
    analysis = analyze(scan, index)
    report = render_markdown(analysis, redact=True)
    assert "gone@example.com" not in report
    assert "contractor@partner.example" not in report
    assert pseudonym("gone@example.com") in report
    assert "`f11`" in report  # entity ids stay, so findings remain actionable


def test_redaction_covers_subjects_and_identities_embedded_in_ids(index: DirectoryIndex) -> None:
    """SHOULD-FIX B: redacting the principal column is not redacting the report.

    The proposed-mapping table prints the target `sub`, and several carriers
    build their entity id out of the principal itself. Both leak the identity
    one column over from the one that was redacted.
    """

    from collab_hub_api.identity_inventory.scan import scan_active_frames

    scan = scan_of([sidecar("f16", owners=["alice@example.com"])])
    scan_active_frames(
        scan,
        FakeDatabase(
            {
                "frames_server_active_frames": [
                    {"org_id": "org", "workspace_id": "default", "user_id": "gone@example.com"}
                ]
            }
        ),
    )
    analysis = analyze(scan, index)

    plain = render_markdown(analysis)
    assert ALICE.sub in plain  # the proposed target subject
    assert "org/default/gone@example.com" in plain  # an id built from a principal

    report = render_markdown(analysis, redact=True)
    assert "alice@example.com" not in report
    assert "gone@example.com" not in report
    assert ALICE.sub not in report
    assert pseudonym(ALICE.sub) in report
    assert pseudonym("gone@example.com") in report

    payload = render_json(analysis, redact=True)
    assert "alice@example.com" not in payload
    assert "gone@example.com" not in payload
    assert ALICE.sub not in payload


def test_json_report_round_trips(index: DirectoryIndex) -> None:
    analysis = analyze(scan_of([owned_by_a_certain_subject("f12")]), index)
    payload = json.loads(render_json(analysis))
    assert payload["read_only"] is True
    assert payload["verdict"] == VERDICT_CLEAR
    assert payload["clear_to_proceed"] is True
    assert payload["coverage_gaps"] == []
    assert any(carrier["id"] == "task_devices.payload" for carrier in payload["carriers"])
    assert set(payload["confidence_model"]) == {"certain", "unverified"}


def _cli_fixture(tmp_path: Path, frames: dict[str, dict], users: list[dict]) -> tuple[Path, Path]:
    export = tmp_path / "users.json"
    export.write_text(json.dumps(users), "utf-8")
    root = tmp_path / "frames"
    for frame_id, metadata in frames.items():
        (root / frame_id).mkdir(parents=True)
        (root / frame_id / "metadata.json").write_text(json.dumps(metadata), "utf-8")
    return export, root


ALICE_EXPORT = {
    "id": ALICE.sub,
    "username": "alice",
    "email": "alice@example.com",
    "enabled": True,
    "createdTimestamp": int(LONG_AGO.timestamp() * 1000),
}


def test_report_written_by_the_cli_is_owner_readable_only(tmp_path: Path, monkeypatch) -> None:
    from collab_hub_api.identity_inventory import cli

    export, frames = _cli_fixture(tmp_path, {"aaa": owned_by_a_certain_subject("aaa")}, [ALICE_EXPORT])
    output = tmp_path / "report.md"
    # A leftover world-readable report from a previous run: os.open's create
    # mode does not apply to an existing file, so the mode has to be set on the
    # descriptor or yesterday's 0644 would silently persist.
    output.write_text("stale", encoding="utf-8")
    output.chmod(0o644)

    monkeypatch.delenv("COLLAB_HUB_API__FRAMES__POSTGRES__URL", raising=False)
    code = cli.main(
        [
            "--directory-json",
            str(export),
            "--frames-backend",
            "local",
            "--frames-path",
            str(frames),
            "--skip-postgres",
            "--output",
            str(output),
        ]
    )
    # Skipping Postgres is itself a coverage gap, so the verdict cannot be clear.
    assert code == cli.EXIT_SCAN_FAILED
    assert oct(os.stat(output).st_mode & 0o777) == "0o600"
    body = output.read_text(encoding="utf-8")
    assert "Collab identity inventory" in body
    assert "incomplete_scan" in body


def test_private_report_writer_refuses_a_symlink_without_touching_its_target(tmp_path: Path) -> None:
    from collab_hub_api.identity_inventory.cli import _write_private

    target = tmp_path / "must-stay.txt"
    target.write_text("keep me", encoding="utf-8")
    output = tmp_path / "identity-inventory.md"
    output.symlink_to(target)

    with pytest.raises(UnsafePathError, match="refusing symlink path"):
        _write_private(str(output), "report")

    assert target.read_text(encoding="utf-8") == "keep me"


def test_cli_exit_code_signals_blocking_findings(tmp_path: Path, monkeypatch) -> None:
    from collab_hub_api.identity_inventory import cli

    export, frames = _cli_fixture(
        tmp_path,
        {"bbb": sidecar("bbb", owners=["gone@example.com"])},
        [ALICE_EXPORT],
    )
    monkeypatch.delenv("COLLAB_HUB_API__FRAMES__POSTGRES__URL", raising=False)
    monkeypatch.setattr(cli, "run_scan", _complete_scan_wrapper(cli.run_scan))
    code = cli.main(
        [
            "--directory-json",
            str(export),
            "--frames-backend",
            "local",
            "--frames-path",
            str(frames),
            "--skip-postgres",
            "--output",
            str(tmp_path / "report.md"),
        ]
    )
    assert code == cli.EXIT_BLOCKING_FINDINGS


def test_cli_never_exits_zero_when_a_record_could_not_be_read(tmp_path: Path, monkeypatch) -> None:
    """BLOCKER 1: a partial scan must not be able to say "clear".

    One good frame, one unreadable sidecar. Everything the tool *did* see is
    fine, and that is exactly the situation in which a clean verdict would be a
    lie: the unreadable record could be the orphan.
    """

    from collab_hub_api.identity_inventory import cli

    export, frames = _cli_fixture(tmp_path, {"aaa": owned_by_a_certain_subject("aaa")}, [ALICE_EXPORT])
    (frames / "broken").mkdir()
    (frames / "broken" / "metadata.json").write_text("{not json", encoding="utf-8")

    monkeypatch.delenv("COLLAB_HUB_API__FRAMES__POSTGRES__URL", raising=False)
    monkeypatch.setattr(cli, "run_scan", _complete_scan_wrapper(cli.run_scan))
    output = tmp_path / "report.md"
    code = cli.main(
        [
            "--directory-json",
            str(export),
            "--frames-backend",
            "local",
            "--frames-path",
            str(frames),
            "--skip-postgres",
            "--output",
            str(output),
        ]
    )
    assert code == cli.EXIT_SCAN_FAILED
    body = output.read_text(encoding="utf-8")
    assert "incomplete_scan" in body
    assert "No blocking findings" not in body


def _complete_scan_wrapper(original):
    """Run the real scan, then drop the deliberate ``--skip-postgres`` gap.

    Lets a test exercise a *findings* verdict without also tripping the
    coverage gate, which is covered on its own above.
    """

    def wrapper(args):
        result = original(args)
        result.coverage = [item for item in result.coverage if item.scanned or "skipped" not in item.detail]
        return result

    return wrapper


def test_verdict_is_incomplete_when_a_source_was_not_scanned(index: DirectoryIndex) -> None:
    scan = scan_of([owned_by_a_certain_subject("ok")])
    scan.coverage.append(SourceCoverage("frames_server_groups", False, "table does not exist"))
    analysis = analyze(scan, index)
    assert analysis.orphans == []
    assert analysis.verdict == VERDICT_INCOMPLETE
    assert not analysis.clear_to_proceed
    report = render_markdown(analysis)
    assert "did not cover everything" in report


def test_verdict_is_incomplete_when_the_scan_recorded_an_error(index: DirectoryIndex) -> None:
    scan = scan_of([owned_by_a_certain_subject("ok")])
    scan.errors.append("frames (S3): could not read frames/zzz/metadata.json: ClientError")
    analysis = analyze(scan, index)
    assert analysis.verdict == VERDICT_INCOMPLETE


def test_whitespace_padded_principal_is_reported_never_trimmed(index: DirectoryIndex) -> None:
    """BLOCKER 6: the service compares exactly, so neither does the report repair.

    ``" alice "`` grants nothing today. Trimming it to reach a real account
    would invent access that does not exist.
    """

    frame = sidecar("f13", owners=[ALICE.sub], readers=[" alice@example.com "])
    analysis = analyze(scan_of([frame]), index)
    padded = analysis.padded
    assert [item.principal for item in padded] == [" alice@example.com "]
    assert padded[0].resolution.status is ResolutionStatus.unmapped
    assert padded[0].resolution.sub is None
    assert frame["readers"] == [" alice@example.com "]
    assert analysis.verdict == VERDICT_NEEDS_CONFIRMATION
    assert "surrounding whitespace" in render_markdown(analysis)


def test_padded_owner_cannot_clear_an_entity(index: DirectoryIndex) -> None:
    analysis = analyze(scan_of([sidecar("f14", owners=[f" {ALICE.sub} "])]), index)
    assert [item.kind for item in analysis.orphans] == [ORPHAN_UNMAPPED]


def test_task_device_payload_is_scanned(index: DirectoryIndex) -> None:
    """BLOCKER 2: the DeviceRecord in the payload repeats the principal."""

    from collab_hub_api.identity_inventory.scan import scan_tasks

    scan = ScanResult()
    scan_tasks(
        scan,
        FakeDatabase(
            {
                "nexus_task_devices": [
                    {
                        "org_id": "org",
                        "workspace_id": "default",
                        "user_id": "alice@example.com",
                        "device_id": "d1",
                        "payload": {
                            "device_id": "d1",
                            "org_id": "org",
                            "workspace_id": "default",
                            "user_id": "alice@example.com",
                        },
                    }
                ]
            }
        ),
    )
    carriers = {item.carrier for item in scan.occurrences}
    assert "task_devices.payload" in carriers
    payload_hit = next(item for item in scan.occurrences if item.carrier == "task_devices.payload")
    assert payload_hit.principal == "alice@example.com"


def test_unknown_sidecar_fields_are_swept_too(index: DirectoryIndex) -> None:
    frame = sidecar("f15", owners=[ALICE.sub], extra={"delegates": {"primary": ALICE.sub}})
    analysis = analyze(scan_of([frame]), index)
    assert any(item.carrier == "frame.other_fields" for item in analysis.scan.occurrences)


# --- live Postgres --------------------------------------------------------------

LIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS frames_server_groups (
    id text PRIMARY KEY, org_id text NOT NULL, workspace_id text NOT NULL,
    name text NOT NULL, description text NOT NULL DEFAULT '', created_by text NOT NULL,
    owners jsonb NOT NULL, visibility text NOT NULL DEFAULT 'private', frame_ids jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS frames_server_history (
    id text PRIMARY KEY, org_id text NOT NULL, workspace_id text NOT NULL, entity_type text NOT NULL,
    entity_id text NOT NULL, event text NOT NULL, actor text NOT NULL, detail jsonb,
    created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS frames_server_active_frames (
    org_id text NOT NULL, workspace_id text NOT NULL, user_id text NOT NULL, frame_ids jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (org_id, workspace_id, user_id));
CREATE TABLE IF NOT EXISTS frames_server_usage_users (
    org_id text NOT NULL, workspace_id text NOT NULL, user_id text NOT NULL, email text,
    first_seen timestamptz NOT NULL DEFAULT now(), last_seen timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, workspace_id, user_id));
CREATE TABLE IF NOT EXISTS frames_server_usage_events (
    id text PRIMARY KEY, org_id text NOT NULL, workspace_id text NOT NULL, user_id text NOT NULL,
    event text NOT NULL, detail jsonb, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS nexus_task_state (
    org_id text NOT NULL, workspace_id text NOT NULL, owner_id text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb, updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, workspace_id, owner_id));
CREATE TABLE IF NOT EXISTS nexus_task_devices (
    org_id text NOT NULL, workspace_id text NOT NULL, user_id text NOT NULL, device_id text NOT NULL,
    payload jsonb NOT NULL, last_seen_at timestamptz NOT NULL, expires_at timestamptz NOT NULL,
    PRIMARY KEY (org_id, workspace_id, user_id, device_id));
"""


def test_scan_and_read_only_enforcement_against_a_real_database() -> None:
    """The carriers, the server-side read-only guarantee, and server-side cursors.

    The fakes above prove the scan logic; only a real server proves that
    ``default_transaction_read_only=on`` actually took effect and that the
    streaming cursor works, which are the two claims the whole tool rests on.
    """

    url = os.getenv("COLLAB_HUB_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("COLLAB_HUB_TEST_POSTGRES_URL is not set")

    import psycopg

    from collab_hub_api.identity_inventory.readonly import read_only_postgres
    from collab_hub_api.identity_inventory.scan import scan_postgres

    with psycopg.connect(url, autocommit=True) as setup:
        setup.execute(LIVE_SCHEMA)
        for table in (
            "frames_server_groups",
            "frames_server_history",
            "frames_server_active_frames",
            "frames_server_usage_users",
            "frames_server_usage_events",
            "nexus_task_state",
            "nexus_task_devices",
        ):
            setup.execute(f"DELETE FROM {table}")
        setup.execute(
            "INSERT INTO frames_server_groups (id, org_id, workspace_id, name, created_by, owners, frame_ids)"
            " VALUES ('g1','org','default','Pack','gone@example.com','[\"gone@example.com\"]'::jsonb,'[\"f1\"]'::jsonb)"
        )
        setup.execute(
            "INSERT INTO frames_server_history (id, org_id, workspace_id, entity_type, entity_id, event, actor, detail)"
            " VALUES ('h1','org','default','frame','f1','owners_changed','alice@example.com',"
            ' \'{"added":["bob@example.com"],"removed":[],"note":"retired@example.com"}\'::jsonb)'
        )
        setup.execute(
            "INSERT INTO frames_server_active_frames (org_id, workspace_id, user_id, frame_ids)"
            " VALUES ('org','default','alice@example.com','[\"f1\"]'::jsonb)"
        )
        # Same principal that is buried under a non-identity key in the history
        # detail above; this structural sighting is what lets the by-value rule
        # recognise it there.
        setup.execute(
            "INSERT INTO frames_server_active_frames (org_id, workspace_id, user_id, frame_ids)"
            " VALUES ('org','default','retired@example.com','[]'::jsonb)"
        )
        setup.execute(
            "INSERT INTO frames_server_usage_users (org_id, workspace_id, user_id, email)"
            " VALUES ('org','default','alice@example.com','alice@example.com')"
        )
        setup.execute(
            "INSERT INTO frames_server_usage_events (id, org_id, workspace_id, user_id, event)"
            " VALUES ('e1','org','default','bob','chat_created')"
        )
        setup.execute(
            "INSERT INTO nexus_task_state (org_id, workspace_id, owner_id, payload)"
            " VALUES ('org','default','alice@example.com',"
            ' \'{"tasks":[{"id":"t1","owner_id":"alice@example.com"}]}\'::jsonb)'
        )
        setup.execute(
            "INSERT INTO nexus_task_devices"
            " (org_id, workspace_id, user_id, device_id, payload, last_seen_at, expires_at)"
            " VALUES ('org','default','bob','d1',"
            ' \'{"device_id":"d1","user_id":"bob","org_id":"org"}\'::jsonb,'
            " now(), now() + interval '1 day')"
        )

    scan = ScanResult()
    with read_only_postgres(url) as db:
        assert db.verify_read_only() == "on"
        scan_postgres(scan, db)
        # The server itself refuses the write, bypassing this tool's own guard.
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            db.connection.execute(
                "INSERT INTO frames_server_usage_events (id, org_id, workspace_id, user_id, event)"
                " VALUES ('e2','org','default','x','y')"
            )

    found = {item.carrier for item in scan.occurrences}
    assert {
        "group.created_by",
        "group.owners",
        "history.actor",
        "history.detail",
        "active_frames.user_id",
        "usage_users.user_id",
        "usage_users.email",
        "usage_events.user_id",
        "task_state.owner_id",
        "task_state.payload",
        "task_devices.user_id",
        "task_devices.payload",
    } <= found
    assert all(item.scanned for item in scan.coverage)

    analysis = analyze(scan, DirectoryIndex([ALICE, BOB, RETIRED]))
    # "retired@example.com" sits under a non-identity key in history detail and
    # is only counted because it is a principal elsewhere — the by-value rule.
    detail_hits = {item.principal for item in scan.occurrences if item.carrier == "history.detail"}
    assert detail_hits == {"bob@example.com", "retired@example.com"}
    # The group is unmapped and blocking; the task-state document is owned by a
    # merely unverified email match, which is a finding of its own kind rather
    # than either "clear" or "orphaned".
    assert sorted((item.entity_id, item.kind) for item in analysis.orphans) == [
        ("g1", ORPHAN_UNMAPPED),
        ("org/default/alice@example.com", ORPHAN_UNVERIFIED_ONLY),
    ]
    assert analysis.verdict == VERDICT_BLOCKED

    # The rows are exactly as they were: a dry run changes nothing.
    with psycopg.connect(url, autocommit=True) as check:
        remaining = check.execute("SELECT count(*) FROM frames_server_usage_events").fetchone()[0]
        assert remaining == 1
        owners = check.execute("SELECT owners FROM frames_server_groups WHERE id = 'g1'").fetchone()[0]
        assert owners == ["gone@example.com"]


# --- live object store ----------------------------------------------------------


def test_s3_scan_and_write_refusal_against_a_real_object_store() -> None:
    """The S3 path against a real endpoint (SeaweedFS in dev), including the write refusal.

    Gated on ``NEXUS_TEST_S3_ENDPOINT_URL`` the way the Postgres tests are gated
    on ``COLLAB_HUB_TEST_POSTGRES_URL``. Worth a live endpoint because the botocore
    hook is the load-bearing S3 guarantee, and a mocked client would only prove
    that the mock was configured correctly.
    """

    endpoint = os.getenv("NEXUS_TEST_S3_ENDPOINT_URL")
    if not endpoint:
        pytest.skip("NEXUS_TEST_S3_ENDPOINT_URL is not set")

    import boto3
    from botocore.config import Config

    from collab_hub_api.identity_inventory.readonly import ReadOnlyS3
    from collab_hub_api.identity_inventory.scan import scan_s3_frames

    bucket = os.getenv("NEXUS_TEST_S3_BUCKET", "frames-identity-inventory-test")
    setup = boto3.client("s3", endpoint_url=endpoint, config=Config(s3={"addressing_style": "path"}))
    try:
        setup.create_bucket(Bucket=bucket)
    except setup.exceptions.BucketAlreadyOwnedByYou:  # pragma: no cover - rerun path
        pass
    setup.put_object(
        Bucket=bucket,
        Key="frames/aaa/metadata.json",
        Body=json.dumps(sidecar("aaa", owners=["alice@example.com"], readers=["contractor@partner.example"])).encode(),
    )
    setup.put_object(
        Bucket=bucket,
        Key="frames/bbb/metadata.json",
        Body=json.dumps(sidecar("bbb", owners=["gone@example.com"], name="Orphan")).encode(),
    )

    reader = ReadOnlyS3(bucket=bucket, prefix="frames", endpoint_url=endpoint, region_name="us-east-1")
    scan = ScanResult()
    scan_s3_frames(scan, reader)
    assert sorted(frame.frame_id for frame in scan.frames) == ["aaa", "bbb"]

    analysis = analyze(scan, DirectoryIndex([ALICE]))
    assert sorted((item.entity_id, item.kind) for item in analysis.orphans) == [
        # aaa's only owner matched a mutable email: a proposal, not a clearance.
        ("aaa", ORPHAN_UNVERIFIED_ONLY),
        ("bbb", ORPHAN_UNMAPPED),
    ]

    # Even reaching past the wrapper to the underlying client cannot write.
    with pytest.raises(ReadOnlyViolationError):
        reader._client.put_object(Bucket=bucket, Key="frames/aaa/metadata.json", Body=b"{}")
    with pytest.raises(ReadOnlyViolationError):
        reader._client.delete_object(Bucket=bucket, Key="frames/bbb/metadata.json")

    keys = sorted(item["Key"] for item in setup.list_objects_v2(Bucket=bucket)["Contents"])
    assert keys == ["frames/aaa/metadata.json", "frames/bbb/metadata.json"]
    for key in keys:
        setup.delete_object(Bucket=bucket, Key=key)


# --- fakes ---------------------------------------------------------------------


class FakeDatabase:
    """Stands in for a ReadOnlyPostgres session over fixture rows."""

    def __init__(self, tables: dict[str, list[dict]]):
        self.tables = tables

    def table_exists(self, name: str) -> bool:
        return name in self.tables

    def _table_for(self, sql: str) -> list[dict]:
        for name, rows in self.tables.items():
            if name in sql:
                return rows
        return []

    def rows(self, sql: str, params: tuple = ()) -> list[dict]:
        return list(self._table_for(sql))

    def iter_rows(self, sql: str, params: tuple = (), *, batch_size: int = 1000):
        yield from self._table_for(sql)
