"""Execution contracts shared by the Hub's Cog and Op implementations.

Experimental: Python interfaces, the worker protocol, and event schemas are
subject to breaking changes. See execution/README.md for current limitations.

Guarantees and non-goals
------------------------
``LifecycleRunner`` runs Ops on the durability backend its configuration names.
Only ``none`` is built, and it is single-owner and not durable:

- **Not durable.** Nothing survives the host. When a host starts, ``start()``
  records every run a stopped host left running or waiting at a Gate as
  ``interrupted``; none of them resumes. ``retry()`` continues an interrupted
  run's attempt under its idempotency key, and runs a failed step as a new
  attempt with a new key. Submitting a run again never resumes it.
- **Single-owner.** It holds no cross-replica lease, so one run must be advanced
  by one owner at a time. The Postgres Track's one-submission-per-run index guards
  a duplicate *submission*, not two callers concurrently *advancing* the same run.
- **Once per attempt, through the keyed claim.** A worker reserves its
  attempt's idempotency key on the hub before it acts and commits its envelope
  after (``claims``), so a retried attempt is answered with the envelope it
  already produced, and one whose worker was lost between acting and committing
  ends ``outcome-unknown`` instead of acting twice. A worker given no hub to
  claim against acts on every invocation. Terminal runs are immutable apart from
  ``retry()``; runs that exhausted a duration/token/cost budget require a new run id.

Do not use it for multi-replica production execution until the ``dbos`` backend
(#104) lands. ``WorkflowEngine`` and the Track, executor and backend interfaces
separate these concerns, but their shapes are still experimental.
"""

from . import intents
from .backends import DURABILITY_BACKENDS, BackendNotImplemented, DurabilityBackend
from .binding import (
    BindingResolutionError,
    CapabilityRequirement,
    ContextCog,
    DeclaredCapabilityResolver,
    ModelBinding,
    ModelCog,
)
from .claims import (
    CLAIM_RETENTION,
    Claim,
    ClaimRefused,
    ClaimStore,
    InMemoryClaimStore,
    PostgresClaimStore,
    SqliteClaimStore,
)
from .envelope import (
    ENVELOPE_VERSION,
    ERROR_CODES,
    EnvelopeError,
    EnvelopeInvalid,
    Problem,
    ResultEnvelope,
)
from .gates import DEFAULT_APPROVERS, Gate, GateOutcome, envelope_digest, escalation_id
from .kubernetes import KubernetesCogExecutor, cog_slug, label_value, resource_name
from .lifecycle import (
    BudgetExceeded,
    BudgetTracker,
    RunBudget,
)
from .locations import AGENT_LOCATIONS, LocationNotImplemented
from .ops import InMemoryCogExecutor, OpDefinition, OpStep, WorkflowEngine
from .runner import STEP_FUNCTIONS, LifecycleRunner, UsageUnavailable
from .states import (
    CogInstall,
    InstallState,
    InvalidTransition,
    Run,
    RunState,
    StaleEscalation,
    StepAttempt,
    StepAttemptState,
    Worker,
    WorkerState,
)
from .track import (
    PAYLOAD_INLINE_MAX_BYTES,
    SCHEMA_VERSION,
    InMemoryTrackStore,
    OneSubmissionPerRun,
    PostgresTrackStore,
    SqliteTrackStore,
    TrackEvent,
    TrackStore,
    derive_run_status,
    upgrade,
)

__all__ = [
    "CLAIM_RETENTION",
    "Claim",
    "ClaimRefused",
    "ClaimStore",
    "InMemoryClaimStore",
    "PostgresClaimStore",
    "SqliteClaimStore",
    "BindingResolutionError",
    "CapabilityRequirement",
    "ContextCog",
    "DeclaredCapabilityResolver",
    "BudgetExceeded",
    "BudgetTracker",
    "InMemoryTrackStore",
    "OneSubmissionPerRun",
    "PAYLOAD_INLINE_MAX_BYTES",
    "SCHEMA_VERSION",
    "SqliteTrackStore",
    "upgrade",
    "PostgresTrackStore",
    "RunBudget",
    "CogInstall",
    "InstallState",
    "InvalidTransition",
    "Run",
    "RunState",
    "StaleEscalation",
    "StepAttempt",
    "StepAttemptState",
    "Worker",
    "WorkerState",
    "TrackEvent",
    "TrackStore",
    "derive_run_status",
    "ModelBinding",
    "ModelCog",
    "AGENT_LOCATIONS",
    "intents",
    "LocationNotImplemented",
    "DURABILITY_BACKENDS",
    "BackendNotImplemented",
    "DurabilityBackend",
    "LifecycleRunner",
    "STEP_FUNCTIONS",
    "InMemoryCogExecutor",
    "ENVELOPE_VERSION",
    "ERROR_CODES",
    "EnvelopeError",
    "EnvelopeInvalid",
    "Problem",
    "ResultEnvelope",
    "UsageUnavailable",
    "OpDefinition",
    "OpStep",
    "DEFAULT_APPROVERS",
    "Gate",
    "GateOutcome",
    "envelope_digest",
    "escalation_id",
    "WorkflowEngine",
    "KubernetesCogExecutor",
    "cog_slug",
    "label_value",
    "resource_name",
]
