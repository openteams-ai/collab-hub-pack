# Glossary

The vocabulary used across Collab Hub's issues, ADRs, and docs, defined in
one place. When a word here has a narrower meaning than its everyday one,
the narrower meaning is the one intended. Every entry cites where its
definition comes from; when a new ADR introduces a term, its entry is added
here in the same PR.

Two tiers. **Platform concepts** come from the public
[Intelligence Hub whitepaper](https://github.com/openteams-ai/inthub-whitepaper/blob/main/whitepaper.md),
which is the authority on what they mean; where the hub's usage is narrower
than the whitepaper's, the entry says so. **Hub execution vocabulary** is
defined by [ADR-0001](adr/0001-cog-execution.md),
[ADR-0002](adr/0002-lifecycle-runner-durability-and-placement.md) and the
[cog-execution docs](cog-execution/README.md) — these terms do not appear
in the whitepaper and this glossary is their home.

## Platform concepts (defined by the whitepaper)

**Cog.** A packaged, installable AI worker (whitepaper §4.4: "a unit that
can be installed and replaced"). A *complete* Cog is the model, the
weights, the model runtime (including any harness), and the context. A Cog
is described by its manifest (`cog.yaml`, following the shared Cog
specification as a profile) and runs itself: it carries its own pinned
environment and its own entry points. The hub never supplies a Cog's
runtime. The whitepaper also describes governance parameters a Cog
carries; in the hub those are enforced by the environment (Guards, Gates,
the capability list), not by the Cog's own code.

**Op.** A supervised, multi-step workflow that a person kicks off
(whitepaper §4.5: "a supervising model that may coordinate Cog execution…
and human-in-the-loop feedback points"). Ops compose Cogs' usage entry
points into steps and wrap Frames, Guards, Gates, and Tracks around them.
"Run a Cog once" and "a five-step proposal with human sign-off" are the
same concept at two scales.

**Frame.** Organizational context supplied to a step — saved Markdown
context, org policy, the material a Cog is asked to work from. The hub's
Frames API is the service that stores and serves them.

**Guard.** An independent, system-side verifier of the *system's*
requirements, run by the Op layer on a step's result envelope (whitepaper
§5.1 names Schema, Source, Policy, and Privacy Guards). A Guard produces
findings; it does not decide. A Cog's own self-validation is a *contract
check*, not a Guard — see below.

**Gate.** The decision point on a step: ok / ok-with-problems / escalate
to a human (whitepaper §5). Gates consume the envelope (payload and
problems) and Guard findings. Gates decide; Cogs never do. A gate is
declared on the Op step, not implemented inside the Cog, and a Cog cannot
pause a run. Its outcomes are `pass`, `pass_with_problems` and `escalate`;
by default any problem with severity `error` escalates. An escalation has
an id minted over the step attempt and the envelope, and names who may
decide it (its `approvers`, organization owners and platform operators when
it names none). A decision answers one escalation, as an actor, with an
outcome — **approve** completes the step with the result the approver saw,
**reject** ends the run `REJECTED`, **send back** re-runs the step with the
findings — and a decision on an escalation that is no longer open is
refused as stale. (ADR-0002 D8; [states](cog-execution/states.md).)

**Track.** The durable, append-only record of a run: which Cogs ran under
which bindings, which Guards and Gates fired, who approved, what came out
(whitepaper §5.3). Status is derived from the Track, never held only in
memory. The Track is the accountability record — a Gate signature is only
meaningful because the Track can say *which* model, *which* weights,
*which* evidence produced the thing that was signed. Its events follow a
versioned schema ([track](cog-execution/track.md)): `step_completed` names
the Cog, its digest, the binding, the problems, the usage and the Frames
behind a result, `gate_decided` names the escalation, the actor and the
result decided on, and `step_failed` carries more than a class name.

## Hub execution vocabulary (defined by ADR-0001, ADR-0002 and the cog-execution docs)

**A4.** The current delivery milestone. ADR-0001 scopes several decisions
"for A4"; it is a schedule boundary, not an architecture concept.

**ACP (Agent Client Protocol).** An open protocol, JSON-RPC over stdio,
through which a client drives an agent harness: it opens a session, sends
prompts, and receives streamed updates and permission requests. The worker
SDK's first harness adapter speaks ACP, so any harness that implements it
can be wrapped without new adapter code. (ADR-0002 D5.)

**Agent location.** Where a Cog's worker runs, relative to the run
controller: `local` (a process on the controller's host) or `remote` (a
workload on a cluster). One configuration value; the lifecycle runner drives
both through the same executor interface and the same seam, and one
conformance suite holds them to the same behaviour. Not *locality*, which is
where a satisfier runs, nor the *run target*, which is where Collab sends a
run. (ADR-0002 D12.)

**Binding record.** The output of resolution: which pinned model, which
endpoint, which harness, which evidence — the identity that every result
envelope and every Track entry carries. Credentials appear in a binding
only by reference, never as values. (ADR-0001 D8; seam note.)

**Bundled op.** A usage entry point shipped in a Cog's package: a
single-Cog, zero-gate op (`ask`, `chat`, build-a-bundle-and-ask). Bundled
ops appear in the catalog card. Lifecycle entry points are *not* bundled
ops. (Seam note, "Bundled ops".)

**Capability list.** What a hosting environment publishes: the satisfiers
it offers and the requirements it places on Cogs that run there (envelope
shape, io values, entry-point forms). Environment-owned; needs no
ratification in the Cog spec. (Seam note, "Who builds what".)

**Catalog card.** A Cog's entry in the catalog — name, function, io, and
its usage entry points — derived from the manifest, never a hub-invented
schema. How Cogs appear in the Op builder's picker. (Seam item 4; ADR-0001
D9.)

**Cog classes.** Declared in the manifest, not separate code paths:
*model* Cogs serve a model (or describe one the environment serves — a
*descriptor* Cog); *harness* Cogs supply interaction machinery; *context*
Cogs carry prompts, schemas, and frames and point at a model rather than
carrying one; *complete* Cogs carry all three. Classes compose through
`provides`/`requires`. (ADR-0001 D3.)

**Conformance suites (lifecycle, durability).** Two test suites shared by
every durability backend. The *lifecycle suite* (multi-step completion, a
Gate escalating and each human decision, cancel, a budget stop, retry as a
new attempt, the Track's contents) passes unchanged on every backend. The
*durability suite* runs one set of scenarios (a kill mid-step, a restart
while a run waits at a Gate, several replicas) with an outcome that depends
on the backend: on `dbos` and `temporal` the run resumes and no step runs
on two replicas at once; on `none` the run ends `interrupted` and is never
resumed. Passing both is what makes a backend swappable. (ADR-0002 D1–D2.)
A third, the *location suite*, is shared by every agent location instead:
materialize, ready, an `/invoke` round trip, teardown, a cancel
mid-interaction, two workers at once, and a controller killed with a worker
in flight. (ADR-0002 D12.)

**Contract check.** A Cog's own in-package validation of its declared
contract (schema, grounding, citation, identity), self-reported in the
envelope's `problems` list. Input to Guards, never a Guard's verdict —
and never called a Guard. (Envelope doc, `problems`.)

**Data catalog.** The environment service that rates data sources on the
shared sensitivity scale. Labels are born on data, in the catalog — not
declared by Cogs. (ADR-0001 D10; sensitivity doc.)

**Durability backend.** What schedules the lifecycle runner's step
functions and decides whether progress between them is checkpointed:
`none` (no durability engine — the functions are called in process and
nothing is checkpointed), `dbos` (the steps of a DBOS workflow) or
`temporal` (the activities of a Temporal workflow). Selected by
configuration. It never contains lifecycle logic and never answers what a
run's status is — the Track does. (ADR-0002 D1–D3.)

**Engine (orchestration engine).** ADR-0001's name for the run-facing
boundary behind which an Op's steps run: submit a run, signal a paused
step, observe its state. It sits behind its own interface so the
implementation is swappable, and is distinct from the executor. ADR-0002
splits what is behind it: the *lifecycle runner* runs the steps, a
*durability backend* decides whether progress between them is checkpointed
(`none` checkpoints nothing), and a *run controller* advances runs on a
host. (ADR-0001 D5; ADR-0002 D1, D4; issue #2.)

**Entry point.** A declared, invokable interface of a Cog. Two audiences:
*usage* entry points (`ask`, `chat`, …) face end users and the Op layer;
*lifecycle* entry points (`resolve`, `serve`, `check`, `eval`, …) face the
hosting environment. (Cog-execution README; seam note.)

**Executor.** The component that materializes and tears down Cog workers
on some substrate (Kubernetes is the default implementation). Pluggable;
no raw cluster primitives leak past it into orchestration. Which one a
runner uses is its *agent location*; an executor holds no lifecycle logic.
(ADR-0001 D5, invariant 2; ADR-0002 D12, invariant 6.)

**Grant.** A user's standing, revocable permission for Cogs to use one
connector, with given scopes, on that user's behalf when no request of
theirs is in flight — what an unattended run needs. The hub holds whatever
backs a grant; a worker never does. (ADR-0001 *Out of scope / deferred*;
issue #8.)

**Harness adapter.** The part of the worker SDK that drives one kind of
harness — an ACP agent over stdio, or a direct OpenAI-compatible model
call — behind one interface (`start`, `interact`, `cancel`, `close`). The
SDK turns what an adapter returns into a result envelope. (ADR-0002 D5.)

**Hosting environment.** The place a Cog is installed and run — the hub,
or later a desktop. It owns the lifecycle entry points, offers the
satisfier inventory, and publishes its capability list. (Seam note, "Who
builds what".)

**Hosts / invokes.** The two verbs of "running a Cog": the environment
*hosts* a Cog by invoking its lifecycle entry points (install, resolve,
serve, check) and *invokes* it by calling its usage entry points.
Orchestrator code never executes a Cog's internals. (ADR-0001 D2; seam
note.)

**Install.** Making a Cog invokable: fetch the package, provision its
environment, run its checks, resolve its requirements, record the binding,
derive its catalog card. Distinct from materialize — installing does not
start a worker. (Cog-execution README, "Materialize / worker".)

**Install reference.** The pinned `<host>/<repository>@<digest>` of one
indexed Cog version — what a client hands to `nebi import`, and what the
catalog read API answers at `…/versions/{digest}/reference`. It names one
location of the artifact; identity is the digest, so the same digest may have
several install references across sources and repositories.
([cog-registry.md](cog-registry.md#read-api).)

**Interrupted.** The terminal status of a run that a host was advancing
when it stopped, under a backend that cannot resume it (`none`). Recorded
on the Track when the host next starts. The run is never resumed; retrying
it continues the attempt that was in flight, under the same idempotency key,
so a committed claim answers instead of the work running again. (ADR-0002 D2;
[states](cog-execution/states.md).)

**Keyed claim.** How a step executed again within the same attempt avoids
repeating a side effect: each interaction carries an idempotency key for its
step attempt, the first writer of a key records its envelope, and a later
invocation with that key gets the envelope back instead of acting again. An
attempt lasts until its outcome is on the Track, so the claim covers a
durable backend resuming after a crash, and a retry of a run interrupted
before that step's outcome was recorded. A step whose failure was recorded
runs again on retry, as a new attempt with a new key. Not a lease: it does
not decide who runs a step, only that one attempt acts once. (ADR-0002 D1;
issue #1.)

**Lifecycle runner.** The one component that runs a Cog's lifecycle —
resolve, materialize, interact, read the envelope, Guards, Gate, idle or
teardown, with budgets, cancellation and Track recording — written once as
plain step functions. A durability backend schedules those functions; it
never reimplements them. (ADR-0002 D1; ADR-0001 invariant 1.)

**Locality.** Where a satisfier runs and where data goes when it is used —
in-cluster, on-prem, or an external provider. A bind-time constraint that
resolution enforces so data stays where policy allows; the sensitivity
model generalizes it. (Sensitivity doc.)

**Local worker.** A Cog worker at the `local` agent location: the Cog
package's `serve` task, run as a process on the controller's host in the
package's own pixi environment, listening on a loopback port the executor
chose. It is given its environment and nothing else of its controller, and it
cannot outlive it: its launcher kills its process group when the controller
lets go of it or dies. For development and the desktop. (ADR-0002 D12.)

**Materialize / worker.** To materialize a Cog is to bring up a running
instance of its `serve` entry point where the hub can reach it — on
Kubernetes, a pod behind a Service. That running instance is a **worker**.
Materialization happens behind the executor; teardown reclaims it. The
first invocation of an installed Cog materializes a worker. (ADR-0001 D5;
cog-execution README.)

**Nebi.** The package manager for Cogs: `nebi pull` installs a Cog's
files, `nebi run` executes one of its entry points, and publishing goes
through Nebi to an OCI registry the hub indexes. (ADR-0001 D6.)

**Remote worker.** A Cog worker at the `remote` agent location: a workload
on a cluster, reached over the cluster's network through the same seam as a
local worker. What a Kubernetes hub always runs. (ADR-0002 D12.)

**Result envelope.** The structured return of a usage entry point: `ok`,
`payload`, `problems`, `binding`, plus usage and timing — exactly what
Guards check, Gates read, and Tracks record. (The
[envelope doc](cog-execution/result-envelope.md).)

**Run controller.** What advances runs on a host: it runs the lifecycle
runner and owns the executor. On the hub it is its own process and the only
identity allowed to create workloads; the public API records a run's intent
and reads its Track, and never calls the executor. On the user's machine,
the local run host plays the same role. (ADR-0002 D4, D6; ADR-0001
invariant 2; issue #6.)

**Turn.** One exchange with a Cog whose step holds a session: a client's
text, recorded on the run's Track by the API, delivered by the run controller
to the Cog's worker (`POST /turn`), and the worker's answer recorded beside it.
`collab-hub run say` asks one; `collab-hub run connect` turns each prompt of
an ACP client into one. (`docs/cog-execution/runs.md`.)

**Run intent.** What a client asked of a run, recorded on its Track by the
API for the run controller to act on: `op_submitted`, `cancel_requested`,
`turn_requested` and `decision_requested`.
The API writes intent and reads status; it never calls the controller.
(ADR-0002 D4.)

**Run pickup.** How exactly one of several run controllers starts a
submitted run, under every backend: `run_picked_up`, naming the controller,
written only if nobody picked the run up first. Under `none` the run then
belongs to that controller, which alone advances it, and ends `interrupted`
when the controller next starts after stopping; under `dbos` and `temporal` the
engine takes ownership after pickup. (ADR-0002 D4,
`docs/cog-execution/runs.md`.)

**Run controller id.** The name a run controller holds while it runs and
records on each run it picks up: the same across its restarts, so a restarted
controller takes its runs back, and different between replicas.

**Run target.** Where Collab sends a run: the hub, or the local host on the
user's machine. Both serve the same run API. Placement chooses the target,
and a run bound to local-only resources never reaches the hub. (ADR-0002
D6–D7.)

**Run token.** The credential of one materialized worker: minted by the
controller when it materializes the worker, delivered in the worker's
environment, expired when the worker is torn down. The controller presents it
to the worker's `/invoke`, and the worker presents it to the hub endpoints it
calls. Only its hash is recorded, on the run's Track. (ADR-0002 D12.)

**Satisfier / resolution.** A Cog declares what it `requires` (a model
capability, a harness, a connector); a *satisfier* is something in the
environment that `provides` it. *Resolution* is the Cog's own `resolve`
lifecycle entry point selecting satisfiers from the inventory the hosting
environment offers; the result is the binding record. (ADR-0001 D3,
invariant 4; cog-execution README.)

**Sensitivity label / effective label.** A rating a data source carries in
the data catalog, on a shared multi-axis scale. A run's *effective* label
is the strictest value on each axis across the sources actually bound; a
step's output label is at least the strictest of its inputs, absent a
declared, Guard-verified, Gate-signed downgrade. (ADR-0001 D10, invariants
6–7; sensitivity doc.)

**State machines.** The four machines that hold every state of Cog
execution, built on the state pattern: one class per state, the context
delegating each event to its current state, and an event the state does not
accept refused. A Cog's **install**: `PUBLISHED`, `FETCHED`, `BOUND`,
`INVOKABLE`, `UNINSTALLED`. A **worker**: `MATERIALIZED`, `READY`,
`INTERACTING`, `IDLE`, `TEARING_DOWN`, `TORN_DOWN`, `WORKER_FAILED`. A **step
attempt** under the keyed claim: `INVOKED`, `RESERVED`, `COMMITTED`,
`RECORDED`, `OUTCOME_UNKNOWN`. A **run**: `SUBMITTED`, `RUNNING`,
`WAITING_AT_GATE`, `COMPLETED`, `FAILED`, `REJECTED`, `CANCELLED`,
`BUDGET_EXCEEDED`, `INTERRUPTED`. A run's status is its Track replayed through
the run machine. Each state is described in
[states](cog-execution/states.md). (ADR-0002 D11; ADR-0001 invariant 3.)

**Worker SDK.** An optional library that implements the worker side of the
seam once — `/invoke` returning an envelope, the health probe, the keyed
claim, the binding, cancellation, usage — and hands each interaction to a
harness adapter. A Cog may use it; the hub requires only the seam and never
imports the SDK. (ADR-0002 D5.)
