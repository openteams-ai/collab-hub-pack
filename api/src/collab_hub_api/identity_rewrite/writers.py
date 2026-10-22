"""The writes themselves: substitution, primary-key merges, and the manifest.

Every function here takes the substitution table as data and returns what it
changed. Nothing derives a mapping, and nothing decides whether a principal is
safe to rewrite — :mod:`.plan` did that, in front of a person.

Three properties are load-bearing, and each exists because its absence was
demonstrated rather than imagined:

* **A match is only substituted where an identity belongs.** The document is
  still swept whole — that is how the scanner found the carrier its first draft
  missed — but a value equal to a mapped principal is *rewritten* only under an
  identity-bearing key. A ``description`` that happens to equal an address is
  reported and left alone; nothing is corrupted while the operator decides.
* **A change is recorded only once its write has succeeded.** An entry for a
  write that failed or rolled back makes the record untrue, so planned, pending,
  committed and failed are distinct states.

  The manifest is a **diagnostic and targeted-reversal aid, not the authoritative
  rollback mechanism.** It is written at startup and at termination, so an
  abruptly killed run can leave committed file or object writes unrecorded, and
  it is per-run rather than a durable journal. After an interrupted run the
  authoritative description of what is stored is a fresh read-only inventory,
  and the authoritative way back is verified database and frame-store
  backup/restore. See :mod:`collab_hub_api.identity_rewrite` for the contract
  this deliberately does not claim.
* **Local writes stay inside the configured root, and land atomically.** A
  planted ``metadata.json`` symlink otherwise rewrites a file outside the store,
  and an interrupted in-place write leaves a truncated sidecar.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..identity_inventory.readonly import UnsafePathError, open_no_follow
from ..identity_inventory.scan import IDENTITY_KEYS

PLANNED = "planned"
PENDING = "pending"
COMMITTED = "committed"
FAILED = "failed"

# Carriers deliberately not rewritten, and why: cases where the inventory is
# right to *record* an identity string and this tool would be wrong to change it.
EXCLUDED_CARRIERS = {
    "usage_users.email": (
        "a contact/display column holding an address, not an ACL principal. Replacing it with a "
        "subject would destroy the address and gain nothing: authorization keys on user_id, which "
        "is rewritten."
    ),
}

# How every carrier the inventory reports is handled here. This exists so the
# two tools cannot drift: a carrier the inventory learns to *find* and this tool
# does not know how to *write* is a silent half-migration — a task row whose
# primary key moved while the identity inside its payload stayed legacy. A
# parity test walks ``scan.CARRIERS`` and fails on any id missing from this map,
# so adding a carrier upstream breaks the build rather than the migration.
SIDECAR_SWEEP = "rewritten by the frame sidecar sweep"
DECLINED_BY_DEFAULT = "found by the sweep; substituted only via --allow-path, and reported otherwise"

CARRIER_COVERAGE: dict[str, str] = {
    "frame.created_by": SIDECAR_SWEEP,
    "frame.owners": SIDECAR_SWEEP,
    "frame.readers": SIDECAR_SWEEP,
    "frame.suggestions.submitted_by": SIDECAR_SWEEP,
    "frame.legacy_owner": SIDECAR_SWEEP,
    "frame.other_fields": DECLINED_BY_DEFAULT,
    "group.created_by": "rewrite_text_columns",
    "group.owners": "rewrite_json_columns",
    "history.actor": "rewrite_text_columns",
    "history.detail": "rewrite_json_columns",
    "active_frames.user_id": "merge_active_frames",
    "usage_users.user_id": "merge_usage_users",
    "usage_users.email": "excluded (see EXCLUDED_CARRIERS)",
    "usage_events.user_id": "rewrite_text_columns",
    "usage_events.detail": "rewrite_json_columns",
    "task_state.owner_id": "merge_task_state",
    "task_state.payload": "rewrite_json_columns",
    "task_devices.user_id": "merge_task_devices",
    "task_devices.payload": "rewrite_json_columns",
}


@dataclass(frozen=True)
class Change:
    """One substitution, recorded so it can be reviewed or reversed.

    ``state`` separates what was intended from what is true on disk. A dry run
    produces only ``planned``; an apply promotes each entry to ``committed``
    after its write returns, or marks it ``failed``.
    """

    carrier: str
    entity: str
    location: str
    before: str
    after: str
    state: str = PLANNED
    before_image: dict | None = None
    """Both rows as they were, for a collision merge.

    ``before``/``after`` alone cannot describe such a change: two rows became
    one, and the merge either combined values or discarded a side. **Both sides
    are captured, including the one that survives** — when the legacy row is
    newer its payload overwrites the subject's, so the subject's prior payload is
    destroyed by the operation and is recorded here or nowhere."""


@dataclass(frozen=True)
class UnexpectedPath:
    """A value equal to a mapped principal, found where no identity belongs."""

    carrier: str
    entity: str
    path: str
    principal: str


@dataclass
class Manifest:
    """Everything the run changed, plus what it deliberately did not."""

    changes: list[Change] = field(default_factory=list)
    unexpected_paths: list[UnexpectedPath] = field(default_factory=list)
    merges: list[str] = field(default_factory=list)
    skipped_carriers: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    applied: bool = False
    rolled_back: bool = False

    def record(self, change: Change) -> None:
        self.changes.append(change)

    def commit(self, pending: list[Change]) -> None:
        """Promote changes whose write *is* its own commit — an object or a file."""

        for change in pending:
            self.changes.append(Change(**{**asdict(change), "state": COMMITTED}))

    def stage(self, pending: list[Change]) -> None:
        """Record database changes as ``pending``: the statement ran, the transaction did not commit.

        A later failure in another carrier rolls the whole transaction back, so
        marking these committed at statement time would leave the record
        asserting changes the database discarded. They are promoted by
        :meth:`promote_pending` once the surrounding transaction has committed,
        and only then.
        """

        for change in pending:
            self.changes.append(Change(**{**asdict(change), "state": PENDING}))

    def promote_pending(self) -> None:
        """Called after the database transaction commits: pending becomes committed."""

        self.changes = [
            Change(**{**asdict(change), "state": COMMITTED}) if change.state == PENDING else change
            for change in self.changes
        ]

    def fail_pending(self, reason: str) -> None:
        """Called when the transaction did not commit: pending becomes failed."""

        self.changes = [
            Change(**{**asdict(change), "state": FAILED}) if change.state == PENDING else change
            for change in self.changes
        ]
        self.rolled_back = True
        self.errors.append(reason)

    def fail(self, pending: list[Change], reason: str) -> None:
        """Record changes whose write did not succeed, and why."""

        for change in pending:
            self.changes.append(Change(**{**asdict(change), "state": FAILED}))
        self.errors.append(reason)

    @property
    def committed(self) -> list[Change]:
        return [item for item in self.changes if item.state == COMMITTED]

    @property
    def counts(self) -> dict[str, int]:
        totals: dict[str, int] = {}
        for change in self.changes:
            key = f"{change.carrier} ({change.state})"
            totals[key] = totals.get(key, 0) + 1
        return dict(sorted(totals.items()))

    def to_json(self) -> str:
        return (
            json.dumps(
                {
                    "applied": self.applied,
                    "transaction_rolled_back": self.rolled_back,
                    "total_changes": len(self.changes),
                    "committed_changes": len(self.committed),
                    "by_carrier_and_state": self.counts,
                    "unexpected_paths": [asdict(item) for item in self.unexpected_paths],
                    "merges": self.merges,
                    "skipped_carriers": self.skipped_carriers,
                    "errors": self.errors,
                    "changes": [asdict(change) for change in self.changes],
                },
                indent=2,
            )
            + "\n"
        )


def rewrite_json_document(
    payload: object,
    mapping: dict[str, str],
    path: str = "$",
    *,
    allow_paths: frozenset[str] | None = None,
    identity_root: bool = False,
    _identity: bool = False,
) -> tuple[object, list[tuple[str, str, str]], list[tuple[str, str]]]:
    """Return *payload* with identity-bearing matches substituted.

    Three results: the new document, the substitutions made as
    ``(json_path, before, after)``, and matches **declined** as
    ``(json_path, principal)``.

    **Whole string values only, and only where an identity belongs.** A value is
    a candidate when it is exactly a mapped principal, never when it merely
    contains one — substring replacement would rewrite prose that mentions an
    address. Being an exact match is not sufficient either: the key has to be one
    the service stores identities under (:data:`IDENTITY_KEYS`, shared with the
    scanner), or the path has to be named in *allow_paths* by an operator who
    reviewed it. A ``description`` equal to a mapped address is a coincidence,
    not a principal, and rewriting it would corrupt ordinary content.

    The document is still swept **whole** rather than at known paths — that is
    the lesson the scanner learned when its first draft read
    ``nexus_task_devices.payload`` field by field and missed the identity inside
    it. Sweeping finds; the key decides whether finding becomes writing.
    """

    allowed = allow_paths or frozenset()
    _identity = _identity or identity_root
    if isinstance(payload, str):
        replacement = mapping.get(payload)
        if replacement is None or replacement == payload:
            return payload, [], []
        if _identity or path in allowed:
            return replacement, [(path, payload, replacement)], []
        return payload, [], [(path, payload)]
    if isinstance(payload, dict):
        result: dict = {}
        changes: list[tuple[str, str, str]] = []
        declined: list[tuple[str, str]] = []
        for key, value in payload.items():
            new_value, found, skipped = rewrite_json_document(
                value, mapping, f"{path}.{key}", allow_paths=allow_paths, _identity=key in IDENTITY_KEYS
            )
            result[key] = new_value
            changes.extend(found)
            declined.extend(skipped)
        return result, changes, declined
    if isinstance(payload, list):
        items = []
        changes = []
        declined = []
        for index, value in enumerate(payload):
            new_value, found, skipped = rewrite_json_document(
                value, mapping, f"{path}[{index}]", allow_paths=allow_paths, _identity=_identity
            )
            items.append(new_value)
            changes.extend(found)
            declined.extend(skipped)
        return items, changes, declined
    return payload, [], []


# --- Postgres -----------------------------------------------------------------

# Plain text columns. None participates in a primary key, so two principals
# collapsing onto one subject simply produces two rows holding the same value.
# Each carries the column identifying its rows, so the manifest names them
# individually rather than recording a count nothing can reverse.
TEXT_COLUMNS = (
    ("history.actor", "frames_server_history", "actor", "id"),
    ("group.created_by", "frames_server_groups", "created_by", "id"),
    ("usage_events.user_id", "frames_server_usage_events", "user_id", "id"),
)

# jsonb columns: read, sweep, write back. Run **before** the primary-key merges
# so each document is located by the key it still has.
#
# The fourth element is ``identity_root``: whether the *column itself* is the
# identity list rather than a document that happens to contain identity fields.
# ``frames_server_groups.owners`` stores a bare JSON array — ``["alice@x"]``, not
# ``{"owners": ["alice@x"]}`` — so its elements sit at ``$[0]`` with no key above
# them, and a rule that decides by key name has nothing to match. Without this
# flag the substitution declines an ACL carrier and reports it as an unexpected
# path: a group's `created_by` migrates while its `owners` stay legacy, leaving
# a group its owners cannot manage. Found by the first dry run against real data
# (dev-nexus-internal, 4 groups), which is what a rehearsal is for.
#
# The payload/detail columns are the opposite case and must stay False: they are
# arbitrary documents where only identity-bearing *keys* may be rewritten, which
# is the property the previous review established.
JSON_COLUMNS = (
    ("group.owners", "frames_server_groups", "owners", ("id",), True),
    ("history.detail", "frames_server_history", "detail", ("id",), False),
    ("usage_events.detail", "frames_server_usage_events", "detail", ("id",), False),
    (
        "task_state.payload",
        "nexus_task_state",
        "payload",
        ("org_id", "workspace_id", "owner_id"),
        False,
    ),
    (
        "task_devices.payload",
        "nexus_task_devices",
        "payload",
        ("org_id", "workspace_id", "user_id", "device_id"),
        False,
    ),
)


def rewrite_text_columns(conn, mapping: dict[str, str], manifest: Manifest, *, apply: bool) -> None:
    """Substitute mapped principals in the plain text identity columns.

    Row ids are read first so the manifest records which rows changed. A count
    is not a reversal instruction.
    """

    for carrier, table, column, key_column in TEXT_COLUMNS:
        if not _table_exists(conn, table):
            manifest.skipped_carriers.append(f"{carrier}: {table} does not exist")
            continue
        for principal, sub in mapping.items():
            rows = conn.execute(
                f"SELECT {key_column} FROM {table} WHERE {column} = %s", (principal,)
            ).fetchall()
            if not rows:
                continue
            pending = [
                Change(carrier, f"{table}:{_value(row, key_column)}", column, principal, sub)
                for row in rows
            ]
            if not apply:
                for change in pending:
                    manifest.record(change)
                continue
            try:
                conn.execute(f"UPDATE {table} SET {column} = %s WHERE {column} = %s", (sub, principal))
            except Exception as exc:  # noqa: BLE001 - the manifest must stay true
                manifest.fail(pending, f"{carrier}: {type(exc).__name__}: {exc}")
                raise
            manifest.stage(pending)


def rewrite_json_columns(
    conn,
    mapping: dict[str, str],
    manifest: Manifest,
    *,
    apply: bool,
    allow_paths: frozenset[str] | None = None,
) -> None:
    """Sweep the jsonb identity documents row by row."""

    for carrier, table, column, key_columns, identity_root in JSON_COLUMNS:
        if not _table_exists(conn, table):
            manifest.skipped_carriers.append(f"{carrier}: {table} does not exist")
            continue
        keys = ", ".join(key_columns)
        for row in conn.execute(f"SELECT {keys}, {column} FROM {table}").fetchall():
            document = _value(row, column)
            if document is None:
                continue
            if isinstance(document, str):
                document = json.loads(document)
            updated, found, declined = rewrite_json_document(
                document, mapping, allow_paths=allow_paths, identity_root=identity_root
            )
            entity = "/".join(str(_value(row, name)) for name in key_columns)
            for path, principal in declined:
                manifest.unexpected_paths.append(UnexpectedPath(carrier, f"{table}:{entity}", path, principal))
            if not found:
                continue
            pending = [Change(carrier, f"{table}:{entity}", path, before, after) for path, before, after in found]
            if not apply:
                for change in pending:
                    manifest.record(change)
                continue
            where = " AND ".join(f"{name} = %s" for name in key_columns)
            params = (json.dumps(updated), *[_value(row, name) for name in key_columns])
            try:
                conn.execute(f"UPDATE {table} SET {column} = %s WHERE {where}", params)
            except Exception as exc:  # noqa: BLE001
                manifest.fail(pending, f"{carrier}: {type(exc).__name__}: {exc}")
                raise
            manifest.stage(pending)


def merge_active_frames(conn, mapping: dict[str, str], manifest: Manifest, *, apply: bool) -> None:
    """Move active-frame selections onto the subject, unioning on collision.

    ``frame_ids`` is a set of choices, so two rows for one person merge by union
    — dropping either side would silently deselect frames the user had open.
    """

    table = "frames_server_active_frames"
    if not _table_exists(conn, table):
        manifest.skipped_carriers.append(f"active_frames.user_id: {table} does not exist")
        return
    for principal, sub in mapping.items():
        rows = conn.execute(
            f"SELECT org_id, workspace_id, frame_ids, updated_at FROM {table} WHERE user_id = %s",
            (principal,),
        ).fetchall()
        for row in rows:
            org_id, workspace_id = _value(row, "org_id"), _value(row, "workspace_id")
            entity = f"{org_id}/{workspace_id}/{principal}"
            existing = conn.execute(
                f"SELECT frame_ids FROM {table} WHERE org_id = %s AND workspace_id = %s AND user_id = %s",
                (org_id, workspace_id, sub),
            ).fetchone()
            if existing is None:
                pending = [Change("active_frames.user_id", entity, "user_id", principal, sub)]
                if not apply:
                    manifest.record(pending[0])
                    continue
                conn.execute(
                    f"UPDATE {table} SET user_id = %s WHERE org_id = %s AND workspace_id = %s AND user_id = %s",
                    (sub, org_id, workspace_id, principal),
                )
                manifest.stage(pending)
                continue
            legacy = _as_list(_value(row, "frame_ids"))
            target = _as_list(_value(existing, "frame_ids"))
            merged = _union(legacy, target)
            pending = [
                Change(
                    "active_frames.user_id",
                    entity,
                    "user_id (merged)",
                    principal,
                    sub,
                    before_image={"legacy_frame_ids": legacy, "subject_frame_ids": target},
                )
            ]
            note = (
                f"active_frames {entity}: unioned {len(legacy)} + {len(target)} frame ids into "
                f"{len(merged)} under the subject"
            )
            if not apply:
                manifest.record(pending[0])
                manifest.merges.append(note)
                continue
            conn.execute(
                f"UPDATE {table} SET frame_ids = %s, updated_at = now() "
                "WHERE org_id = %s AND workspace_id = %s AND user_id = %s",
                (json.dumps(merged), org_id, workspace_id, sub),
            )
            conn.execute(
                f"DELETE FROM {table} WHERE org_id = %s AND workspace_id = %s AND user_id = %s",
                (org_id, workspace_id, principal),
            )
            manifest.stage(pending)
            manifest.merges.append(note)


def merge_usage_users(conn, mapping: dict[str, str], manifest: Manifest, *, apply: bool) -> None:
    """Move the usage roster onto the subject, widening the seen-window on collision.

    ``first_seen``/``last_seen`` describe one person's activity, so a merge takes
    the earliest first and the latest last. Keeping either row wholesale would
    shorten the recorded history of someone who used two clients.
    """

    table = "frames_server_usage_users"
    if not _table_exists(conn, table):
        manifest.skipped_carriers.append(f"usage_users.user_id: {table} does not exist")
        return
    for principal, sub in mapping.items():
        rows = conn.execute(
            f"SELECT org_id, workspace_id, email, first_seen, last_seen FROM {table} WHERE user_id = %s",
            (principal,),
        ).fetchall()
        for row in rows:
            org_id, workspace_id = _value(row, "org_id"), _value(row, "workspace_id")
            entity = f"{org_id}/{workspace_id}/{principal}"
            existing = conn.execute(
                f"SELECT email, first_seen, last_seen FROM {table} "
                "WHERE org_id = %s AND workspace_id = %s AND user_id = %s",
                (org_id, workspace_id, sub),
            ).fetchone()
            if existing is None:
                pending = [Change("usage_users.user_id", entity, "user_id", principal, sub)]
                if not apply:
                    manifest.record(pending[0])
                    continue
                conn.execute(
                    f"UPDATE {table} SET user_id = %s WHERE org_id = %s AND workspace_id = %s AND user_id = %s",
                    (sub, org_id, workspace_id, principal),
                )
                manifest.stage(pending)
                continue
            pending = [
                Change(
                    "usage_users.user_id",
                    entity,
                    "user_id (merged)",
                    principal,
                    sub,
                    before_image={
                        "legacy": {
                            "email": _value(row, "email"),
                            "first_seen": _stamp(_value(row, "first_seen")),
                            "last_seen": _stamp(_value(row, "last_seen")),
                        },
                        "subject": {
                            "email": _value(existing, "email"),
                            "first_seen": _stamp(_value(existing, "first_seen")),
                            "last_seen": _stamp(_value(existing, "last_seen")),
                        },
                    },
                )
            ]
            note = f"usage_users {entity}: merged into the subject's row, widening first_seen/last_seen"
            if not apply:
                manifest.record(pending[0])
                manifest.merges.append(note)
                continue
            conn.execute(
                f"UPDATE {table} SET first_seen = LEAST(first_seen, %s), last_seen = GREATEST(last_seen, %s), "
                "email = COALESCE(email, %s) WHERE org_id = %s AND workspace_id = %s AND user_id = %s",
                (
                    _value(row, "first_seen"),
                    _value(row, "last_seen"),
                    _value(row, "email"),
                    org_id,
                    workspace_id,
                    sub,
                ),
            )
            conn.execute(
                f"DELETE FROM {table} WHERE org_id = %s AND workspace_id = %s AND user_id = %s",
                (org_id, workspace_id, principal),
            )
            manifest.stage(pending)
            manifest.merges.append(note)


def merge_task_state(conn, mapping: dict[str, str], manifest: Manifest, *, apply: bool) -> None:
    """Move task state onto the subject, keeping the newer row on collision.

    ``payload`` is an opaque document owned by the client. Two payloads cannot be
    merged without inventing state, so the more recently updated row wins and the
    other is named in the manifest rather than silently dropped.
    """

    table = "nexus_task_state"
    if not _table_exists(conn, table):
        manifest.skipped_carriers.append(f"task_state.owner_id: {table} does not exist")
        return
    for principal, sub in mapping.items():
        rows = conn.execute(
            f"SELECT org_id, workspace_id, payload, updated_at FROM {table} WHERE owner_id = %s",
            (principal,),
        ).fetchall()
        for row in rows:
            org_id, workspace_id = _value(row, "org_id"), _value(row, "workspace_id")
            entity = f"{org_id}/{workspace_id}/{principal}"
            existing = conn.execute(
                f"SELECT payload, updated_at FROM {table} "
                "WHERE org_id = %s AND workspace_id = %s AND owner_id = %s",
                (org_id, workspace_id, sub),
            ).fetchone()
            if existing is None:
                pending = [Change("task_state.owner_id", entity, "owner_id", principal, sub)]
                if not apply:
                    manifest.record(pending[0])
                    continue
                conn.execute(
                    f"UPDATE {table} SET owner_id = %s WHERE org_id = %s AND workspace_id = %s AND owner_id = %s",
                    (sub, org_id, workspace_id, principal),
                )
                manifest.stage(pending)
                continue
            legacy_at, target_at = _value(row, "updated_at"), _value(existing, "updated_at")
            legacy_wins = legacy_at is not None and target_at is not None and legacy_at > target_at
            pending = [
                Change(
                    "task_state.owner_id",
                    entity,
                    "owner_id (merged)",
                    principal,
                    sub,
                    before_image={
                        "legacy": {"updated_at": _stamp(legacy_at), "payload": _value(row, "payload")},
                        "subject": {"updated_at": _stamp(target_at), "payload": _value(existing, "payload")},
                        "kept": "legacy" if legacy_wins else "subject",
                    },
                )
            ]
            note = (
                f"task_state {entity}: both rows exist; kept the "
                f"{'legacy' if legacy_wins else 'existing subject'} payload as the newer one"
            )
            if not apply:
                manifest.record(pending[0])
                manifest.merges.append(note)
                continue
            if legacy_wins:
                payload = _value(row, "payload")
                conn.execute(
                    f"UPDATE {table} SET payload = %s, updated_at = %s "
                    "WHERE org_id = %s AND workspace_id = %s AND owner_id = %s",
                    (
                        payload if isinstance(payload, str) else json.dumps(payload),
                        legacy_at,
                        org_id,
                        workspace_id,
                        sub,
                    ),
                )
            conn.execute(
                f"DELETE FROM {table} WHERE org_id = %s AND workspace_id = %s AND owner_id = %s",
                (org_id, workspace_id, principal),
            )
            manifest.stage(pending)
            manifest.merges.append(note)


def merge_task_devices(conn, mapping: dict[str, str], manifest: Manifest, *, apply: bool) -> None:
    """Move device registrations onto the subject, keeping the newer on collision.

    The primary key is ``(org_id, workspace_id, user_id, device_id)``, so a
    collision needs the *same device* already registered under the subject —
    which happens when one machine authenticated before and after the pin. A
    device record is a last-seen fact rather than an accumulation, so the newer
    ``last_seen_at`` wins; the discarded row's payload and timestamps go into the
    manifest as a before-image so the choice is reversible.
    """

    table = "nexus_task_devices"
    if not _table_exists(conn, table):
        manifest.skipped_carriers.append(f"task_devices.user_id: {table} does not exist")
        return
    for principal, sub in mapping.items():
        rows = conn.execute(
            f"SELECT org_id, workspace_id, device_id, payload, last_seen_at, expires_at "
            f"FROM {table} WHERE user_id = %s",
            (principal,),
        ).fetchall()
        for row in rows:
            org_id = _value(row, "org_id")
            workspace_id = _value(row, "workspace_id")
            device_id = _value(row, "device_id")
            entity = f"{org_id}/{workspace_id}/{principal}/{device_id}"
            existing = conn.execute(
                f"SELECT payload, last_seen_at, expires_at FROM {table} "
                "WHERE org_id = %s AND workspace_id = %s AND user_id = %s AND device_id = %s",
                (org_id, workspace_id, sub, device_id),
            ).fetchone()
            if existing is None:
                change = Change("task_devices.user_id", entity, "user_id", principal, sub)
                if not apply:
                    manifest.record(change)
                    continue
                conn.execute(
                    f"UPDATE {table} SET user_id = %s "
                    "WHERE org_id = %s AND workspace_id = %s AND user_id = %s AND device_id = %s",
                    (sub, org_id, workspace_id, principal, device_id),
                )
                manifest.stage([change])
                continue
            legacy_at, target_at = _value(row, "last_seen_at"), _value(existing, "last_seen_at")
            legacy_wins = legacy_at is not None and target_at is not None and legacy_at > target_at
            before = {
                "legacy": {
                    "last_seen_at": _stamp(legacy_at),
                    "expires_at": _stamp(_value(row, "expires_at")),
                    "payload": _value(row, "payload"),
                },
                "subject": {
                    "last_seen_at": _stamp(target_at),
                    "expires_at": _stamp(_value(existing, "expires_at")),
                    "payload": _value(existing, "payload"),
                },
                "kept": "legacy" if legacy_wins else "subject",
            }
            change = Change(
                "task_devices.user_id", entity, "user_id (merged)", principal, sub, before_image=before
            )
            note = (
                f"task_devices {entity}: the same device exists under both; kept the "
                f"{'legacy' if legacy_wins else 'existing subject'} registration as the newer one"
            )
            if not apply:
                manifest.record(change)
                manifest.merges.append(note)
                continue
            if legacy_wins:
                payload = _value(row, "payload")
                conn.execute(
                    f"UPDATE {table} SET payload = %s, last_seen_at = %s, expires_at = %s "
                    "WHERE org_id = %s AND workspace_id = %s AND user_id = %s AND device_id = %s",
                    (
                        payload if isinstance(payload, str) else json.dumps(payload),
                        legacy_at,
                        _value(row, "expires_at"),
                        org_id,
                        workspace_id,
                        sub,
                        device_id,
                    ),
                )
            conn.execute(
                f"DELETE FROM {table} "
                "WHERE org_id = %s AND workspace_id = %s AND user_id = %s AND device_id = %s",
                (org_id, workspace_id, principal, device_id),
            )
            manifest.stage([change])
            manifest.merges.append(note)


def rewrite_postgres(
    conn,
    mapping: dict[str, str],
    manifest: Manifest,
    *,
    apply: bool,
    allow_paths: frozenset[str] | None = None,
) -> None:
    """Run every Postgres carrier in one transaction's worth of work."""

    for carrier, reason in EXCLUDED_CARRIERS.items():
        manifest.skipped_carriers.append(f"{carrier}: {reason}")
    rewrite_text_columns(conn, mapping, manifest, apply=apply)
    rewrite_json_columns(conn, mapping, manifest, apply=apply, allow_paths=allow_paths)
    merge_active_frames(conn, mapping, manifest, apply=apply)
    merge_usage_users(conn, mapping, manifest, apply=apply)
    merge_task_state(conn, mapping, manifest, apply=apply)
    merge_task_devices(conn, mapping, manifest, apply=apply)


# --- Frame sidecars -----------------------------------------------------------


def rewrite_sidecar(
    metadata: dict, mapping: dict[str, str], *, allow_paths: frozenset[str] | None = None
) -> tuple[dict, list[tuple[str, str, str]], list[tuple[str, str]]]:
    """Sweep one raw frame metadata document.

    Raw ``dict`` in, raw ``dict`` out — never through
    :class:`~collab_hub_api.frames.models.Frame`. A record holding a legacy
    scalar ``owner``, a tag that no longer passes ``TAG_PATTERN``, or a reader
    list on an ``internal`` frame is exactly the record a migration must not
    lose, and the model would reject it or silently repair it on the way in.
    Unknown keys survive untouched for the same reason.
    """

    return rewrite_json_document(metadata, mapping, allow_paths=allow_paths)  # type: ignore[return-value]


def rewrite_s3_sidecars(
    s3,
    bucket: str,
    prefix: str,
    mapping: dict[str, str],
    manifest: Manifest,
    *,
    apply: bool,
    allow_paths: frozenset[str] | None = None,
) -> None:
    """Rewrite every ``metadata.json`` under *prefix*, leaving bodies untouched.

    A change is committed to the manifest only after ``put_object`` returns. A
    failed write must not leave the reversal record claiming an object changed.
    """

    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=f"{prefix.strip('/')}/"):
        for item in page.get("Contents", []) or []:
            key = item["Key"]
            if not key.endswith("/metadata.json"):
                continue
            raw = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            metadata = json.loads(raw.decode("utf-8"))
            updated, changes, declined = rewrite_sidecar(metadata, mapping, allow_paths=allow_paths)
            for path, principal in declined:
                manifest.unexpected_paths.append(UnexpectedPath("frame.sidecar", key, path, principal))
            if not changes:
                continue
            pending = [Change("frame.sidecar", key, path, before, after) for path, before, after in changes]
            if not apply:
                for change in pending:
                    manifest.record(change)
                continue
            try:
                s3.put_object(
                    Bucket=bucket,
                    Key=key,
                    Body=(json.dumps(updated, indent=2) + "\n").encode("utf-8"),
                    ContentType="application/json",
                )
            except Exception as exc:  # noqa: BLE001
                manifest.fail(pending, f"frame.sidecar {key}: {type(exc).__name__}: {exc}")
                raise
            manifest.commit(pending)


def rewrite_local_sidecars(
    root,
    mapping: dict[str, str],
    manifest: Manifest,
    *,
    apply: bool,
    allow_paths: frozenset[str] | None = None,
) -> None:
    """Rewrite every ``metadata.json`` beneath *root*, at any depth.

    Walked recursively rather than one level deep: partitioning storage by
    organization (#46) adds a directory level, and a one-level walk would report
    a clean pass over a store it never entered.

    **Symlinks and anything that is not a regular file are refused, and a
    resolved path outside *root* is refused.** A planted ``metadata.json``
    symlink otherwise rewrites a file outside the configured store — the write
    lands on the operator's host, not in the frames store. The replacement is
    atomic (temporary file in the same directory, then ``os.replace``) so an
    interrupted run cannot leave a truncated sidecar, and the original file mode
    is preserved rather than silently tightened or loosened.
    """

    base = Path(root)
    if not base.is_dir():
        manifest.skipped_carriers.append(f"frame.sidecar: {root} is not a directory")
        return
    resolved_base = base.resolve()
    for sidecar in sorted(base.rglob("metadata.json")):
        if sidecar.is_symlink() or not sidecar.is_file():
            manifest.skipped_carriers.append(f"frame.sidecar: refusing {sidecar} (not a regular file)")
            continue
        try:
            if not sidecar.resolve().is_relative_to(resolved_base):
                manifest.skipped_carriers.append(f"frame.sidecar: refusing {sidecar} (escapes {root})")
                continue
        except OSError as exc:
            manifest.skipped_carriers.append(f"frame.sidecar: refusing {sidecar} ({exc})")
            continue
        metadata = json.loads(sidecar.read_text(encoding="utf-8"))
        updated, changes, declined = rewrite_sidecar(metadata, mapping, allow_paths=allow_paths)
        for path, principal in declined:
            manifest.unexpected_paths.append(UnexpectedPath("frame.sidecar", str(sidecar), path, principal))
        if not changes:
            continue
        pending = [Change("frame.sidecar", str(sidecar), path, before, after) for path, before, after in changes]
        if not apply:
            for change in pending:
                manifest.record(change)
            continue
        try:
            _replace_atomically(sidecar, json.dumps(updated, indent=2) + "\n")
        except Exception as exc:  # noqa: BLE001
            manifest.fail(pending, f"frame.sidecar {sidecar}: {type(exc).__name__}: {exc}")
            raise
        manifest.commit(pending)


def _replace_atomically(target: Path, content: str) -> None:
    """Write *content* over *target* without a window where it is truncated."""

    mode = target.stat().st_mode & 0o777
    temp = target.with_name(f".{target.name}.rewrite")
    handle = open_no_follow(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, mode)
        os.replace(temp, target)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def write_private_file(path: str | Path, content: str) -> None:
    """Write *content* to *path* as a private regular file, atomically.

    ``os.open`` applies its mode only when it *creates* the file, so writing
    over an existing world-readable path leaves it world-readable. Symlinks are
    refused rather than followed: the manifest path is operator-supplied, and
    following a link there truncates an unrelated file.
    """

    target = Path(path)
    temp = target.with_name(f".{target.name}.tmp")
    handle = open_no_follow(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, 0o600)
        if target.is_symlink():
            raise UnsafePathError(f"refusing to write through symlink: {target}")
        os.replace(temp, target)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


# --- helpers ------------------------------------------------------------------


def _table_exists(conn, table: str) -> bool:
    row = conn.execute("SELECT to_regclass(%s) AS reg", (table,)).fetchone()
    return _scalar(row) is not None


def _value(row, name: str):
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)


def _scalar(row) -> object:
    if row is None:
        return None
    if isinstance(row, dict):
        return next(iter(row.values()), None)
    return row[0]


def _stamp(value: object) -> object:
    """Render a timestamp for the manifest without assuming its type."""

    return value.isoformat() if hasattr(value, "isoformat") else value


def _as_list(value: object) -> list:
    if isinstance(value, str):
        value = json.loads(value)
    return list(value) if isinstance(value, list) else []


def _union(left: list, right: list) -> list:
    seen = list(right)
    for item in left:
        if item not in seen:
            seen.append(item)
    return seen
