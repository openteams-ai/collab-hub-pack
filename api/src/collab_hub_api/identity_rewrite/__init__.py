"""The production identity rewrite: legacy principals become OIDC subjects (issue #68).

The read-only inventory (issue #65, :mod:`~collab_hub_api.identity_inventory`)
answers *what would break*. This package performs the rewrite it describes. It
is the writing counterpart of that tool and the mirror image of its guarantees:
where the inventory is arranged to be structurally incapable of writing, this
one is arranged so that every write is derived from a reviewed plan, recorded,
and reversible.

**Every rewrite this tool performs rests on an unverified match.** The mapping
confidence model has exactly two levels: ``certain`` means the stored value is
already a subject — nothing to migrate — and ``unverified`` means it matched a
mutable, reusable email or username. So by construction, *the only principals
this tool ever changes are the ones whose mapping is unverified*. That is the
nature of the problem rather than a defect in the mapper, and it decides the
shape of everything here:

* **The plan is an input, not a derivation.** The tool consumes the inventory's
  ``--json-output`` and never contacts Keycloak. There is exactly one place a
  legacy principal is turned into a subject, it is the read-only tool, and its
  output is reviewed by a person before it reaches this one.
* **Dangerous classes are refused, not resolved.** ``ambiguous`` (two subjects
  match), ``reassignment_suspected`` (the account is newer than the data) and
  ``padded`` (the stored value carries whitespace) are never rewritten, and
  their mere presence stops the run until an operator acknowledges them by
  name. Trimming a padded value to reach an account would *invent* access.
* **Unmapped principals are left exactly as they are.** A reader address that
  never matched an account still expresses an intent to grant; deleting it
  during a migration silently revokes access nobody agreed to revoke.
* **A match is substituted only where an identity belongs.** Documents are swept
  whole — that is how the scanner found the carrier its first draft missed — but
  a value equal to a mapped principal is *rewritten* only under an
  identity-bearing key, or at a path an operator named with ``--allow-path``. A
  ``description`` that happens to equal an address is a coincidence, not a
  principal; it is reported, left alone, and the run exits non-zero.
* **Nothing is written without ``--apply``.** The default run plans, reports and
  exits.
* **A redacted report is refused.** ``--redact`` replaces every principal with
  ``principal:<hash>``; applying one would rewrite real ACLs to hashes. The
  prefix is detected and the run refuses rather than trusting the operator to
  have used the right file.

**Idempotent by construction, which is why there is no progress ledger.** The
plan is keyed on legacy principals, and a subject is never itself a legacy key,
so a completed rewrite leaves nothing for a second run to match. Re-running is a
no-op that reports zero changes, which delivers "safely retried" without
tracking state across an interruption — an interrupted run is resumed by running
it again. What an interrupted run needs instead is the manifest below.

**The manifest is a diagnostic and a targeted-reversal aid — not the
authoritative rollback mechanism.** Saying so plainly matters more than the
stronger claim would: an operator who believes the record is complete will not
take the snapshot that actually gets them home.

What it does give you. Every change is recorded as ``(carrier, entity, location,
before, after, state)``, where ``state`` distinguishes ``planned`` from
``pending`` from ``committed`` from ``failed``. A database statement running is
not a commit, so its entry is ``pending`` until the surrounding transaction
commits and only then promoted — a rollback demotes the whole batch to ``failed``
rather than leaving the record asserting changes the database discarded. Object
and file writes *are* their own commit and are recorded when they return. A
collision merge additionally carries a ``before_image`` holding **both** rows as
they were, because two rows becoming one cannot be reversed from the principal
pair, and because the surviving row's payload can itself be overwritten by the
merge. The destination is reserved before the first mutation, so a run cannot
change data and then discover it has nowhere to record it.

What it does not give you, stated so nobody plans around it:

* **It is not durable across an abrupt termination.** The file is written at
  startup and at termination. A run killed mid-way can leave committed file or
  object writes absent from it. Making it durable would mean checkpointing intent
  and outcome around every independent commit — a journal, not a report — and
  that is deliberately not what this is.
* **It is not a complete pre-image of every carrier.** The merges capture both
  rows; the ordinary substitutions capture the value they replaced, not the whole
  record.
* **So after an interrupted run, the authoritative description of what is stored
  is a fresh read-only inventory**, and the authoritative way back is verified
  database and frame-store backup/restore. The runbook takes the snapshot first
  for exactly this reason.

**Primary keys are merged, not overwritten.** Three carriers are primary-key
components — ``frames_server_active_frames.user_id``,
``frames_server_usage_users.user_id`` and ``nexus_task_state.owner_id``. Two
legacy principals routinely map to one subject (an email and a username for the
same person), and a person may already hold rows under both a legacy principal
and their subject. A plain ``UPDATE`` there raises a unique violation at best
and silently drops a row at worst, so each of those carriers has an explicit
merge rule chosen for its data — see :mod:`.writers`.

**Not in scope: relocating frame objects.** Partitioning storage by
organization (openteams-ai/collab-hub-pack#46) moves the same objects this tool
rewrites — but they are separate operations with separate failure modes, and
fusing them into one window would mean
a mapping error and a path error share a blast radius.
"""

from __future__ import annotations

from .plan import (
    ACKNOWLEDGEABLE_CLASSES,
    REDACTED_PREFIX,
    REWRITABLE_STATUSES,
    Mapping,
    Plan,
    PlanRefusedError,
    Skipped,
    build_plan,
    load_inventory,
)
from .writers import (
    CARRIER_COVERAGE,
    COMMITTED,
    FAILED,
    PENDING,
    PLANNED,
    Change,
    Manifest,
    UnexpectedPath,
    rewrite_json_document,
    write_private_file,
)

__all__ = [
    "ACKNOWLEDGEABLE_CLASSES",
    "CARRIER_COVERAGE",
    "COMMITTED",
    "FAILED",
    "PENDING",
    "PLANNED",
    "REDACTED_PREFIX",
    "REWRITABLE_STATUSES",
    "Change",
    "Manifest",
    "Mapping",
    "Plan",
    "PlanRefusedError",
    "Skipped",
    "UnexpectedPath",
    "build_plan",
    "load_inventory",
    "rewrite_json_document",
    "write_private_file",
]
