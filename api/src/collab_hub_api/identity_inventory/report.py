"""Rendering: a report a person reads, and a JSON file a tool reads.

The Markdown report is the deliverable. Someone has to sit with it against real
internal-hub data and decide whether Gate D may proceed, so it is ordered by
what that decision needs:

1. the verdict, and every reason it is not ``clear``;
2. **coverage** — what was scanned and, more importantly, what was not, because
   an unscanned source is an unproven claim rather than an absence of findings,
   and it alone forces a non-clear verdict;
3. the **orphan check**, with every finding named and locatable;
4. mappings that cannot be trusted on their own: ambiguity, suspected
   reassignment, and unverified matches;
5. unmapped principals, with the explicit statement that they stay put;
6. the proposed rewrite, labelled with confidence, so the mapping can be
   spot-checked;
7. carrier inventory, including which issue named each carrier.

**Personal data.** The report necessarily lists emails and usernames — that is
what a stored principal *is* — so the rendered file is personal data about staff
and should be handled like the production access records it describes: written
``0600``, kept out of tickets and chat, and deleted once Gate D is decided.
``--redact`` replaces every principal, subject, account label, and every
occurrence of one embedded inside an entity id or a storage path with a stable
``principal:<sha256 prefix>``, so counts, carriers, and orphan structure can be
shared without the identities. That is pseudonymisation, not anonymisation — a
reviewer holding the user list can reverse it — and the redacted report is
therefore safer to circulate, not safe to publish.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime

from .analysis import (
    ORPHAN_DISABLED,
    ORPHAN_NO_OWNERS,
    ORPHAN_REASSIGNED,
    ORPHAN_UNMAPPED,
    ORPHAN_UNVERIFIED_ONLY,
    VERDICT_BLOCKED,
    VERDICT_CLEAR,
    VERDICT_INCOMPLETE,
    VERDICT_NEEDS_CONFIRMATION,
    InventoryAnalysis,
)
from .directory import MappingConfidence, ResolutionStatus
from .scan import CARRIERS

ORPHAN_TITLES = {
    ORPHAN_NO_OWNERS: "No owners recorded (already unmanageable today)",
    ORPHAN_UNMAPPED: "No owner maps to a subject (unreachable after migration)",
    ORPHAN_REASSIGNED: "Owner maps only to an account created AFTER the record (address reassigned)",
    ORPHAN_UNVERIFIED_ONLY: "Every mapping owner is an UNVERIFIED match (needs human confirmation)",
    ORPHAN_DISABLED: "Every mapping owner is a disabled account",
}

ORPHAN_ORDER = (
    ORPHAN_UNMAPPED,
    ORPHAN_REASSIGNED,
    ORPHAN_NO_OWNERS,
    ORPHAN_UNVERIFIED_ONLY,
    ORPHAN_DISABLED,
)

VERDICT_HEADLINES = {
    VERDICT_INCOMPLETE: (
        "**The scan did not cover everything. No migration decision can rest on this report.**\n"
        "A source that was not scanned proves nothing about the records inside it."
    ),
    VERDICT_BLOCKED: "**Blocking findings present. Do not run the migration on this data yet.**",
    VERDICT_NEEDS_CONFIRMATION: (
        "**Needs human confirmation before the migration may run.**\n"
        "Nothing found would be made unreachable outright, but some entities depend on mappings "
        "that a machine cannot verify — see sections 3 and 4."
    ),
    VERDICT_CLEAR: (
        "**No findings.** Every source was scanned, every frame, group, and task owner keeps at "
        "least one owner whose mapping is *certain* (the stored value is already a subject), and "
        "no principal is ambiguous, reassigned, padded, or unverified."
    ),
}


def pseudonym(value: str) -> str:
    """Stable, non-reversible-by-eye stand-in for a principal."""

    return "principal:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


class _Redactor:
    """Replaces identities everywhere they appear, not only where they are a field.

    Entity ids for the active-frame, usage, and task carriers are built *from*
    the principal (``org/workspace/<principal>``), and storage paths can embed
    one too, so redacting only the principal column would leave the identity in
    plain sight one column over. Substitution is longest-first so that one
    identity that contains another cannot be partly replaced.
    """

    def __init__(self, redact: bool, secrets: set[str] | None = None):
        self.redact = redact
        self._secrets = sorted((secrets or set()), key=len, reverse=True)

    def __call__(self, value: str | None) -> str:
        if value is None:
            return ""
        if not self.redact or not value:
            return value or ""
        return pseudonym(value)

    def text(self, value: str | None) -> str:
        """Redact any identity embedded inside free text (ids, paths, labels)."""

        if value is None:
            return ""
        if not self.redact or not value:
            return value or ""
        redacted = value
        for secret in self._secrets:
            if secret and secret in redacted:
                redacted = redacted.replace(secret, pseudonym(secret))
        return redacted


def _secrets_of(analysis: InventoryAnalysis) -> set[str]:
    """Every string whose presence anywhere in the report would identify someone."""

    secrets = {item.principal for item in analysis.principals}
    for user in analysis.unseen_directory_users:
        secrets.update({user.sub, user.username, user.email or ""})
    for summary in analysis.principals:
        for candidate in summary.resolution.candidates:
            secrets.update({candidate.sub, candidate.username, candidate.email or ""})
    secrets.update(analysis.duplicate_directory_emails)
    return {value for value in secrets if value}


def render_markdown(analysis: InventoryAnalysis, *, redact: bool = False, context: dict | None = None) -> str:
    """Render the human-reviewable dry-run report."""

    show = _Redactor(redact, _secrets_of(analysis))
    context = context or {}
    lines: list[str] = []
    out = lines.append

    out("# Collab identity inventory — dry run (issue #65)")
    out("")
    out(f"- Generated: {datetime.now(UTC).isoformat(timespec='seconds')}")
    out("- Mode: **READ-ONLY**. This tool performs no writes to Postgres, S3, the local")
    out("  filesystem, or Keycloak. The production rewrite is Gate D (issue #68).")
    for key, value in context.items():
        out(f"- {key}: {value}")
    out(f"- Directory: {analysis.directory_source or 'unknown'}, {analysis.directory_size} accounts")
    for note in analysis.directory_notes:
        out(f"  - NOTE: {show.text(note)}")
    if redact:
        out("- Principals, subjects, and identities embedded in ids and paths are **redacted**")
        out("  to stable `principal:<hash>` pseudonyms. Pseudonymised, not anonymised.")
    else:
        out("- Contains personal data (emails, usernames). Handle as a production access record.")
    out("")

    out("## 1. Verdict")
    out("")
    out(f"### `{analysis.verdict}`")
    out("")
    out(VERDICT_HEADLINES[analysis.verdict])
    out("")
    if analysis.coverage_gaps:
        out("Coverage gaps and errors, each of which alone prevents a clear verdict:")
        for gap in analysis.coverage_gaps:
            out(f"- {show.text(gap)}")
        out("")
    if analysis.blocking_orphans:
        out(f"- {len(analysis.blocking_orphans)} entities would have no usable owner mapping.")
    if analysis.ambiguous:
        out(f"- {len(analysis.ambiguous)} stored principals match more than one account.")
    if analysis.reassignment_suspects:
        out(f"- {len(analysis.reassignment_suspects)} principals map to accounts newer than the records using them.")
    if analysis.by_kind(ORPHAN_UNVERIFIED_ONLY):
        out(f"- {len(analysis.by_kind(ORPHAN_UNVERIFIED_ONLY))} entities rest only on unverified matches.")
    if analysis.padded:
        out(f"- {len(analysis.padded)} principals are stored with surrounding whitespace.")
    if analysis.scan.data_notes:
        out(f"- {len(analysis.scan.data_notes)} stored-data oddities need a human reading (section 3).")
    out("")
    out("| Measure | Count |")
    out("| --- | ---: |")
    out(f"| Distinct stored principals | {len(analysis.principals)} |")
    out(f"| — already subjects (mapping CERTAIN) | {len(analysis.by_status(ResolutionStatus.already_sub))} |")
    out(f"| — would be rewritten (mapping UNVERIFIED) | {len(analysis.needs_rewrite)} |")
    out(f"| — mapped to an account newer than the record | {len(analysis.reassignment_suspects)} |")
    out(f"| — unmapped (left in place) | {len(analysis.unmapped)} |")
    out(f"| — ambiguous (never auto-mapped) | {len(analysis.ambiguous)} |")
    out(f"| — stored with surrounding whitespace | {len(analysis.padded)} |")
    out(f"| — empty strings stored | {len(analysis.empty)} |")
    out(f"| Identity occurrences | {len(analysis.scan.occurrences)} |")
    out(f"| Frames scanned | {len(analysis.scan.frames)} |")
    out(f"| Groups scanned | {len(analysis.scan.groups)} |")
    out(f"| Task-owner documents scanned | {len(analysis.scan.task_owners)} |")
    out(f"| Orphan findings (blocking) | {len(analysis.blocking_orphans)} |")
    out(f"| Orphan findings (need confirmation) | {len(analysis.orphans) - len(analysis.blocking_orphans)} |")
    out(f"| Directory accounts never seen in any store | {len(analysis.unseen_directory_users)} |")
    out("")

    out("## 2. Coverage")
    out("")
    out("A source that was not scanned proves nothing. Any gap here forces the verdict to")
    out("`incomplete_scan` regardless of what the other sections say.")
    out("")
    out("| Source | Scanned | Detail |")
    out("| --- | --- | --- |")
    for item in analysis.scan.coverage:
        out(f"| {item.source} | {'yes' if item.scanned else '**NO**'} | {show.text(_cell(item.detail))} |")
    out("")
    if analysis.scan.errors:
        out("### Errors during the scan")
        out("")
        out("Each of these is a record that was **not** inventoried.")
        out("")
        for error in analysis.scan.errors:
            out(f"- {show.text(error)}")
        out("")

    out("## 3. Orphan check")
    out("")
    out("Every frame, group, and task-owner document must keep at least one owner that maps")
    out("to a live subject. One that does not is unmanageable after the migration: nobody")
    out("can publish, rename, re-own, or delete it through the API.")
    out("")
    out("A mapping only *clears* an entity when it is **certain** — the stored value is")
    out("already a subject. An email or username match is a proposal, because those claims")
    out("are mutable and reusable; such entities are listed here as needing confirmation.")
    out("")
    if not analysis.orphans:
        out("No findings — every owned record keeps at least one certain, live owner mapping.")
        out("")
    for kind in ORPHAN_ORDER:
        findings = analysis.by_kind(kind)
        if not findings:
            continue
        out(f"### {ORPHAN_TITLES[kind]} — {len(findings)}")
        out("")
        out("| Type | Id | Name | Tenant | Owners (principal → status) | Stored at |")
        out("| --- | --- | --- | --- | --- | --- |")
        for finding in findings:
            owners = (
                ", ".join(f"`{show(item.principal)}` → {item.summary}" for item in finding.owner_status) or "_(none)_"
            )
            tenant = f"{finding.org_id}/{finding.workspace_id}"
            out(
                f"| {finding.entity_type} | `{show.text(finding.entity_id)}` | {_cell(finding.name)} | {tenant} "
                f"| {owners} | {show.text(_cell(finding.origin))} |"
            )
        out("")
    if analysis.scan.data_notes:
        out("### Stored-data oddities")
        out("")
        for note in analysis.scan.data_notes:
            out(f"- {show.text(note)}")
        out("")

    out("## 4. Mappings that cannot be trusted on their own")
    out("")
    out("### 4a. Ambiguous principals")
    out("")
    out("One stored string, two or more accounts. The migration will not choose; picking")
    out("wrong hands one person's frames to another. Resolve each of these by hand.")
    out("")
    if not analysis.ambiguous:
        out("None.")
        out("")
    else:
        out("| Principal | Candidates | Occurrences | Carriers |")
        out("| --- | --- | ---: | --- |")
        for summary in analysis.ambiguous:
            candidates = ", ".join(
                f"`{show(user.label)}` (`{show(user.sub)}`)" for user in summary.resolution.candidates
            )
            out(f"| `{show(summary.principal)}` | {candidates} | {summary.count} | {', '.join(summary.carriers)} |")
        out("")

    out("### 4b. Suspected address reassignment")
    out("")
    out("The matched account was created *after* the record that uses this principal was")
    out("last written, so it cannot be the account that principal named. This is what a")
    out("released-and-reissued address looks like; the mapping is discarded, not weakened.")
    out("")
    if not analysis.reassignment_suspects:
        out("None detected. Note that this check can only see reassignments where the *new*")
        out("account postdates the record — a reassignment within one account's lifetime")
        out("leaves no trace in a point-in-time directory read.")
        out("")
    else:
        out("| Principal | Would have matched | Account created | Earliest record written | Carriers |")
        out("| --- | --- | --- | --- | --- |")
        for summary in analysis.reassignment_suspects:
            user = summary.resolution.user
            created = user.created_at.isoformat(timespec="seconds") if user and user.created_at else "unknown"
            written = (
                summary.earliest_record_written_at.isoformat(timespec="seconds")
                if summary.earliest_record_written_at
                else "unknown"
            )
            label = show(user.label) if user else ""
            sub = show(user.sub) if user else ""
            out(
                f"| `{show(summary.principal)}` | `{label}` (`{sub}`) "
                f"| {created} | {written} | {', '.join(summary.carriers)} |"
            )
        out("")

    out("### 4c. Principals stored with surrounding whitespace")
    out("")
    out("The service compares principals exactly, so a padded string grants nothing today.")
    out("It is reported, never trimmed: trimming during a migration would invent access")
    out("that does not currently exist.")
    out("")
    if not analysis.padded:
        out("None.")
        out("")
    else:
        out("| Stored principal (quoted) | Resolution | Occurrences | Carriers |")
        out("| --- | --- | ---: | --- |")
        for summary in analysis.padded:
            out(
                f"| `{show(summary.principal)!r}` | {summary.resolution.status.value} | {summary.count} "
                f"| {', '.join(summary.carriers)} |"
            )
        out("")

    out("## 5. Unmapped principals — LEFT IN PLACE")
    out("")
    out("These strings match no Keycloak account. They are **not** rewritten and **not**")
    out("removed. `readers` in particular are arbitrary, unvalidated strings: an address")
    out("that never matched an account still records an intent to grant, and deleting it")
    out("during a migration silently revokes access nobody agreed to revoke.")
    out("")
    if not analysis.unmapped:
        out("None.")
        out("")
    else:
        out("| Principal | Grants access? | Occurrences | Carriers | Example location |")
        out("| --- | --- | ---: | --- | --- |")
        for summary in analysis.unmapped:
            example = summary.occurrences[0]
            out(
                f"| `{show(summary.principal)}` | {'yes' if summary.in_acl_carrier else 'no (provenance only)'} "
                f"| {summary.count} | {', '.join(summary.carriers)} "
                f"| {_cell(example.entity_type)} `{show.text(example.entity_id)}` "
                f"{show.text(_cell(example.location))} |"
            )
        out("")

    if analysis.empty:
        out("### Empty principals stored")
        out("")
        out("| Occurrences | Carriers |")
        out("| ---: | --- |")
        for summary in analysis.empty:
            out(f"| {summary.count} | {', '.join(summary.carriers)} |")
        out("")

    out("## 6. Proposed mapping (dry run — nothing is written)")
    out("")
    out("Confidence is not decoration. `certain` means the stored value already *is* the")
    out("subject. `unverified` means it matched a mutable, reusable claim: correct only if")
    out("that address or username never changed hands, which a point-in-time directory read")
    out("cannot establish. Every `unverified` row is a proposal for a human to accept.")
    out("")
    if not analysis.needs_rewrite:
        out("No stored principal would be rewritten.")
        out("")
    else:
        out("| Stored principal | Would become (sub) | Confidence | Matched by | Account | Enabled | Occurrences |")
        out("| --- | --- | --- | --- | --- | --- | ---: |")
        for summary in analysis.needs_rewrite:
            user = summary.resolution.user
            confidence = summary.confidence.value
            if summary.reassignment_suspected:
                confidence = "**DISCARDED — account newer than record**"
            out(
                f"| `{show(summary.principal)}` | `{show(user.sub) if user else ''}` | {confidence} "
                f"| {summary.resolution.status.value.removeprefix('matched_')} "
                f"| `{show(user.label) if user else ''}` | {'yes' if user and user.enabled else '**no**'} "
                f"| {summary.count} |"
            )
        out("")
    already = analysis.by_status(ResolutionStatus.already_sub)
    if already:
        out(f"{len(already)} principals are already subjects (confidence `certain`) and would not change.")
        out("")

    out("## 7. Carrier inventory")
    out("")
    out("Every place an identity string is persisted, what writes it, and which issue named")
    out("it. Provenance matters: a carrier no issue listed is a carrier a migration plan")
    out("would have missed.")
    out("")
    counts: dict[str, int] = {}
    for occurrence in analysis.scan.occurrences:
        counts[occurrence.carrier] = counts.get(occurrence.carrier, 0) + 1
    out("| Carrier | Location | Written by | Named in | ACL? | Occurrences found |")
    out("| --- | --- | --- | --- | --- | ---: |")
    for carrier in CARRIERS:
        out(
            f"| `{carrier.id}` | {carrier.location} | {carrier.written_by} | {carrier.provenance} "
            f"| {'yes' if carrier.acl else 'no'} | {counts.get(carrier.id, 0)} |"
        )
    out("")

    out("## 8. Directory accounts never seen in any store")
    out("")
    out("Accounts that exist in Keycloak but own, read, or acted on nothing. Expected for a")
    out("hub with more accounts than Collab users; listed so the mapping's coverage can be")
    out("sanity-checked from the other direction.")
    out("")
    out(f"{len(analysis.unseen_directory_users)} of {analysis.directory_size} accounts.")
    if analysis.duplicate_directory_emails:
        out("")
        out("**Duplicate emails in Keycloak** (every principal using one of these is ambiguous):")
        for email in analysis.duplicate_directory_emails:
            out(f"- `{show(email)}`")
    out("")

    return "\n".join(lines) + "\n"


def render_json(analysis: InventoryAnalysis, *, redact: bool = False) -> str:
    """Render the same findings as JSON, for diffing successive dry runs."""

    show = _Redactor(redact, _secrets_of(analysis))
    payload = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "read_only": True,
        "verdict": analysis.verdict,
        "clear_to_proceed": analysis.clear_to_proceed,
        "coverage_gaps": [show.text(gap) for gap in analysis.coverage_gaps],
        "directory": {
            "source": analysis.directory_source,
            "accounts": analysis.directory_size,
            "notes": [show.text(note) for note in analysis.directory_notes],
            "duplicate_emails": [show(email) for email in analysis.duplicate_directory_emails],
            "never_seen_in_stores": len(analysis.unseen_directory_users),
        },
        "coverage": [
            {"source": item.source, "scanned": item.scanned, "detail": show.text(item.detail)}
            for item in analysis.scan.coverage
        ],
        "errors": [show.text(error) for error in analysis.scan.errors],
        "data_notes": [show.text(note) for note in analysis.scan.data_notes],
        "totals": {
            "principals": len(analysis.principals),
            "already_sub": len(analysis.by_status(ResolutionStatus.already_sub)),
            "would_rewrite": len(analysis.needs_rewrite),
            "unmapped": len(analysis.unmapped),
            "ambiguous": len(analysis.ambiguous),
            "reassignment_suspected": len(analysis.reassignment_suspects),
            "padded": len(analysis.padded),
            "empty": len(analysis.empty),
            "occurrences": len(analysis.scan.occurrences),
            "frames": len(analysis.scan.frames),
            "groups": len(analysis.scan.groups),
            "task_owners": len(analysis.scan.task_owners),
        },
        "orphans": [
            {
                "kind": finding.kind,
                "entity_type": finding.entity_type,
                "entity_id": show.text(finding.entity_id),
                "name": finding.name,
                "org_id": finding.org_id,
                "workspace_id": finding.workspace_id,
                "origin": show.text(finding.origin),
                "blocking": finding.blocking,
                "owners": [
                    {
                        "principal": show(item.principal),
                        "status": item.status,
                        "confidence": item.confidence,
                        "enabled": item.enabled,
                        "reassignment_suspected": item.reassignment_suspected,
                    }
                    for item in finding.owner_status
                ],
            }
            for finding in analysis.orphans
        ],
        "principals": [
            {
                "principal": show(summary.principal),
                "status": summary.resolution.status.value,
                "confidence": summary.confidence.value,
                "sub": show(summary.resolution.sub) if summary.resolution.sub else None,
                "enabled": (summary.resolution.user.enabled if summary.resolution.user else None),
                "reassignment_suspected": summary.reassignment_suspected,
                "padded": summary.resolution.padded,
                "occurrences": summary.count,
                "sampled": summary.sampled_total is not None,
                "carriers": summary.carriers,
                "grants_access": summary.in_acl_carrier,
            }
            for summary in analysis.principals
        ],
        "carriers": [asdict(carrier) for carrier in CARRIERS],
        "confidence_model": {
            MappingConfidence.certain.value: "the stored value is already a subject; subjects are immutable",
            MappingConfidence.unverified.value: (
                "matched a mutable, reusable email or username; a unique current match is not proof "
                "that the string meant this account when it was written"
            ),
        },
    }
    return json.dumps(payload, indent=2, sort_keys=False) + "\n"


def _cell(value: str) -> str:
    """Keep free text from breaking the Markdown table it lands in."""

    return value.replace("|", "\\|").replace("\n", " ") if value else "—"
