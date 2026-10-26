# Experimental Cog and Op execution

This package is a reference implementation for local integration and discovery.
Python interfaces, the `/invoke` protocol, and Track event schemas may change
without backward compatibility guarantees.

## Running, stopping, and what a restart does

`LifecycleRunner` runs Ops on the durability backend its `backend` setting
names — `none`, `dbos` or `temporal`; only `none` is built, and the other two
are refused when the runner is constructed. `submit()`, `decide()` and
`retry()` run synchronously until the run completes, fails, waits at a Gate or
is cancelled, and return the run's state.

**`none` keeps nothing across a restart.** When a host starts, it calls
`start()`, which records every run the Track shows running or waiting at a
Gate — one a stopped host left behind — as `INTERRUPTED`, and returns their
ids. Such a run is never resumed: submitting it again returns its state, and it
continues only through `retry()`, which continues the attempt that was in
flight under its idempotency key. A run waiting at a Gate cannot survive a
restart either (decision 3 of the plan): once interrupted, its retry runs the
escalated step again. Submitting a run that was submitted and never picked up
starts it. See [`docs/cog-execution/runs.md`](../docs/cog-execution/runs.md).

**`cancel(run_id, actor=...)`** ends a run `CANCELLED` and records the actor. A
run this host is advancing is cancelled at its next step boundary: its live
worker is torn down at once, a worker still being brought up is torn down
before it is invoked, and the call advancing it records `cancelled` without
keeping the result of an interaction that was in flight. A run that is
submitted, running or waiting at a Gate and not advancing here is cancelled at
once; an ended run cannot be, even one that ends while the request is on its
way. A worker the cancel could not tear down is tried again, and recorded as a
`step_failed` with `TeardownFailed` beside `cancelled` if that fails too.

Every call that moves a run claims it first, so two calls in one host never
move one run at once: a second `submit` waits for the first to write the
submission, is refused if its Op differs, and otherwise returns the status; a
second `retry` or `decide` is refused; and `start()` leaves a claimed run alone. See
[`docs/cog-execution/runs.md`](../docs/cog-execution/runs.md#one-run-one-call-at-a-time).

A runner named for a controller (`controller=`) shares its Track with other
controllers: it picks a run up only if nobody has, moves only the runs it
picked up, and `start()` interrupts only those, reaping their workers left
alive. An unnamed runner is a host alone on its Track, and `start()` takes
every unfinished run as its own. The reference worker does not persist results by key, so
a retried attempt may repeat a completed side effect until the keyed claim
(#102) answers for it.

`retry()` starts a new attempt for a failed run. Completed runs and runs that
exhausted their duration, token, or cost budget cannot be retried; start a new
run with a new id. (The run machine allows a retry after a budget stop as a new
budget epoch; the engine does not offer it until #4 builds epochs.) Budgets are
not reset by retrying. Duration is checked at step boundaries; it does not interrupt an interaction already in progress.
Token and cost accounting happens after an interaction and can overshoot.

## The run controller and intents

`collab-hub-run-controller --track FILE_OR_URL --id NAME --packages DIR --work-dir DIR [--models FILE]`
(or `python -m collab_hub_execution.controller`) is the process that advances
runs (ADR-0002 D4). It watches a Track, a SQLite file or Postgres (install the
`postgres` extra), picks up each run that was submitted and not picked up, and
delivers what was asked of the runs it owns: cancels, turns, and decisions on
Gates. Several controllers share a Track, each under its own id, and one picks
up each run. `--models` is the hub's `models:` block (`binding.ModelsBlock`),
and `--health-port` serves `/healthz` and `/readyz`.
`collab_hub_execution.intents` is the other half, used by the hub's API:
`submit()`, `request_cancel()`, `request_turn()` and `request_decision()`
record what a client asked for, and `describe()` and `list_runs()` read runs
back, each step with its state and its output, and the controller that owns
it. Neither half calls the other; the Track is all they share. See
[`docs/cog-execution/runs.md`](../docs/cog-execution/runs.md#the-run-controller-and-the-run-api).

## The lifecycle runner

The lifecycle lives in `LifecycleRunner` (`collab_hub_execution.runner`), as
plain step functions registered in `STEP_FUNCTIONS`, in the order an attempt
reaches them: `resolve` names the step attempt and its idempotency key,
`materialize` brings up its worker, `interact` invokes the entry point,
`read_envelope` checks the answer and accounts for its usage, and `teardown`
releases the worker, whatever happened, since it is one-shot. Only then does
the step end: `fail` when it produced no result; otherwise `evaluate_gate` asks
the step's Gate, which leads to `complete` or `escalate`. Outside an attempt,
`complete_approved` completes a step from an approved escalation, and
`stop_for_budget` stops a run at a boundary its budget has passed. Each step function moves the state machines below by their
transitions and writes the records they return; none assigns a state itself.

`LifecycleRunner` implements the `WorkflowEngine` contract (`submit`, `decide`,
`retry`, `cancel`, `observe`, `open_escalation`) itself; `DurableWorkflowEngine`,
which recovered runs by replaying the Track on a resubmit, is gone with that
recovery. The step functions are sequenced by the runner's driver (`_advance`),
which is lifecycle logic too: it picks the run up and completes it, skips
completed steps, completes an approved escalation, checks and consumes the
budget at step boundaries, maps a failure to the attempt's outcome, and ends a
run cancelled at a step boundary. Every durability backend (ADR-0002 D1,
`collab_hub_execution.backends`) shares that one driver: the driver hands each
step function to `DurabilityBackend.run_step`, and the backend decides only how
it is scheduled and whether its result is checkpointed. `none` calls it in
process. Callers never import a backend; the `backend` setting is the only
switch. The Op and the seam's types —
`OpDefinition`, `OpStep`, `CogWorker`, `CogExecutor`, `InMemoryCogExecutor` —
are in `collab_hub_execution.ops`.

## States

Every state is one of four state machines in `collab_hub_execution.states`,
built on the state pattern: a Cog's install (`CogInstall`), a worker
(`Worker`), a step attempt under the keyed claim (`StepAttempt`) and a run
(`Run`). Each state is a class behind its machine's interface; the context
object delegates every event to its current state, and an event the state does
not accept raises `InvalidTransition`, naming both. Transitions are pure: an
event returns a `Transition(after, records)` and the lifecycle runner writes the
records to the Track. The states, their transitions and what each records are
[`docs/cog-execution/states.md`](../docs/cog-execution/states.md), which a test
holds to the code.

The engine returns and `observe()` reports a `RunState` — `RunState.RUNNING`,
`RunState.WAITING_AT_GATE` and so on — sent on the wire as its name in lower
case. `observe()` is the run machine folded over the Track (`Run.replay`, or
`derive_run_status` over a Track), and `None` for a run never submitted. A
duration stop is `RunState.BUDGET_EXCEEDED`, recorded as `budget_exceeded`
with `dimension: duration`. The worker's own states are not the
run's: between steps, or while a worker idles, the run is `RUNNING`.

Each call reads the Track once to act on it.

## Gates and decisions

A Cog cannot pause a run. It reports `problems` in its envelope, and the step's
`Gate` decides what they mean. `OpStep.gate` defaults to `Gate()`, which
escalates any problem with severity `error`; `Gate(escalate=...)` takes
`"never"`, `"error"`, `"warn"` (any problem) or `"always"` (a sign-off on every
result), and `approvers` names the roles that may decide — none means
organization owners and platform operators (`DEFAULT_APPROVERS`). A result
passes, passes with its problems recorded, or escalates.

An escalation records the step, the attempt, the envelope, the Gate's reason
and its approvers, under an escalation id minted over the attempt and the
envelope; `open_escalation(run_id)` returns it, and is on the engine contract
beside `decide()` so a caller can find what a decision must name. An escalation
recorded before Gates existed has no id, envelope or approvers: it reads with
those fields empty, a decision names its id as `None`, and an approval runs the
step again, since no envelope was recorded to complete it with. `decide(run_id, escalation=,
actor=, outcome=, findings=)` answers it:

- `approve` completes the step with the envelope the approver saw — the step
  does not run again — and the run goes on. An escalation recorded before Gates
  holds no result, so approving one is refused: send it back, which asks for the
  work again, or reject it;
- `reject` ends the run `REJECTED`;
- `send_back` re-runs the step with the findings as its `signal`, under a new
  attempt key, and its next result goes through the Gate again, with a new
  escalation id.

`findings` is a sequence of findings. One string, a mapping or a set is refused
rather than sent back as its characters, its keys, or an arbitrary order;
`None` is no findings at all.

Each decision is recorded with its escalation, the actor, the outcome, the
findings and `envelope_digest`, a stable id for the result it decided on, so a
reader of decisions identifies that result without joining to the escalation.

A budget stop never discards a result that was paid for. When an interaction
crosses a spending limit and its result needs review, the escalation is
recorded, and the stop lands at the next boundary — the end of the run
included — by when no further work has been spent. So an approval, which spends
nothing, keeps the work, and the run then stops on its budget.

A Gate recorded on the Track is read, never refused, so a run stays decidable
even after a rollback: a policy this engine does not know reads as `always`,
the strictest, and approvers that are not role names read as none declared. A
Gate a caller *declares* is still refused.

A decision naming an escalation that is no longer open raises
`StaleEscalation` and changes nothing, so a late approval never approves a
revision its reviewer did not see. With `max_revisions=N`, a step may be sent
back N times; the next send back ends the run `FAILED` with error
`revise_limit_exceeded`. Who may decide is the run API's to check (#103); the
engine records who did.

## The Track

Every write to the Track is an event of schema v1
([`docs/cog-execution/track.md`](../docs/cog-execution/track.md)). Three kinds
share it: run events, which are the run machine's records (`gate_escalated`,
`gate_decided`, `failed`, `budget_exceeded` and the rest); step facts, which say
what one attempt did and produced; and worker facts. `step_completed` alone
names what produced a result — the Cog, its digest, the binding, the problems,
the usage, the Frames — with the result inline or, above
`payload_inline_max_bytes` (64 KiB by default), kept by reference and named by
`payload_ref`; `TrackStore.get_payload` returns it. `step_failed` records a
step's failure with its idempotency key, the Cog, the error code or exception
class and a message bounded to 1024 characters, before the run's own `failed`.

A Track written before v1 is never rewritten: `track.upgrade` reads its events
in the v1 shape, and everything that reads a Track goes through it.

Three stores implement `TrackStore` and pass the same conformance suite:
`InMemoryTrackStore`, `SqliteTrackStore` (one file in WAL mode, for the
processes of one host; `SqliteTrackStore.ensure_schema(path)` creates it) and
`PostgresTrackStore`. On the hub the Postgres tables come from the API's
migration registry, never from the store's `ensure_schema`. Every store assigns
sequences, refuses a duplicate event id, and refuses a second `op_submitted`
for a run with `OneSubmissionPerRun`.

## The result envelope

Every `CogWorker.interact()` returns a `ResultEnvelope`, the version-1 envelope
of [`docs/cog-execution/result-envelope.md`](../docs/cog-execution/result-envelope.md).
Over HTTP a worker answers `/invoke` with that envelope as JSON, for example:

```json
{"envelope": 1, "ok": true, "payload": {"answer": "..."}, "problems": [], "usage": {"tokens": 10, "cost": 0.002}}
```

`ResultEnvelope.parse` reads it: the version is `envelope`, unknown fields are
ignored, and anything that is not an envelope — no version, `ok` not a
boolean, `ok: false` without an `error`, `ok: true` with one — fails the step
as `EnvelopeInvalid`. In-memory handlers return a `ResultEnvelope` or an
envelope-shaped mapping; any other value becomes the payload of a successful
envelope with unknown usage.

`ok: false` needs `error: {code, detail}`, with the code one of the five the
envelope document lists — any other code is an invalid envelope, not a new
kind of failure. The step fails: `step_failed` records the code, the detail,
the key and the worker, and the run's `failed` event keeps the code as its
`error` and the detail as its `reason`. These rules hold on construction
as well as on parsing, so an envelope a worker builds in process cannot
sidestep them.
`ok: true` with a non-empty `problems` list is **not** a failure: the step
completes, and the problems are recorded on `step_completed` for a Gate to
decide — see *Gates and decisions*. A `binding`, when the worker reports one,
is recorded there too.

Over HTTP the statuses follow the envelope document: 200 carries an envelope
(`ok` true or false); 422 (`invalid-input`), 502 (`model-call-failed`,
`model-response-malformed`) and 503 (`model-unavailable`, `binding-invalid`)
carry an error envelope, and when the body is not one — a proxy's page, a worker
that died mid-answer — the status alone names the failure with that code. Any
other status is a transport error and fails the step by its exception's name.

## Usage accounting

`usage` is read from the envelope, never from inside `payload`. `tokens` is a
non-negative integer; `cost` is a finite, non-negative number in the same units
as `RunBudget.max_cost`. Reports cover this interaction only, not cumulative
usage.

Missing usage is unknown. A configured token limit requires `tokens`, and a
configured cost limit requires `cost`; explicit zero is valid. Missing required
or malformed usage fails the run with `UsageUnavailable` before another step
starts. With neither spending limit configured, absent usage is allowed.

The Track records each report before teardown — results that escalate and
`ok: false` answers included — so spending survives recovery. Unknown usage from a failed interaction prevents retry from advancing
under a spending limit. These reports are worker-supplied accounting, not
independent metering or hard per-request caps.

## Workers

Where a worker runs is the runner's `location` setting, `local` or `remote`
(ADR-0002 D12); only `local` is built behind the switch, and `remote` is
refused until Phase 21:

```python
LifecycleRunner(track=track, location="local", location_settings={
    "packages": ["dev/cogs"],      # directories Cog packages are found under
    "allow": ["echo"],             # the names that may run; every package when omitted
    "work_dir": "dev/.local/runs", # each worker's stdout and stderr, per run
})
```

A local worker is the package's `serve` task (`pixi.toml`, `[tasks]`), run in
the package's own pixi environment on a loopback port the executor chooses. It
is told where to listen (`COLLAB_COG_HOST`, `COLLAB_COG_PORT`), which Cog and
run it is (`COLLAB_COG_ID`, `COLLAB_RUN_ID`) and its run token
(`COLLAB_RUN_TOKEN`), which the controller presents as a bearer token on
`/invoke`. What a binding delivers is asked per worker (`deliver`, a function of
the Cog, the run and the instance) and reaches that worker alone. A package
needs its `pixi.lock`, and neither it nor the manifest may be a symbolic link.
It inherits nothing else of the controller's environment, and it is
killed with its whole process group at teardown, or when the controller dies.
The Track records `worker_started` (where it ran, and the hash of its token)
and `worker_stopped`. An executor may still be handed to the runner directly
(`executor=`), which is how the in-memory executor of the tests and the
Kubernetes executor are used today. See
[`docs/cog-execution/runs.md`](../docs/cog-execution/runs.md#agent-locations).

A step that is sent back receives its original `input` and, separately, the
findings as `signal`. The field is absent until a send back; an empty list of
findings is still a signal. In-memory handlers that are sent back need a
keyword `signal` parameter. Crash recovery of an attempt reuses its key. A
worker that answers `{"pause": true}` is not answering with an envelope, and
the step fails as `EnvelopeInvalid`.

`KubernetesCogExecutor(interaction_timeout=300)` sets the default worker HTTP
client's read/write timeout in seconds (default: 60); `None` disables only those
timeouts. Connect and pool timeouts remain 5 seconds. An injected `worker_http`
controls its own timeout. This is an HTTP timeout, not a run deadline.
Only connection failures are retried automatically. Read/write timeouts and
protocol failures propagate because the worker may already have executed the
request; an HTTP failure does not prove that a side effect did not occur.

## Development

Run `uv run --group test pytest` from this directory. Set `TEST_POSTGRES_URL`
to a disposable database to include the Postgres tests; they recreate Track
tables. The kind workflow also exercises the worker transport and resume path.
The location tests start real worker processes from the fake Cog packages of
`dev/cogs/`, under the test interpreter; the one that runs a package in its
pixi environment is skipped unless `pixi` is on `PATH`.
