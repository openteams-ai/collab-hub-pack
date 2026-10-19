# Cog execution: the plan

How Cog execution lands in this hub, one pull request at a time. Every phase below is an issue — in this repository for the hub, in the Collab desktop repository for the desktop — and its status is kept current here as branches move. Comment on this document through its pull request; a decision that changes it is recorded in [§10](#10-decisions) and, when it is expensive to reverse, in [ADR-0002](docs/adr/0002-lifecycle-runner-durability-and-placement.md).

**Origin.** The program's parent issue is apollo-desktop#690, *Cog execution in the hub*; its children are #1–#9 here and apollo-desktop#719, and the first prototype (#20, PR #23) was superseded by #35. The brief behind it: Cog execution in this hub, with local execution too, both launched by the Collab desktop; `none`, `dbos` and `temporal` as durability backends, `none` first; Hermes ([site](https://hermes-agent.nousresearch.com), [docs](https://hermes-agent.nousresearch.com/docs), [source](https://github.com/NousResearch/hermes-agent)) as the first harness, with any other supported; the [glossary](docs/GLOSSARY.md) and [`docs/cog-execution/`](docs/cog-execution/README.md) as the vocabulary.

The requirements are the issues *and their comment threads*. The threads matter: most carry review from ADR-0001's author that narrows or corrects the issue body (who decides a pause, who re-drives a run, install versus materialize, the Cog's own `resolve`, no credential in the worker). Where the two disagree, this plan follows the comment.

## 1. Goals

The hub runs **Cogs** — packaged AI workers — and **Ops** — supervised, multi-step workflows over them — and Collab, the desktop client, launches, watches and decides them. Cog execution by the hub will support:

- **Two locations.** A worker runs as **`local`** — a child process of the run controller, on the same OS — or **`remote`** — a Kubernetes pod. Same runner, same Cog, one configuration value; `local` first.
- **Three durability backends.** **`none`** (no durability engine), **`dbos`** and **`temporal`**, behind one seam. **The priority is `none`**: it ships first, `dbos` is the next brought to a full implementation, `temporal` comes last.
- **Any harness.** The hub never learns which agent harness a Cog uses. **Hermes** is the first harness Cog; **pi** and **OpenCode** follow, and any harness that speaks ACP wraps with the adapter Hermes uses. Supporting a new harness is an adapter in the Worker SDK, never a change to the hub. This is the decision the rest of the plan rests on (§4).
- **Launched from Collab, and from a terminal.** The desktop submits a run, watches its Track, decides its Gates and grants connector access — on the hub first, and later on the user's own machine, through the same run API. The `collab-hub` CLI drives the same endpoints, so every feature is scriptable from the day it exists.
- **Accountable and bounded.** Every run has a Track that names what produced each result and who signed it. Gates are declared on Op steps and decided by people. Budgets, least-privilege workloads and egress restriction hold the trust boundary.

The first thing that runs end to end is the primary goal: **the hub launches Hermes as a separate local process and an Op step completes with a valid envelope**, on a laptop, with no cluster. The same abstraction then launches the same Cog in a pod, then `dbos` makes runs survive a restart, then `temporal`.

## 2. Requirements

| Requirement | What it means here | Where |
|---|---|---|
| Harness-agnostic | The hub speaks only the seam (a task entry point, the result envelope, a health probe, a catalog card). Hermes first; pi, OpenCode and any ACP harness next, each an adapter in the Worker SDK. | Phases 13, 14; [§4](#4-target-architecture) |
| Agent location: `local` and `remote` | `location: local \| remote` selects the executor: a child process on the controller's host, or a pod. Chosen by configuration, like the durability backend. | Phases 9, 21 |
| Durability: `none`, `dbos`, `temporal` — in that order | One lifecycle runner; a backend decides only scheduling and checkpointing. `none` marks in-flight runs `interrupted` after a restart and never resumes them. | Phases 8, 26, 32 |
| Launched from Collab | The desktop drives runs on the hub, then on the user's machine, through one run API served by two hosts. | Phases 12, 17, 19, 20, 31 |
| Scriptable from a terminal, alongside the primary goal | A `collab-hub` CLI on Typer that holds nothing up: it signs in and lists the Cogs the hub offers before any run can start, and launches a Cog — Hermes included — and terminates it as soon as the run API can launch one, beside the SDK and Hermes; each later phase adds its own commands. | Phases 6, 15, 18 |
| Runnable from `dev/`, asserted in CI, in the same PR | Every phase adds its `make` target at the lowest level that can host it, documents it, and asserts its contract in CI at that level. | [§5](#5-the-dev-environment-and-the-docs-carry-every-phase) |
| Documented in the same PR | Every phase updates the documents it makes stale, names them in the PR, and adds its terms to the glossary. | [§5](#5-the-dev-environment-and-the-docs-carry-every-phase) |
| Python 3.13 and 3.14 | The API supports both and CI tests both; the execution package, the Worker SDK and the CLI support 3.11 and up. A Cog pins its own interpreter. | Phase 1 (done) |

## 3. Where we start

**Landed**

- **#94** — the local development environment in `dev/`: four levels (`make api` with no containers; `make api-pg` with Postgres and an S3 store; `make api-oidc` with Keycloak, plus `api-fakes`, `api-full`, `api-membership`; `make kind-up` for the chart), fake Google, Slack and GitHub providers, a `nebari` realm, `seed-org` for owner and operator grants, and a single-port front door so Collab signs in at `http://localhost:9080` (`make api-desktop`). `dev-env.yaml` runs level 1 on Linux and macOS, levels 2–3 on Linux, and level 4 rendered only.
- **#35** — the standalone `execution/` distribution `collab-hub-execution` (Python ≥ 3.11, depends only on `httpx`): `WorkflowEngine` and the reference `DurableWorkflowEngine`, which recovers from the Track when a caller resubmits; `CogExecutor` with in-memory and Kubernetes implementations (a per-run Deployment + Service + ingress-only NetworkPolicy, no ServiceAccount token on workers); `TrackStore` with in-memory and Postgres adapters; `CogLifecycle`, `RunBudget`; `DeclaredCapabilityResolver`; a kind E2E running a gated two-step Op (`test-execution.yaml`, `test-execution-e2e.yaml`).
- **#111** and **#112** — Phases 0 and 1: the API runs on Python 3.13 with a CI job proving it, and ADR-0002 records the decisions the phases below depend on. **#115** moved the dev MinIO images to quay.io on the way.
- **#120** — Phase 2: `CogWorker.interact` returns the result envelope, which the engine reads everywhere it read `{output, usage}`. `ok: false` with an `error.code` fails the step and keeps the code; `ok: true` with `problems` completes it and records them for a Gate. The envelope's invariants hold on construction as on parsing, and the worker client maps the document's HTTP statuses. `InteractionResult` is gone.
- **#133** — Phase 3: the four state machines on the state pattern, in `collab_hub_execution.states` — a Cog's install, a worker, a step attempt and a run. The engine moves the run and worker machines and writes the records they return; a run's status is its Track replayed through the run machine, and a Track the machine could not have written is refused. `CogLifecycle` and `RunStatus` are gone: a paused run reports `WAITING_AT_GATE`, a duration stop `BUDGET_EXCEEDED`, and a revise limit of N allows N revisions. [`docs/cog-execution/states.md`](docs/cog-execution/states.md) is the reference, held to the code by a test, and ADR-0002 gains D11.
- **#134** — Phase 4: every Op step has a Gate, evaluated over the step's envelope, and a Cog can no longer pause a run — `PauseRequest` and `signal()` are gone. An escalation is recorded with its attempt, envelope and approvers under an id minted over both; `decide()` answers it as an actor, approve completing the step with the envelope its approver saw, reject ending the run `REJECTED`, send back re-running the step with the findings, and a decision on a closed escalation refused. The dev S3 frame store moved off MinIO on the way, whose images left quay.io as they had left Docker Hub (#114, #115), to SeaweedFS with the AWS CLI creating its bucket.
- **#158** — Phase 5: Track event schema v1, versioned on every event. `step_completed` names the Cog, its digest, the binding, the problems, the usage and the Frames behind a result, inline or by reference above a threshold; `step_failed` carries the key, the worker, the code and a bounded message; `gate_escalated` and `gate_decided` carry the escalation id, the actor and the digest of the result decided on. A Track written before v1 reads through `track.upgrade`. A SQLite store joins the in-memory and Postgres ones under one conformance suite, and the hub's tables come from migration 12, which CI asserts. [`docs/cog-execution/track.md`](docs/cog-execution/track.md) is the reference.
- **#159** — Phase 6: the `collab-hub` CLI under `cli/`, on Typer. `login` signs in the way the desktop does — the realm's `apollo-desktop` client, authorization code with PKCE and a `127.0.0.1` loopback redirect, with `--port` for an SSH forward and `--with-token` for scripts — and `logout` ends the realm session and revokes its refresh token. The session is stored `0600`, sent only to its own hub, renewed before it expires, kept through a realm outage, and ended at the realm when a new sign-in replaces it. `whoami` reads the new `GET /v1/me`; the public `GET /v1/auth/cli` names the issuer and client, so the hub's URL is all a user types. `cog list` and `cog show` read the catalog. CI signs in against the real dev realm at level 3. #125, which asked for a device flow, stays open.
- **#162** — Phase 7: the lifecycle lives in `LifecycleRunner`, as step functions registered in `STEP_FUNCTIONS` — resolve, materialize, interact, read the envelope, teardown, evaluate the Gate, then complete, escalate or fail — and the one driver that sequences them. Every Track write the suite makes stayed identical, and no existing test changed. The Op and the seam's types moved to `ops.py`.
- **#163** — Phase 8: the runner takes a `backend` setting, `none`, `dbos` or `temporal`, and hands every step function to that backend through one driver they all share; `none` is built, and the other two are refused when a runner starts. A run `none` had in flight when its host stopped is recorded `interrupted` when a host starts, and never resumed: `retry()` continues its attempt under the same key. `cancel(run_id, actor)` ends a run `cancelled` and tears its worker down, and every call that moves a run claims it first. `DurableWorkflowEngine` and its Track-based recovery are gone. The lifecycle and durability conformance suites hold every backend, and `make op OP=<name>` runs the fake Ops at level 1. [`docs/cog-execution/runs.md`](docs/cog-execution/runs.md) is the reference.
- **#174** — Phase 9: the runner takes a `location` setting, `local` or `remote`, beside its backend. `local` is built: the local executor runs a Cog package's `serve` task in the package's own pixi environment, on a loopback port, through a launcher that kills the worker's process group when the controller lets go of it or dies, `SIGKILL` included. A package comes from a directory source, needs its lock, and is identified by the digest of its manifest and lock. Each worker has one run token, whose hash `worker_started` records and which expires at `worker_stopped`. The location conformance suite holds `local` and the in-memory executor, the fake Cogs are packages, and `make op LOCATION=local` runs them as real processes. ADR-0002 gains D12 and a sixth invariant.
- **#178** — Phase 10: `examples/cog-local`, the walking skeleton. Hermes is a Cog (`cogs/hermes`, Hermes Agent 0.19 over ACP, no tools, its model delivered to it alone, Claude when `ANTHROPIC_API_KEY` is set), launched from the `collab-hub` CLI on a hub of two processes signed in through Keycloak, listed, talked to from Toad through `run connect`, and terminated. It built the first forms of the run controller (one per SQLite Track), of intent on the Track (`intents`, with `cancel_requested`, turns, and `append_if`), of the run API behind `cog_runs`, and of the CLI's `cog launch` and `run` commands, with sessions on the seam (`POST /turn`). `make demo` runs it all in CI (`examples.yaml`).
- **#81, #82, #83** — the Cog bundle reader, the OCI client and the registry sources (PRs #89, #88, #90).
- **#23** — superseded; #20 was closed in favour of #2–#5.

**In flight — not this plan's to build**

- **#7** is decomposed into **#81–#87**. All but one have landed: the catalog store (#84, PR #127), the catalog read API (#85, PR #147) and its configuration (#87, PR #118), after #81–#83. #86, registry webhooks, is still open. Phase 22 consumes the catalog; Phase 9 needs none of it, since it reads a package from a directory through a source of its own.
- **#78** moves the profile manifest into `pixi.toml` under `[tool.cog]`.

**Gaps between #35 and the requirements** — nearly all named in review on the issues:

| Today (#35) | Required | Source |
|---|---|---|
| ~~Worker answers `{output, usage}`~~ — *the envelope landed in #120; `{"pause": true}` is still how a Cog pauses* | The result envelope; the **step's Gate** decides a pause, never the Cog | #2 comment, seam note |
| One engine, durable only through Track replay a caller triggers | One **lifecycle runner**, with the durability engine absent (`none`) or plugged in (`dbos`, `temporal`) | #2 |
| The executor is whichever class the caller constructs, and the only real one needs a cluster | An **agent location**, `local` or `remote`, chosen by configuration like the backend — and `local`, a child process, first | this plan |
| At-least-once across pod replacement | A keyed claim, so a re-invoked step never repeats a side effect | #1 comment |
| A step event's payload is the raw output; a failure is a class name | Binding identity per step, actor per gate decision, bounded failure records | #5 comment |
| The Postgres Track creates its own tables | Hub tables through the `collab_schema` migration registry | #84, #42 |
| The hub's resolver selects the model; the worker only ever gets `COG_ID` | Hub offers inventory, the Cog's `resolve` selects, hub records **and delivers** the binding | #3 comment |
| A baked runner image | The installed Cog package running its own `serve` entry point | #1 comment |
| `collab_hub_api` does not import `collab_hub_execution` | A run controller and a run API | #1 |
| Nothing in the desktop calls a run API | Launch, watch and decide from Collab | apollo-desktop#690, #719 |
| The hub has no model configuration | A minimal `models:` block, generalized by the inventory later | this plan |

**Facts that constrain the design** — verified, not assumed:

- **Hermes and the hub share a Python, and still not an interpreter.** `hermes-agent` requires `>=3.11,<3.14`; the API requires `>=3.13`. The harness still lives in its Cog's own environment — ADR-0001 D2 and the seam put it there, and a Cog pins its own interpreter whatever the hub runs.
- **Collab already drives Hermes over ACP**, headlessly configured, in its own environment (Python 3.13, `hermes-agent` 0.17 with the `acp` and `mcp` extras), with tools sandboxed in Docker. Known hazard: Hermes 0.17's ACP adapter reports a failed turn as `stopReason: end_turn`.
- **Collab's chat runs on [Ravnar](https://github.com/nebari-dev/ravnar)**, the AG-UI agent server behind [chat-pack](https://github.com/nebari-dev/chat-pack), and that is where the desktop already handles per-turn context, Frames and streaming for a conversation. Cog execution needs the same three features and does not run on Ravnar to get them: Ravnar is a chat engine, not an execution engine, and a Cog must run on the hub with no desktop code in it (decision 19).
- **The desktop has a local Python host** above the execution package's floor, and already models local-versus-remote placement for what it runs.
- **Both durability engines support every Python the hub does.** DBOS 2.31 and temporalio 1.32 require `>=3.10` and list 3.13 and 3.14.
- **DBOS can checkpoint to SQLite**, so a desktop can run durably without Postgres. Temporal needs a Temporal service and cannot.
- **DBOS creates its own databases** on Postgres when they are missing, so its role needs `CREATEDB` or the databases must exist first.
- **The hub has no model configuration.** The only `llm` in `config.py` and the chart is a Keycloak group path; #35's resolver takes a model inventory as an argument and nothing builds one. The first Hermes run needs a minimal `models:` block (Phase 11), which Phase 24's inventory later generalizes.
- **A local worker needs nothing #35 lacks.** `CogExecutor` is already a protocol with an in-memory implementation, and the seam is HTTP on a port: to the runner, a child process on loopback is indistinguishable from a pod.

## 4. Target architecture

```mermaid
flowchart LR
  subgraph desk["Collab desktop"]
    ui["Run view · Gate decisions · grants"]
    rt["Run target<br/>hub or local"]
    ui --> rt
  end

  cli["collab-hub CLI<br/>Typer · signs in as the desktop does"]

  subgraph hub["collab-hub-pack — the hub"]
    api["Run API<br/>/v1/runs · /v1/claims<br/>writes intent · reads the Track"]
    ctl["Run controller<br/>Lifecycle runner<br/>location: local | remote<br/>durability: none | dbos | temporal"]
    track[("Track · claims · run pickup<br/>Postgres, collab_schema")]
    lexec["LocalProcessCogExecutor"]
    kexec["KubernetesCogExecutor"]
    gw["Model egress gateway · connector proxy"]
    api --> track
    ctl --> track
    ctl --> lexec
    ctl --> kexec
  end

  subgraph proc["Local worker — a child process, the same Cog package"]
    psdk["Worker SDK<br/>/invoke → envelope"]
    pad["Harness adapter"]
    pharn["Harness<br/>Hermes over ACP · pi · OpenCode"]
    psdk --> pad --> pharn
  end

  subgraph pod["Remote worker — a pod, the same Cog package"]
    sdk["Worker SDK<br/>/invoke → envelope"]
    ad["Harness adapter"]
    harn["Harness<br/>Hermes over ACP · pi · OpenCode"]
    sdk --> ad --> harn
  end

  subgraph local["Local run host — inside the desktop"]
    lapi["Run API<br/>the same routes"]
    lctl["Lifecycle runner<br/>none, or dbos on SQLite"]
    lw["Local worker<br/>the same Cog package"]
    lapi --> lctl --> lw
  end

  rt -->|"hub · bearer via loopback proxy"| api
  rt -->|"local · loopback"| lapi
  cli -->|"hub · bearer"| api
  lexec -->|"spawn · loopback · run token"| psdk
  kexec -->|"materialize · /invoke with run token"| sdk
  psdk -. "claims, with the run token" .-> api
  sdk -. "claims, with the run token" .-> api
  sdk -. "model · connectors, hub-mediated" .-> gw
```

**The boxes.** Each name below is the name the phases use.

| Box | What it is | Where it runs | Built in |
|---|---|---|---|
| **Collab desktop** — Run view, Run target | The desktop client: submits runs, renders the Track, decides Gates, manages grants. The Run target picks the hub or the local run host through the desktop's existing placement model. | The user's machine | Phases 19, 20, 30 |
| **collab-hub CLI** | A Typer client over the same REST endpoints: sign in the way the desktop does, list the hub's Cogs, launch one and terminate it, then a command per run-API endpoint. Imports no hub package. | A terminal, a script, a CI job | Phases 6, 15, 18 |
| **Run API** | `/v1/runs` and `/v1/claims`: records intent (`op_submitted`, decisions, cancel) and reads the Track; never calls an executor. The claim routes are worker-facing, authorized by the run token. | The API process (`collab-hub-api`) | Phase 16 for claims, Phases 12 and 17 for runs |
| **Run controller** | The process that advances runs: picks them up, owns the executor, hosts the Lifecycle runner, watches the Track for signals, minting each worker's run token. The only identity that can create workloads. | Its own process at level 1, its own Deployment on a hub | Phase 11 |
| **Lifecycle runner** | The lifecycle written once as step functions: resolve → materialize → ready → interact → envelope → Guards → Gate → idle or teardown. A durability backend schedules and checkpoints those steps: `none` in process, `dbos` as workflow steps, `temporal` as activities. | Inside the Run controller, and inside the Local run host | Phases 3, 7, 8, 26, 32 |
| **Track · claims · run pickup** | The store both processes share: the Track (the only source of run status), the keyed-claim store, the pickup records and the run token hashes. | Postgres through `collab_schema` on a hub; SQLite at level 1 and on the desktop | Phases 5, 11, 16 |
| **LocalProcessCogExecutor** | The `local` executor: spawns the Cog package's `serve` as a child process in its own process group, on loopback, and kills it when the controller dies. | Inside the Run controller | Phase 9 |
| **KubernetesCogExecutor** | The `remote` executor: a per-run Deployment, Service and NetworkPolicy, the artifact pulled by an init container. | Inside the Run controller, on a hub | Phases 21, 22 |
| **Model egress gateway · connector proxy** | The hub Services a `remote` worker may reach, by their cluster-internal names: the gateway forwards to the endpoints in `models:` that lie outside the cluster — a model served in the same cluster is selected by the egress policy directly — and the proxy acts as the user under a grant. Both take the run token. | The hub | Phases 28, 29 |
| **Local worker** / **Remote worker** | The same Cog package, materialized as a child process or as a pod. Nothing in the package knows which. | The controller's host / the controller's namespace | Phases 9, 21 |
| **Worker SDK** | The seam implemented once: `POST /invoke` returning an envelope, `GET /healthz`, cancellation, the binding loader, the claim client, run-token verification — and what every harness needs per turn: the model's limits, the step's Frames in the prompt, `context-exhausted`. A library a Cog *may* use; the hub never imports it. | Inside a worker | Phase 13; its claim client with Phase 16 |
| **Harness adapter** | The `HarnessAdapter` protocol between the SDK and an agent harness: `start`, `interact`, `cancel`, `close`. One adapter covers a protocol family; ACP first. | Inside a worker | Phase 13 |
| **Harness** | The agent the adapter drives: Hermes over ACP first, pi and OpenCode next. It lives in the Cog's own environment, never in the hub, and owns its loop: per-turn context, overflow recovery, compaction (decision 19). | Inside a worker | Phase 14; decision 17 |
| **Local run host** | `collab-hub-execution` embedded in the desktop, serving the same Run API routes and claim transport on a bearer-gated loopback surface, with the `local` executor and `none` or `dbos` on SQLite. | The user's machine | Phase 31 |


**The seams.** Every requirement lands behind one of these, which is what keeps ADR-0001 invariants 2 and 5 true as implementations multiply:

| Seam | Contract | Implementations in this plan |
|---|---|---|
| Durability | `DurabilityBackend` under the lifecycle runner, plus two conformance suites | `none` (Phase 8), `dbos` (Phase 26), `temporal` (Phase 32) |
| Location | `location`, `local` or `remote`, selecting a `CogExecutor`; plus the location conformance suite | `local`, a child process (Phase 9); `remote`, a pod (Phase 21; from its artifact in Phase 22); in-memory (tests) |
| States | one machine per level of §11 — install, worker, step attempt, run — on the state pattern; `states.md` + the transition tests | Phase 3 |
| Track | `TrackStore` + the Track conformance suite | Postgres via migrations, SQLite, in-memory (Phase 5) |
| Resolution | hub inventory → the Cog's `resolve` → binding record | Phase 24 |
| Harness | `HarnessAdapter` inside the Worker SDK | ACP (Phase 13) → Hermes (Phase 14), then pi and OpenCode; an OpenAI-compatible direct call (Phase 13) |
| Placement | one run API served by two hosts; the desktop's run target | hub (Phases 12, 17, 19), local (Phase 31) |

### The lifecycle runner and its durability

There is **one component that runs a Cog's lifecycle** — the lifecycle runner. It owns the lifecycle, written once: resolve → materialize → ready → interact → read the envelope → Guards → Gate → idle or teardown, with budgets, cancellation and Track recording, all as plain step functions.

Each step function moves one of the state machines of Phase 3 by an event; a move a machine refuses is a bug, never a status. A **durability backend** decides only two things: how those step functions are scheduled, and whether progress between them is checkpointed.

- **`none`** — no durability engine. The runner calls the step functions directly, in process.
- **`dbos`** — the same step functions, run as the steps of a DBOS workflow.
- **`temporal`** — the same step functions, run as activities of a Temporal workflow.

No lifecycle logic lives inside a backend. That is ADR-0001 invariant 1 carried one level down — one lifecycle, never divergent code paths — and a test enforces that all three backends call the same step functions.

| | `none` | `dbos` | `temporal` |
|---|---|---|---|
| Durability engine | none | DBOS | Temporal |
| Process restart mid-step | the run ends **`interrupted`** | resumes | resumes |
| Run paused at a Gate, then a restart | the run ends **`interrupted`** | resumes at the Gate | resumes at the Gate |
| Who owns a run across replicas | the controller that picked it up | DBOS queues, after pickup | Temporal task queues, after pickup |
| A step invoked again after a crash or retry | keyed claim | keyed claim | keyed claim |
| Where checkpoints live | nowhere | DBOS system database — Postgres on the hub, SQLite locally | Temporal persistence |
| **Run status comes from** | **the Track** | **the Track** | **the Track** |
| Extra infrastructure | none | none beyond Postgres or SQLite | a Temporal service |
| Local execution | yes, the default | yes, on SQLite | no |
| Suits | one-shot runs, development, tests, the desktop | production hub runs, gated Ops | organizations already operating Temporal |

**The Track is not a durability mechanism.** Under every backend it is the accountability record and the only source of run status (ADR-0001 D8). Under `none` it records what happened — including that a run was interrupted — but it is never replayed to resume execution. Keeping that line sharp is what stops `none` from quietly growing into a fourth, weaker engine. It also retires #35's Track-based recovery. DBOS's system tables and Temporal's event history are checkpoint machinery; they never answer "what is this run's status".

### Agent location: where a worker runs

The second axis under the runner. Where the durability backend decides what survives a restart, the **agent location** decides where the worker process lives — chosen the same way: one configuration value, one `CogExecutor` behind it, no lifecycle logic inside.

- **`local`** — the worker is a child process of the run controller, on the same OS: the Cog package's `serve` task in its own pixi environment, bound to a loopback port, with a run token in its environment. Nothing to schedule, nothing to pull; it shares the controller's host and network identity.
- **`remote`** — the worker is a Kubernetes pod materialized by `KubernetesCogExecutor`, with its own Service and NetworkPolicy and, from Phase 22, the Cog's artifact pulled into it.

| | `local` | `remote` |
|---|---|---|
| The worker is | a child process of the controller | a pod in the controller's namespace |
| Materialize | spawn `serve` in the package's pixi environment | Deployment + Service, plus an init container pulling the artifact (Phase 22) |
| Reached at | `127.0.0.1:<port>` | the Service |
| Isolation | the OS process boundary; the host's network identity | the pod; per-worker ingress and, from Phase 28, egress NetworkPolicy |
| Needs | pixi on the host | a cluster, and RBAC for the controller (Phase 21) |
| Resource limits | none beyond the host's — budgets (Phase 25) are the only bound | the pod's requests and limits |
| The controller dies | the launcher kills the worker when the controller's pipe closes | the pod outlives it until the next controller start reaps it |
| Suits | development and the desktop (decision 11) | every Kubernetes hub — and anything the trust boundary must hold |
| First in | Phase 9 | Phase 21 |

Location and backend are independent: either location runs under any backend, and the conformance suites cover the grid — the lifecycle and durability suites per backend, the location suite per executor. The SDK never learns its location: the run token and the binding reach a child process and a pod the same way, so a Cog that runs at one location runs at the other unchanged.

**Implementation priority.** Neither axis is built all at once, and the order is the primary goal read backwards. `none` ships first (Phase 8) with the `DurabilityBackend` slot carrying all three values, and `local` ships first (Phase 9) with the `location` slot carrying both, so nothing later changes a caller or the runner. The first thing that runs end to end is the hub launching the Hermes harness Cog as a child process (Phase 14), on the shortest path there: the runner and the controller, the slice of the run API that launches a run (Phase 12), and the Worker SDK (Phase 13) — not the keyed claim, the rest of the run API or the CLI, none of which a tool-less Hermes step needs. The CLI runs alongside and holds nothing up: it signs in and lists the hub's Cogs already (Phase 6), and launches and terminates a Cog, Hermes included, as soon as Phase 12 lands (Phase 15). Collab launching and watching that same run comes next (Phases 19, 20). The same abstraction then launches it in a pod — `remote`, Phase 21 — still on `none`. `dbos` (Phase 26) is the next backend brought to a full implementation: the production backend (decision 2), needing no infrastructure beyond the Track's own Postgres. `temporal` (Phase 32) is deliberately last: two backends behind one seam already demonstrate it is swappable before the one that brings a new service is built.

### Harness neutrality

**The load-bearing decision.** The hub never learns which harness a Cog uses: it POSTs a task to `/invoke` and reads an envelope. "Any harness is supported" is therefore something the hub has by construction; the work is making a harness cheap to wrap. The Worker SDK (Phase 13) implements the seam once — envelope, health, binding, cancellation, usage, and, with Phase 16, the keyed claim — and hands the interaction to a `HarnessAdapter`. ACP comes first because it is how Collab already drives Hermes and a protocol other agent harnesses implement too, so one adapter covers a family rather than one product: Hermes is the first Cog built on it (Phase 14), pi and OpenCode the next, each a Cog package and, where the harness does not speak ACP, an adapter — never a change to the hub. The SDK is a library a Cog author *may* use; the hub requires only the seam, and an import-boundary test keeps the hub from ever depending on it.

**What each harness owns, and what the seam hands every harness.** An agent loop does per-turn work the hub never sees: reserving output headroom, recovering from a context overflow, compacting history, choosing what enters each turn. Under this seam that work belongs to the harness, inside the Cog, so each harness owns it — Hermes does it in its own loop. `RunBudget` (Phase 25) is not that work: it bounds a whole run from outside. What is *not* left to each harness is what every harness needs in order to do it, and the Worker SDK hands that to all of them the same way (decision 19): the model's context window and maximum output, in the binding; the step's Frames, snapshotted at submit and placed in the prompt; one envelope error, `context-exhausted`, for an input that could not fit; and, once decision 20 settles, the harness's progress while a step runs. So none of this is rediscovered per harness. The desktop's chat solves the same problems for a conversation, on Ravnar; a Cog run solves them at this seam instead.

**Python.** Each distribution declares its own floor and CI tests that floor and the newest version: the API `>=3.13`, the execution package, the Worker SDK and the CLI `>=3.11`. A Cog's environment pins its own interpreter, so none of these constrains what a harness needs.

## 5. The dev environment and the docs carry every phase

`dev/` (#94) is how this program gets built and checked, not an afterthought to it. It already runs the hub at four levels with fake connector providers, a realm Collab can sign in to, and a CI workflow whose assertions relax as levels get expensive. Cog execution grows inside that structure.

**The dev rule** (ADR-0002 D9). A PR that adds a component also, in the same PR: makes it runnable from `dev/` at the **lowest level that can host it**, through a `make` target; documents it in `dev/README.md` — the target, the level, what persists, a troubleshooting row for its first failure mode; and asserts its contract in CI at that level, under the rule `dev-env.yaml` already follows: no containers at level 1, no large image downloads, and real clusters and heavy engines in their own path-gated workflows.

**The docs rule** (ADR-0002 D10). A PR that changes what the hub does, or how it is deployed, operated or developed, also in the same PR: updates the reference document for what it changed; deletes or corrects every statement the change makes false, and names them in the PR description; adds each new term to `docs/GLOSSARY.md` and moves its issue's row in the issue map of `docs/cog-execution/README.md`; and writes user-facing content as a Markdown page that stands on its own, so the pack docs site can take it over.

| Level | Already there (#94) | What this plan adds | First phase |
|---|---|---|---|
| **1** — no containers | `make api`: dev auth, frames on local disk | a SQLite Track in `dev/.local/` shared by the API and the controller; fake Cogs under `dev/cogs/`, each with a `pixi.toml`; `make op` to submit a fake Op and print its Track; `make controller LOCATION=local` running the lifecycle runner on `none`, so workers are real child processes — the Hermes Cog from `cogs/hermes` among them, against `make fake-model`; `make cli`, the `collab-hub` CLI against dev auth | 5, 6, 8, 9, 11, 14 |
| **2** — Postgres, S3 | `make api-pg`, `make psql` | the Track, claims and run pickup on Postgres; `make controller BACKEND=dbos` — DBOS is a library over the same Postgres, so no new image; a `fake-cog` HTTP worker and the `fake-model` endpoint in the `fakes` profile; optional `registry` and `temporal` profiles | 16, 24, 26, 32 |
| **3** — Keycloak | `make api-oidc`, `api-fakes`, `seed-org`, `token` | Gate approvers from `seed-org`'s owner and operator; grants backed by the `dev` user's offline token; the connector proxy against the existing fakes; `collab-hub login` against the realm, which is where the CLI's browser sign-in is real; `examples/cog-local`, a Cog's whole life signed in to that realm and talked to from Toad | 6, 10, 17, 29 |
| **4** — kind | `make kind-up` with `values/kind.yaml`, `kind-smoke` | the run controller Deployment and its Role from the chart, with `location: remote`; install from the local registry; `KIND_CNI=calico`, because kind's default CNI does not enforce NetworkPolicy | 21, 22, 28 |
| **Collab** | `make api-desktop`, `api-desktop-fakes`, the front door on `:9080` | runs launched, watched and decided from Collab, and grants made there — the front door already forwards everything that is not Keycloak to the API, so `/v1/runs` needs no proxy change | 19, 20, 30 |

**Level 1 is where the primary goal is demonstrated.** `make controller LOCATION=local` and `make op OP=hermes`, with no container running, is the hub launching Hermes as a separate process (Phase 14). Level 4 is the same Op with `location: remote` (Phase 21); nothing in between changes the Cog.

**Why level 1 gets a SQLite Track.** Production runs the API and the controller as separate processes, and Phase 12 enforces that the API never calls the executor. Level 1 keeps that shape — two processes, still no containers — which an in-memory Track cannot serve, since the two processes could not see each other's runs. So `SqliteTrackStore` lands in Phase 5, where it is also the Track the desktop needs later.

**What CI asserts as the phases land** — contracts, the pattern `dev-env.yaml` already follows:

| Workflow · job | Assertions added |
|---|---|
| `test.yaml` · `test-python-floor` | the API suite and `ruff check` on Python 3.13, beside `test` on 3.14 (Phase 1, done) |
| `dev-env.yaml` · `level-1` (Linux, macOS) | a fake Op completes as a child process under `LOCATION=local`, and a killed controller leaves no worker behind — on macOS too, where process-tree teardown and pixi portability are the risk (9); the same across `make controller` and `make op` as two processes, the run ending `interrupted` when the controller is killed (11); a fake Cog launched over HTTP, listed and cancelled mid-step (12); a fake Cog reserves and commits its claim over HTTP (16); a Gate escalates and a decision advances it over HTTP, and a retry across a controller restart is answered by the claim (17); fake Cogs that break policy or grounding escalate with findings (27); the CLI reports an unauthenticated session and the empty catalog (6), launches a fake Cog and terminates it (15), then submits a fake Op, watches it and decides its Gate, with the documented exit codes (18) |
| `dev-env.yaml` · `levels-2-3` | the Track schema comes only from migrations (5); two controllers never both start one run (11); a member without an approver role is refused (17); on `dbos`, a killed controller resumes the run, including one waiting at a Gate (26); a run with no bearer in flight reads a fake connector, and a revoked grant fails the next run (29); `collab-hub login` against the realm, with `whoami` naming the `dev` user (6) |
| `dev-env.yaml` · `level-4-render` | the controller Deployment and a Role limited to materialized kinds render, and the API ServiceAccount has no workload verbs (21); a worker Pod pulls a digest in an init container (22) and carries an egress NetworkPolicy (28) |
| `test-execution.yaml` | every (state, event) pair of the four state machines, and their agreement with `states.md` (3); the lifecycle and durability suites (8), the location suite (9), the claim conformance suite against SQLite and Postgres (16), budgets (25), `dbos` on SQLite (26), and Temporal through the Temporal CLI's `temporal server start-dev` rather than a cluster image (32) |
| `test-hermes.yaml` (path-gated, new) | the Hermes harness Cog through the local executor on Linux, against the stdlib fake model — it downloads Hermes' environment, so it stays out of `dev-env.yaml` (14) |
| `test-execution-e2e.yaml` (kind, path-gated) | the location suite against `remote` (21), materialization of a published fake Cog by digest (22), installing one and running it once (23), a worker configured from its delivered binding (24), and a denied egress connection recorded on the Track on a policy-enforcing CNI (28) |

**The document map** — which reference document each phase keeps current:

| Document | Reference for | Phases |
|---|---|---|
| `docs/adr/0002-…`, `docs/adr/README.md` | the decisions | 0 (done); 3 appends D11, the state pattern; 9 appends D12, agent location; each decision in §10 as it settles |
| `docs/GLOSSARY.md` | vocabulary | 0 (done); every phase that names something new — 3 first, with every state |
| `docs/cog-execution/README.md` | review checklist; issue map | 0 (done); every phase moves its issue's row |
| `docs/cog-execution/op-cog-seam.md`, `result-envelope.md` | what crosses the seam | 2, 4, 13, 16, 22, 24, 28 |
| `docs/cog-execution/states.md` (new) | the four state machines: states, transitions, events | 3, and every phase that emits a new event |
| `docs/cog-execution/track.md` (new) | Track event schema v1 | 5, 27, 29 |
| `docs/cog-execution/runs.md` (new) | running Ops: backends, locations, statuses, the controller, the run API, budgets | 8, 9, 10, 11, 12, 17, 18, 21, 23, 25, 26, 27, 31, 32 |
| `execution/README.md` | the package's contract and its limits | 2, 3, 4, 7, 8, 9, 11, 16, 25 |
| `worker/README.md` (new) | writing a Cog on the SDK | 13, 28 |
| `docs/standalone-deployment.md` — *Protection map*, *Namespace ownership* | public routes; what each identity may do | 6, 12, 21, 23, 28, 29 |
| `docs/frames-operations.md` — tables, migrations, the shared Postgres | operator runbooks | 5, 16, 26, 29, 32 |
| `docs/auth-flow.md`, the `docs/*-connector.md` pages | how identity reaches a connector | 29 |
| `helm/collab-hub/values.yaml`, `values.schema.json`, `values-example.yaml` | configuration | 21, 22, 24, 25, 26, 28, 32 |
| `README.md` — *Architecture*, *Documentation*, *Known limitations* | the front door | 6, 8, 11, 21 |
| `dev/README.md` — *Running Cogs and Ops* | the dev environment | every phase with a dev target |
| `cogs/hermes/README.md` | the Hermes harness Cog | 14 |
| `cli/README.md` | the `collab-hub` CLI: signing in, the commands, output and exit codes | 6, 10, 15, 18, and every phase that adds a command |
| the desktop's contributor docs | the desktop side | 19, 20, 30, 31 |

## 6. Phases

Each phase is one pull request from the branch it names, numbered in build order. It lists its issue, what it depends on, a rough size (S/M/L) and its status; acceptance lines are written to be testable in that PR. Branch names follow the convention in use — `<type>/<slug>`, ending with the issue number when the phase closes one issue that already existed.

### Index

| Phase | Issue | Title | Branch | Depends on | Size | Status |
|---|---|---|---|---|---|---|
| 0 | #96 | Record the decisions for running Ops: lifecycle runner, durability backends, placement (ADR-0002) | `docs/adr-0002-cog-runs` | — | S | merged, #112 |
| 1 | #97 | Run the API on Python 3.13 as well as 3.14 | `feat/api-python-3.13` | — | S | merged, #111 |
| 2 | #98 | Return the result envelope from Cog workers | `feat/cog-result-envelope` | 0 | M | merged, #120 |
| 3 | #130 | State machines for the Cog, the worker, the step attempt and the run, on the state pattern | `feat/cog-state-machines` | 2 | M | merged, #133 |
| 4 | #99 | Declare Gates on Op steps, and take pausing away from Cogs | `feat/cog-step-gates` | 2, 3 | M | merged, #134 |
| 5 | #5 | Record a durable, replayable Track of every run | `feat/cog-track-record-5` | 2, 3, 4 | M | merged, #158 |
| 6 | #125, in part | The `collab-hub` CLI: sign in, and list the Cogs the hub offers | `feat/cli-auth` | — | M | merged, #159 |
| 7 | #100 | Extract a lifecycle runner from the execution engine, with no behaviour change | `enh/cog-lifecycle-runner` | 3, 5 | M | merged, #162 |
| 8 | #101 | Run Ops without a durability engine (`none`), and mark interrupted runs honestly | `feat/cog-durability-none` | 7 | L | merged, #163 |
| 9 | #109 | Agent location: run a Cog as a local process first, a pod behind the same switch | `feat/cog-agent-location` | 8 | M | merged, #174 |
| 10 | #177 | Hermes as a Cog, launched from the `collab-hub` CLI on a local hub and talked to from Toad: an example | `feat/cog-local-example` | 6, 9 | M | merged, #178 |
| 11 | #121 | Run controller and run pickup | `feat/cog-run-controller` | 8, 9, 10 | M | in review, #186 |
| 12 | #103, first half | The hub run API: launch, list and terminate runs | `feat/cog-run-launch` | 10, 11 | M | in review, #202 |
| 13 | #107 | Add a Worker SDK with Harness adapters, ACP first | `feat/cog-worker-sdk` | 2, 9 | M | not started |
| 14 | #108 | Publish a Hermes harness Cog, launched by the hub as a local process | `feat/hermes-acp-harness` | 9, 12, 13 | M | not started |
| 15 | #126, first half | Launch, list and terminate Cogs from the `collab-hub` CLI | `feat/cli-launch` | 6, 10, 12 | S | not started |
| 16 | #102 | Never repeat a completed side effect when a step is invoked again | `feat/cog-keyed-claim` | 8, 11, 13 | M | not started |
| 17 | #103, closes it | The hub run API: Frames, payloads, decisions and retry | `feat/cog-run-api` | 12, 14, 16 | M | not started |
| 18 | #126, closes it | Decide, read and retry runs from the `collab-hub` CLI | `feat/cli-runs` | 15, 17 | S | not started |
| 19 | apollo-desktop#825 | Launch and watch hub runs from the desktop | `feat/hub-run-client-690` | 12, 17 | M | not started |
| 20 | apollo-desktop#719 | Let a human approve, reject, or send back a paused step from the desktop | `feat/run-view-gate-decisions-719` | 4, 19 | M | not started |
| 21 | #6 | The remote location: Cog workers in pods, and least-privilege RBAC for the controller | `feat/cog-remote-location-6` | 9, 11 | M | not started |
| 22 | #105 | Materialize a Cog worker from its published artifact, running its own `serve` | `feat/cog-materialize-serve` | 14, 21 | L | not started |
| 23 | #106 | Install and uninstall a Cog by digest | `feat/cog-install-by-digest` | 12, 22 | M | not started |
| 24 | #3 | Resolve a Cog's model binding into its worker's connection config | `feat/cog-model-binding-3` | 23 | M | not started |
| 25 | #4 | Enforce time and cost limits on every Cog run, and clean up idle workers | `feat/cog-budgets-warm-workers-4` | 8, 24 | M | not started |
| 26 | #104 | Add a DBOS durability backend so a restart does not lose a run | `feat/cog-durability-dbos` | 8, 11, 16 | L | not started |
| 27 | #9 | Check Cog outputs with Guards, not just schema | `feat/cog-guards-9` | 4, 5, 8 | M | not started |
| 28 | #11 | Restrict a Cog worker's network egress to hub-mediated paths | `feat/cog-worker-egress-11` | 13, 22, 24 | L | not started |
| 29 | #8 | Let a Cog act as the user for its connectors when it runs unattended on the hub | `feat/cog-connector-grants-8` | 28 | L | not started |
| 30 | apollo-desktop#826 | Grant and revoke unattended connector access from the desktop | `feat/connector-grants` | 19, 29 | S | not started |
| 31 | apollo-desktop#827 | Run Ops locally from the desktop through an embedded run host | `feat/local-run-host` | 9, 19, 26 | M | not started |
| 32 | #110 | Add a Temporal durability backend | `feat/cog-durability-temporal` | 8, 11, 16 | L | not started |

### The desktop side (apollo-desktop, private)

Four phases are desktop work. Their issues live in the Collab desktop repository, `openteams-ai/apollo-desktop`, which is private; they are listed here so the sequence reads whole, and all of them, like every hub phase, are children of the program's parent issue, apollo-desktop#690.

| Phase | Issue | What Collab gains | Needs from the hub |
|---|---|---|---|
| 19 | apollo-desktop#825 | Submit a run on the hub and watch its events live, through the desktop's loopback proxy | the run API (Phases 12, 17) |
| 20 | apollo-desktop#719 | Approve, reject or send back a paused step; retry an `interrupted` run | step-declared Gates (Phase 4) |
| 30 | apollo-desktop#826 | Grant and revoke a Cog's unattended access to the user's connectors | grants and the connector proxy (Phase 29) |
| 31 | apollo-desktop#827 | Run the same Op on the user's own machine, on `none` or durably on `dbos` over SQLite | the `local` location (Phase 9), `dbos` on SQLite (Phase 26) |

---

### M0 — Decisions and the Python floor

Both merged on 2026-09-16 (#112, #111).

#### Phase 0 — ADR-0002 and issue hygiene
**Issue** #96 · **Branch** `docs/adr-0002-cog-runs` · **Size** S · **Status** merged as #112

ADR-0002 records the decisions this program forces before code depends on them: one lifecycle runner with durability as a backend; `none` not durable and saying so; the Track as the only source of run status; runs advancing in a run controller; harness-neutral workers; local execution embedding the same package behind the same run API; Gates declared on steps; and the dev and docs rules of §5. It amends ADR-0001 D1 in place, adds the new terms to the glossary, six questions to the review checklist and the same-PR rules to `CONTRIBUTING.md`.

*Acceptance*
- [x] ADR-0002 is merged and ADR-0001 D1 amended.
- [x] Every later phase links an issue that exists.
- [x] The review checklist asks the dev and docs questions.

#### Phase 1 — Run the API on Python 3.13
**Issue** #97 · **Branch** `feat/api-python-3.13` · **Size** S · **Status** merged as #111

`requires-python` drops to `>=3.13`; ruff targets the floor, so it reports 3.14-only syntax; a `Test (Python 3.13)` job runs `ruff check` and the suite; `api/.python-version` and the image stay on 3.14 (decision 8).

*Acceptance*
- [x] `uv sync` resolves on 3.13 and 3.14 from the committed lock.
- [x] The API suite passes on both.
- [x] `ruff check` fails on a reintroduced 3.14-only `except`.
- [x] No document states 3.14 as the minimum.

---

### M1 — The seam and the states, correct in the engine

No infrastructure in these four: they fix what crosses the seam, the states everything moves through, and what the Track records, and every later phase builds on that shape.

#### Phase 2 — Return the result envelope from Cog workers
**Issue** #98 · **Branch** `feat/cog-result-envelope` · **Depends on** Phase 0 · **Size** M · **Status** merged as #120

Workers return `envelope: 1`; the engine reads it everywhere it read `{output, usage}`.

*In scope*
- A `ResultEnvelope` model matching `docs/cog-execution/result-envelope.md`: version discriminated by `envelope`, unknown fields ignored, the invariants enforced on construction as on parsing.
- `CogWorker.interact` returns the envelope; `InteractionResult` is removed — the package is documented as experimental, so this is a permitted break.
- `usage` read from the envelope, keeping #35's rule that a configured budget with missing usage fails the run.
- `ok: false` with `error.code` fails the step and keeps the code; `ok: true` with `problems` is **not** a failure — the problems are recorded for a Gate.
- The worker client maps the envelope document's HTTP statuses (422 / 502 / 503). The kind E2E runner emits envelopes; its pause fixture survives only until Phase 4.

*Dev and CI* — no new target; `test-execution-e2e.yaml` stays green with envelope-emitting workers. *Docs* — `execution/README.md` describes usage accounting in the envelope's terms; `result-envelope.md` records the HTTP mapping as implemented.

*Acceptance*
- [x] No engine path reads an `output` key.
- [x] Envelope tests cover `ok` with problems, each error code, unknown fields, and missing usage under a budget.
- [x] The kind E2E passes with envelope-emitting workers.

#### Phase 3 — State machines for the Cog, the worker, the step attempt and the run
**Issue** #130 · **Branch** `feat/cog-state-machines` · **Depends on** Phase 2 · **Size** M · **Status** merged as #133

One machine per level of §11's state diagram, built before anything moves through them, so every later phase emits events into states that already exist instead of growing statuses of its own. It is ADR-0001 invariant 3 — "a tested lifecycle state machine with per-run budgets, timeouts, and warm/idle/teardown" — built first, since it is the reliability core everything else relies on.

*In scope*
- Four machines in `collab_hub_execution.states`, one module each: the Cog's install (`PUBLISHED` to `UNINSTALLED`), a worker (`MATERIALIZED` to `TORN_DOWN` or `WORKER_FAILED`), a step attempt under the keyed claim (`INVOKED` to `RECORDED` or `OUTCOME_UNKNOWN`), and a run (`SUBMITTED` to its terminal statuses, `INTERRUPTED` included). Every state and transition in §11 and no other, named as there.
- **The [state pattern](https://en.wikipedia.org/wiki/State_pattern).** Each state is a class implementing its machine's interface, one method per event — `pickup`, `escalate`, `decide`, `cancel`, `host_stopped`, `retry` and so on. The context object — `CogInstall`, `Worker`, `StepAttempt`, `Run` — holds the data and delegates every event to its current state, which returns the next one. An event a state does not handle is refused by the base class with `InvalidTransition`, naming the state and the event, so an illegal move fails loudly instead of becoming a status (decision 18).
- **Pure transitions.** A transition takes an event and returns the next state and the Track events that record it; it performs no I/O, reads no clock and calls no executor. The Lifecycle runner applies it (Phase 7) and a durability backend schedules the runner, so every backend moves the same machines. Replaying a run's Track events through the run machine is how its status is derived (Phase 5): each run event goes through the handler it was written by and must record what was recorded, an event no run records is refused, and a Track written before `run_picked_up` existed reads its first step start as the pickup — the one derivation the API, the controller and the desktop's Local run host share (ADR-0002 D3).
- **Guards live in the state that owns them**, for every transition §11 labels with a condition: retry from `INTERRUPTED` keeps the attempt and its idempotency key, from `FAILED` opens a new attempt and key, from `BUDGET_EXCEEDED` a new budget epoch; `OUTCOME_UNKNOWN` leaves only through reconciliation; a decision must name the open escalation; a revise limit of N allows N revisions, and a send back that would produce revision N+1 ends the run `FAILED` with reason `revise_limit_exceeded`. Until Phase 4 gives a decision its outcome, #35's `signal()` cannot say whether it approves or sends back, so the engine applies the limit where #35 did — when the step escalates again after N revisions — rather than charging every signal.
- States later phases reach are implemented and tested now, in isolation — `WAITING_AT_GATE` before Phase 4 wires Gates, `INTERRUPTED` before Phase 8, the claim states before Phase 16, the install states before Phase 23. Those phases only emit events.
- #35's `CogLifecycle` transition table and the statuses in `RunStatus` are replaced by the worker and run machines, and the engine moves them: it asks a machine and writes the records it returns. Wire values are the state names in lower case — `waiting_at_gate` — so a paused run reports `WAITING_AT_GATE` and a duration stop `BUDGET_EXCEEDED`. The Track keeps its event names — `paused`, `signal_received`, `timed_out` — and the run machine's replay reads them; Phase 5's schema v1 renames them, and its pre-v1 reader keeps older Tracks replaying.
- The engine records `run_picked_up` when it first advances a submitted run, so every run-level move after submission — a budget stop before the first step included — starts from `RUNNING`. Phase 11 makes that record the atomic pickup.
- Tests: every (state, event) pair of every machine — an allowed pair reaches the state §11 names, every other raises — and a test that parses the diagram in `docs/cog-execution/states.md` and asserts its transitions equal the machines', so the document and the code cannot drift.

*Dev and CI* — no new target: a library with no process of its own, like Phase 7. The transition tests run in `test-execution.yaml`; from Phase 8, `make op` prints the state each Track event moves the run to. *Docs* — a new `docs/cog-execution/states.md` takes §11's diagram and state table as the reference, and this plan's appendix points to it; the glossary gains *State machines*, naming every state, and its *Interrupted* entry, which said a retry is a new attempt, is corrected to say it continues the attempt in flight; `execution/README.md` gains *States*; the review checklist asks whether a state change goes through its machine; decision 18 becomes **ADR-0002 D11**. `test-execution.yaml` also runs when `states.md` changes on `main`.

*Acceptance*
- [x] Every state and transition in §11 exists in exactly one machine, and nothing else does; the test holding `states.md` to the code passes.
- [x] Every (state, event) pair §11 does not allow raises `InvalidTransition`, naming both.
- [x] Replaying a run's Track events through the run machine yields its status, `INTERRUPTED` and `WAITING_AT_GATE` included, with no I/O.
- [x] No module outside `collab_hub_execution.states` compares or assigns a state by string.

#### Phase 4 — Gates declared on the step; human decisions as signals
**Issue** #99 · **Branch** `feat/cog-step-gates` · **Depends on** Phases 2, 3 · **Size** M · **Status** merged as #134

Move approval out of the Cog and onto the Op step, where the glossary puts it.

*In scope*
- `OpStep.gate`: a declared policy over the envelope (and, from Phase 27, Guard findings) with three outcomes — `pass`, `pass_with_problems`, `escalate`. Every step has one, and a step that declares none gets the default: any `problem` with severity `error` escalates. The policy is a threshold — `never`, `error`, `warn` (any problem) or `always`, a sign-off on every result — and it is part of the Op recorded at submission.
- Each escalation gets an **escalation id**, minted over the step attempt and the envelope that escalated, and recorded with it. It is what a decision answers, so a decision is bound to the revision its reviewer actually saw; a send back closes it and the next escalation on that step gets a new one.
- A decision signal `{escalation, actor, outcome, findings[]}` with outcome `approve | reject | send_back`: approve completes the step with the envelope its approver saw — the step does not run again — and advances; reject ends the run `rejected`; send back re-runs the step with the findings as its signal, bounded by #35's existing revise limit. The engine's `decide(run, escalation, actor, outcome, findings)` replaces `signal()`, and `open_escalation(run)` says what a run waits on; both are on the engine contract, so a client needs no engine of its own to find what a decision must name. A budget stop never discards a result that was paid for: the escalation is recorded, and the stop lands at the next boundary, the end of a run included. A decision naming a closed escalation is refused as stale rather than applied to whatever is open now.
- Every move above is an event on Phase 3's run machine — `escalate`, `decide` — so `WAITING_AT_GATE` and the stale-escalation refusal are the machine's, not code in this phase.
- A Gate declares `approvers` (roles); one that declares none is decided by organization owners and platform operators (decision 1). The engine records the decision; Phase 5 versions it and Phase 17 authorizes it.
- `PauseRequest` and the worker `{"pause": true}` leave the protocol — an answer asking to pause is not an envelope, and fails the step; the E2E fixture becomes a step-declared sign-off Gate that the driver sends back once and approves.

*Dev and CI* — the fake Cog set Phase 8 introduces includes `needs-review`, whose output carries an `error` problem, so the default Gate policy escalates without the Cog asking to pause. *Docs* — `execution/README.md` loses `PauseRequest`; the glossary's Gate entry gains the outcomes and decisions; `op-cog-seam.md` states that a Cog cannot pause a run.

*Acceptance*
- [x] A Cog cannot pause a run; only a step's Gate can.
- [x] approve, reject and send back each drive the run as stated; send back past the revise limit ends the run `FAILED` with reason `revise_limit_exceeded`.
- [x] Each decision is recorded with its escalation id, actor, outcome, findings, and a stable digest of the envelope it decided on — the envelope itself is on the escalation the decision names.
- [x] A decision naming an escalation that a send back has closed is refused, and the run is unchanged.

#### Phase 5 — The Track as the accountability record
**Issue** #5 · **Branch** `feat/cog-track-record-5` · **Depends on** Phases 2, 3, 4 · **Size** M · **Status** merged as #158

Make the Track answer "what produced this, and who signed it".

*In scope*
- Event schema v1, versioned by a `schema` field: `step_completed` carries the envelope's `binding`, `problems`, `usage`, a **reference** to the payload, not the payload inline, and the Frames the step was given, each as its id and snapshot digest — empty until Phase 17 delivers any; `gate_escalated` and `gate_decided` (each with its escalation id, the latter with the actor) brought into v1; `step_failed` carries a bounded message, the error code, the idempotency key and the worker name. Pre-v1 events still replay through a reader, with a fixture in the conformance suite.
- Large payloads stored by reference above a size threshold.
- Hub Track tables through `COLLAB_SCHEMA_MIGRATIONS`; the standalone store's `ensure_schema` stays for standalone and local use, and the hub never calls it.
- `SqliteTrackStore`, so level 1 runs the API and the controller as two processes over one Track file, and the desktop has its Track later.
- A **Track conformance suite** — append, replay from a sequence, live stream, status derivation — run against in-memory, SQLite and Postgres. Status derivation is Phase 3's run machine folded over the run's events, and nothing else.
- An optional `label` field reserved on step events for #13; additive, unused. #5's acceptance loses the catalog, which #7 owns.

*Dev and CI* — at level 2, `make psql` shows the Track tables created by migrations, and `levels-2-3` asserts the schema comes from nowhere else; at level 1 the SQLite Track lives in `dev/.local/`. *Docs* — a new `docs/cog-execution/track.md`; `docs/frames-operations.md` lists the tables.

*Acceptance*
- [x] For any completed step, the Track alone names the Cog digest, binding, problems, usage and Frames that produced it; for any gate, who decided.
- [x] Status is derived from the Track after an API restart.
- [x] The hub's Track schema is created only by migrations.
- [x] In-memory, SQLite and Postgres stores all pass the Track conformance suite, including the pre-v1 fixture.

---

### M2 — The `collab-hub` CLI, signed in

The hub's first client, built first: it needs nothing the phases after it build, and each of them adds its commands to it instead of waiting for a client to exist. Every Cog execution feature is reachable from a terminal over the same REST endpoints Collab uses, which is also how a script, a CI job or an operator drives the hub. It starts with what every other command needs, signing in, and with the one Cog surface the hub already serves, its catalog.

#### Phase 6 — The `collab-hub` CLI: sign in, and list the Cogs the hub offers
**Issue** #125, in part — the device flow and the `collab-hub` realm client it also asks for are not in this plan, and #125 stays open for them · **Branch** `feat/cli-auth` · **Depends on** nothing in this plan · **Size** M · **Status** merged as #159

A terminal client for the hub, and the thing every later command needs first: a way to sign in. It is a client, not a second implementation — every command is an HTTP call to the hub's REST API.

*In scope*
- A new `collab-hub-cli` distribution under `cli/` (Python ≥ 3.11) whose console script is `collab-hub`, built on [Typer](https://typer.tiangolo.com): one command group per hub surface, `--help` that reads like documentation, shell completion for free.
- **Authentication first, the way the desktop does it.** `collab-hub login [--hub URL]` signs in against the hub's Keycloak realm with the client and the flow the Collab desktop already uses: the public `apollo-desktop` client, the authorization code flow with PKCE (S256), and a loopback redirect to a listener on `127.0.0.1` and a port the system picks (RFC 8252), asking for `offline_access` so the session renews. It opens the realm's sign-in page in a browser on the same machine. No realm client, grant or mapper is added: the desktop's client already allows loopback redirects, and its tokens already carry the audience the hub checks.
- **The hub's URL is all a user types.** A public `GET /v1/auth/cli` names the issuer and the client id the CLI signs in with, so nobody copies a realm URL, and says when the hub runs the dev-auth shortcut instead.
- `collab-hub login --with-token` reads a bearer token from stdin instead, for CI and for a script that already holds one; such a token is used as given and never refreshed.
- The token is written to the user's config directory, readable only by its owner, and sent only to the hub it was obtained for. It is renewed before it expires, and forgotten only when the realm refuses the refresh, never because the realm is down. A new sign-in ends the session it replaces at the realm, unless both are one realm session. `collab-hub logout` ends the realm session through its end-session endpoint, as the desktop's sign-out does, revokes the refresh token (RFC 7009), and forgets it.
- `collab-hub whoami` prints the subject, the organization and its roles, the hub and the token's expiry. The identity comes from a new `GET /v1/me`, which answers with what the hub resolved for the caller and how it authenticated them, so `whoami` reports what the hub will act on rather than what the token claims.
- Profiles, so several hubs are one flag apart: `~/.config/collab-hub/config.toml`, overridden by `--hub`, `--profile` or `COLLAB_HUB_URL`.
- Level 1 has no realm, so the CLI also works against the dev-auth shortcut and says on `whoami` that the session is unauthenticated dev auth, never implying a signed-in user.
- **The Cogs the hub offers.** `collab-hub cog list` over the catalog read API (#85): one row per Cog at its newest version, the API's filters as options, and every page followed. `collab-hub cog show ID` prints one Cog's card and its versions. `cog launch` joins this group in Phase 15, and `cog install` in Phase 23.
- Human-readable tables by default, `--json` for scripts, and exit codes a script can branch on: 0 success, 1 the hub refused or failed the request, 2 a usage error, 5 not signed in or the session has expired.
- An import-boundary test: the CLI imports neither `collab_hub_api` nor `collab_hub_execution` — the rule the Worker SDK follows, for the same reason.

*Dev and CI* — `make cli` installs it into the dev environment and prints the login line for the level in use. CI: a path-gated `test-cli.yaml` runs the CLI's own suite on Python 3.11 and 3.14, the SDK's rule; `level-1` runs `collab-hub whoami --json` and `collab-hub cog list --json` against `make api`'s dev auth and asserts an unauthenticated session and the empty catalog; `levels-2-3` runs the real sign-in against the dev realm — a stdlib stand-in for the browser, given to `webbrowser` through `BROWSER`, fills the realm's login form as `dev` and follows the redirect back to the CLI's listener — then asserts `whoami` names the `dev` user, the session file is `0600`, and `logout` leaves nothing on disk, and signs in again with `--with-token` and a token from `make token`; level 4 asserts `apollo-desktop` still allows PKCE and the loopback redirect the CLI depends on. The flow is also unit-tested against a stub realm, including a real redirect to the listener and a refused one carrying another sign-in's `state`.

*Docs* — a new `cli/README.md`: installing, signing in, profiles, the catalog commands, output and exit codes. `dev/README.md` gains *Signing in from the CLI* at level 3 and a table of the dev realm's users; the root `README.md` links the CLI from its documentation section; `docs/standalone-deployment.md` says the CLI reuses `apollo-desktop`, which must keep its loopback redirect, and lists the public protection-map entry `GET /v1/auth/cli` needs.

*Acceptance*
- [x] `collab-hub login` signs in through the desktop's flow against the dev realm, `whoami` names the signed-in user, and `logout` ends the realm session and leaves nothing on disk.
- [x] The stored token is readable only by its owner and is refreshed when it expires.
- [x] At level 1 the CLI works against dev auth and says the session is unauthenticated.
- [x] `collab-hub cog list` lists every Cog the catalog serves, across pages, as a table and as `--json`.
- [x] The CLI imports no hub package, enforced by a test.

---

### M3 — The runner on `none`, and a worker as a local process

Where the two configuration axes are born, each with its first value: `none` for durability, `local` for location. By the end of it the hub's controller runs a fake Cog as a child process, picks runs up from the Track, and answers honestly when it is killed. Between the two axes and the controller's full form sits Phase 10, an example: the thinnest slice of the controller, the run API and the CLI that lets a user launch a Cog from a terminal on a hub running on their own machine.

#### Phase 7 — Extract the lifecycle runner, no behaviour change
**Issue** #100 · **Branch** `enh/cog-lifecycle-runner` · **Depends on** Phases 3, 5 · **Size** M · **Status** merged as #162

A pure refactor, so the phase that changes behaviour is reviewed against a known baseline rather than inside a move.

*In scope*
- A `LifecycleRunner` that owns the lifecycle as plain step functions: resolve, materialize, interact, read the envelope, evaluate the Gate, idle or teardown — with budgets, cancellation and Track recording. The lifecycle logic in #35's `DurableWorkflowEngine` moves here unchanged.
- `DurableWorkflowEngine` keeps its public methods and delegates every one to the runner, including its Track-based recovery, which Phase 8 removes.
- A registry of the runner's step functions, which Phase 8 uses to prove every backend calls the same ones.
- Each step function moves Phase 3's machines by emitting events; none assigns a state itself, and the runner records the Track events a transition returns.

*Dev and CI* — no new target; every existing test passes unmodified — a PR that has to change an existing test is not a refactor. *Docs* — `execution/README.md` names `LifecycleRunner` as where the lifecycle lives.

*Acceptance*
- [x] Every existing execution test passes unmodified.
- [x] `DurableWorkflowEngine` makes no lifecycle decision itself.

#### Phase 8 — The durability seam and the `none` backend
**Issue** #101 · **Branch** `feat/cog-durability-none` · **Depends on** Phase 7 · **Size** L · **Status** merged as #163

The runner, with the durability engine plugged in or absent — and `none`, the absent case, ships first.

*In scope*
- A `DurabilityBackend` protocol and selection by configuration: `none | dbos | temporal`. `dbos` and `temporal` fail at startup with *not implemented* until Phases 26 and 32, so the configuration shape is fixed now and callers never import a backend.
- **One driver, shared by every backend.** Phase 7 leaves the runner in two parts: the step functions registered in `STEP_FUNCTIONS`, and the driver (`_advance`) that sequences them and holds the rest of the lifecycle — pickup and completion, skipping completed steps, the approved-escalation shortcut, the budget boundaries, and the mapping from a failure to an outcome. The `DurabilityBackend` protocol is how the driver hands each step function to a backend to run and checkpoint; a backend copies none of the driver.
- **The `none` backend**: calls the step functions directly, in process; nothing is checkpointed.
- **Honest non-durability.** A terminal status, `interrupted`. When a host starts, every run it owned and did not finish is recorded `interrupted` on the Track — never resumed, never left looking like it is running. Retrying an interrupted run is explicit, and it continues the attempt that was in flight: that step keeps its idempotency key, so Phase 16's claim can answer for a side effect the worker completed before the controller died. A step whose *failure* was recorded runs again as a new attempt, with a new key.
- #35's Track-based recovery is removed, and `DurableWorkflowEngine` with it.
- `cancel(run_id, actor)`: ends the run `cancelled`, tears down the worker, records the actor.
- Two conformance suites. **Lifecycle** — every backend passes: multi-step completion, a Gate escalating and each human decision, cancel, a budget stop, retry as a new attempt, the Track's contents. **Durability** — run by Phases 26 and 32: kill mid-step and resume without resubmission; a run paused at a Gate resuming there after a restart; replicas never both running one step. For `none`, the suite asserts the `interrupted` contract instead.
- The executor is still whichever the caller constructs; Phase 9 adds the `location` switch beside `backend`, on the same pattern.

*Dev and CI* — `make op OP=<name>` at level 1 runs a fake Op in process and prints its Track; Op definitions live in `dev/ops/<name>.yaml`, fake Cogs under `dev/cogs/` (`echo`, `needs-review`, `fails`, `slow`, `spender`); both suites run in `test-execution.yaml`. *Docs* — a new `docs/cog-execution/runs.md` opening with the backends table; the root `README.md` gains *Known limitations*, starting with "`none` does not survive a restart".

*Acceptance*
- [x] A multi-step Op with a Gate completes on `none` through the runner.
- [x] After a restart, every run `none` had in flight is `interrupted` on the Track, and none of them resumes.
- [x] No caller imports a concrete backend; the configuration value is the only switch.
- [x] A test proves all backends call the same step functions, by spying on `STEP_FUNCTIONS` under each, and none reimplements the driver.

#### Phase 9 — Agent location: `local` first, `remote` behind the same switch
**Issue** #109 · **Branch** `feat/cog-agent-location` · **Depends on** Phase 8 · **Size** M · **Status** merged as #174

Where a worker runs is the second axis under the runner, chosen by configuration exactly as the durability backend is — and the first value implemented is the one that needs no cluster.

*In scope*
- An **agent location**, `location: local | remote`, selecting the `CogExecutor` the controller constructs. `remote` fails at startup as *not implemented* until Phase 21, so the configuration shape is fixed now and callers never import an executor.
- **`local`**: `LocalProcessCogExecutor`. Materialize runs the Cog package's declared `serve` task as a child process of the controller, in the package's own pixi environment, bound to a loopback port the executor chooses, with a run token in its environment; ready polls `/healthz`; teardown kills the process tree and reaps it. The package comes from a directory, through a **directory package source** this phase adds: it maps an allowlisted name to a directory under a configured root, reads from the package's `pixi.toml` and `pixi.lock` the two things a worker needs, the `serve` task and the digest, and refuses a name or path that escapes the roots, a manifest or lock that is a symbolic link, and a package without its lock, which pixi then runs `--locked`. It does not call the bundle reader #81 merged, which lives in the API package: the execution package does not import the API. It is not #83's `static` registry source, which enumerates an OCI registry and cannot resolve a local path. A package resolved this way is recorded on the Track and in claims by that name plus the digest of its manifest and lock, so a development run is identifiable and never mistaken for a published one. `nebi pull` and install by digest (Phases 22, 23) are not needed here.
- The **run token**, defined here because every hub-mediated path a worker uses later relies on it: one per materialized worker, minted by the controller when it materializes the worker and expiring at teardown, an opaque secret whose hash is recorded beside the run in the store the API and the controller share — so either process verifies it without asking the other. It reaches the worker in its environment at `local` and mounted into the pod at `remote` (Phase 21). The worker presents it to every hub endpoint it calls — the claim transport (Phase 16), the model egress gateway (Phase 28), the connector proxy (Phase 29) — and the controller presents it to the worker's `/invoke` as a bearer token from this phase on, which the fake Cogs check and Phase 28 makes the SDK verify. One token, both directions, never a second one. On the Track the hash is on `worker_started`, and the token is valid until that worker's `worker_stopped`, or until the run ends or is interrupted.
- A `local` worker cannot outlive its controller by accident, and the executor does not rely on it being a child for that: an orphaned child survives a killed parent on Linux and macOS alike. The executor starts each worker in its own process group through a small launcher of its own, which holds a pipe from the controller and kills the group when that pipe closes — on any controller death, `SIGKILL` included — with `PR_SET_PDEATHSIG` as a second line on Linux. The Cog knows nothing of this. The controller also records each worker's pid and process group on the run, so its next start (Phase 11) reaps a survivor that beat the launcher.
- The executor never binds a non-loopback address; a worker inherits nothing from the controller's environment beyond what the binding delivers. The worker's stdout and stderr are captured to a per-run directory and referenced from the Track, never inlined. A binding's `auth_ref` is resolved by the controller and the value enters only the child's environment: never the Track, never a file on disk.
- A **location conformance suite** — materialize, ready, an `/invoke` round trip, teardown, cancel mid-interaction, the controller dying with a worker in flight (killed with `SIGKILL`, not a signal it can handle; the worker must not outlive it), two workers on one host without a port collision — run by `local` now and `remote` in Phase 21.
- #35's `KubernetesCogExecutor` is untouched here; Phase 21 puts it behind `remote`.

*Dev and CI* — `make op OP=echo LOCATION=local` at level 1 — no containers, the SQLite Track — runs the `echo` fake Cog as a real process, and without `LOCATION` the fake Cogs still answer in process; `make controller LOCATION=local` arrives with the controller itself, in Phase 11. The fake Cogs become packages: a `pixi.toml` with a `serve` task, its lock, and a `serve.py`. pixi joins level 1's prerequisites, marked as needed only for `LOCATION=local`. CI: `level-1` runs one fake Cog through the local executor on Linux and macOS (Windows is out of scope, decision 15) and asserts a killed controller leaves no worker behind; the location suite runs in `test-execution.yaml`. *Docs* — ADR-0002 gains **D12, agent location**, appended and never renumbered, and a sixth invariant: no lifecycle logic inside an executor, and `location` is the only switch; the glossary gains *Agent location*, *Local worker*, *Remote worker*; `runs.md` gains the location table.

*Acceptance*
- [x] With `location: local`, an Op step runs in a child process of the controller and completes with a valid envelope, at dev level 1, on Linux and macOS.
- [x] Killing the controller with `SIGKILL` leaves no worker process running, on Linux and macOS.
- [x] `location` is the only switch; no caller imports an executor.
- [x] `local` and the in-memory executor pass the location conformance suite.
- [x] At level 1 with registry access disabled, the directory source finds and launches a package by name, and a name or path outside the configured roots is refused.

#### Phase 10 — Hermes as a Cog, launched from the CLI on a local hub and talked to from Toad: the example
**Issue** #177 · **Branch** `feat/cog-local-example` · **Depends on** Phases 6, 9 · **Size** M · **Status** merged, #178

The whole path a user takes, demonstrable now, on a hub that is two plain processes on one machine: sign in, launch Hermes as a Cog, see it running, talk to it from Toad, an ACP client, stop it. It is a walking skeleton, the thinnest slice of three later phases, built first so there is something to show and to point a newcomer at; Phases 11, 12 and 15 then build each part out, and the example keeps running through all of them.

*In scope*
- **The Hermes harness Cog, in its first form** — `cogs/hermes/`, where decision 16 puts it: a pixi package pinning `hermes-agent[acp,anthropic]==0.19.0` on Python 3.12, and a standard-library worker that is Hermes's ACP client. It starts `hermes acp`, opens a session, and serves two entry points: `session`, which holds the Hermes session and answers each turn with what Hermes said, and `ask`, one prompt. Its model is delivered by the controller to this Cog alone (`--deliver hermes:COLLAB_MODEL_PROVIDER`, and the endpoint, name and key), the stopgap decision 13 allows until Phase 11's `models:` block: an OpenAI-compatible endpoint, or Claude through Hermes's own Anthropic provider; `dev/fake-model` is a standard-library endpoint that answers with no account, the default at dev level and in CI, and the example switches to Claude when `ANTHROPIC_API_KEY` is set, which `make claude` checks. Hermes's environment is an allowlist, its `HOME` its own workspace, so no other key or sign-in on the machine reaches it. Hermes gets a home of its own per session, the run token and the key stay out of its environment, and it runs with no tools (decision 14): 0.19's ACP adapter enables its whole toolset and reads no setting that narrows it, so the Cog starts it through a launcher that gives each session none, which a test checks against a model that orders a shell command. Phase 14 rebuilds this worker on the Worker SDK.
- **`examples/cog-local/`**: a `hello` Cog package — one standard-library worker serving `/healthz`, `/invoke` and `/turn`, a `pixi.toml` with a `serve` task, its lock — with two entry points, `run`, which answers once, and `session`, which stays open and answers turns until it is told `bye` or its run is terminated. It is the smallest Cog that holds a session, the one to copy. The example itself launches Hermes: a Makefile with one target per step — `env` installs what the example uses (the CLI, the hub's Python, Toad, pixi and Hermes's environment), then `hub`, `credentials`, `claude` (checks `ANTHROPIC_API_KEY`, with which Hermes chats with Claude), `model`, `api`, `controller` (or `start`), `login`, `cogs`, `launch`, `list`, `connect` (Toad, talking to Hermes), `say`, `stop`, `shutdown` — a README that walks through them; `make demo`, which runs them all against the fake model, each explained under a banner, and checks each one, chatting with Claude too when a key is set; and `make toad`, which does the same steps on Claude and ends with Toad open on Hermes, ready to chat. `examples/README.md` indexes the examples and their conventions.
- **A real sign-in.** The example runs at level 3: Postgres and Keycloak in containers, the realm's `dev` user, `collab-hub login` through the browser (or `--with-token` from the realm, for CI), and the API verifying the realm's tokens.
- **The run controller, in its first form** (`python -m collab_hub_execution.controller`): it watches a SQLite Track, starts each run that was submitted and not picked up on the lifecycle runner, with `backend` and `location` from its arguments, and delivers each request to cancel and each turn. It alone constructs an executor. When it starts, every run a stopped controller left unfinished is recorded `interrupted`. One controller per Track, held by a lock beside the file; it polls.
- **Intent on the Track** (`collab_hub_execution.intents`): the API's half. `op_submitted` gains who submitted the run (`submitted_by`: user, organization, workspace, and a name for showing), and three new facts: `cancel_requested`, and `turn_requested` with the controller's `turn_answered` or `turn_failed`. A cancel request is written with the Track's new conditional append (`append_if`), so none lands after a run's end and racing requests leave one. A run is read back as a view: its status, each step's state, each completed step's output, and its turns.
- **Turns**: a Cog whose entry point holds a session answers `POST /turn` while its step is in flight. The controller delivers a run's waiting turns to its live worker, in order, and records each answer; a client never reaches the worker.
- **The run API, in its first slice**, behind the `cog_runs` feature flag and mounted only when it is on: `POST /v1/runs` (a step's `cog` names a package of the directory source; any other is refused with 422, naming the packages that can be launched), `GET /v1/runs/launchable`, `GET /v1/runs` (organization-scoped, filtered by status, paged), `GET /v1/runs/{id}` (with each completed step's inline output), `POST /v1/runs/{id}/cancel` (202; 409 naming the status for a run that has ended), and `POST /v1/runs/{id}/turns` with `GET /v1/runs/{id}/turns/{turn}`. Every answer names the backend and the location. The API writes intent and reads the Track, constructs no executor, and imports the execution package in the run router only, which a test enforces.
- The execution package is a dependency group of the API, not a dependency: the image does not ship it until Phase 12 retires the flag, and a deployment without the flag never imports it.
- **CLI**: `collab-hub cog launch NAME [--entry NAME] [--input JSON | -] [--gate POLICY] [--watch]`, a one-step Op the CLI builds; `cog list --launchable`; `run list [--status]`; `run show ID`, which prints what each step answered; `run watch ID`; `run say ID TEXT`, one turn; `run connect ID`, the run served as an [ACP](https://agentclientprotocol.com) agent on stdio, so an ACP client such as Toad talks to the Cog, each prompt one turn; `run terminate ID [--no-wait]`. Watching polls `GET /v1/runs/{id}`. Exit codes 3, 4 and 6 for a watched run that ended `interrupted`, waits at a Gate, or was cancelled.

*Not here* — what the slice leaves to the phases it is cut from. Phase 11: pickup two replicas can race for, the Track on Postgres, reaping a survivor by its recorded pid, health endpoints, the `models` block, delivering a Gate decision. Phase 12: the event stream, the flag retired and the package in the image, the protection-map entry. Phase 13: how a harness Cog maps turns onto its own agent loop, which the Worker SDK defines. Phase 14: Hermes on that SDK, configured from the binding, its tools off (decision 14), its failures as error codes, published. Phase 15: `run watch` over the event stream, surviving a dropped connection.

*Dev and CI* — `make controller`, and `make api` and `make api-oidc` serve the run API, all over `dev/.local/track.sqlite` and all finding packages in `COGS` (`dev/cogs` and `examples/cog-local/cogs`); the controller sets no limit on an interaction, since a session lasts as long as someone talks to it. `make fake-model` is the model the Hermes Cog calls by default. CI: a new path-gated `examples.yaml` runs `make -C examples/cog-local demo` against the realm — sign in, launch Hermes, list it, talk to it over ACP with a scripted client and with `run say`, terminate it, and no worker left — apart from `dev-env.yaml` because Hermes's environment is about 400 MB; the controller, intents and turns are tested in `test-execution.yaml` with real worker processes, the Hermes Cog's worker there against a stand-in ACP agent, the routes in `test.yaml`, the commands and the ACP bridge in `test-cli.yaml`. *Docs* — `runs.md` gains *The run controller and the run API*, with turns; `track.md` gains `cancel_requested`, the turn facts and `submitted_by`; `op-cog-seam.md` gains sessions and `/turn`; `cogs/hermes/README.md`; `examples/README.md`; the glossary gains *Run intent* and *Turn*; `feature-flags.md` lists `cog_runs`; `cli/README.md` gains the launch, run and connect commands and the exit codes; `dev/README.md` gains `make controller` and the example; the root `README.md` points to the example.

*Acceptance*
- [x] With Docker and uv, `make env`, then the README's steps in `examples/cog-local`, sign in through the realm with the CLI, launch Hermes, list it, talk to it from Toad, and stop it.
- [x] Hermes answers through any OpenAI-compatible endpoint the controller is given, and the fake model by default; it never receives the run token, and runs no command even when its model orders one.
- [x] `make demo` does the same with no browser and no terminal interface, against the fake model, checking each answer, in CI.
- [x] `collab-hub run terminate` ends a run `cancelled` with its actor on the Track, no worker left, and no more turns taken.
- [x] The API constructs no executor, and reaches the execution package from the run router only, enforced by a test.
- [x] A Cog the hub cannot launch is refused at submit with 422, naming those it can, and the CLI reports them.
- [x] A member of another organization can neither see, cancel nor talk to a run.

#### Phase 11 — Run controller and run pickup
**Issue** #121 · **Branch** `feat/cog-run-controller` · **Depends on** Phases 8, 9, 10 · **Size** M · **Status** in review, #186

The process that advances runs, separate from the process that accepts them — the shape production has, kept from level 1 up. Phase 10 built its first form: one controller polling a SQLite Track, starting submitted runs and delivering cancels. This phase makes it the controller a deployment runs.

*In scope*
- `collab-hub-run-controller`: the execution package's own entrypoint, with each option also read from a `COLLAB_CONTROLLER_*` variable for a container; the image ships it with the execution package in Phase 12, and the chart runs it with the `remote` location in Phase 21. It runs the lifecycle runner with `backend` and `location` from configuration, and it alone constructs an executor; the API process constructs none, which a test enforces from here on, both on the source and in a running API with the run routes on.
- **Run pickup**, under every backend: the API records `op_submitted` and nothing else — it never enqueues into an engine, which is what keeps it backend-agnostic — and a controller takes the run with an atomic pickup record — the `run_picked_up` event Phase 3 introduced, now naming the controller and written with the Track's conditional append, so it lands only if nobody picked the run up or cancelled it — so two replicas never both start it. What differs per backend is ownership *after* pickup. Under `none` a run in flight belongs to the process that picked it up and ends `interrupted` with it; under `dbos` and `temporal` the picking controller hands the run to the engine's queue, and the engine decides which replica resumes it when that controller dies.
- **A controller is known by its id** (`--id`, the host's name by default), held for as long as it runs — a lock beside a SQLite Track, an advisory lock on Postgres — so two live controllers never share one, and the same across its restarts, so a restart takes its runs back. A run picked up before controllers named themselves belongs to the first to start.
- On start, the controller records `interrupted` for every run it owned and did not finish, and only those, and reaps, by the pid and process group it recorded, any `local` worker of its own left behind — once the pid is shown to still be that run's worker, so a reused pid is never killed. `worker_stopped` records whether it was reaped.
- **Signals travel through the Track.** The API records what a client asked for — `cancel_requested`, and a decision on a Gate as `decision_requested`, written only while the run waits on that escalation — and the controller's Track watcher delivers it to the run it owns: under `none` straight to the waiting runner, which records `gate_decided` and advances the run (or the controller records `decision_refused`), under `dbos` and `temporal` as the engine's own message or signal (Phases 26, 32). Nothing calls the controller, so the API needs no route to it and no engine client. The route that writes a decision is Phase 17's.
- Controller health and readiness: `/healthz` while it passes over the Track, `/readyz` once it has started and its last pass read it.
- A minimal `models:` configuration block — endpoint, model id, `auth_ref`, and the model's context window and maximum output tokens, with the Cogs bound to each — in TOML, the key read from the controller's environment by `auth_ref` and never written down, since the hub has none today (§3). It replaces Phase 10's `--deliver`, is what the controller delivers each Cog's model from until Phase 24, and what Phase 24's inventory is generated from after; the chart's values render it with Phase 21.
- The Track on Postgres for the controller (`--track postgresql://...`), its tables from the hub's migrations; the API's run routes move to it in Phase 12.

*Dev and CI* — `make controller` — SQLite Track at level 1, Postgres at level 2 (`TRACK=`), `BACKEND=` and `LOCATION=` selecting each axis, `CONTROLLER_ID=`, `ENVIRONMENT=host` with no pixi, `HEALTH_PORT=` — writes the models block from `COLLAB_MODEL_*` (`make models`); `make submit OP=` records intent for the controller to pick up and follows the run, and `make op` stays the one-process host, as the controller `make-op`, for what a runner alone configures (a budget, until Phase 25). CI: `level-1` asserts the fake Ops complete across the two processes and that killing the controller with `SIGKILL` leaves the run `interrupted` when it starts again; `levels-2-3` asserts two controllers on one Postgres Track complete eight runs submitted at once and never both start one; `test-execution.yaml` races two controller processes on SQLite and on Postgres, the reaper, ownership and decisions. *Docs* — `runs.md` gains the controller (pickup and ownership, signals, configuration, health, the models block); `track.md` the new fields and facts; the root `README.md` *Architecture* diagram gains it and its workers; the glossary's *Run pickup* entry and ADR-0002 D4, which still say `dbos` and `temporal` replace pickup, are corrected to say they take over ownership after it, and the glossary gains *Run controller id*; `dev/README.md` gains *Run pickup*.

*Acceptance*
- [x] Two controller replicas never both start the same run.
- [x] A run in flight ends `interrupted` when its controller is killed, and the next controller start records it so.
- [x] The API process constructs no executor, enforced by a test.

---

### M4 — Hermes, launched by the hub

The primary goal, as soon as the controller runs a `local` worker: the slice of the run API that launches, lists and terminates a run, the SDK that makes a harness a worker, and the Hermes Cog itself — running as a local process, at dev level 1, with no cluster. Nothing on this path waits for the CLI, the keyed claim or the rest of the run API: a Hermes step with no tools takes a prompt and returns an envelope, so there is no side effect for a claim to guard, and nothing in this slice invokes a step twice.

One limit of this milestone is stated rather than discovered. Every step has a Gate, and the default escalates on an `error` problem, but deciding an escalation arrives with Phase 17. Until then a run whose step escalates waits at its Gate and can only be terminated. So the dev `hermes` Op (`dev/ops/hermes.yaml`) declares `gate: never`, and `collab-hub cog launch` takes `--gate` (Phase 15), which keeps M4's demonstration from stopping at a Gate nobody can yet decide.

#### Phase 12 — The hub run API: launch, list and terminate
**Issue** #103, first half · **Branch** `feat/cog-run-launch` · **Depends on** Phases 10, 11 · **Size** M · **Status** in review, #202

State a Cog for execution, see what is running, stop it — as endpoints Collab and any other client can use. Decisions, payloads and retry follow in Phase 17. Phase 10 built the first slice behind the `cog_runs` feature flag: submit, list, read and cancel, over a SQLite Track. This phase adds the event stream, serves the routes on the Track a deployment uses — Postgres, which the controller reads since Phase 11 — ships the execution package in the image, with the controller's entrypoint, and retires the flag.

*In scope* — under `/v1`, authenticated, with protection-map entries and OpenAPI:
- `POST /v1/runs` — submit an Op: JSON mirroring `OpDefinition` (steps with `name`, `cog`, `entry_point`, `input`, `gate`). Until Phase 23 installs by digest, a step's `cog` names a package that Phase 9's directory package source holds, which is how level 1 names `cogs/hermes` and the fake Cogs; any other name is refused with 422, naming the packages the source does hold. The catalog is not involved: `GET /v1/cogs` lists what registries publish, and catches up with what can be launched when Phase 23 installs by digest.
- `GET /v1/runs`, `GET /v1/runs/{id}` — organization-scoped, status derived from the Track, `interrupted` included, with each step's Cog and state.
- `GET /v1/runs/{id}/events` — replay from `after`, and `text/event-stream` for a live run.
- `POST /v1/runs/{id}/cancel` — terminate a run: the API records `cancel_requested` with the actor, the controller's Track watcher (Phase 11) tears the worker down, and the run ends `cancelled`. Cancelling a run that has already ended is a conflict naming its status.
- The response to a submission names the durability backend and the location it will run under, so a client never assumes a `none` run survives a restart (decision 3) or that a `local` worker is isolated (decision 11).
- The API writes intent and reads the Track and never calls the executor, enforced by an import-boundary test.

- **Where the Track is.** The API's Track is the hub's own database (`frames.postgres.url`), its tables from the hub's migrations, the same Track a controller on Postgres reads (Phase 11); `runs.track_path` names a SQLite file instead, for a hub without Postgres (dev level 1). With neither, every run route answers 503 `runs_unavailable`, the way the Cog catalog answers without a database. Each run names `picked_up_by`, the controller that advances it.
- **Known limit, planned.** Listing runs reads every run's Track and filters afterwards; the run index of Phase 17 makes it one query. The API's run views read each run under a lock of its own, outside the lock that guards which runs are kept, and keep at most 10,000 runs, the least recently read let go.
- **The flag retired.** `cog_runs` moves to the retired flags, and the execution package becomes a dependency of the API: the image installs it from the repository root, with the controller's entrypoint `collab-hub-run-controller`, so the controller runs from the same image. A deployed hub then serves the run API and launches nothing: the chart gives it no Cog packages and runs no controller until Phase 21 (the `remote` location), and catalog Cogs become launchable by digest in Phase 23.

*Dev and CI* — `make submit` launches a fake Op through `POST /v1/runs` and follows it (`make op` stays the one-process host of Phase 11); `make run-events RUN=` streams a run; `make api-pg` serves the run API on the hub's Postgres. CI: `level-1` launches the `echo` and `slow` fake Cogs over HTTP, sees them in `GET /v1/runs`, streams a run's events to its end, cancels `slow` mid-step and asserts it ends `cancelled` with no worker process left; `levels-2-3` runs the run API on Postgres with two controllers. `build-images.yaml` builds on `execution/` changes too. *Docs* — `docs/standalone-deployment.md` *Protection map* gains `/v1/runs/**`; `runs.md` gains the events route and where the Track is; `feature-flags.md` retires `cog_runs`.

*Acceptance*
- [x] At level 1, a client launches a fake Cog, lists it, streams its events and sees it complete using only these endpoints.
- [x] Cancelling a run mid-step ends it `cancelled` with its actor on the Track, and leaves no worker behind.
- [x] A step naming a Cog the controller cannot launch is refused at submit with 422.
- [x] A member of another organization can neither see nor cancel a run, and unauthenticated requests are refused under the hardened path map.

#### Phase 13 — Worker SDK and harness-neutral adapters
**Issue** #107 · **Branch** `feat/cog-worker-sdk` · **Depends on** Phases 2, 9 · **Size** M

Implement the seam once, so wrapping a harness is an adapter rather than a server — Hermes first, pi and OpenCode after it.

*In scope*
- A new `collab-hub-cog-worker` distribution (Python ≥ 3.11) serving the seam: `POST /invoke` returning an envelope, `GET /healthz`, cancellation, the binding loader — a file in the format Phase 24 later fills from the Cog's own `resolve`; until then the controller writes it from the hub's `models:` block — usage accounting, and an `/invoke` authentication hook that Phase 28 fills in.
- A `HarnessAdapter` protocol: `start(binding)`, `interact(task, context, signal)`, `cancel()`, `close()`.
- `AcpHarnessAdapter`: spawns any ACP agent over stdio, runs `session/new` then `session/prompt`, and folds `session/update` notifications into `raw` and `payload`. It derives `ok` and `error` from explicit failure signals, **not from `stopReason` alone** — Hermes 0.17 reports a failed turn as `end_turn`. Permission requests outside the Cog's declared tools are denied.
- `OpenAICompatAdapter`: one model call, no tools. It proves neutrality and is what a plain context Cog needs anyway.
- **Frames, the same way to every harness.** When the task bundle carries the step's Frame snapshots — from Phase 17, which snapshots them at submit; until then a task has none — the SDK hands them to the adapter: `AcpHarnessAdapter` sends each in `session/prompt` as an embedded `resource` block (`text/markdown`, a URI naming the Frame) when the agent advertises `promptCapabilities.embeddedContext` at `initialize`, and as a text block when it does not; `OpenAICompatAdapter` sends them as context messages. A harness Cog never calls the Frames API; Frames a Cog has to find while it runs come later, through the hub-mediated Frames MCP (Phase 29).
- **The model's limits, in the binding.** The binding file carries the model's context window and maximum output tokens, and the SDK hands them to the adapter, so a harness reserves output headroom from a number rather than a guess. The SDK does not run a harness's turns: an agent harness runs its own loop and owns its context within it (decision 19).
- **One error for a context that did not fit.** `context-exhausted` joins the envelope's closed error-code set — `result-envelope.md` and the hub's parser change in this PR, before any worker emits it — mapped to 422: the input and Frames could not fit the bound model even after the harness's own recovery. Retrying on the same binding would not help, so a Gate or a person chooses a larger model or fewer Frames.
- A stdlib fake ACP agent and a stdlib fake OpenAI-compatible model server, so both adapters test at level 1 without installing a harness or starting a container.
- An import-boundary test: the API and the execution package never import the SDK. A Cog's pixi environment gets the SDK as a git dependency on this repository at a commit (decision 12).

*Dev and CI* — both stdlib fakes sit in `scripts/testdata/`; `make fake-model` runs the model one at level 1; the adapter conformance suite runs in its own path-gated workflow on Python 3.11 and 3.14. *Docs* — a new `worker/README.md`: writing a Cog on the SDK, the adapter protocol, the authentication hook, the `end_turn` hazard, and what a harness owns per turn against what the SDK hands it (decision 19); `result-envelope.md` gains `context-exhausted`.

*Acceptance*
- [ ] Both adapters pass one shared adapter conformance suite through the same seam server, on Python 3.11 and 3.14.
- [ ] A step's Frames reach both fakes' prompts — as embedded resources or as text, as the fake ACP agent advertises — and an input larger than the binding's context window ends the step `ok: false` with `context-exhausted`.
- [ ] A worker built on the SDK runs as a `local` worker under Phase 9's executor; Phase 22's materialization later runs it unchanged.

#### Phase 14 — The Hermes harness Cog, launched by the hub as a local process
**Issue** #108 · **Branch** `feat/hermes-acp-harness` · **Depends on** Phases 9, 12, 13 · **Size** M

The primary goal: `POST /v1/runs` on a laptop starts Hermes as a separate process, and the Op step completes with a valid envelope. No cluster, no registry, no durability engine, no keyed claim and no CLI — those come after or alongside, and none of them changes this Cog.

Phase 10 built this Cog's first form: `cogs/hermes` with a worker of its own that drives `hermes acp`, a `session` entry point answering turns and an `ask`, its model delivered by the controller, and the fake model — so the hub already launches Hermes and Toad talks to it. This phase rebuilds the worker on the Worker SDK (Phase 13), configures Hermes from the binding rather than delivered variables, turns its tools off, and adds what is listed below.

*In scope*
- `cogs/hermes/` in this repository (decision 16), published as `<registry>/cogs/hermes` by the publish workflow Phase 22 adds: a pixi environment on Python 3.13 — Hermes does not support 3.14 — with `hermes-agent[acp,mcp]==0.19.0` pinned, whose `serve` is the SDK with `AcpHarnessAdapter` running `hermes acp`.
- Hermes configured headlessly from the binding at start, **porting** the desktop's headless Hermes configuration (written for 0.17, re-verified against 0.19) rather than re-deriving it: model endpoint and key by reference.
- **Hermes over ACP, not the desktop's chat.** The Cog drives `hermes acp` through the Worker SDK and does not reuse the desktop's chat pipeline, which runs on Ravnar and needs the desktop. Hermes runs its own agent loop, so what it keeps in context from turn to turn is Hermes' own; the Cog hands it the binding's limits as far as its configuration takes them, and once Phase 17 snapshots a step's Frames, they arrive in its prompt as ACP resources through the SDK (Phase 13), with no change to this Cog and no tools needed.
- **No tools at first** (decision 14): no MCP servers configured, Hermes' terminal and file tools disabled — prompt in, envelope out. Hub-mediated MCP (Frames, connectors, restricted to hub endpoints) arrives with Phase 29's grants; Hermes' terminal backend, which would make the worker the sandbox, only once Phase 28's egress restriction and authenticated `/invoke` exist, and only for `remote`.
- Level 1 runs it straight from `cogs/hermes` through Phase 9's directory package source — no registry — until Phase 23 installs it by digest.
- A CI test against the stdlib fake model; one opt-in test against a real model.

*Dev and CI* — `make controller LOCATION=local` and `make op OP=hermes` at level 1, against `make fake-model` or a real endpoint given by environment — the demonstration of M4. `dev/ops/hermes.yaml` declares `gate: never`, since deciding a Gate arrives with Phase 17. CI: a path-gated `test-hermes.yaml` runs the Cog through the local executor on Linux against the fake model; it downloads Hermes' environment, so it stays out of `dev-env.yaml`. *Docs* — `cogs/hermes/README.md`; the catalog card describes the harness; `docs/cog-execution/README.md` links it as the reference harness Cog; `dev/README.md` gains *Running Hermes*.

*Acceptance*
- [ ] An Op step naming this Cog's bundled `ask`, submitted through `POST /v1/runs`, completes in a child process of the controller with a valid envelope, at level 1. (A context Cog *requiring* a harness resolving to it is Phase 24's, where resolution lives.)
- [ ] A Hermes failure surfaces as `ok: false` with an error code, not as a silent `end_turn`; a turn that overflows the bound model and cannot recover surfaces as `context-exhausted`.
- [ ] The Cog contains nothing that knows whether it is a process or a pod.

*Risk* — 0.19's ACP surface is not the 0.17 one Collab drives today; the configuration port, the `end_turn` handling and whether 0.19 advertises `embeddedContext` are re-verified against 0.19 in the first CI run, and the pin moves only by a deliberate change.

---

### M5 — The whole run API, and the CLI over it

What the primary goal did not need: launching and terminating from the CLI — small, and parallel to M4 from the moment Phase 12 lands — then the keyed claim that makes invoking a step again safe, the rest of the run API over it, and the CLI's commands for that rest.

#### Phase 15 — Launch, list and terminate Cogs from the `collab-hub` CLI
**Issue** #126, first half · **Branch** `feat/cli-launch` · **Depends on** Phases 6, 10, 12 · **Size** S

The three things a user first asks of a Cog hub, from a terminal: list the Cogs, launch one, terminate what was launched. Phase 10 built `cog launch`, `run list`, `run show`, `run watch` and `run terminate` over polling; this phase moves watching to the event stream and completes what is listed below.

*In scope*
- `collab-hub cog launch NAME [--entry NAME] [--input JSON | -] [--gate POLICY]` — a one-step Op the CLI builds itself, until Phase 23's run-one-installed-Cog form exists, submitted through `POST /v1/runs`. `NAME` is a package of the directory source until then (Phase 12), and a name the hub refuses is reported with the names it launches. `--gate` sets the step's Gate policy, since a run held at a Gate cannot be decided before Phase 17. It prints the run id, the backend and the location, and with `--watch` follows the run to its end.
- `collab-hub run list` — what was launched, with each run's Cog, status and age, filterable by status; `run show ID`; `run watch ID`, the event stream rendered as it arrives, `--json` emitting one event per line.
- `collab-hub run terminate ID` — cancels the run and waits until the Track says `cancelled`, unless `--no-wait`.
- `run watch` survives a dropped connection by replaying from the last event it saw, the same `after` the API takes.
- Exit codes beyond Phase 6's: 3 the run ended `interrupted`, 4 it is waiting at a Gate, 6 it was cancelled; 0 and 1 are completed and failed. `--json` on every command, shaped for `jq`.

*Dev and CI* — at level 1, `collab-hub cog launch echo --input '{}' --watch` runs a fake Cog end to end with no container, and `collab-hub cog launch hermes --entry ask --input - --gate never` reruns M4's demonstration from the terminal. CI: `level-1` launches `slow` by its package name, finds it in `run list`, terminates it and asserts the exit codes, all through the CLI. *Docs* — `cli/README.md` gains the launch, list and terminate commands with a worked example each and the exit-code table; `dev/README.md` shows the CLI beside `make op`.

*Acceptance*
- [ ] At level 1, `collab-hub cog launch` starts a fake Cog by its package name, `run list` shows it running and `run terminate` stops it, over the hub's endpoints only; a name the hub cannot launch is refused with the names it can.
- [ ] `run watch` survives a dropped connection without losing or repeating an event.
- [ ] Exit codes distinguish completed, failed, `interrupted`, waiting at a Gate and cancelled, and every command has `--json`.

#### Phase 16 — No repeated side effects: the keyed claim
**Issue** #102 · **Branch** `feat/cog-keyed-claim` · **Depends on** Phases 8, 11, 13 · **Size** M

A step can be invoked again after it already acted: a durable backend resuming after a crash, or someone retrying an interrupted `none` run. Neither may repeat a side effect.

*In scope*
- Every interaction carries an idempotency key, stable for one step attempt.
- A durable **keyed claim** store on the hub, with two states. A worker *reserves* the key before it acts and *commits* the envelope after; a replay of a committed key returns the envelope without acting. A replay that finds a key reserved but never committed — the worker died between the side effect and its result — must not act again: the step fails as `outcome-unknown`, and only a person, or an entry point the Cog declares idempotent, resolves it. The claim narrows the crash window to the reserve-to-commit gap and makes it visible; it does not pretend to close it.
- **Two stores, both first-class**: SQLite for level 1 and the desktop, Postgres for the hub, behind one protocol and one suite — the same shape Phase 5 gives the Track, and what keeps level 1 free of containers.
- **A worker-facing transport**, since the worker is a separate process even at level 1: `POST /v1/claims/{key}/reserve` and `/commit`, and `GET /v1/claims/{key}`, served by the API process, authorized by Phase 9's run token — which the API verifies against the hash the controller recorded, never by asking the controller — and scoped to that run's own keys, so a worker can neither read nor commit another run's. Phase 28's "claim endpoint" is this one, and Phase 13's SDK is its client.
- The claim contract documented for workers; the reference worker implements it, and the Worker SDK (Phase 13) gains the claim client, so every adapter — Hermes' included — honours it with no change to its Cog.
- The lifecycle suite gains "retry of an interrupted run does not repeat a completed side effect"; the durability suite gains "a worker replaced after reserving, before committing" (the step ends `outcome-unknown`, nothing acts twice) and "a worker replaced after committing, before the step was recorded" (the envelope is returned). A **claim conformance suite** runs reserve, commit, replay of a committed key, replay of a reserved-but-uncommitted key, and expiry, against SQLite and Postgres alike.

*Dev and CI* — at level 1 the SQLite claim store sits beside the SQLite Track in `dev/.local/`, so a fake Cog running as a child process (Phase 9) reserves and commits over HTTP with no container; the `fake-cog` compose service joins the `fakes` profile at level 2. CI: the claim conformance suite runs in `test-execution.yaml` against both stores, and `level-1` asserts a fake Cog child process reserves and commits its key over HTTP with the run token the controller minted, and is refused another run's key. Replaying a claim across a controller restart needs the run API's retry, so it is asserted in Phase 17. *Docs* — `op-cog-seam.md` gains the claim contract; `frames-operations.md` lists the claims table and how long claims are kept.

*Acceptance*
- [ ] Re-invoking a step whose side effect already happened returns the recorded envelope and does not act again, under `none` retry.
- [ ] A worker replaced between reserving a key and committing its envelope leaves the step `outcome-unknown`; nothing acts twice.
- [ ] Both claim stores pass the claim conformance suite, and a run token reaches only its own run's keys.
- [ ] The claim contract is documented and implemented by the reference worker and the Worker SDK.

#### Phase 17 — The hub run API: Frames, payloads, decisions and retry
**Issue** #103, closes it · **Branch** `feat/cog-run-api` · **Depends on** Phases 12, 14, 16 · **Size** M

The rest of the run API: a step grounded in Frames, its output read back, its Gate decided, and a run retried without repeating a side effect.

*In scope* — under `/v1`, authenticated, with protection-map entries and OpenAPI:
- **A step's Frames**, by id, on `POST /v1/runs`. At submit the API checks that each exists and that the submitter can read it — 404 or 403 naming the Frame otherwise, the "verify existence and access" of apollo-desktop#690 — and snapshots its body, stored by reference like a large payload (Phase 5), with each Frame's id and snapshot digest recorded on `op_submitted`. Frames have no revisions — their history is an event log, not versions — so the snapshot is what makes a retried or resumed step see what was submitted rather than the Frame as edited since.
- **A run index**, so listing runs costs one indexed query rather than a read of every run. Phase 12's `GET /v1/runs` reads the Track of every run on the hub, one query each, and filters by organization and status afterwards. A row per run (`run_id`, `org_id`, `status`, `submitted_at`, `updated_at`), created with `op_submitted` and updated in the same transaction as each event that moves the run, from the run machine's own transition, makes a page one query on `(org_id, status, submitted_at)`. A new migration adds the table for the Postgres Track, and the SQLite and in-memory stores keep the same index under the store conformance suite.
- `GET /v1/runs/{id}` gains, for a step waiting at a Gate, the open escalation id a decision must name.
- `GET /v1/runs/{id}/steps/{step}/payload` — the output a step event references, under the run's own authorization; errors are read from `step_failed` events.
- `POST /v1/runs/{id}/steps/{step}/decision` — approve, reject or send back with findings; authorized against the Gate's `approvers`; the actor from the auth context. The body carries the **escalation id** the reviewer is answering (Phase 4 mints one per escalation, over the step attempt and the envelope it escalated on). A decision is accepted atomically and only while that escalation is the open one: a second decision on the same escalation is idempotent if it repeats the first and a conflict otherwise, and a decision naming an escalation already closed — the revision it reviewed has been sent back and replaced — is refused as stale, naming the escalation that is open now. Two reviewers racing on one escalation cannot both win, and a late approval can never approve a revision its author never saw.
- `POST /v1/runs/{id}/retry`, which carries Phase 8's retry identity into the API: a run interrupted mid-step resumes that step under its existing attempt and idempotency key, so a committed claim answers instead of the work running twice; a step whose failure was recorded runs again as a new attempt with a new key. A step left `outcome-unknown` (Phase 16) is not retried this way at all — it is reconciled explicitly, and the endpoint refuses with a reason naming the step. The response says which of the three happened.

*Dev and CI* — `make run-decide RUN= STEP= OUTCOME=` decides a Gate; at level 3 the approver roles come from `make seed-org`. CI: `level-1` asserts a Gate decision advances a run over HTTP, and restarts a worker's controller after the worker committed its claim and asserts `POST .../retry` answers from the claim; `levels-2-3` asserts a non-approver gets 403. *Docs* — `runs.md` gains the decision, payload and retry endpoints.

*Acceptance*
- [ ] At level 1 and against the kind stack, a client submits, streams events, reads a step's payload, decides a gate and sees completion using only the run API.
- [ ] A real local worker commits its claim, the controller is killed before the Track records the step, and `POST .../retry` returns the original envelope with the work having run once — at level 1, with no Postgres, and against the kind stack.
- [ ] Two conflicting decisions on one escalation: one wins, the other is a conflict; an approval naming an escalation closed by a send-back is refused as stale.
- [ ] A member without an approver role gets 403, and a member of another organization cannot read a run's payload; every accepted decision is on the Track with its actor.
- [ ] A step naming a Frame its submitter cannot read is refused at submit, and a retried step is given the snapshot taken at submit, not the Frame as edited since.
- [ ] A Hermes step (Phase 14) given a Frame completes with the Frame in Hermes' prompt, and the Track names the Frame and its snapshot digest.

#### Phase 18 — Decide, read and retry runs from the `collab-hub` CLI
**Issue** #126, closes it · **Branch** `feat/cli-runs` · **Depends on** Phases 15, 17 · **Size** S

The rest of the run API from a terminal: the same endpoints, the same authorization, the same Track.

*In scope*
- `collab-hub run submit` — an Op from a file (`--file op.yaml`, or `-` for stdin), several steps with their Gates and Frames; `cog launch` stays the one-Cog shorthand.
- `collab-hub run payload ID STEP`, and `run retry ID`, reporting which of Phase 17's three retry outcomes applied, including a refusal to retry an `outcome-unknown` step.
- `collab-hub run decide ID STEP --approve | --reject | --send-back --finding ...`, carrying the escalation id it answers (Phase 17). A stale decision is reported as such, naming the escalation that is open now, rather than silently applying to a revision the reviewer never saw.
- Later phases hang their own commands off this client rather than building another: `collab-hub cog install|uninstall` with Phase 23, `collab-hub grant` with Phase 29.

*Dev and CI* — `collab-hub run submit --file dev/ops/needs-review.yaml`, then `run decide`, shows a Gate decided from the terminal, and `run payload` reads the result. CI: `level-1` submits a fake Op, watches it to completion and decides an escalated Gate, all through the CLI. *Docs* — `cli/README.md` gains these commands with a worked example each; `make op`, `make run-events` and `make run-decide` become thin wrappers over the CLI, so the dev environment has one client; `docs/cog-execution/runs.md` notes that every endpoint it documents has a CLI command.

*Acceptance*
- [ ] At level 1, an Op submitted with `collab-hub run submit` completes and `run payload` prints the step's payload.
- [ ] A Gate escalation is decided from the CLI and recorded on the Track with its escalation id and actor; a stale decision is refused with a message naming the open escalation.
- [ ] Every command has `--json`, and the schema is documented.

---

### M6 — Collab launches hub runs

Needs only the run API, Phases 12 and 17, so it lands before the worker becomes a pod: from here on Collab is the demonstration surface, with the CLI as its scripted twin, and every later milestone is shown from it.

#### Phase 19 — Desktop run client and placement
**Issue** apollo-desktop#825 · **Branch** `feat/hub-run-client-690` · **Depends on** Phases 12, 17 · **Size** M

*In scope* — the desktop's loopback proxy carries the run routes, so the bearer is stamped there and the webview never holds it; a run target with a hub implementation, the local one stubbed for Phase 31; placement chosen through the desktop's existing local-or-remote placement model, not a new selector; bindings for submit, list, observe (the event stream relayed to the frontend), read a step's payload, cancel and retry.

*Dev and CI* — develop against `make api-desktop` plus `make controller`: the Hub address stays `http://localhost:9080`, the front door already forwards `/v1/runs`, and fake Cogs make runs start instantly; the desktop's own CI covers the proxy's denied paths. *Docs* — the desktop's contributor docs; `dev/README.md`'s desktop section lists the run routes the front door carries.

*Acceptance*
- [ ] From Collab, a user submits a run on the hub and watches its events live.
- [ ] Only the declared run routes pass the proxy, with denied-path tests.

#### Phase 20 — Desktop run view and Gate decisions
**Issue** apollo-desktop#719 · **Branch** `feat/run-view-gate-decisions-719` · **Depends on** Phases 4, 19 · **Size** M

*In scope* — a run list and a run view rendering Track events and step payloads; a pending decision surface for escalated gates showing the envelope's `problems` and, once Phase 27 lands, Guard findings; approve, reject and send back with findings; cancel and retry; an `interrupted` run shown as such, with retry offered, and the backend and location a run runs under visible; once decision 20 settles, a step's progress while it runs.

*Dev and CI* — `make op OP=needs-review` leaves a pending decision to act on in Collab, and `make seed-org SUB=$(make -s sub)` makes the signed-in `dev` user an approver. *Docs* — the desktop's docs; `dev/README.md` gains the recipe above.

*Acceptance*
- [ ] An operator approves, rejects or sends back a paused step from Collab, and the run advances, stops or re-runs.
- [ ] Every decision shown in Collab — who, when, outcome, findings — is read back from the Track, not from local state.

---

### M7 — `remote`: workers in pods, still on `none`

The same runner, backend and Cog; the worker becomes a pod. Then the rest of the lifecycle a production hub needs: the artifact, install, the Cog's own `resolve`, budgets.

#### Phase 21 — The `remote` location: workers in pods, and least-privilege RBAC
**Issue** #6 · **Branch** `feat/cog-remote-location-6` · **Depends on** Phases 9, 11 · **Size** M

Same runner, same `none`, same Hermes Cog — the worker becomes a pod. The only identity able to create workloads is the controller, never the public API.

*In scope*
- `location: remote` constructs #35's `KubernetesCogExecutor` (a per-run Deployment + Service + ingress-only NetworkPolicy). Until Phase 22 the pod runs the baked runner image, as #35's E2E does.
- It passes Phase 9's location conformance suite, whose controller-death case is written per location rather than one rule for both — the way the durability suite already differs per backend. `local`: the worker dies with its controller, because Phase 9's launcher kills it when the controller's pipe closes. `remote`: the pod survives the controller, and the next controller start reaps it; the suite asserts the reap, and that no run advances and no claim is acted on in between. Neither location leaves a worker serving an orphaned run indefinitely, and orphan cleanup and claim reconciliation both precede any retry that could act again.
- Chart: the controller's Deployment, a ServiceAccount and a namespace-scoped Role and RoleBinding for the controller only, over the kinds actually materialized and the verbs used; `values.yaml` and `values.schema.json` change together; the API pod holds no workload permissions; `automountServiceAccountToken: false` on workers.
- The run token and the binding reach the pod the way they reach a child process — environment and a mounted file — so the SDK does not know its location.
- That reap is the `remote` half of Phase 11's start-up sweep; a `local` worker seldom needs it, the launcher having killed it, and the pid record covers a survivor.

*Dev and CI* — `make kind-up` deploys the controller from the chart with `location: remote`, and `make op` on kind materializes a worker pod. CI: `level-4-render` asserts the controller Deployment renders, the Role is limited to materialized kinds and the API ServiceAccount has no workload verbs; `test-execution-e2e.yaml` runs the location suite against kind. *Docs* — `runs.md`'s location table gains `remote`; `docs/standalone-deployment.md` *Namespace ownership* states the controller's Role; the chart values are described.

*Acceptance*
- [ ] On kind with default RBAC, the controller materializes a worker pod, and `kubectl auth can-i` shows the API ServiceAccount cannot create one.
- [ ] The grant is namespace-scoped and limited to materialized kinds, and workers carry no ServiceAccount token.
- [ ] The fake Cog runs in a pod through `location: remote` with no change to the Op that ran it as a child process.
- [ ] `remote` passes the location conformance suite, including its own controller-death expectation: the pod is reaped on the next controller start, and nothing advances or acts on a claim in between.

#### Phase 22 — Workers run the Cog's own `serve`, from its artifact
**Issue** #105 · **Branch** `feat/cog-materialize-serve` · **Depends on** Phases 14, 21, and the catalog (#84, #85), which has landed · **Size** L

The materialize half of install-versus-materialize: a worker is the Cog package, not a shared image. `local` already runs the package from a directory (Phase 9); this gives `remote` the same package, pulled from its artifact.

*In scope*
- An init container runs `nebi pull <reference>` into an `emptyDir`; the main container runs the package's declared `serve` task in its own pixi environment. The hub supplies no harness, model client or runtime.
- The executor takes a pinned `<host>/<repo>@<digest>` reference; until Phase 23, the controller is handed one directly.
- An environment cache keyed by the `pixi.lock` digest, so a second run does not re-solve. Part of the phase, not a later optimization.
- The chart stops using the baked runner image; tests keep it.

*Dev and CI* — an optional `registry` compose profile with `make cog-publish COG=` for a fake Cog bundle and for `cogs/hermes`, and a `publish-cogs.yaml` workflow that publishes `cogs/*` with Nebi to the registry the catalog indexes, on a tag; level 1 keeps Phase 9's directory source, and CI enumerates the `registry` profile through #83's `static` source instead of the catalog. CI: `level-4-render` asserts the worker's init container pulls a digest; `test-execution-e2e.yaml` materializes a published fake Cog by digest. *Docs* — `op-cog-seam.md` describes materialization as implemented; the chart values for the cache and the pull.

*Acceptance*
- [ ] A published fake Cog, referenced by digest, materializes a worker running its own `serve`.
- [ ] Context comes from the artifact at the pinned digest; nothing is baked into a shared image.
- [ ] A second run on the same `pixi.lock` does not solve the environment again.
- [ ] The Hermes Cog from Phase 14, published to the registry, completes an Op step in a pod with no change to the Cog.

*Risk* — solving a pixi environment at pod start is slow; the cache is what makes this usable.

#### Phase 23 — Install and uninstall a Cog by digest
**Issue** #106 · **Branch** `feat/cog-install-by-digest` · **Depends on** Phases 12, 22 · **Size** M

Pinning a Cog and proving it works happens once, not on every run.

*In scope*
- `POST /v1/cogs/installs` pins `<host>/<repo>@<digest>` from the catalog and records the card. An install moves through **fetched → bound → invokable**, in that order, because a Cog's `check` may probe what its binding gives it — a context Cog's `check` that asks its model for health cannot pass before resolution has chosen that model. So: fetch and provision the environment; resolve against the hub's inventory and admit the binding (Phase 24; until then the controller's `models:` stopgap supplies it); *then* run `check` through Phase 22's materialization with that binding delivered. A failing check leaves the install `bound`, not invokable, and says which step failed. `GET` shows the state, so a Cog stuck before `invokable` is visible rather than silently absent.
- `GET /v1/cogs/installs`; `DELETE /v1/cogs/installs/{id}` removes the install and every runtime resource it created — Phase 25 extends that to its warm pool.
- `POST /v1/runs` gains the "run one installed Cog once" form: a bundled op on an installed digest. A run on a digest that is not installed is refused.
- `collab-hub cog install|uninstall` join the CLI's `cog` group (Phase 6), so an install is reachable from a terminal as well as from Collab.

*Dev and CI* — `make cog-install COG=` installs from the `registry` profile at level 2, or from Phase 9's directory source at level 1; `test-execution-e2e.yaml` installs a published fake Cog and runs it once. *Docs* — the protection map gains `/v1/cogs/**`; `runs.md` gains installing, uninstalling and running a Cog once.

*Acceptance*
- [ ] Installing a context Cog makes it invokable with no hand-authored per-Cog deployment.
- [ ] A Cog whose `check` fails stays `bound`, is not invokable, and says which step failed.
- [ ] A context Cog with no prior binding and no default model server installs from the host's model configuration alone: it is bound before its `check` runs, and the `check` sees that binding.
- [ ] Uninstall removes every runtime resource the install created.

#### Phase 24 — Model binding: inventory, the Cog's `resolve`, delivery
**Issue** #3 · **Branch** `feat/cog-model-binding-3` · **Depends on** Phase 23 · **Size** M

The hub offers, the Cog selects, the hub records and delivers — so the hub does not grow a parallel resolver (#3 thread). Since Phase 11 the controller has written the binding file from the hub's `models:` block, and Phase 13's SDK has read it; this phase retires that stopgap: the Cog's `resolve` becomes the producer, and the file the worker reads does not change.

*In scope*
- A satisfier **inventory** generated from the hub's model configuration: one descriptor model Cog per served model, carrying endpoint, model id, transport, locality, `auth_ref`, and its context window and maximum output tokens, which the binding passes on so a harness sizes its turns from them.
- Resolution invokes the installed Cog's `resolve` lifecycle entry point with that inventory, and the hub records the **binding record**. `DeclaredCapabilityResolver` becomes a test double.
- The binding is delivered to the worker as a mounted file plus environment; secrets arrive by reference. A check refuses literal secrets in manifests and bindings.
- The binding id appears on every step event and in the envelope's `binding`.
- A plug point for #10: the inventory is filtered before `resolve` sees it. No filter is implemented here.
- The install sequence Phase 23 defines does not change: fetch and provision, bind, then `check`. This phase replaces what supplies the binding at the bind step — the Cog's own `resolve` over the inventory, instead of the controller's `models:` stopgap — so a Cog's `check` still runs against a real binding.
- Resolution covers `requires: harness` as it covers models: the inventory lists the installed harness Cogs, the Cog's `resolve` selects one, and the binding names it — which is when a context Cog first resolves to the Hermes Cog of Phase 14.

*Dev and CI* — Phase 13's stdlib fake model joins the `fakes` compose profile as `fake-model`, and the dev model configuration generates the inventory from it; `test-execution-e2e.yaml` asserts a worker's connection comes only from its delivered binding. *Docs* — `op-cog-seam.md` gains binding delivery; `values-example.yaml` shows the model configuration; `docs/cog-execution/sensitivity.md` names the inventory filter as #10's plug point.

*Acceptance*
- [ ] A worker is configured from its delivered binding alone: no model-connection value is baked into the Cog package, the image or the Deployment spec.
- [ ] Re-pointing the bound model Cog changes the next run's endpoint with no other change.
- [ ] Credentials appear only by reference in manifests, bindings and the Track.

#### Phase 25 — Budgets enforced, warm workers, idle teardown
**Issue** #4 · **Branch** `feat/cog-budgets-warm-workers-4` · **Depends on** Phases 8, 24 · **Size** M

*In scope*
- A terminal status per dimension: `budget_exceeded` with `dimension: duration | tokens | cost`.
- **Duration** is a hard pre-check at step boundaries plus an interaction deadline that cancels the in-flight worker through the executor.
- **Tokens and cost** stay post-interaction accounting, documented as such; the hard per-request cap is the binding's gateway `max_tokens`.
- A budget bounds the run from outside. It is not per-turn context management — headroom within a turn, overflow recovery and what enters each turn are the harness's (decision 19) — and `runs.md` says so, so no one reads a budget as one.
- **Warm mode**: an `idle_timeout` per step or per Cog; a warm pool keyed by `(digest, binding id)`; `IDLE` observable in the lifecycle before teardown. One-shot runs release immediately; uninstall drains the pool.
- `retry()` after a budget stop opens a new budget epoch, so the reconstructed budget does not trip again at once (#4 thread).
- All of this lives in the runner's step functions, so it behaves identically under every backend.

*Dev and CI* — the `slow` and `spender` fake Cogs exercise each dimension, and `make op OP=warm` shows a warm worker idling down; lifecycle tests run in `test-execution.yaml`. *Docs* — `runs.md` gains budgets; the pool bounds (decision 4) are described as chart values.

*Acceptance*
- [ ] Each budget dimension ends a run with a status naming the dimension, and nothing hangs.
- [ ] One-shot releases its worker; warm stays up and idles down on its policy; both covered by lifecycle tests.

---

### M8 — Durable runs

The second durability backend, brought to a full implementation once `none` runs a worker at both locations — `temporal` (M11) does not start until this one is done.

#### Phase 26 — The `dbos` backend
**Issue** #104 · **Branch** `feat/cog-durability-dbos` · **Depends on** Phases 8, 11, 16 · **Size** L

The first durability engine behind the runner — what makes #2's "a restart does not lose the run" true.

*In scope*
- The runner's step functions run as the steps of one DBOS workflow per run; a Gate's human decision is a durable wait on a message, sent by Phase 11's Track watcher when `gate_decided` lands, so the API still only writes the Track; cancellation uses DBOS cancellation, raised the same way.
- The DBOS system database: Postgres on the hub, on the same instance as the Track; SQLite for the desktop and for level 1.
- The chart provisions DBOS's databases, or documents granting `CREATEDB` — DBOS otherwise tries to create them at startup and fails.
- Ownership across replicas from DBOS queues and a unique executor id per replica, taking over from Phase 11's pickup once a run is picked up: the controller that picks a run up enqueues it as a DBOS workflow, and from then on the queue decides who resumes it. The API still only writes `op_submitted`. No hand-built lease.
- Step re-execution after a crash is made safe by Phase 16's keyed claim.
- Passes the lifecycle suite and the durability suite unchanged — and passes the durability suite on a SQLite system database over the SQLite Track too, so a local run can be made durable without Postgres.

*Dev and CI* — `make controller BACKEND=dbos` at level 1 over the SQLite Track, and at level 2 on the existing Postgres — no new image; `dev/sql/bootstrap.sql` creates DBOS's database so the dev role needs no extra privilege. CI: `levels-2-3` kills the controller mid-step and asserts the run resumes, including one waiting at a Gate. *Docs* — `runs.md` gains `dbos`; `frames-operations.md` adds DBOS's databases and that nothing may read run status from them; the chart's production values select `dbos` (decision 2), the default stays `none`.

*Acceptance*
- [ ] Killing the controller mid-step and restarting it completes the run with no caller resubmitting.
- [ ] A run paused at a Gate resumes at that Gate after a restart.
- [ ] Replacing a worker after a completed side effect does not repeat it.
- [ ] `backend: dbos` is a configuration change only, and status still derives from the Track.
- [ ] The durability suite passes on SQLite as well as Postgres.

*Risks* — an executor id shared across pods makes recovery claim the wrong workflows. DBOS keeps workflow status in its system database; nothing may read it for run status. DBOS runs `CREATE DATABASE` when its databases are missing, so a least-privilege role fails at startup unless they are pre-created. Changing a workflow that has runs in flight needs versioning before rollout.

---

### M9 — The trust boundary

#### Phase 27 — Guards declared on Op steps
**Issue** #9 · **Branch** `feat/cog-guards-9` · **Depends on** Phases 4, 5, 8 · **Size** M

*In scope*
- A `Guard` protocol: `(envelope, step context) -> findings`. Guards produce findings; they never decide.
- Built-in Guards: **Schema** (payload against the card's output schema), **Source-grounding** (quoted spans present in the step's sources, and — once Phase 17 snapshots a step's Frames and records them on the Track — in those snapshots too; until then a step has no Frames to check), **Policy** (declarative organization rules plus the card's `prohibits`).
- An Op declares `guards` per step. Findings are recorded as `guard_evaluated` events and fed to the step's Gate. Guards run as a runner step function, so they behave the same under every backend.
- A Cog's own `problems` are input to Guards, never their verdict.
- A plug point for #13's downgrade verification.

*Dev and CI* — fake Cogs gain `ungrounded` and `prohibited`; `level-1` asserts each escalates its Gate with findings on the Track. *Docs* — `track.md` gains `guard_evaluated`; the glossary's Guard entry names the built-in Guards; `runs.md` gains declaring Guards and Gates, with an example.

*Acceptance*
- [ ] Source-grounding and Policy Guards run on a step's output, their findings are on the Track, and they can escalate the step through its Gate.
- [ ] An Op declares the Guards it runs, and a Guard failure is never silently retried.

#### Phase 28 — Worker egress restriction and authenticated `/invoke`
**Issue** #11 · **Branch** `feat/cog-worker-egress-11` · **Depends on** Phases 13, 22, 24 · **Size** L

Not in the original list, but #8 cannot be built safely without it, and it is what turns ADR-0001 invariant 7 from advice into enforcement. #35 already did ingress.

*In scope*
- A per-worker egress NetworkPolicy: DNS plus hub-mediated endpoints only, each an in-cluster Service the policy can select — a NetworkPolicy cannot name an arbitrary hostname, and resolved addresses change. So model traffic goes through a **model egress gateway** on the hub, a stable Service that forwards to the endpoints in `models:`; a `remote` worker's binding (Phase 24) names the gateway, a `local` worker's the endpoint itself. The connector proxy and the claim endpoint are hub Services already.
- A `remote` worker always runs in the hub's own cluster, so it reaches the gateway by its cluster-internal Service name — no ingress, no public route, no TLS termination to leave the cluster and come back. For the same reason a model that is itself served in that cluster, as the llm-serving-pack's endpoint is, needs no gateway at all: the egress policy selects its Service directly and the binding names it. The gateway stays in the path only for endpoints outside the cluster, which a policy cannot name.
- `/invoke` authentication with Phase 9's run token: the controller already presents it on every `/invoke`, and the SDK's authentication hook (Phase 13) starts verifying it against the value the worker was materialized with. No second token is minted.
- A denied egress attempt recorded as a Track boundary event, reported through the executor (invariant 6).
- `local` workers are out of scope: they share the controller's network identity, which ADR-0002 D12 records as the reason the trust boundary is `remote`'s.

*Dev and CI* — `make kind-up KIND_CNI=calico` installs a policy-enforcing CNI, since kind's default does not enforce NetworkPolicy; `level-4-render` asserts the egress policy renders; `test-execution-e2e.yaml` asserts a denied connection is recorded on the Track. *Docs* — `docs/standalone-deployment.md` gains *Worker network policy*; `op-cog-seam.md` and `worker/README.md` gain `/invoke` authentication.

*Acceptance*
- [ ] A `remote` worker cannot reach an undeclared destination — an external model host included — and the attempt is observable.
- [ ] `/invoke` rejects a request without a valid run token, even from inside the namespace.

*Risk* — NetworkPolicy is only enforced by some CNIs; the chart documents the requirement and the E2E runs on one that enforces it.

#### Phase 29 — Unattended connector access: grants and the connector proxy
**Issue** #8 · **Branch** `feat/cog-connector-grants-8` · **Depends on** Phase 28 · **Size** L

The hub's brokered connectors act as the user; the Cog never holds a credential (#8 thread).

*In scope*
- A standing, revocable **grant** per `(user, connector, scopes)`, backed by a Keycloak offline token (`offline_access`) that the hub stores encrypted (decision 5). The hub runs the consent flow through its confidential client, so the offline token never passes through the desktop.
- `POST /v1/grants`, `GET /v1/grants`, `DELETE /v1/grants/{id}` for the user's own grants, and `collab-hub grant list|add|revoke` over them, in this phase, added to the CLI that Phase 6 starts.
- A run-scoped connector proxy on the hub (MCP) that a worker calls with its run token. The hub exchanges the grant for the user's brokered provider token server-side, through the existing broker path.
- Revocation deletes the grant and revokes the offline session, effective on the next run. Every use is recorded on the Track — grant id, connector, scope — with no token material.
- Unattended runs are the case durability exists for; scheduled runs default to a durable backend where one is configured.

*Dev and CI* — at level 3, `make grant CONNECTOR=` creates a grant from the `dev` user's offline token, and the connector proxy reaches the existing fake Google, Slack and GitHub providers; `levels-2-3` asserts a run with no bearer in flight reads a fake connector, and that revoking the grant fails the next run. *Docs* — `docs/auth-flow.md` gains the grant flow; each connector page gains *Unattended runs*; the protection map gains `/v1/grants/**` and the proxy; `track.md` gains grant-use events; `frames-operations.md` gains the grants table and a revocation runbook.

*Acceptance*
- [ ] A scheduled run with no request in flight reads a user's connector as that user.
- [ ] No user credential is present in the worker — its pod spec, environment or filesystem.
- [ ] Revoking the grant makes the next run's connector call fail with a clear reason; grants and their uses are auditable.

#### Phase 30 — Grants in Collab
**Issue** apollo-desktop#826 · **Branch** `feat/connector-grants` · **Depends on** Phases 19, 29 · **Size** S

*In scope* — the loopback proxy carries the grant routes beside the run routes; a connectors screen listing the user's grants with their scopes; creating a grant starts Phase 29's hub-side consent flow in the system browser; revoking one; a run that failed on a revoked grant links to that screen.

*Dev and CI* — develop against `make api-desktop-fakes`, whose fake providers give a grant something to act on; the desktop's CI covers the grant routes' denied paths. *Docs* — the desktop's docs.

*Acceptance*
- [ ] A user grants and revokes connector access from Collab, and revocation takes effect on the next run.
- [ ] Neither the webview nor the desktop process ever holds the offline token.

---

### M10 — Local execution on the desktop

The desktop embeds what M3 built: `local` is its executor, and Phase 26's `dbos` on SQLite its durable option. Nothing here is a second implementation.

#### Phase 31 — Local run host in the desktop, and its local run target
**Issue** apollo-desktop#827 · **Branch** `feat/local-run-host` · **Depends on** Phases 9, 19, 26 · **Size** M

*In scope*
- `collab-hub-execution` embedded in the desktop's local Python host, serving **the same run API routes as Phases 12 and 17**, and Phase 16's claim transport for its own workers, on its bearer-gated loopback surface, backed by Phase 9's `local` executor — `none` by default, Phase 26's `dbos` on SQLite selectable.
- Hermes runs locally through the same harness Cog, with the desktop's existing Docker terminal sandbox rather than in-process tools.
- A local Cog run stays separate from the desktop's chat, though both drive Hermes over ACP. A chat is a conversation on Ravnar, steered turn by turn, with no Track; a Cog run is a step of an Op, through the Worker SDK, with an envelope, a Track and Gates. They share the Hermes pin, its headless configuration and its ACP failure handling, not the loop that drives it; a chat that wants a Cog run starts one through this run API (decision 19).
- Phase 19's local run target implemented; a run bound to local-only resources never reaches the hub.
- Local Tracks stay local. Linux and macOS only, as `local` is (decision 15).

*Dev and CI* — the desktop's local host is the same composition as this repository's `make controller LOCATION=local`, so a scenario reproduced in one reproduces in the other. *Docs* — the desktop's docs; `runs.md` notes that two hosts serve one API and where their behaviour differs.

*Acceptance*
- [ ] The same Op runs locally and on the hub from Collab, with the same events and gate flow.
- [ ] A local-only run is refused by the hub target and served by the local one.
- [ ] Quitting Collab mid-run leaves a `none` run `interrupted` and a `dbos` run resumable on the next start.

---

### M11 — The third durability backend

#### Phase 32 — The `temporal` backend
**Issue** #110 · **Branch** `feat/cog-durability-temporal` · **Depends on** Phases 8, 11, 16 · **Size** L

*In scope* — one workflow per run; the runner's step functions — Cog interaction, executor calls and Track appends — as **activities**, so workflow code stays deterministic; signals for gate decisions and cancellation, raised by Phase 11's Track watcher; ownership across replicas from task queues, which Phase 11's pickup hands the run to as it does for `dbos`; Phase 16's keyed claim for activity re-execution; Temporal's persistence on the same Postgres instance as the Track; configurable TLS to the frontend.

*Dev and CI* — an optional `temporal` compose profile runs the Temporal CLI's `temporal server start-dev` — one process, not a cluster image — and `make controller BACKEND=temporal` uses it at level 2; `test-execution.yaml` runs both conformance suites against it; `dev-env.yaml` does not, keeping its cost rule. *Docs* — `runs.md` gains `temporal`; `frames-operations.md` adds Temporal's persistence; the chart values are described.

*Acceptance*
- [ ] The lifecycle suite and the durability suite pass unchanged.
- [ ] `backend: temporal` is a configuration change only, and status derives from the Track, never from Temporal history.

*Risks* — schema setup needs `CREATEDB` or a pre-created database. Workflow changes need Temporal's patching API. Temporal history and the Track must not diverge, so the Track-append activity is idempotent on event id.

## 7. Sequencing

```mermaid
flowchart TD
  ph1["1 · Python 3.13"]
  ph0["0 · ADR-0002"] --> ph2["2 · envelope"] --> ph3["3 · state machines"] --> ph4["4 · Gates"] --> ph5["5 · Track"] --> ph7["7 · runner"] --> ph8["8 · none"]
  ph6["6 · CLI sign-in, Cog list"]
  ph3 --> ph5 & ph7
  ph8 --> ph9["9 · local"]
  ph6 & ph9 --> ph10["10 · example: Hermes from the CLI, Toad"]
  ph8 & ph9 & ph10 --> ph11["11 · controller"] --> ph12["12 · launch, terminate"]
  ph2 & ph9 --> ph13["13 · Worker SDK"]
  ph9 & ph12 & ph13 --> ph14["14 · Hermes, local"]
  ph6 & ph10 & ph12 --> ph15["15 · CLI launch, terminate"]
  ph10 --> ph12
  ph8 & ph11 & ph13 --> ph16["16 · claim"]
  ph12 & ph14 & ph16 --> ph17["17 · Frames, decide, payload, retry"]
  ph15 & ph17 --> ph18["18 · CLI runs"]
  ph12 & ph17 --> ph19["19 · desktop client"]
  ph4 & ph19 --> ph20["20 · run view"]
  ph9 & ph11 --> ph21["21 · remote"]
  ph14 & ph21 --> ph22["22 · artifact"]
  ph12 & ph22 --> ph23["23 · install"] --> ph24["24 · binding"]
  ph8 & ph24 --> ph25["25 · budgets"]
  ph8 & ph11 & ph16 --> ph26["26 · dbos"]
  ph4 & ph5 & ph8 --> ph27["27 · Guards"]
  ph13 & ph22 & ph24 --> ph28["28 · egress"] --> ph29["29 · grants"]
  ph19 & ph29 --> ph30["30 · grants in Collab"]
  ph9 & ph19 & ph26 --> ph31["31 · local run host"]
  ph8 & ph11 & ph16 --> ph32["32 · temporal"]

  classDef m0 fill:#ffffff,stroke:#4a4a6a,color:#1a1a2e
  classDef m1 fill:#e8faf8,stroke:#20aaa1,color:#0d5d57
  classDef m2 fill:#e6f7ff,stroke:#1f7a99,color:#0b3d4d
  classDef m3 fill:#eeeef3,stroke:#4a4a6a,color:#1a1a2e
  classDef m4 fill:#dbeefc,stroke:#1f5f99,color:#0b2e4d
  classDef m5 fill:#fff4c2,stroke:#c9a227,color:#5c4a00
  classDef m6 fill:#f3e8fc,stroke:#c840e9,color:#6b21a8
  classDef m7 fill:#fef0db,stroke:#e8952c,color:#7c4a03
  classDef m8 fill:#dff3e4,stroke:#2e8b57,color:#14532d
  classDef m9 fill:#fde8e8,stroke:#c0392b,color:#7b1d14
  classDef m10 fill:#e8f0fe,stroke:#3b6fd8,color:#1c3d7a
  classDef m11 fill:#f1f1f1,stroke:#777,color:#333
  class ph0,ph1 m0
  class ph2,ph3,ph4,ph5 m1
  class ph6 m2
  class ph7,ph8,ph9,ph10,ph11 m3
  class ph12,ph13,ph14 m4
  class ph15,ph16,ph17,ph18 m5
  class ph19,ph20 m6
  class ph21,ph22,ph23,ph24,ph25 m7
  class ph26 m8
  class ph27,ph28,ph29,ph30 m9
  class ph31 m10
  class ph32 m11
```

| Milestone | Phases | What becomes demonstrable |
|---|---|---|
| **M0** Decisions and the Python floor | 0–1 | *Done.* The hub's suite green on Python 3.13 and 3.14; ADR-0002 merged |
| **M1** The seam and the states, correct in the engine | 2–5 | Every state in §11 held by one machine per level, and an Op paused by its step's Gate, not by its Cog, with a Track that names the binding and the approver |
| **M2** The `collab-hub` CLI, signed in | 6 | Sign in from a terminal the way the desktop does — or as dev auth at level 1, said so — and list the Cogs the hub's catalog offers |
| **M3** The runner on `none`, a worker as a local process | 7–11 | On one machine, `examples/cog-local` signs in, launches `hello`, talks to it from Toad and stops it, through the API and the controller (Phase 10); at level 1, no container, the controller runs a fake Cog as a child process, picks runs up from the Track — of two controllers, one per run — and its in-flight runs end `interrupted` when it is killed and starts again |
| **M4** Hermes, launched by the hub | 12–14 | `POST /v1/runs` on a laptop starts Hermes as a separate process, and the Op step completes with a valid envelope — the primary goal, straight after the runner, with no keyed claim or CLI on its path. Phase 10 shows it first, in the example: Hermes launched from the CLI and talked to from Toad, on a worker of its own that Phase 14 moves onto the SDK |
| **M5** The whole run API, and the CLI over it | 15–18 | `collab-hub cog launch hermes` starts it from a terminal, `run list` shows it and `run terminate` stops it; then retries that never repeat a side effect, Frames in the prompt, and a Gate decided and a payload read from the CLI |
| **M6** Collab launches hub runs | 19, 20 | The same run launched, watched and, at a Gate, approved from the desktop |
| **M7** `remote`: workers in pods, still on `none` | 21–25 | The same Op and the same Hermes Cog on kind, the worker a pod pulled from its artifact, bound by the Cog's own `resolve`, under budgets |
| **M8** Durable runs | 26 | Kill the controller mid-step, on `dbos`: the run resumes, including one waiting at a Gate — on Postgres and on SQLite |
| **M9** The trust boundary | 27–30 | Guards on output, egress-restricted pods, a scheduled run acting as the user, granted from Collab |
| **M10** Local execution on the desktop | 31 | The same Op run on the user's machine from Collab, on `none` or durably on `dbos` over SQLite |
| **M11** The third backend | 32 | The same suites green on `temporal` |

**Parallel lanes.** M1, M3 and M4 are the critical path to the primary goal and are built in order: nothing off that path is waited for. Phase 3 comes before anything that moves a run, a worker or an install, because its machines are where every later phase's transitions already live. M2, the CLI's sign-in (Phase 6), depends on nothing in this plan and blocks nothing, and M5's first phase — launching and terminating from the CLI (Phase 15) — can land as soon as Phase 12 does, beside the SDK and Hermes. Once Phase 17 lands, Collab's lane (M6) is independent of the rest, and so are M7 (Phases 21–25) and M8 (Phase 26). That independence is about milestones that build features on `none` and `local`, not about the two axes underneath them, whose order is fixed regardless of what runs alongside: `local` first, `remote` next; `none` first, `dbos` brought to a full implementation next, `temporal` last. Phase 27 can start after Phase 8, whose runner its Guards run in and whose fake Cogs its CI uses. M9 needs M7, since its egress policy is per pod. M10 needs Phase 26. Phase 32 can start after Phase 11 but is sequenced last: two backends behind one seam already prove it is swappable, and Temporal is the one that brings new infrastructure.

## 8. Coverage

| Requirement | Phases |
|---|---|
| **The hub launches Hermes locally, as a separate process** | 9, 10, 13, 14 |
| **The same abstraction launches workers in pods — agent location `local` / `remote`** | 9, 21; §4 |
| **Harness-agnostic: Hermes first, pi and OpenCode next, any harness supported** | 13, 14; §4 |
| **Every Cog execution feature reachable from a CLI, over the same REST API** | 6, 15, 18, and each later phase's own commands |
| **An example that launches a Cog from the CLI on a local dev hub, as soon as a worker can be a local process** | 10 |
| **The CLI alongside: sign in, list the hub's Cogs, launch one and terminate it as soon as the run API can, without delaying Hermes** | 6, 10, 12, 15 |
| **Location priority: `local` first, `remote` next, both on `none` before any durable backend** | 9, 21, 26 |
| **The states of the Cog, a worker, a step attempt and a run, each held by one tested machine on the state pattern, early** | 3; §11 |
| **Per-turn context, Frames and progress named once for every harness, not rediscovered per harness** | 11, 13, 14, 17, 25; decisions 20, 21 |
| **Backend priority: `none` first, `dbos` next to a full implementation, `temporal` last** | 8, 26, 32 |
| A lifecycle component without a durability engine (`none`) or with one (`dbos`, `temporal`) | 7, 8, 26, 32 |
| apollo-desktop#690 — state a Cog for execution, run it, read output and errors, stop it | 10, 12, 15, 17, 18, 19, 22, 23 |
| · read a Cog, set up its environment | 9, 22, 23 (catalog from #85) |
| · find the model | 24 (Phase 12's stopgap until then) |
| · find Frames, data and other elements; verify existence and access | 13, 17, 24, 27, 29 |
| · execute the Cog | 7, 8, 9, 10, 11, 13, 14, 21 |
| · capture and relay output | 2, 12, 15, 17, 18, 19 |
| · log start, stop and errors | 5 |
| apollo-desktop#719 — approve, reject or send back from the desktop | 4, 17, 20 |
| #2 — durable multi-step Ops on a swappable engine | 4, 7, 8, 26, 32 |
| #1 — worker lifecycle; no repeated side effects; permission isolation | 3, 7, 8, 9, 11, 16, 21, 22, 23 |
| #3 — model binding into the worker's connection config | 24 |
| #4 — time and cost limits, idle workers | 25 |
| #5 — durable, replayable Track | 2, 5, 12, 17 |
| #6 — least-privilege workload permissions | 21 |
| #7 — Cogs from a registry | #81–#85 and #87 landed, #86 (webhooks) open; consumed by 22, 23 |
| #8 — a Cog acting as the user unattended | 28, 29, 30 |
| #9 — Guards beyond schema | 27 |
| Local execution, launched by Collab | 9, 26, 31 |
| The API on Python 3.13 | 1 (done) |
| Every phase runnable from `dev/` and asserted in CI at its level | §5, and each phase's *Dev and CI* |
| Every phase documented in the same PR | §5, and each phase's *Docs* |

## 9. Not in this plan

- **Sensitivity (#10, #12, #13).** Blocked on the rating scale, per `docs/cog-execution/sensitivity.md`. The plug points are placed: the inventory filter (Phase 24), the reserved Track `label` field (Phase 5), and downgrade verification as a Guard (Phase 27).
- **The registry and catalog (#81–#87).** All have landed except #86, registry webhooks, which stays outside this plan; Phases 22 and 23 consume the catalog.
- **The pack docs site.** A GA requirement; the pack is `beta`. §5 keeps every page this plan writes ready for it; decision 9 says when to start.
- **Syncing local Tracks to the hub.** Decision 7.
- **Scheduling runs.** Nothing here submits a run on a timer; an unattended run (Phase 29) is one a user submitted and left. A scheduler is a client of the run API — the CLI in a cron job is the first one — and gets its own issue when someone needs it.
- **Authoring Cogs** — ADR-0001 D7.
- **Metering beyond the envelope's `usage`** — ADR-0001's deferred list.

## 10. Decisions

Opened by ADR-0002. Settled ones say so, with the date; the rest are due by the phase that needs them — 4 by Phase 25, 5 by Phase 29, 20 by Phase 20, 7 by Phase 31, 9 once M5 lands, 8 at the next Python release.

1. **Default approvers** when a Gate declares none — *decided 2026-09-16:* organization owners and platform operators, the two roles `seed-org` already grants (Phases 4, 17).
2. **The hub's production durability backend** — *decided 2026-09-16:* `dbos` — it reuses the Track's Postgres and adds no service; the chart's production values select it once Phase 26 lands. `none` stays the default for development, tests and the desktop.
3. **Gated Ops on `none`** — *decided 2026-09-16: allowed.* A run waiting at a Gate cannot survive a restart there, so the submission response names the backend (Phase 12) and a client never assumes the pause survives.
4. **Warm pool bounds** — the pool cap and the default idle timeout.
5. **Encryption at rest for grants' offline tokens** — Kubernetes Secrets with envelope encryption, or a KMS.
6. **Where the Worker SDK lives** — *decided 2026-09-16 with decision 12:* this repository, under `worker/`.
7. **Whether local Tracks ever sync to the hub** — out of scope here; confirm.
8. **The Python the hub image and `api/.python-version` run.** Phase 1 makes 3.13 supported and tested; both stay on 3.14. Proposal: keep 3.14 unless a deployment target ships only 3.13.
9. **When the pack docs site starts.** Proposal: as its own issue once M5 lands, when the whole run API is there for users to read about.
10. **Where this plan lives** — *decided 2026-09-17: here*, at the root of this repository, reviewed through pull requests; the desktop phases are listed with their issues in the desktop repository.
11. **`local` in production** — *decided 2026-09-16: development and the desktop only.* A `local` worker shares the controller's host and network identity, so the trust boundary does not hold for it. A Kubernetes hub always runs `remote`: the hub image ships neither pixi nor a Cog environment, and the chart does not offer `location: local`. The controller still accepts the value, because level 1 is the hub as a plain process. ADR-0002 D12 (Phase 9) records it.
12. **How a Cog's environment gets the Worker SDK** — *decided 2026-09-16: a git dependency on this repository*, pinned to a commit (`#subdirectory=worker`), so no PyPI release is needed; `dev/cogs/*` may pin it by path.
13. **What the first Hermes run binds to** — *decided 2026-09-16:* an OpenAI-compatible endpoint from the hub's `models:` block (Phase 11) — given by environment at level 1, by chart values on a hub — with the stdlib fake model in CI. The llm-serving-pack's internal endpoint is one such entry, not a special case. Until that block exists, the controller delivers the endpoint, its name and its key from its own environment to the Hermes Cog alone (Phase 10), with `dev/fake-model` by default.
14. **What the first Hermes run may do** — *decided 2026-09-16:* no tools at M4 — prompt in, envelope out. Hub-mediated MCP (Frames, connectors) follows Phase 29's grants.
15. **Windows for `local`** — *decided 2026-09-16: no.* Linux and macOS in CI; the desktop's local run host (Phase 31) inherits the limit.
16. **Which Hermes to pin, and where the Cog lives** — *decided 2026-09-16:* `hermes-agent` 0.19.0, and the Cog lives in this repository at `cogs/hermes/`, published with Nebi by a `publish-cogs.yaml` workflow to the registry the catalog indexes. The configuration it ports (written for 0.17) is re-verified against 0.19.
17. **The next harness Cogs.** pi and OpenCode follow Hermes, each its own Cog under `cogs/`, in that order unless a user need reorders them; a harness that does not speak ACP gets an adapter in the SDK. Each owns its per-turn context as Hermes does, and gets its Frames, the model's limits and `context-exhausted` from the SDK (decision 19), so neither rediscovers them. Open: which ships first, and whether either needs an adapter of its own.
18. **How the state machines are built** — *decided 2026-09-18:* on the [state pattern](https://en.wikipedia.org/wiki/State_pattern), one class per state behind its machine's interface, the context delegating every event to its current state (Phase 3); ADR-0002 D11 records it. A transition table — what #35's `CogLifecycle` has — was the alternative; with the pattern each state owns its guards and the Track events it records, and a state added later is a class added rather than a table edited in several places.
19. **Where per-turn context and Frames live in a Cog run** — *decided 2026-09-19:* per-turn context — output headroom, overflow recovery, compaction, what enters each turn — is the harness's, inside the Cog, and the hub never sees it; `RunBudget` bounds the run from outside. What every harness needs to do that is handed to all of them the same way by the Worker SDK: the model's context window and maximum output in the binding (Phases 11, 13, 24), the step's Frames checked and snapshotted at submit and placed in the prompt (Phases 13, 17), and `context-exhausted` for an input that cannot fit (Phase 13). Cog execution does not run on Ravnar, the desktop's chat engine: Ravnar solves these features for a conversation, a Cog run solves them at the seam, and a Cog needs no desktop code. The desktop's chat and its local Cog runs both drive Hermes over ACP and stay separate, conversational and workflow (Phase 31); they share the Hermes pin and its configuration, and a chat that needs a Cog run starts one through the run API.
20. **Progress within a step.** The Track and the run API's stream are at step granularity: a step is one event, when it ends. A harness's progress inside a step — message chunks, tool calls and plans from ACP's `session/update` — reaches no one. Proposal: the Worker SDK may answer `/invoke` as a stream of progress lines ending with the envelope, a plain JSON answer staying valid for a Cog not built on the SDK; the controller writes the lines to a short-lived progress log beside the Track — never the Track, pruned when the step ends — and the run API's live stream interleaves them, while a replay with `after` does not return them.
21. **Hermes first, the CLI beside it** — *decided 2026-09-27, revised 2026-09-28:* the `collab-hub` CLI signs in with the Keycloak realm, client and flow the desktop already uses, not a client and a grant of its own (Phase 6), and depends on nothing, so it lands first without holding anything up. The Hermes run keeps the front of the line: the run API is split so the slice that launches, lists, streams and cancels (Phase 12) needs no keyed claim — nothing in it invokes a step twice — and the Worker SDK (Phase 13) and the Hermes Cog (Phase 14) follow it directly; a tool-less Hermes step has no side effect for a claim to guard. Launching and terminating from the CLI (Phase 15) runs alongside them. The claim (Phase 16), which then gives the SDK its claim client, the rest of the run API — Frames, payloads, decisions, retry (Phase 17) — and the CLI's commands over it (Phase 18) follow the primary goal instead of preceding it.

## 11. Appendix — two launches, and the states of a Cog

Two sequences through the boxes of §4, one per client and per pair of axis values — both run the same Hermes Cog and the same Op; what differs is who submits, where the worker lives and what survives a restart — then the state machine they both move through.

### A. From the CLI — `local` and `none`, at dev level 1

The shape of `make api` plus `make controller LOCATION=local`: two processes, a SQLite Track and claim store, no container, driven by `collab-hub cog launch` (Phases 6, 15, 18).

```mermaid
sequenceDiagram
  autonumber
  actor User as User at a terminal
  participant CLI as collab-hub CLI
  participant API as Run API
  participant Store as Track · claims · pickup<br/>SQLite at level 1
  participant Ctl as Run controller<br/>Lifecycle runner on none
  participant Exec as LocalProcessCogExecutor
  participant W as Local worker<br/>Worker SDK · Harness adapter · Hermes
  participant Model as Model endpoint<br/>from the models: block
  User->>CLI: collab-hub cog launch hermes --entry ask --input - --watch
  CLI->>API: POST /v1/runs (bearer, or dev auth at level 1)
  API->>Store: op_submitted
  API-->>CLI: 202 run id, backend none, location local
  CLI->>API: GET /v1/runs/{id}/events (stream)
  Ctl->>Store: pickup, atomic: the run is this controller's
  Ctl->>Exec: materialize cogs/hermes through the directory source
  Note over Ctl,Exec: run token minted, its hash recorded in the store
  Exec->>W: the launcher spawns serve in its own process group<br/>run token in the environment, binding file from models:
  Exec->>W: GET /healthz until ready
  Ctl->>W: POST /invoke (task, idempotency key, run token)
  W->>API: POST /v1/claims/{key}/reserve (run token)
  W->>Model: the Harness adapter drives hermes acp, which calls the model
  W->>API: POST /v1/claims/{key}/commit (envelope)
  W-->>Ctl: envelope
  Ctl->>Store: step_completed (binding, problems, usage, payload reference)
  opt the step's Gate escalates
    Ctl->>Store: gate_escalated (escalation id)
    API-->>CLI: event: waiting at a Gate, exit code 4
    User->>CLI: collab-hub run decide ID STEP --approve
    CLI->>API: POST .../decision (escalation id, approve)
    API->>Store: gate_decided (actor)
    Store-->>Ctl: the Track watcher delivers the decision to the waiting runner
  end
  Ctl->>Exec: teardown: the process group is killed
  Ctl->>Store: run_completed
  API-->>CLI: event: completed, exit code 0
  User->>CLI: collab-hub run payload ID STEP
```

If the controller is killed between steps 10 and 15, the launcher kills the worker when the controller's pipe closes, the next controller start records the run `interrupted`, and `collab-hub run retry ID` resumes the step under its existing key: a claim already committed answers instead of Hermes running again (Phases 8, 16, 17).

### B. From Collab — `remote` and `dbos`, on a Kubernetes hub

Production's shape: the desktop's loopback proxy, the API and the controller as separate Deployments, the Track and DBOS's system database on one Postgres, the worker a pod pulled from its artifact (Phases 19–22, 26, 28).

```mermaid
sequenceDiagram
  autonumber
  actor User as User in Collab
  participant Collab as Collab desktop<br/>Run view · loopback proxy
  participant API as Run API
  participant Store as Track · claims · pickup<br/>Postgres
  participant Ctl as Run controller<br/>Lifecycle runner on dbos
  participant DBOS as DBOS<br/>queue · system database
  participant Exec as KubernetesCogExecutor
  participant Pod as Remote worker pod<br/>Worker SDK · Harness adapter · Hermes
  participant GW as Model egress gateway
  User->>Collab: launch the Op, run target: hub
  Collab->>API: POST /v1/runs (bearer stamped by the proxy)
  API->>Store: op_submitted
  API-->>Collab: 202 run id, backend dbos, location remote
  Collab->>API: GET /v1/runs/{id}/events (stream, relayed to the run view)
  Ctl->>Store: pickup, atomic
  Ctl->>DBOS: enqueue the run as a workflow: ownership is the queue's from here
  DBOS->>Ctl: run the step functions as DBOS steps, each checkpointed
  Ctl->>Exec: materialize the installed digest
  Note over Ctl,Exec: run token minted, its hash recorded, mounted into the pod
  Exec->>Pod: Deployment, Service, ingress and egress NetworkPolicy<br/>init container: nebi pull, then serve
  Exec->>Pod: GET /healthz until ready
  Ctl->>Pod: POST /invoke (task, idempotency key, run token verified by the SDK)
  Pod->>API: POST /v1/claims/{key}/reserve (run token)
  Pod->>GW: model call over the internal Service, the only egress the policy allows
  Pod->>API: POST /v1/claims/{key}/commit (envelope)
  Pod-->>Ctl: envelope
  Ctl->>Store: step_completed
  Ctl->>Store: gate_escalated (escalation id)
  Ctl->>DBOS: durable wait for the decision
  API-->>Collab: events: a pending decision in the run view
  User->>Collab: approve
  Collab->>API: POST .../decision (escalation id, approve)
  API->>Store: gate_decided (actor)
  Store-->>Ctl: the Track watcher sees gate_decided
  Ctl->>DBOS: send the decision to the waiting workflow
  opt a controller replica dies mid-step
    DBOS->>Ctl: another replica recovers the workflow by executor id
    Ctl->>Exec: reap the orphaned pod, materialize again
    Ctl->>Pod: POST /invoke again, same idempotency key
    Pod->>API: reserve finds the key committed: the envelope is returned, nothing acts twice
  end
  Ctl->>Store: run_completed
  Ctl->>Exec: teardown: the pod and its objects are deleted
  API-->>Collab: completion in the run view
```

What `dbos` buys: steps 8 to 25 are checkpointed, so a controller restart resumes the workflow, at the Gate too, with nobody resubmitting; the same run under `none` would end `interrupted` (decision 3). What `remote` buys: the pod's egress policy makes step 14 the only way out, and the API's ServiceAccount could not have created the pod (Phase 21).

### C. The states of a Cog

"The state of a Cog" is four questions, each with its own small machine: is the Cog **installed** on this hub, is a **worker** of it up, what happened to one **step attempt** on that worker, and where is the **run** that asked. They nest: only an `INVOKABLE` Cog is materialized, a `RUNNING` run drives step attempts one at a time, and each attempt holds a worker `INTERACTING`. The names below are this plan's, in capitals; where #35's code already has a state, the table gives its spelling. **Phase 3 built these four machines on the state pattern** (#133), before anything moves through them, and [`docs/cog-execution/states.md`](docs/cog-execution/states.md) is now the reference: a test holds it to the code, so a transition added there and not in the code, or in the code and not there, fails CI. A change to a state or a transition is made in that document and the code together; this section keeps the plan's view of them.

```mermaid
stateDiagram-v2
  direction TB

  state "The Cog on a hub — install, once per digest" as cog {
    direction LR
    [*] --> PUBLISHED
    PUBLISHED --> FETCHED: install by digest
    FETCHED --> BOUND: binding admitted
    BOUND --> INVOKABLE: check passed
    FETCHED --> FETCHED: no binding admitted, recorded
    BOUND --> BOUND: check failed, recorded
    FETCHED --> UNINSTALLED: uninstall
    BOUND --> UNINSTALLED: uninstall
    INVOKABLE --> UNINSTALLED: uninstall
    UNINSTALLED --> [*]
  }

  state "A worker — once per materialization" as worker {
    direction LR
    [*] --> MATERIALIZED
    MATERIALIZED --> READY: healthz answers
    READY --> INTERACTING: invoke
    INTERACTING --> IDLE: envelope returned
    IDLE --> INTERACTING: next step, warm
    IDLE --> TEARING_DOWN: one-shot, idle timeout, or orphan reaped
    READY --> TEARING_DOWN: cancel, or orphan reaped
    INTERACTING --> TEARING_DOWN: cancel, deadline, or orphan reaped
    TEARING_DOWN --> TORN_DOWN
    MATERIALIZED --> WORKER_FAILED
    READY --> WORKER_FAILED
    INTERACTING --> WORKER_FAILED
    IDLE --> WORKER_FAILED
    TEARING_DOWN --> WORKER_FAILED: teardown failed
    TORN_DOWN --> [*]
    WORKER_FAILED --> [*]
  }

  state "A step attempt — the keyed claim" as step {
    direction LR
    [*] --> INVOKED
    INVOKED --> INVOKED: worker lost before reserving, invoked again
    INVOKED --> RESERVED: the worker reserves the key
    RESERVED --> COMMITTED: the envelope is committed
    COMMITTED --> RECORDED: the Track records the step
    RESERVED --> OUTCOME_UNKNOWN: worker lost before committing
    OUTCOME_UNKNOWN --> RECORDED: reconciled by a person
    RECORDED --> [*]
  }

  state "A run — status from the Track only" as run {
    direction LR
    [*] --> SUBMITTED
    SUBMITTED --> RUNNING: pickup
    RUNNING --> WAITING_AT_GATE: a Gate escalates
    WAITING_AT_GATE --> RUNNING: approve, or send back
    WAITING_AT_GATE --> REJECTED: reject
    WAITING_AT_GATE --> FAILED: send back past the revise limit
    RUNNING --> COMPLETED
    RUNNING --> FAILED
    RUNNING --> BUDGET_EXCEEDED
    SUBMITTED --> CANCELLED: cancel
    RUNNING --> CANCELLED: cancel
    WAITING_AT_GATE --> CANCELLED: cancel
    RUNNING --> INTERRUPTED: host stops, on none
    WAITING_AT_GATE --> INTERRUPTED: host stops, on none
    INTERRUPTED --> RUNNING: retry, same attempt and key
    FAILED --> RUNNING: retry, new attempt and key
    BUDGET_EXCEEDED --> RUNNING: retry, new budget epoch
    COMPLETED --> [*]
    REJECTED --> [*]
    CANCELLED --> [*]
  }

  INVOKABLE --> MATERIALIZED: a step of a run needs this Cog
```

| State | Of | Meaning | Defined in |
|---|---|---|---|
| `PUBLISHED` | the Cog | The package is in a registry the catalog indexes, addressable as `<host>/<repo>@<digest>`; nothing of it is on the hub yet. A development package read from a directory (Phase 9) has no install states: it is materialized straight from its directory. | ADR-0001 D6; #7 |
| `FETCHED` | the Cog | Install has pulled the package and provisioned its environment. Not invokable. A binding that cannot be admitted leaves it here, recorded. | glossary, *Install*; Phase 23 |
| `BOUND` | the Cog | Its requirements are resolved and the binding admitted — from the `models:` stopgap, then from the Cog's own `resolve`. A failing `check` leaves it here, with the failing step recorded. | Phases 23, 24 |
| `INVOKABLE` | the Cog | `check` passed against the delivered binding and the catalog card is recorded; a run may name the digest. Installing starts no worker. | glossary, *Install*; ADR-0001 D5 |
| `UNINSTALLED` | the Cog | The install and every runtime resource it created are removed, its warm pool drained. Reachable from every state in which the install holds something on the hub — `FETCHED`, `BOUND`, `INVOKABLE` — so an install stuck before `INVOKABLE` can be removed; a `PUBLISHED` Cog has nothing to remove. | Phases 23, 25 |
| `MATERIALIZED` | a worker | The executor has brought up the package's `serve` — a child process at `local`, a pod at `remote` — and minted its run token; it is not answering yet. | `LifecycleState.MATERIALIZED`; glossary, *Materialize / worker* |
| `READY` | a worker | `/healthz` answers; the worker can take an `/invoke`. | `LifecycleState.READY` |
| `INTERACTING` | a worker | An `/invoke` is in flight: the Harness adapter is driving the harness. Cancellation and the duration deadline act here. | `LifecycleState.INTERACTING` |
| `IDLE` | a worker | The envelope is back. A warm worker waits here for the next step until its idle timeout; a one-shot worker leaves at once. | `LifecycleState.IDLE`; Phase 25 |
| `TEARING_DOWN` | a worker | The executor is reclaiming it: the process group killed, or the pod and its objects deleted — after a one-shot step, an idle timeout, a cancel, or the duration deadline cutting an interaction short. Also how an orphan is reaped when the next controller starts. | `LifecycleState.TEARING_DOWN`; Phases 9, 11, 21 |
| `TORN_DOWN` | a worker | Gone. A local assertion only: the Track records a teardown that failed, never this state. | `LifecycleState.TORN_DOWN` |
| `WORKER_FAILED` | a worker | Materialization, readiness, an interaction or teardown failed — the last recorded as `teardown_failed`; terminal for this worker, and the run decides what follows. | `LifecycleState.FAILED` |
| `INVOKED` | a step attempt | The controller has POSTed the task with the attempt's idempotency key. A worker lost before reserving is simply invoked again under the same key: nothing has acted. | Phase 16 |
| `RESERVED` | a step attempt | The worker reserved the key before acting. | Phase 16; ADR-0002 D1 |
| `COMMITTED` | a step attempt | The worker committed its envelope; any replay of the key returns that envelope without acting again. | Phase 16 |
| `RECORDED` | a step attempt | The Track holds `step_completed` or `step_failed` for the attempt. | Phase 5 |
| `OUTCOME_UNKNOWN` | a step attempt | The key was reserved and never committed: the worker was lost between the side effect and its result. Nothing acts again, an ordinary retry is refused, and a person — or an entry point the Cog declares idempotent — reconciles it. | Phases 16, 17 |
| `SUBMITTED` | a run | The API recorded `op_submitted`; no controller has picked the run up. | `RunStatus.SUBMITTED`; Phase 11 |
| `RUNNING` | a run | A controller owns it and the Lifecycle runner is advancing its steps. #35's finer statuses — `materialized`, `ready`, `interacting`, `idle`, `tearing_down` — are the worker's states surfaced while the run is here. | `RunStatus.RUNNING` |
| `WAITING_AT_GATE` | a run | A step's Gate escalated; the run waits for a decision naming the open escalation id. A send back re-runs the step, bounded by the revise limit: a limit of N allows N revisions, past which the run ends `FAILED` with reason `revise_limit_exceeded`. `paused` in #35's code, where the Cog asked for it; Phase 4 moves the decision to the step. | `RunStatus.PAUSED`; Phases 4, 12 |
| `COMPLETED` | a run | Every step completed, and every Gate passed or was approved. Terminal. | `RunStatus.COMPLETED` |
| `FAILED` | a run | A step's envelope came back `ok: false` with an error code, its worker failed, a step attempt ended `OUTCOME_UNKNOWN`, or a send back went past the revise limit — each recorded with its reason. Retry runs a recorded failure as a new attempt under a new key. | `RunStatus.FAILED`; Phase 8 |
| `REJECTED` | a run | A reviewer rejected at a Gate. Terminal. | Phase 4 |
| `CANCELLED` | a run | A client cancelled, before pickup or after: any worker is torn down and the actor recorded. Terminal. | Phase 8 |
| `BUDGET_EXCEEDED` | a run | A budget dimension — `duration`, `tokens` or `cost` — stopped it; `timed_out` in #35's code is the duration case. Retry opens a new budget epoch. | `RunStatus.BUDGET_EXCEEDED`; Phase 25 |
| `INTERRUPTED` | a run | Its host stopped under `none`, which cannot resume; recorded when the host next starts. Retry continues the attempt in flight under its existing key. Under `dbos` and `temporal` a host stop leaves the run `RUNNING` or `WAITING_AT_GATE`, and it resumes. | glossary, *Interrupted*; ADR-0002 D2; Phase 8 |
