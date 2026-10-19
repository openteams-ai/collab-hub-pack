# Running Ops: backends, statuses, and what a restart does

How a run advances, what its status means, and what survives when the process
advancing it stops. The code is `collab_hub_execution.runner` (the lifecycle
runner), `collab_hub_execution.backends` (the durability backends) and
`collab_hub_execution.locations` (the agent locations); the
decisions behind them are [ADR-0002](../adr/0002-lifecycle-runner-durability-and-placement.md)
D1–D3 and D12. The states and events each status comes from are
[states.md](states.md); what the Track records is [track.md](track.md).

## Durability backends

A run is advanced by the lifecycle runner: its step functions (`resolve`,
`materialize`, `interact`, `read_envelope`, `teardown`, `evaluate_gate`, then
`complete`, `escalate` or `fail`) and the one driver that sequences them. A
*durability backend* is handed each step function by that driver and decides
only how the call is scheduled and whether what it returned is checkpointed. It
holds no lifecycle logic, and it never says what a run's status is: the Track
does.

| Backend | Setting | Built | A run in flight when its host stops |
|---|---|---|---|
| `none` | `backend="none"` (the default) | yes | is recorded `interrupted` when a host starts, and never resumes; a person retries it |
| `dbos` | `backend="dbos"` | Phase 26 of the plan (#104) | resumes from its last completed step, on Postgres or SQLite |
| `temporal` | `backend="temporal"` | Phase 32 (#110) | resumes, its steps run as Temporal activities |

The setting is the only switch: nothing imports a backend, and a runner
configured with a backend that is not built yet refuses to start
(`BackendNotImplemented`), so the configuration shape is fixed before the
backends exist.

## Agent locations

Where a step's worker runs is the *agent location*, a second setting beside the
backend. The runner drives every location the same way: it asks an executor to
materialize a worker, speaks the seam to it (`GET /healthz`, `POST /invoke`),
and asks the executor to tear it down. An executor holds no lifecycle logic.

| Location | Setting | Built | A worker is | For |
|---|---|---|---|---|
| `local` | `location="local"` | yes | a process on the controller's host: the Cog package's `serve` task, in the package's own pixi environment, on a loopback port | development and the desktop |
| `remote` | `location="remote"` | Phase 21 of the plan (#6) | a workload on a cluster, reached over the cluster's network | a Kubernetes hub, always |

The setting is the only switch: nothing imports an executor, and a runner
configured with a location that is not built yet refuses to start
(`LocationNotImplemented`). The Kubernetes executor that exists today is what
`remote` will select; until Phase 21 it is handed to a runner directly, as the
in-memory executor of the tests is.

**What a local worker is given.** Its environment, and nothing else of its
controller: `PATH`, `HOME` and the few variables a process needs to run at all;
`COLLAB_COG_HOST` (always `127.0.0.1`) and `COLLAB_COG_PORT`, where it must
listen; `COLLAB_COG_ID` and `COLLAB_RUN_ID`; `COLLAB_RUN_TOKEN`; and whatever
the binding delivers. Delivery is asked once per worker, for that Cog, run and
step, and enters that one child's environment only: never another worker's,
never the Track, never a file. The controller's own configuration and credentials are
not passed on. Its stdout and stderr go to a directory of its run,
under `<work_dir>`, one level per run and one per step attempt, each named by a
readable prefix of the id and a digest of all of it, so two ids never share a
directory. `worker_started` gives the path as `logs`. The controller reaches a
worker directly on loopback, ignoring any proxy its own environment names.

**What a local worker leaves.** Nothing. Teardown kills the worker's whole
process group. A controller that dies without tearing down, killed with
`SIGKILL` included, still leaves no worker: each worker is started through a
launcher that holds a pipe from the controller, and kills the worker's process
group when that pipe closes, for whatever reason. On Linux the worker is also
killed by the kernel if the launcher itself dies.

**Packages.** At `local` a Cog's name resolves through the *directory package
source*: an allowlisted name under a configured root, refused when the name or
a symbolic link leaves the root. The package is a directory with a `pixi.toml`
that declares a `serve` task and the `pixi.lock` that pins its environment,
both files of the package itself: a package without its lock, or with a
symbolic link in place of either, is refused, and pixi runs it `--locked`. It
is identified on the Track by its name and the sha256 of its manifest and lock, so a development run is never mistaken for a
published Cog. Resolving a published reference is Phase 22.

**The run token.** One per worker, minted when it is materialized. The
controller presents it as a bearer token on `/invoke`; the worker will present
it to the hub endpoints it calls. `worker_started` records its sha256 on the
run's Track, the store the API and the controller share, and
`collab_hub_execution.run_tokens.verify` answers whether a token belongs to a
worker of that run that is still up. It expires when the worker's
`worker_stopped` is recorded, or when the run ends or is interrupted.

`local` is not for a deployed hub: a local worker shares its controller's host
and network identity, so the isolation a cluster gives a worker does not hold.

## The run controller and the run API

The process that accepts runs is not the one that advances them (ADR-0002 D4).

- **The API** (`/v1/runs`) records intent on the Track and reads a run's
  status from it. It constructs no executor and never calls the controller.
  The code is `collab_hub_execution.intents`. Its Track is the hub's own
  database (`frames.postgres.url`), whose tables come from the hub's
  migrations, or `runs.track_path`, a SQLite file for a hub without Postgres;
  with neither, every route answers 503 `runs_unavailable`. The Cogs it can
  launch are the packages under `runs.packages`, the same directories its
  controller is given.
- **The run controller** (`collab-hub-run-controller`, or
  `python -m collab_hub_execution.controller`) watches the Track. It picks up
  each run that was submitted and not yet picked up and starts it on the
  lifecycle runner, and delivers what clients asked of the runs it owns: a
  cancel, which tears the run's worker down and ends it `CANCELLED`, a turn, and
  a decision on a Gate. It alone constructs an executor.

| Route | What it does |
|---|---|
| `POST /v1/runs` | Submit an Op: `steps`, each with `name`, `cog`, `entry_point`, `input` and `gate`, and optionally the run's own `name`, a label for listings. Records `op_submitted` with who submitted it, and answers 201 with the run, `SUBMITTED`. A `cog` that is not a package the controller can launch is a 422 naming the packages it can |
| `GET /v1/runs` | The caller's organization's runs, newest first; `status`, `limit` and `offset` |
| `GET /v1/runs/{id}` | One run: its status, each step's state, each completed step's `output`, and `picked_up_by`, the controller that advances it |
| `GET /v1/runs/{id}/events` | The run's Track: the events after `after` (a sequence), `limit` at a time, with `next_after` when there are more. Asked for `text/event-stream`, a live stream instead: each event as it lands, named by its sequence, then an `end` event with the run's status once it has ended; a client that lost the connection reconnects with `Last-Event-ID` and misses nothing and repeats nothing. Each event's payload carries only the fields named for its type (an allowlist, so a field recorded later is not served until it is named); where a local worker is on the controller's host (`pid`, `pgid`, `logs`) is never among them |
| `GET /v1/runs/launchable` | The Cog packages a step's `cog` may name |
| `POST /v1/runs/{id}/turns` | Ask a Cog whose step holds a session something: records `turn_requested` with the caller and answers 202 with the turn, `pending`. A run that has ended is a 409 |
| `GET /v1/runs/{id}/turns/{turn}` | The turn: `pending`, then `answered` with the Cog's text, or `failed` with why |
| `POST /v1/runs/{id}/cancel` | Records `cancel_requested` with the caller, once, and answers 202. A run that has ended is a 409 naming its status, checked as the request is written |

Every answer names the `backend` and the `location` the run is advanced with,
so a client never assumes a run survives a restart or that its worker is
isolated. A step that was running, or waiting at its Gate, when its run ended
is reported in the state the run ended in. A run belongs to the organization that submitted it; to any other it
is a 404. The routes are authenticated, like every route the protection map
does not open.

### Pickup and ownership

Several controllers can share one Track: a SQLite file on one host, or
Postgres (`--track postgresql://...`) anywhere. Each is known by its id
(`--id`, the host's name by default), which it holds for as long as it runs — a
lock beside a SQLite Track, a Postgres advisory lock — so a second controller
under the same id refuses to start, and one that loses its hold stops.

- **Pickup is atomic.** A controller picks a run up by recording
  `run_picked_up` with its id, and the record lands only if, as it does,
  nobody has picked the run up or cancelled it: the check and the write are
  one step on the Track (`append_if`). Of several controllers passing over one
  submitted run, one starts it.
- **A picked-up run is its controller's.** Under `none`, that controller alone
  advances it and delivers its cancel, its turns and the decision on its Gate;
  another refuses to (`RunOwnedElsewhere`). A run nobody has picked up is
  cancelled by whichever controller gets there first, with the same
  conditional write. Under `dbos` and `temporal` the engine takes ownership
  after pickup, and decides which replica resumes a run (Phases 26 and 32).
- **A retry hands the run to the controller that retries it**, named on
  `retry_requested`.
- **When a controller starts**, it records `interrupted` for every run it
  picked up and did not finish, and only those; another controller's runs are
  left for that controller's own restart. A run picked up before controllers
  named themselves is taken by the first to start. A worker of an interrupted
  run that is still alive — its `worker_started` has no `worker_stopped` — is
  reaped by the pid and process group `worker_started` recorded, once the pid
  is shown to still be that run's worker, and `worker_stopped` records
  `reaped: true`; a pid the system has given to another process since is left
  alone. Normally there is nothing to reap: the launcher kills its worker when
  the controller dies, and the stop is recorded `reaped: false`.

An id is what lets a restarted controller take its runs back, so it is the
same across restarts and different between replicas: a StatefulSet's pod
names, or `CONTROLLER_ID=` in `dev/`.

### Signals travel through the Track

The API records what a client asked for; the controller that owns the run
delivers it. Nothing calls the controller, so the API needs no route to it and
no engine client.

| Asked | Recorded by the API | Delivered by the controller |
|---|---|---|
| Cancel | `cancel_requested`, once, while the run has not ended | `cancelled`, the worker torn down |
| A turn | `turn_requested` | `turn_answered`, or `turn_failed` |
| A decision on a Gate | `decision_requested` with the escalation, the outcome, the actor and any findings, only while the run waits on that escalation and holds no other decision | `gate_decided`, and the run advances; or `decision_refused` with why |

`intents.request_decision` writes a decision; the route that calls it is the
run API's later phase.

### Configuration, health and models

| | |
|---|---|
| `--track` | A SQLite file, or a `postgresql://` URL (`collab-hub-execution[postgres]`). On Postgres the Track's tables come from the hub's migrations; the controller refuses a database without them |
| `--id` | The controller's name, held while it runs, recorded on each run it picks up |
| `--packages`, `--allow`, `--work-dir`, `--environment` | The `local` location's directory package source, and where workers write their output |
| `--backend`, `--location` | The two axes, `none` and `local` built |
| `--models FILE` | The hub's `models:` block, below |
| `--health-port`, `--health-host` | `GET /healthz`, 200 while the controller passes over the Track, and `GET /readyz`, 200 once it has started and its last pass read the Track; 503 otherwise, with the reason |

Each option also reads the `COLLAB_CONTROLLER_*` variable named after it, for a
container: `COLLAB_CONTROLLER_TRACK`, `COLLAB_CONTROLLER_ID`,
`COLLAB_CONTROLLER_ENVIRONMENT`, `COLLAB_CONTROLLER_HEALTH_PORT` and so on;
`COLLAB_CONTROLLER_PACKAGES` is a path list (`:`-separated) and
`COLLAB_CONTROLLER_ALLOW` a comma-separated one. The command line wins.

The **`models:` block** is the models the hub offers and which Cog talks to
which, in TOML, until a Cog's own binding resolves it (Phase 24, whose
inventory is generated from it):

```toml
[models.default]
provider = "openai-compatible"        # or "anthropic"
endpoint = "http://127.0.0.1:8090/v1"
model = "fake-model"
auth_ref = "env:MODEL_KEY"            # the variable the key is in; never the key
context_window = 131072               # optional
max_output_tokens = 8192              # optional

[cogs]
hermes = "default"
```

Each worker of a Cog the block binds receives its model as `COLLAB_MODEL_*`
variables, and no other Cog's worker does. The key is read from the
controller's environment when the worker starts, so a rotated secret reaches
the next worker, and it is never written to the Track or a file. A block that
names an unknown key, an unknown model, or a key that is not set is refused when
the controller starts.

## What a restart does under `none`

`none` checkpoints nothing. So when a host starts, `LifecycleRunner.start()`
records every run the Track shows `RUNNING` or `WAITING_AT_GATE` — one a stopped
host left behind — as `interrupted`, with the backend that could not resume it,
and returns their ids. A run left looking alive would be a lie the status could
never correct.

- **An interrupted run is never resumed.** Submitting it again returns its
  status. Starting another host interrupts nothing more.
- **Retrying continues the attempt that was in flight.** `retry()` records
  `retry_requested` with `attempt: same`, so the step runs again under the same
  idempotency key, and a worker that honours the key — the keyed claim, #102 —
  answers instead of acting twice. Steps already completed do not run again.
- **A pause does not survive either.** A run waiting at a Gate when its host
  stopped is interrupted too (decision 3 of the plan); its retry runs the
  escalated step again.
- **A failed run retries as a new attempt**, under a new key, since its failure
  was recorded.
- **A run submitted and never picked up** had nothing in flight, so it is not
  interrupted; submitting it starts it.

A runner named for a controller (`controller=`) takes only the runs that
controller picked up, as above; an unnamed one is a host alone on its Track
and takes every unfinished run. At dev level 1, `make op` is the controller
`make-op`.

## Cancelling a run

`cancel(run_id, actor=...)` ends a run `cancelled` and records the actor.

- **A run the host is advancing** is cancelled at its next step boundary. Its
  live worker is torn down at once, so an interaction in flight stops; a worker
  still being brought up is torn down before it is invoked. The call advancing
  the run then records `cancelled`, and keeps no result from an interaction
  that was in flight. `cancel` returns the status the run has until then.
- **A run submitted, running on no host, or waiting at a Gate** is cancelled at
  once.
- **An ended run** cannot be cancelled, and that includes one that ends while
  the request is on its way: a cancel and a run's end are never both recorded.
- **A worker that could not be torn down** is not hidden by the cancel: the
  teardown is tried again, and if that fails too, a `step_failed` with
  `TeardownFailed` and the executor's error is recorded beside `cancelled`.

## One run, one call at a time

Within a host, every call that moves a run — `submit`, `retry`, `decide`,
`cancel` of a run nothing is advancing, and `start` — claims the run before it
reads or writes it, so no two of them move one run at once. A cancel is taken
only while the driver advances the run, under the same lock the driver holds to
write a step's result or the run's end, so the cancel lands before one of those
writes or not at all. A second `submit` of a run another call is moving waits until
that call has written the submission, is refused if its Op differs, and returns
the run's status otherwise; a `retry` or `decide` of it is refused; `start`
leaves it alone.

## Trying it

At dev level 1, with no container: `make -C dev op OP=<name>` runs an Op from
`dev/ops/` with the fake Cogs of `dev/cogs/` on `none`, over a SQLite Track in
`dev/.local/`, in one process, and prints its Track and status. `make -C dev
controller` and `make -C dev submit OP=<name>` run it across two processes, as
the run API and the controller do: `submit` records it, the controller picks
it up, and `submit` follows it to its end and says which controller picked it
up. Kill the controller mid-run and start it again, and it records the run
`interrupted`; start two (`CONTROLLER_ID=one`, `CONTROLLER_ID=two`) and each
run is picked up once. `TRACK=postgresql://...` puts the Track on the level-2
Postgres. The fake Cogs answer in the
same process; add `LOCATION=local` (which needs pixi) and each step's worker is
a real process, whose `worker_started` and `worker_stopped` appear on the
Track and whose output is under `dev/.local/runs/`. `make -C dev api` and
`make -C dev controller` are the two processes of the section above, and
[`examples/cog-local`](../../examples/cog-local/README.md) walks a Cog through
its whole life on them, signed in through Keycloak: `make demo` there runs it. Stop `make op OP=slow` mid-step,
and the next `make op` reports the run `interrupted`. See
[dev/README.md](../../dev/README.md#running-cogs-and-ops).
