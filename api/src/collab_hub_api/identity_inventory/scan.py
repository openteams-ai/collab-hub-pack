"""Every place the Frames server persists an identity string, and how to read it.

:data:`CARRIERS` is the authoritative list — eighteen storage sites plus one
catch-all sweep row — and it is deliberately annotated with *provenance*: which
of them issue #65 named, which issue #61 found while writing its pin, and which
this audit found by walking ``release/public``. That column is not decoration: a
migration report is only as trustworthy as its coverage, and an operator reading
it has to be able to see that the tool went looking somewhere the issue did not
think to mention.

Several carriers are JSON documents rather than columns — ``history.detail``,
``usage_events.detail``, ``task_state.payload``, ``task_devices.payload``, and
the frame sidecar itself — and a named-field read cannot cover those, as this
tool found the hard way: ``nexus_task_state.payload`` was scanned while
``nexus_task_devices.payload``, holding the same shape of record, was missed.
So every JSON document is now swept whole, with two rules:

* **by key** — a string under a key that is identity-bearing by construction
  (``actor``, ``added``/``removed`` from ``list_diff``, ``owner_id``, …);
* **by value** — a string that is *equal to a principal already found in a
  structural carrier*.

The second rule is what catches shapes nobody anticipated, without flooding the
report with frame names and tag values. Both rules record the JSON path and the
event name so a reviewer can judge a false positive at a glance — and since
nothing is ever rewritten, a false positive costs a line of report, not access.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .readonly import UnsafePathError

# Keys whose string values are identity strings wherever they appear in a jsonb
# payload. ``added``/``removed`` come from routers.frames.list_diff, which is
# how owner and reader changes are recorded in history.
IDENTITY_KEYS = frozenset(
    {
        "actor",
        "added",
        "created_by",
        "email",
        "owner",
        "owner_id",
        "owners",
        "principal",
        "readers",
        "removed",
        "submitted_by",
        "user",
        "user_id",
    }
)

# Bounds on the deferred (by-value) jsonb candidate set. History and usage
# events grow without limit, and an inventory that exhausts memory on a real
# hub is not an inventory.
DEFERRED_SAMPLES_PER_VALUE = 5
DEFERRED_VALUE_LIMIT = 100_000

# Sidecar paths already covered by the named-field reads, so the whole-document
# sweep does not double-count them.
_SIDECAR_KNOWN_PATHS = (
    "$.owners",
    "$.owner",
    "$.created_by",
    "$.readers",
    "$.suggestions",
)

LISTED_IN_65 = "issue #65"
FOUND_BY_61 = "issue #61 (absent from #65)"
FOUND_HERE = "this audit (absent from #65 and #61)"


@dataclass(frozen=True)
class Carrier:
    """One persisted location of an identity string."""

    id: str
    location: str
    written_by: str
    provenance: str
    acl: bool
    """Whether this carrier grants access. Non-ACL carriers still have to be
    migrated together with the ACLs or the record splits in two, but they can
    never orphan a frame."""


CARRIERS: tuple[Carrier, ...] = (
    Carrier(
        id="frame.created_by",
        location="frame metadata sidecar (S3 and local FS): created_by",
        written_by="frames/store.py create_frame",
        provenance=LISTED_IN_65,
        acl=False,
    ),
    Carrier(
        id="frame.owners",
        location="frame metadata sidecar (S3 and local FS): owners[]",
        written_by="frames/store.py create_frame, set_owners",
        provenance=LISTED_IN_65,
        acl=True,
    ),
    Carrier(
        id="frame.readers",
        location="frame metadata sidecar (S3 and local FS): readers[]",
        written_by="frames/store.py set_readers",
        provenance=LISTED_IN_65,
        acl=True,
    ),
    Carrier(
        id="frame.suggestions.submitted_by",
        location="frame metadata sidecar (S3 and local FS): suggestions[].submitted_by",
        written_by="frames/store.py create_suggestion",
        provenance=LISTED_IN_65,
        acl=True,
    ),
    Carrier(
        id="frame.legacy_owner",
        location="frame metadata sidecar (S3 and local FS): legacy scalar `owner`",
        written_by="pre-`owners` records, migrated on read by frames.codec.normalize_metadata",
        provenance=FOUND_HERE,
        acl=True,
    ),
    Carrier(
        id="frame.other_fields",
        location="frame metadata sidecar (S3 and local FS): any other field, by whole-document sweep",
        written_by="hand-edited or future sidecar fields; nothing in the service writes these today",
        provenance=FOUND_HERE,
        acl=False,
    ),
    Carrier(
        id="group.created_by",
        location="frames_server_groups.created_by",
        written_by="frames/groups.py create_group",
        provenance=LISTED_IN_65,
        acl=False,
    ),
    Carrier(
        id="group.owners",
        location="frames_server_groups.owners (jsonb array)",
        written_by="frames/groups.py create_group, set_owners",
        provenance=LISTED_IN_65,
        acl=True,
    ),
    Carrier(
        id="history.actor",
        location="frames_server_history.actor",
        written_by="frames/history.py record",
        provenance=LISTED_IN_65,
        acl=False,
    ),
    Carrier(
        id="history.detail",
        location="frames_server_history.detail (jsonb: owners_changed/readers_changed added+removed)",
        written_by="routers/frames.py list_diff via record_frame_history / record_group_history",
        provenance=LISTED_IN_65,
        acl=False,
    ),
    Carrier(
        id="active_frames.user_id",
        location="frames_server_active_frames.user_id (PK component)",
        written_by="frames/active_state.py set_active_frame_ids",
        provenance=LISTED_IN_65,
        acl=False,
    ),
    Carrier(
        id="usage_users.user_id",
        location="frames_server_usage_users.user_id (PK component)",
        written_by="frames/usage.py record_user_seen",
        provenance=LISTED_IN_65,
        acl=False,
    ),
    Carrier(
        id="usage_users.email",
        location="frames_server_usage_users.email",
        written_by="frames/usage.py record_user_seen",
        provenance=FOUND_HERE,
        acl=False,
    ),
    Carrier(
        id="usage_events.user_id",
        location="frames_server_usage_events.user_id",
        written_by="frames/usage.py record_event",
        provenance=FOUND_BY_61,
        acl=False,
    ),
    Carrier(
        id="usage_events.detail",
        location="frames_server_usage_events.detail (jsonb, client-reported)",
        written_by="frames/usage.py record_event",
        provenance=FOUND_HERE,
        acl=False,
    ),
    Carrier(
        id="task_state.owner_id",
        location="nexus_task_state.owner_id (PK component)",
        written_by="tasks/store.py PostgresTaskStore._with_state (owner = auth.user)",
        provenance=FOUND_HERE,
        acl=True,
    ),
    Carrier(
        id="task_state.payload",
        location=(
            "nexus_task_state.payload (jsonb: tasks[].owner_id, runs[].owner_id, "
            "devices[].user_id, notifications[].owner_id)"
        ),
        written_by="tasks/store.py PostgresTaskStore._dump",
        provenance=FOUND_HERE,
        acl=True,
    ),
    Carrier(
        id="task_devices.user_id",
        location="nexus_task_devices.user_id (PK component)",
        written_by="tasks/store.py heartbeat",
        provenance=FOUND_HERE,
        acl=False,
    ),
    Carrier(
        id="task_devices.payload",
        location="nexus_task_devices.payload (jsonb: the DeviceRecord's own user_id)",
        written_by="tasks/store.py heartbeat (record.model_dump)",
        provenance=FOUND_HERE,
        acl=False,
    ),
)

CARRIERS_BY_ID = {carrier.id: carrier for carrier in CARRIERS}


@dataclass(frozen=True)
class Occurrence:
    """One identity string found in one place."""

    carrier: str
    source: str
    entity_type: str
    entity_id: str
    location: str
    principal: str
    org_id: str = ""
    workspace_id: str = ""


@dataclass
class OwnedRecord:
    """Any stored record whose reachability depends on an owner list.

    Frames, groups, and per-user task documents are different features, but the
    orphan question is identical for all three: after the migration, is there
    still somebody who can manage this? Sharing one shape is what keeps a
    carrier from being scanned and then quietly left out of the verdict.
    """

    entity_type: str
    entity_id: str
    org_id: str
    workspace_id: str
    name: str
    owners: list[str]
    created_by: str = ""
    origin: str = ""
    """Where the record came from (an S3 key, a path, a table), so a finding can
    be checked by hand against the actual record."""

    written_at: datetime | None = None
    """When the record was last written, as stored. An account created after
    this cannot be the account its principals referred to — see
    :mod:`.analysis`."""


@dataclass
class FrameRecord(OwnedRecord):
    """A frame as *stored*, for the orphan check. Never validated by pydantic."""

    readers: list[str] = field(default_factory=list)
    source: str = ""

    @property
    def frame_id(self) -> str:
        return self.entity_id


@dataclass
class GroupRecord(OwnedRecord):
    """A frame group as stored, for the orphan check."""

    @property
    def group_id(self) -> str:
        return self.entity_id


@dataclass
class SourceCoverage:
    """Whether one source was actually scanned — reported even when it was not.

    "No findings" and "never looked" are indistinguishable in a summary table
    unless the report says which happened, and only one of them means it is
    safe to migrate.
    """

    source: str
    scanned: bool
    detail: str


@dataclass
class ScanResult:
    """Everything the scan found, before any directory mapping is applied."""

    occurrences: list[Occurrence] = field(default_factory=list)
    frames: list[FrameRecord] = field(default_factory=list)
    groups: list[GroupRecord] = field(default_factory=list)
    task_owners: list[OwnedRecord] = field(default_factory=list)
    coverage: list[SourceCoverage] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    data_notes: list[str] = field(default_factory=list)
    """Stored-data oddities that are findings rather than scan failures — for
    example a sidecar the service reads as ownerless because its ``owners`` key
    is present but empty while a legacy ``owner`` scalar sits beside it."""
    _deferred_limit_reported: bool = False
    deferred_strings: dict[str, list[Occurrence]] = field(default_factory=dict)
    """Candidate jsonb strings keyed by value, resolved against the known
    principal set once every structural carrier has been scanned (rule "by
    value" in the module docstring)."""

    deferred_counts: dict[str, int] = field(default_factory=dict)
    """Total sightings per deferred value, including ones not retained."""

    sampled_values: dict[str, int] = field(default_factory=dict)
    """Principals whose jsonb locations were sampled, with omitted counts."""

    def add(self, occurrence: Occurrence) -> None:
        self.occurrences.append(occurrence)

    def defer(self, value: str, occurrence: Occurrence) -> None:
        """Hold a jsonb string until the known-principal set is complete.

        Bounded twice, because history is unbounded: at most
        ``DEFERRED_SAMPLES_PER_VALUE`` locations are kept per distinct string
        (the total is still counted), and at most ``DEFERRED_VALUE_LIMIT``
        distinct strings are tracked at all. Hitting the second bound is
        reported as an error rather than silently narrowing coverage.
        """

        held = self.deferred_strings.get(value)
        if held is None:
            if len(self.deferred_strings) >= DEFERRED_VALUE_LIMIT:
                if not self._deferred_limit_reported:
                    self._deferred_limit_reported = True
                    self.errors.append(
                        f"jsonb scan: more than {DEFERRED_VALUE_LIMIT} distinct candidate strings; "
                        "by-value matches beyond that point were not tracked"
                    )
                return
            self.deferred_strings[value] = [occurrence]
            self.deferred_counts[value] = 1
            return
        self.deferred_counts[value] += 1
        if len(held) < DEFERRED_SAMPLES_PER_VALUE:
            held.append(occurrence)

    @property
    def owned_records(self) -> list[OwnedRecord]:
        """Every record the orphan check must consider, from every carrier."""

        return [*self.frames, *self.groups, *self.task_owners]

    @property
    def complete(self) -> bool:
        """Whether every source was scanned and nothing failed on the way.

        The verdict depends on this: a report over a partial scan cannot say
        anything is safe, only that nothing was found *where it looked*.
        """

        return all(item.scanned for item in self.coverage) and not self.errors

    @property
    def gaps(self) -> list[str]:
        """Human-readable reasons the scan is incomplete, for the verdict."""

        reasons = [f"{item.source}: {item.detail}" for item in self.coverage if not item.scanned]
        return reasons + list(self.errors)

    @property
    def principals(self) -> set[str]:
        return {item.principal for item in self.occurrences if item.principal.strip()}

    def resolve_deferred(self) -> int:
        """Promote deferred jsonb strings that match a known structural principal.

        Runs after every structural carrier, because a string in a history
        ``detail`` is only interesting when it is an identity, and the cheapest
        reliable evidence of that is that the same string is an owner, reader,
        actor, or usage user somewhere else.
        """

        known = self.principals
        promoted = 0
        for value, occurrences in self.deferred_strings.items():
            if value not in known:
                continue
            for occurrence in occurrences:
                self.occurrences.append(occurrence)
                promoted += 1
            total = self.deferred_counts.get(value, len(occurrences))
            if total > len(occurrences):
                # The report must not imply it listed every location when it
                # listed a sample of them.
                self.sampled_values[value] = total - len(occurrences)
        self.deferred_strings = {}
        self.deferred_counts = {}
        return promoted


def walk_json_identities(payload: object, path: str = "$") -> Iterator[tuple[str, str, bool]]:
    """Yield ``(json_path, value, identity_by_key)`` for every string in a payload.

    ``identity_by_key`` is True when the string sits under an identity-bearing
    key (directly, or as an element of a list under one). Callers use it to
    decide between recording the occurrence outright and deferring it for the
    by-value rule.
    """

    yield from _walk(payload, path, False)


def _walk(payload: object, path: str, inherited: bool) -> Iterator[tuple[str, str, bool]]:
    if isinstance(payload, str):
        yield path, payload, inherited
    elif isinstance(payload, dict):
        for key, value in payload.items():
            yield from _walk(value, f"{path}.{key}", key in IDENTITY_KEYS)
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            yield from _walk(value, f"{path}[{index}]", inherited)


def scan_frame_sidecar(
    result: ScanResult,
    metadata: dict,
    *,
    source: str,
    origin: str,
    fallback_frame_id: str,
) -> None:
    """Record every identity string in one raw frame metadata sidecar.

    Reads the stored document defensively: a missing ``owners``, a legacy scalar
    ``owner``, a ``readers`` entry that is not a string — all of them are things
    an audit has to survive and report, and all of them are things the pydantic
    model would refuse to load.

    The legacy ``owner`` scalar is promoted **exactly** where
    ``frames.codec.normalize_metadata`` promotes it: only when the ``owners`` *key is
    absent*. A sidecar holding ``{"owners": [], "owner": "alice"}`` is ownerless
    to the service, so it must be ownerless here too — reading it more
    generously would clear a frame that is already unmanageable. Where the two
    readings differ the divergence is itself recorded as a finding.
    """

    frame_id = str(metadata.get("id") or fallback_frame_id)
    org_id = str(metadata.get("org_id") or "")
    workspace_id = str(metadata.get("workspace_id") or "")
    name = str(metadata.get("name") or "")

    def record(carrier: str, location: str, value: object) -> str | None:
        if not isinstance(value, str) or not value.strip():
            return None
        result.add(
            Occurrence(
                carrier=carrier,
                source=source,
                entity_type="frame",
                entity_id=frame_id,
                location=location,
                principal=value,
                org_id=org_id,
                workspace_id=workspace_id,
            )
        )
        return value

    owners: list[str] = []
    for index, value in enumerate(_as_list(metadata.get("owners"))):
        stored = record("frame.owners", f"owners[{index}]", value)
        if stored:
            owners.append(stored)

    legacy_owner = metadata.get("owner")
    if isinstance(legacy_owner, str) and legacy_owner.strip():
        record("frame.legacy_owner", "owner", legacy_owner)
        if "owners" not in metadata:
            # Exactly frames.codec.normalize_metadata's condition: the scalar is
            # promoted only when there is no `owners` key at all.
            owners.append(legacy_owner)
        elif not owners:
            result.data_notes.append(
                f"frame {frame_id} ({origin}) stores an empty `owners` list beside a legacy "
                "`owner` scalar. The service reads it as ownerless, so this report does too — "
                "the frame is already unmanageable, independently of any migration."
            )

    created_by = metadata.get("created_by")
    record("frame.created_by", "created_by", created_by)

    readers: list[str] = []
    for index, value in enumerate(_as_list(metadata.get("readers"))):
        stored = record("frame.readers", f"readers[{index}]", value)
        if stored:
            readers.append(stored)

    for index, suggestion in enumerate(_as_list(metadata.get("suggestions"))):
        if isinstance(suggestion, dict):
            record(
                "frame.suggestions.submitted_by",
                f"suggestions[{index}].submitted_by",
                suggestion.get("submitted_by"),
            )

    # Whole-document sweep for anything the named fields above do not cover —
    # an unknown key, a future field, a hand-edited sidecar. Deferred, so only
    # strings that turn out to be principals elsewhere are counted.
    _scan_jsonb(
        result,
        metadata,
        carrier="frame.other_fields",
        entity_type="frame",
        entity_id=frame_id,
        location_prefix="metadata",
        org_id=org_id,
        workspace_id=workspace_id,
        source=source,
        skip_paths=_SIDECAR_KNOWN_PATHS,
    )

    result.frames.append(
        FrameRecord(
            entity_type="frame",
            entity_id=frame_id,
            org_id=org_id,
            workspace_id=workspace_id,
            name=name,
            owners=owners,
            created_by=str(created_by or ""),
            readers=readers,
            source=source,
            origin=origin,
            written_at=_record_timestamp(metadata.get("updated_at"), metadata.get("created_at")),
        )
    )


def scan_local_frames(result: ScanResult, local) -> None:
    """Scan every frame sidecar under a local frames directory."""

    if not local.exists():
        result.coverage.append(
            SourceCoverage("frames (local FS)", False, f"{local.root} does not exist or is not a directory")
        )
        return
    count = 0
    try:
        for sidecar in local.iter_sidecars():
            try:
                metadata = local.read_json(sidecar.path)
            except Exception as exc:
                result.errors.append(f"frames (local FS): could not read {sidecar.path}: {type(exc).__name__}")
                continue
            if not isinstance(metadata, dict):
                result.errors.append(f"frames (local FS): {sidecar.path} is not a JSON object")
                continue
            scan_frame_sidecar(
                result,
                metadata,
                source="local-fs",
                origin=str(sidecar.path),
                fallback_frame_id=sidecar.frame_id,
            )
            count += 1
    except UnsafePathError as exc:
        result.errors.append(f"frames (local FS): {exc}")
        result.coverage.append(SourceCoverage("frames (local FS)", False, str(exc)))
        return
    result.coverage.append(SourceCoverage("frames (local FS)", True, f"{count} frame sidecars under {local.root}"))


def scan_s3_frames(result: ScanResult, s3) -> None:
    """Scan every frame sidecar in the S3 bucket."""

    count = 0
    try:
        keys = list(s3.iter_metadata_keys())
    except Exception as exc:
        result.coverage.append(
            SourceCoverage("frames (S3)", False, f"could not list s3://{s3.bucket}/{s3.prefix}: {type(exc).__name__}")
        )
        result.errors.append(f"frames (S3): listing failed: {type(exc).__name__}")
        return
    for key in keys:
        try:
            metadata = s3.get_json(key)
        except Exception as exc:
            result.errors.append(f"frames (S3): could not read {key}: {type(exc).__name__}")
            continue
        if not isinstance(metadata, dict):
            result.errors.append(f"frames (S3): {key} is not a JSON object")
            continue
        parts = key.split("/")
        scan_frame_sidecar(
            result,
            metadata,
            source="s3",
            origin=f"s3://{s3.bucket}/{key}",
            fallback_frame_id=parts[-2] if len(parts) >= 2 else key,
        )
        count += 1
    result.coverage.append(
        SourceCoverage("frames (S3)", True, f"{count} frame sidecars in s3://{s3.bucket}/{s3.prefix}")
    )


def scan_groups(result: ScanResult, db) -> None:
    """Scan ``frames_server_groups`` for creator and owner principals."""

    if not db.table_exists("frames_server_groups"):
        result.coverage.append(SourceCoverage("frames_server_groups", False, "table does not exist"))
        return
    rows = db.rows(
        "SELECT id, org_id, workspace_id, name, created_by, owners, created_at, updated_at "
        "FROM frames_server_groups ORDER BY id"
    )
    for row in rows:
        group_id = str(row["id"])
        org_id = str(row.get("org_id") or "")
        workspace_id = str(row.get("workspace_id") or "")
        owners: list[str] = []
        for index, value in enumerate(_as_list(row.get("owners"))):
            if isinstance(value, str) and value.strip():
                owners.append(value)
                result.add(
                    Occurrence(
                        carrier="group.owners",
                        source="postgres",
                        entity_type="group",
                        entity_id=group_id,
                        location=f"owners[{index}]",
                        principal=value,
                        org_id=org_id,
                        workspace_id=workspace_id,
                    )
                )
        created_by = row.get("created_by")
        if isinstance(created_by, str) and created_by.strip():
            result.add(
                Occurrence(
                    carrier="group.created_by",
                    source="postgres",
                    entity_type="group",
                    entity_id=group_id,
                    location="created_by",
                    principal=created_by,
                    org_id=org_id,
                    workspace_id=workspace_id,
                )
            )
        result.groups.append(
            GroupRecord(
                entity_type="group",
                entity_id=group_id,
                org_id=org_id,
                workspace_id=workspace_id,
                name=str(row.get("name") or ""),
                owners=owners,
                created_by=str(created_by or ""),
                origin="frames_server_groups",
                written_at=_record_timestamp(row.get("updated_at"), row.get("created_at")),
            )
        )
    result.coverage.append(SourceCoverage("frames_server_groups", True, f"{len(rows)} groups"))


def scan_history(result: ScanResult, db) -> None:
    """Scan ``frames_server_history``: the ``actor`` column and its ``detail`` jsonb."""

    if not db.table_exists("frames_server_history"):
        result.coverage.append(SourceCoverage("frames_server_history", False, "table does not exist"))
        return
    count = 0
    for row in db.iter_rows(
        """
        SELECT id, org_id, workspace_id, entity_type, entity_id, event, actor, detail
        FROM frames_server_history
        """
    ):
        count += 1
        entity_id = str(row.get("entity_id") or "")
        org_id = str(row.get("org_id") or "")
        workspace_id = str(row.get("workspace_id") or "")
        event = str(row.get("event") or "")
        actor = row.get("actor")
        if isinstance(actor, str) and actor.strip():
            result.add(
                Occurrence(
                    carrier="history.actor",
                    source="postgres",
                    entity_type="history",
                    entity_id=str(row.get("id") or ""),
                    location=f"actor ({row.get('entity_type')} {entity_id}, event={event})",
                    principal=actor,
                    org_id=org_id,
                    workspace_id=workspace_id,
                )
            )
        _scan_jsonb(
            result,
            row.get("detail"),
            carrier="history.detail",
            entity_type="history",
            entity_id=str(row.get("id") or ""),
            location_prefix=f"detail (event={event}, {row.get('entity_type')} {entity_id})",
            org_id=org_id,
            workspace_id=workspace_id,
        )
    result.coverage.append(SourceCoverage("frames_server_history", True, f"{count} history rows"))


def scan_active_frames(result: ScanResult, db) -> None:
    """Scan the ``frames_server_active_frames`` primary key."""

    if not db.table_exists("frames_server_active_frames"):
        result.coverage.append(SourceCoverage("frames_server_active_frames", False, "table does not exist"))
        return
    rows = db.rows(
        "SELECT org_id, workspace_id, user_id FROM frames_server_active_frames ORDER BY org_id, workspace_id, user_id"
    )
    for row in rows:
        value = row.get("user_id")
        if isinstance(value, str) and value.strip():
            result.add(
                Occurrence(
                    carrier="active_frames.user_id",
                    source="postgres",
                    entity_type="active_frames",
                    entity_id=f"{row.get('org_id')}/{row.get('workspace_id')}/{value}",
                    location="user_id (PK component)",
                    principal=value,
                    org_id=str(row.get("org_id") or ""),
                    workspace_id=str(row.get("workspace_id") or ""),
                )
            )
    result.coverage.append(SourceCoverage("frames_server_active_frames", True, f"{len(rows)} rows"))


def scan_usage(result: ScanResult, db) -> None:
    """Scan the usage roster and usage events, including the stored ``email``."""

    if db.table_exists("frames_server_usage_users"):
        rows = db.rows(
            "SELECT org_id, workspace_id, user_id, email FROM frames_server_usage_users "
            "ORDER BY org_id, workspace_id, user_id"
        )
        for row in rows:
            org_id = str(row.get("org_id") or "")
            workspace_id = str(row.get("workspace_id") or "")
            user_id = row.get("user_id")
            entity_id = f"{org_id}/{workspace_id}/{user_id}"
            if isinstance(user_id, str) and user_id.strip():
                result.add(
                    Occurrence(
                        carrier="usage_users.user_id",
                        source="postgres",
                        entity_type="usage_user",
                        entity_id=entity_id,
                        location="user_id (PK component)",
                        principal=user_id,
                        org_id=org_id,
                        workspace_id=workspace_id,
                    )
                )
            email = row.get("email")
            if isinstance(email, str) and email.strip():
                result.add(
                    Occurrence(
                        carrier="usage_users.email",
                        source="postgres",
                        entity_type="usage_user",
                        entity_id=entity_id,
                        location="email",
                        principal=email,
                        org_id=org_id,
                        workspace_id=workspace_id,
                    )
                )
        result.coverage.append(SourceCoverage("frames_server_usage_users", True, f"{len(rows)} rows"))
    else:
        result.coverage.append(SourceCoverage("frames_server_usage_users", False, "table does not exist"))

    if db.table_exists("frames_server_usage_events"):
        count = 0
        usage_events_sql = "SELECT id, org_id, workspace_id, user_id, event, detail FROM frames_server_usage_events"
        for row in db.iter_rows(usage_events_sql):
            count += 1
            org_id = str(row.get("org_id") or "")
            workspace_id = str(row.get("workspace_id") or "")
            event_id = str(row.get("id") or "")
            user_id = row.get("user_id")
            if isinstance(user_id, str) and user_id.strip():
                result.add(
                    Occurrence(
                        carrier="usage_events.user_id",
                        source="postgres",
                        entity_type="usage_event",
                        entity_id=event_id,
                        location=f"user_id (event={row.get('event')})",
                        principal=user_id,
                        org_id=org_id,
                        workspace_id=workspace_id,
                    )
                )
            _scan_jsonb(
                result,
                row.get("detail"),
                carrier="usage_events.detail",
                entity_type="usage_event",
                entity_id=event_id,
                location_prefix=f"detail (event={row.get('event')})",
                org_id=org_id,
                workspace_id=workspace_id,
            )
        result.coverage.append(SourceCoverage("frames_server_usage_events", True, f"{count} rows"))
    else:
        result.coverage.append(SourceCoverage("frames_server_usage_events", False, "table does not exist"))


def scan_tasks(result: ScanResult, db) -> None:
    """Scan the scheduled-task tables.

    Not named by #65 or #61 — but ``nexus_task_state`` is keyed by ``auth.user``,
    the very same string, and the document under that key repeats it on every
    task, run, and notification. Migrating the frame ACLs without these leaves
    every user's task list behind under their old principal, which reads to
    them as data loss.

    ``owner_id`` is an authorization boundary in its own right: every task
    endpoint scopes reads and writes to ``(org, workspace, auth.user)``. So each
    row is registered as an owned record and takes part in the orphan check
    exactly like a frame or a group — scanning a carrier, calling it
    ACL-bearing, and then leaving it out of the verdict would be the same
    partial-coverage failure the report exists to prevent.

    Both jsonb columns are walked, not just ``nexus_task_state.payload``:
    ``nexus_task_devices.payload`` stores a whole ``DeviceRecord``, whose own
    ``user_id`` field repeats the principal beside the primary key.
    """

    if db.table_exists("nexus_task_state"):
        count = 0
        for row in db.iter_rows("SELECT org_id, workspace_id, owner_id, payload, updated_at FROM nexus_task_state"):
            count += 1
            org_id = str(row.get("org_id") or "")
            workspace_id = str(row.get("workspace_id") or "")
            owner_id = row.get("owner_id")
            entity_id = f"{org_id}/{workspace_id}/{owner_id}"
            if isinstance(owner_id, str) and owner_id.strip():
                result.add(
                    Occurrence(
                        carrier="task_state.owner_id",
                        source="postgres",
                        entity_type="task_state",
                        entity_id=entity_id,
                        location="owner_id (PK component)",
                        principal=owner_id,
                        org_id=org_id,
                        workspace_id=workspace_id,
                    )
                )
            _scan_jsonb(
                result,
                row.get("payload"),
                carrier="task_state.payload",
                entity_type="task_state",
                entity_id=entity_id,
                location_prefix="payload",
                org_id=org_id,
                workspace_id=workspace_id,
            )
            if isinstance(owner_id, str):
                result.task_owners.append(
                    OwnedRecord(
                        entity_type="task_state",
                        entity_id=entity_id,
                        org_id=org_id,
                        workspace_id=workspace_id,
                        name="scheduled tasks, runs, and notifications",
                        owners=[owner_id] if owner_id.strip() else [],
                        created_by=owner_id,
                        origin="nexus_task_state",
                        written_at=_record_timestamp(row.get("updated_at")),
                    )
                )
        result.coverage.append(SourceCoverage("nexus_task_state", True, f"{count} rows"))
    else:
        result.coverage.append(SourceCoverage("nexus_task_state", False, "table does not exist"))

    if db.table_exists("nexus_task_devices"):
        rows = db.rows("SELECT org_id, workspace_id, user_id, device_id, payload FROM nexus_task_devices")
        for row in rows:
            org_id = str(row.get("org_id") or "")
            workspace_id = str(row.get("workspace_id") or "")
            user_id = row.get("user_id")
            entity_id = f"{org_id}/{workspace_id}/{user_id}/{row.get('device_id')}"
            if isinstance(user_id, str) and user_id.strip():
                result.add(
                    Occurrence(
                        carrier="task_devices.user_id",
                        source="postgres",
                        entity_type="task_device",
                        entity_id=entity_id,
                        location="user_id (PK component)",
                        principal=user_id,
                        org_id=org_id,
                        workspace_id=workspace_id,
                    )
                )
            _scan_jsonb(
                result,
                row.get("payload"),
                carrier="task_devices.payload",
                entity_type="task_device",
                entity_id=entity_id,
                location_prefix="payload",
                org_id=org_id,
                workspace_id=workspace_id,
            )
        result.coverage.append(SourceCoverage("nexus_task_devices", True, f"{len(rows)} rows"))
    else:
        result.coverage.append(SourceCoverage("nexus_task_devices", False, "table does not exist"))


def scan_postgres(result: ScanResult, db) -> None:
    """Scan every Postgres-resident carrier through one read-only session."""

    scan_groups(result, db)
    scan_history(result, db)
    scan_active_frames(result, db)
    scan_usage(result, db)
    scan_tasks(result, db)


def _record_timestamp(*values: object) -> datetime | None:
    """First parseable timestamp among ``values`` (last write preferred).

    Used as "when was this record last written", which is what an account's
    creation time is compared against. Unparseable or missing means no signal,
    never a guess.
    """

    for value in values:
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=UTC)
        if isinstance(value, str) and value:
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError:
                continue
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _scan_jsonb(
    result: ScanResult,
    payload: object,
    *,
    carrier: str,
    entity_type: str,
    entity_id: str,
    location_prefix: str,
    org_id: str,
    workspace_id: str,
    source: str = "postgres",
    skip_paths: tuple[str, ...] = (),
) -> None:
    if payload is None:
        return
    for path, value, by_key in walk_json_identities(payload):
        if not value.strip():
            continue
        if skip_paths and path.startswith(skip_paths):
            continue
        occurrence = Occurrence(
            carrier=carrier,
            source=source,
            entity_type=entity_type,
            entity_id=entity_id,
            location=f"{location_prefix} {path}",
            principal=value,
            org_id=org_id,
            workspace_id=workspace_id,
        )
        if by_key:
            result.add(occurrence)
        else:
            # Deferred: only counted if this exact string turns out to be a
            # principal somewhere structural. Deduplicated by value so a large
            # history table costs one entry per distinct string.
            result.defer(value, occurrence)


def _as_list(value: object) -> list:
    if isinstance(value, list):
        return value
    return []
