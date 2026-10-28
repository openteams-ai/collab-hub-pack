# The Track: event schema v1

The Track is the append-only record of one run: which Cogs ran under which
bindings, what came out, which Gates escalated, who decided, and how it ended.
It is the only source of a run's status (ADR-0002 D3), and it is the
accountability record — a Gate signature is worth something because the Track
can say what was signed. This page is the reference for what the Track holds.
The code is `collab_hub_execution.track` and the lifecycle runner that writes
it, `collab_hub_execution.runner`.

## Events

An event is `{run_id, event_type, payload, occurred_at, event_id, sequence, schema}`.
The store assigns `sequence`, a global order that replay follows; `event_id`
is unique, so an event appended twice is refused; `schema` is the version of
the event's shape, `1` for everything this page describes and everything the
engine writes. An event built without a version is `0` — read as written before
v1 — so one rebuilt from an export without it is still lifted rather than taken
for a v1 event it is not. Every event of a
run carries `run_id`; those about one step carry `step`, and those about one
attempt of it carry `attempt`.

Three kinds of event share the Track:

- **run events** move the run's state machine (`docs/cog-execution/states.md`),
  and a run's status is its Track folded through that machine;
- **step facts** record what one step attempt did and produced;
- **worker facts** record the lifecycle of the worker that served it.

### Run events

| Event | Payload | Written when |
|---|---|---|
| `op_submitted` | `op` — the Op as submitted, its steps with their Gates | a run is submitted; one per run, which the store enforces |
| `run_picked_up` | `controller` — the id of the controller that took it, which owns it from here | a controller takes the run, written only if nobody took it or cancelled it first |
| `gate_escalated` | `step`, `attempt`, `reason`, `escalation` (its id), `envelope` (the result the Gate escalated), `usage`, `approvers` (roles that may decide), `gate` (the policy) | a step's Gate escalates its result |
| `gate_decided` | `step`, `outcome` (`approve`, `send_back`, `reject`), `escalation` (the id answered), `actor`, `value` (the findings), `envelope_digest` (a stable id of the result decided on) | a person decides an escalation |
| `completed` | — | every step completed |
| `failed` | `step`, `error`, `reason` (bounded to 1024 characters), and what the failure carried (`problems`, `binding`, `worker_error`, `revise_limit`) | the run fails; `step_failed` precedes it for a step's failure |
| `budget_exceeded` | `step`, `dimension` (`duration`, `tokens`, `cost`), `reason` | a budget stops the run |
| `cancelled` | `actor` | a client cancels |
| `interrupted` | `backend` | the host stopped under `none` and the run cannot resume |
| `retry_requested` | `from_status`, `attempt` (`same` or `new`), `budget_epoch`, `controller` (the one retrying it, which owns it from here) | a run is retried |

`op_submitted` also carries `submitted_by` (`user`, `org_id`, `workspace_id`,
and a `name` for showing) when a client submitted the run through the run API,
and the run's `name` when the client gave it one, which is what scopes the
run to its organization. `cancel_requested` (`actor`) is what the API records
when a client asks for a run to be cancelled: it leaves the run's state where
it is, and the controller that reads it ends the run, which `cancelled`
records. `turn_requested` (`turn`, `text`, `actor`) is a client's turn for a Cog that
holds a session; the controller records `turn_answered` (`turn`, `text`) or
`turn_failed` (`turn`, `error`) once the worker answered or could not. None of
the three moves the run. A cancel request is written with the store's conditional append (`append_if`), which
checks the run's Track and appends in one step: the request is recorded only
if the run has not ended and holds no request yet, so none is ever written
after a run's end and two racing requests leave one. A request belongs to the
attempt it was made of: one a run outlived, by ending on its own and being
retried, does not cancel the retry.

`decision_requested` (`escalation`, `outcome`, `actor`, `findings`) is a
client's decision on the Gate a run waits at, written with `append_if` only
while the run waits on that escalation and holds no other decision. The
controller that owns the run delivers it, and the runner records
`gate_decided`; when the runner does not take it, the controller records
`decision_refused` (`escalation`, `error`). Neither moves the run. A decision
is pending only while the run waits on the escalation it names: one left
undelivered when the run ends, or when a retry opens a new escalation, lapses
with it, and is neither delivered nor refused.

`retry_asked` (`actor`) is a client's request that an ended run run again,
written with `append_if` only while the run has ended in a state the run
machine retries from and holds no request yet. A controller takes it: the
runner records `retry_requested` as the run moves, or the controller records
`retry_refused` (`error`) when the runner would not. Neither moves the run.

A step's Frames travel by reference: `op_submitted`'s Op carries each step's
`frames`, with the Frame's `id`, `name`, the `digest` of the snapshot taken
at submit and the `ref` it is kept under beside the Track, like a large
payload; `step_completed` names the `id` and `digest` of each Frame the step
was given. The text itself is never on an event.

**The index of runs.** Each store keeps one row per run, derived from the
run's events by the store itself in the same transaction as any event that
moves the run: its status, the organization that submitted it, when it was
submitted and last moved, and whether a retry is asked for. Status is still
the Track's, replayed through the run machine; the row is a projection a
listing or a controller's pass reads instead of every Track, and `reindex()`
rebuilds it from the events. On Postgres it is `collab_track_runs`, from
migration 15. An API process checks once, at its first listing, whether a
submitted run has no row, and reindexes if so: what a Track from before the
index needs. On the upgrade that brings the index, a run an old API pod
accepts after a new pod has checked stays unlisted until an API pod restarts,
which heals it; `run show` reads the Track and is unaffected.

A Track written before controllers named themselves has `run_picked_up` with
no `controller`: such a run belongs to the first controller to start
([runs.md](runs.md#pickup-and-ownership)).

### Step facts

| Event | Payload |
|---|---|
| `step_started` | `step`, `cog`, `digest`, `attempt` |
| `interaction_usage` | `step`, `attempt`, `usage` — what one interaction spent, `null` when unknown |
| `step_completed` | `step`, `attempt`, `cog`, `digest`, `binding`, `problems`, `usage`, `frames`, `escalation` (when an approval completed it), and the result: `payload` inline, or `payload_ref` naming where it is kept |
| `step_failed` | `step`, `attempt`, `key` (the idempotency key), `cog`, `digest`, `error` (the envelope's code, or the exception's class), `message` (bounded to 1024 characters), `problems` and `binding` when the failure carried them, and `teardown_error` when the worker could not be torn down either — the step's own failure is kept, not replaced |

`step_completed` alone answers "what produced this": the Cog and its digest,
the binding, the problems the Cog reported, the usage it spent, and the Frames
it was given (`frames` is empty until Frames are delivered to steps). `label`
is reserved on step events for sensitivity labels (#13) and is not written yet.

**Payloads by reference.** A result whose JSON is larger than the engine's
`payload_inline_max_bytes` (64 KiB by default) is kept beside the Track, and
the event carries `payload_ref` instead of `payload`;
`TrackStore.get_payload(ref)` returns it. The reference is the attempt's
idempotency key, so retrying an interrupted attempt rewrites the same row instead of
leaving another one, and at most one is ever kept per attempt. The result is
rendered as JSON once, and that is what is measured and kept; a payload that
is not JSON fails the step as `EnvelopeInvalid`, and the usage the worker
reported with it is still recorded on `interaction_usage`, since it was spent.
An escalated result follows the same rule: `gate_escalated` carries the
`payload_ref`, and its `envelope` holds `payload: null` and a `payload_digest`
of the result, so the digest a decision names still identifies it. The digest
is the sha256 of the payload's canonical JSON (sorted keys, `,` and `:` as
separators), so it can be recomputed from `get_payload(ref)` on every store,
Postgres included, whose `jsonb` does not keep key order. Approving it completes
the step from the row already kept. Everything else about the step stays on the event, so a
reader that never fetches payloads still knows what produced them.

### Worker facts

`materialized` (`cog`, `digest`), `ready` (`cog`), `interaction_started`
(`step`, `entry_point`), `idle` (`step`), `teardown_started` (`step`,
`reason`) and `teardown_failed` (`step`, `error`) are the worker machine's
records. They leave the run's state where it is.

A worker that is a process or a workload of its own has two more, written by
the runner around the executor's calls:

| Event | Payload |
|---|---|
| `worker_started` | `step`, `attempt`, `instance`, `location`, `run_token_sha256`, and where the worker is: at `local`, `package` (`name`, `digest`), `pid`, `pgid` and `logs`, the directory its stdout and stderr go to |
| `worker_stopped` | `step`, `attempt`, `instance` — the worker was torn down, and its run token has expired; `reaped` when a starting controller wrote it for a run it interrupted, `true` if it killed a worker left alive |

`run_token_sha256` is the hash of the worker's run token; the token itself is
never recorded. A token is valid while its `worker_started` has no
`worker_stopped` and the run has not ended or been interrupted
([runs.md](runs.md#agent-locations)). An in-memory worker records neither.

## Reading a Track written before v1

Events written before schema v1 carry `schema` 0 and are never rewritten.
`collab_hub_execution.track.upgrade` reads one in the v1 shape, keeping its
identity, sequence and time:

| Written as | Read as |
|---|---|
| `submitted` | `op_submitted` |
| `paused` | `gate_escalated` |
| `signal_received` | `gate_decided` with `outcome: send_back` |
| `rejected` | `gate_decided` with `outcome: reject` |
| `timed_out` | `budget_exceeded` with `dimension: duration` |
| `step_completed` with `output` | `step_completed` with `payload` |

`derive_run_status` and the engine read every Track through it, so a run left
waiting by an older engine can still be decided. A pre-v1 escalation has no
`escalation` id, `envelope` or `approvers`: it reads with those empty, a
decision names its id as `None`, and an approval is refused since no result was
recorded to accept — a send back asks for the work again.

## Stores

`TrackStore` is one protocol with three implementations, and one conformance
suite (`execution/tests/test_track_conformance.py`) runs against all of them:

- **`InMemoryTrackStore`** — tests and one-process use.
- **`SqliteTrackStore`** — one file, WAL mode, shared by the processes of one
  host: dev level 1 will run the API and the run controller over
  `dev/.local/track.sqlite`, and the desktop's local run host keeps its Track
  the same way. `SqliteTrackStore.ensure_schema(path)` creates the file and
  its tables.
- **`PostgresTrackStore`** — the hub. Its tables, `collab_track_events` and
  `collab_track_payloads`, come from the `collab_` migration registry
  (version 12) that the API runs at startup, never from the store's own
  `ensure_schema`, which exists for the standalone package and local use. The
  store's `_SCHEMA` is the registry's Track statements in order, and
  `execution/tests/test_track_ddl.py` holds them equal, and the SQLite tables to
  the same columns. A released migration is frozen by its checksum, so a change
  to the Track tables is a new migration with the same statements appended to
  `_SCHEMA`, never an edit to version 12. The store reads rows by position or
  by name, so it works on the hub's shared pool, which returns mappings.

Every store assigns sequences, refuses a duplicate `event_id`, and refuses a
second `op_submitted` for a run (`OneSubmissionPerRun`), so two callers can
never both start it.

**A stream never skips an event.** A reader follows a run with a cursor: the
last sequence it saw. On Postgres a sequence is drawn when a row is inserted
but the row appears when it commits, and two transactions can commit in the
other order — a reader that saw the later one would move past the earlier one
for good. So `PostgresTrackStore` serializes appends to one run: each takes a
transaction-scoped advisory lock on the run before its insert draws a
sequence, and holds it until it commits, so within a run sequence order is
commit order. Appends to different runs do not wait for each other. SQLite and
the in-memory store already write one event at a time.
