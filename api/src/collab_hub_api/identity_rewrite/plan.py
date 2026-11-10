"""Turn a reviewed inventory report into an executable, refusable plan.

Nothing here touches a store. This module decides *what may be rewritten*, and
its bias is to refuse: a principal reaches the mapping only by being an
unambiguous, unsuspected, non-padded match with a subject attached. Everything
else is either skipped and reported, or stops the run outright.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

REDACTED_PREFIX = "principal:"
"""Prefix :func:`~collab_hub_api.identity_inventory.report.pseudonym` gives a
redacted principal. A report rendered with ``--redact`` describes real findings
with fake values; applying one would rewrite live ACLs to hashes."""

REWRITABLE_STATUSES = frozenset({"matched_email", "matched_username"})
"""The only statuses that produce a rewrite.

``already_sub`` needs none. ``unmapped`` gets none — the string stays. Every
other status is a refusal class below."""

ACKNOWLEDGEABLE_CLASSES = ("ambiguous", "reassignment_suspected", "padded", "blocking_orphan")
"""Findings that stop the run until an operator names them.

Not fatal in themselves — a deployment may legitimately hold an ambiguous
principal it has decided to leave alone — but never resolvable by this tool, and
never safe to pass over silently. Acknowledging one means "I have read this
finding and I am proceeding without rewriting it"."""


class PlanRefusedError(RuntimeError):
    """The report cannot be turned into a plan, or not without acknowledgement."""


@dataclass(frozen=True)
class Mapping:
    """One legacy principal and the subject it becomes."""

    principal: str
    sub: str
    status: str
    confidence: str
    carriers: tuple[str, ...]
    occurrences: int
    grants_access: bool


@dataclass(frozen=True)
class Skipped:
    """A principal deliberately left alone, and why."""

    principal: str
    status: str
    reason: str


@dataclass
class Plan:
    """The rewrite, as data: what changes, what does not, and what was waived."""

    mappings: tuple[Mapping, ...] = ()
    skipped: tuple[Skipped, ...] = ()
    acknowledged: tuple[str, ...] = ()
    source_generated_at: str = ""
    source_verdict: str = ""
    coverage_gaps: tuple[str, ...] = ()
    notes: list[str] = field(default_factory=list)

    @property
    def by_principal(self) -> dict[str, str]:
        """The substitution table the writers apply: legacy value -> subject."""

        return {item.principal: item.sub for item in self.mappings}

    @property
    def collapses(self) -> dict[str, list[str]]:
        """Subjects reached by more than one principal.

        Routine — an email and a username for one person both map to their
        subject — and the reason the primary-key carriers merge rather than
        update. Surfaced so the operator sees the merges coming.
        """

        grouped: dict[str, list[str]] = {}
        for item in self.mappings:
            grouped.setdefault(item.sub, []).append(item.principal)
        return {sub: sorted(names) for sub, names in grouped.items() if len(names) > 1}


def load_inventory(path: str | Path) -> dict:
    """Read an inventory report, refusing one that cannot be applied.

    Two refusals happen here rather than later because both mean the operator
    has the wrong file in hand, and every subsequent check would be reasoning
    about fiction.
    """

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise PlanRefusedError(f"{path} is not an inventory report (expected a JSON object).")
    if "principals" not in payload or "carriers" not in payload:
        raise PlanRefusedError(
            f"{path} does not look like an identity inventory report: no 'principals'/'carriers'. "
            "Generate one with `python -B -m collab_hub_api.identity_inventory --json-output`."
        )
    for entry in payload.get("principals") or []:
        value = entry.get("principal") or ""
        if value.startswith(REDACTED_PREFIX):
            raise PlanRefusedError(
                "This report was rendered with --redact: its principals are pseudonyms "
                f"({value!r}), not the values stored in the deployment. Applying it would rewrite "
                "live ACLs to hashes. Re-run the inventory without --redact."
            )
    return payload


def _refuse_conflicting_subjects(principals: list) -> None:
    """Refuse a report where one principal carries two different subjects.

    Checked before anything is classified, because the substitution table is a
    ``dict`` keyed on the principal: a later entry would silently win, and which
    one won would depend on report order. That is a broken report rather than a
    finding to acknowledge, so there is no waiver for it.
    """

    seen: dict[str, str] = {}
    conflicts: list[str] = []
    for entry in principals:
        value = str(entry.get("principal") or "")
        sub = entry.get("sub")
        if not value or not sub:
            continue
        previous = seen.setdefault(value, str(sub))
        if previous != str(sub):
            conflicts.append(value)
    if conflicts:
        raise PlanRefusedError(
            f"One principal maps to more than one subject in this report: {sorted(set(conflicts))!r}. "
            "The substitution table is keyed on the principal, so one mapping would silently win "
            "depending on report order. Resolve it in the inventory rather than here."
        )


def build_plan(payload: dict, *, acknowledge: set[str] | None = None) -> Plan:
    """Classify every principal in *payload*; refuse unless waivers cover the findings.

    The refusal classes are checked before the mapping is built, so a report
    carrying an unacknowledged ambiguity produces no plan at all rather than a
    plan that quietly omits it.
    """

    acknowledged = set(acknowledge or ())
    unknown = sorted(acknowledged - set(ACKNOWLEDGEABLE_CLASSES))
    if unknown:
        raise PlanRefusedError(f"Unknown acknowledgement {unknown!r}: expected any of {list(ACKNOWLEDGEABLE_CLASSES)}.")

    principals = payload.get("principals") or []
    _refuse_conflicting_subjects(principals)
    mappings: list[Mapping] = []
    skipped: list[Skipped] = []
    found: dict[str, int] = {}

    for entry in principals:
        value = str(entry.get("principal") or "")
        status = str(entry.get("status") or "")
        sub = entry.get("sub")
        if not value:
            continue

        if entry.get("padded"):
            found["padded"] = found.get("padded", 0) + 1
            skipped.append(
                Skipped(
                    value,
                    status,
                    "stored with surrounding whitespace; trimming it to reach an account would invent access",
                )
            )
            continue
        if status == "ambiguous":
            found["ambiguous"] = found.get("ambiguous", 0) + 1
            skipped.append(Skipped(value, status, "two or more subjects match; never migrated automatically"))
            continue
        if entry.get("reassignment_suspected"):
            found["reassignment_suspected"] = found.get("reassignment_suspected", 0) + 1
            skipped.append(
                Skipped(
                    value,
                    status,
                    "the matching account is newer than the data; the address may have been reassigned",
                )
            )
            continue
        if status == "already_sub":
            skipped.append(Skipped(value, status, "already a subject"))
            continue
        if status not in REWRITABLE_STATUSES or not sub:
            skipped.append(Skipped(value, status, "no subject to map to; left in place"))
            continue

        mappings.append(
            Mapping(
                principal=value,
                sub=str(sub),
                status=status,
                confidence=str(entry.get("confidence") or ""),
                carriers=tuple(entry.get("carriers") or ()),
                occurrences=int(entry.get("occurrences") or 0),
                grants_access=bool(entry.get("grants_access")),
            )
        )

    blocking = [item for item in (payload.get("orphans") or []) if item.get("blocking")]
    if blocking:
        found["blocking_orphan"] = len(blocking)

    outstanding = sorted(name for name in found if name not in acknowledged)
    if outstanding:
        detail = ", ".join(f"{name} ({found[name]})" for name in outstanding)
        raise PlanRefusedError(
            f"The report carries findings this tool will not resolve: {detail}. "
            "None of them will be rewritten either way — acknowledging one records that you have "
            "read it and are proceeding without it. Re-run with "
            f"--acknowledge {' --acknowledge '.join(outstanding)} once you have."
        )

    plan = Plan(
        mappings=tuple(mappings),
        skipped=tuple(skipped),
        acknowledged=tuple(sorted(acknowledged)),
        source_generated_at=str(payload.get("generated_at") or ""),
        source_verdict=str(payload.get("verdict") or ""),
        coverage_gaps=tuple(payload.get("coverage_gaps") or ()),
    )
    if plan.coverage_gaps:
        plan.notes.append(
            "The report is not a complete scan: "
            + "; ".join(plan.coverage_gaps)
            + ". Carriers that were not scanned are not rewritten."
        )
    for sub, names in plan.collapses.items():
        plan.notes.append(
            f"{len(names)} principals collapse onto one subject ({', '.join(names)}); rows will be merged."
        )
    return plan
