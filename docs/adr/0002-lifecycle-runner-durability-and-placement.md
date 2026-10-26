# ADR-0002 — Lifecycle runner, durability backends, and placement

**Status:** Proposed
**Date:** 2026-09-14
**Amends:** [ADR-0001](0001-cog-execution.md) — D1, *Orchestration & storage*, and *Out of scope / deferred*

> Companion reading: [ADR-0001](0001-cog-execution.md), the
> [glossary](../GLOSSARY.md), and [`docs/cog-execution/`](../cog-execution/README.md).
> Every decision below names the ADR-0001 decision or invariant it preserves.

## Context

#35 landed the `collab-hub-execution` package: a reference engine, executors, a
Track, budgets and model resolution. It fixes the vocabulary, but four things in
it cannot carry the next requirements:

- **Durability is one engine's replay.** `DurableWorkflowEngine` recovers only
  when a caller resubmits an incomplete Op and the Track is replayed. There is
  no way to run without durability, or with a real engine. #2 asks for a
  swappable engine, and three options are required: no durability engine
  (`none`, implemented first), DBOS (`dbos`) and Temporal (`temporal`).
- **Nothing in the hub runs it.** `collab_hub_api` does not import the package;
  no identity advances runs and no API lets a client start one (#1, #6).
- **The Cog decides a pause.** A worker answers `{"pause": true}`; the review on
  #2 puts that decision on the Op step's Gate.
- **Only the hub is considered.** The Collab desktop client must be able to
  launch runs on the user's machine as well as on the hub, and while the first
  harness Cogs will wrap Hermes, execution must not be tied to one harness.

These choices are expensive to reverse once code depends on them, so they are
recorded before it does.

## Decisions

**D1 — One lifecycle runner; durability is a backend.** One component, the
*lifecycle runner*, runs a Cog's lifecycle — resolve, materialize, interact,
read the envelope, Guards, Gate, idle or teardown, with budgets, cancellation
and Track recording — as plain step functions. A *durability backend* decides
only how those functions are scheduled and whether progress between them is
checkpointed: `none` calls them in process; `dbos` runs them as the steps of a
DBOS workflow; `temporal` runs them as activities of a Temporal workflow. A
backend is selected by configuration and contains no lifecycle logic. Every
backend passes the same conformance suites. A step executed again within the
same attempt — by a durable backend resuming after a crash, or by a retry of a
run interrupted before that step's outcome was recorded — is kept from
repeating its side effect by a *keyed claim*, never by a hand-built lease. A
step whose failure was recorded runs again on retry, as a new attempt with a
new key. *(Preserves ADR-0001 D5 and invariants 1–2: one lifecycle, engine
primitives behind an interface.)*

**D2 — `none` is not durable, and says so.** When a host starts, every run it
was advancing under `none` and did not finish is recorded `interrupted` on the
Track. It is never resumed and never left looking like it is running; retrying
it is an explicit new attempt. The Track is never replayed to resume execution
under any backend, so #35's Track-based recovery is retired — durable
resumption is what `dbos` and `temporal` are for. *(Preserves ADR-0001 D8: run
status stays truthful.)*

**D3 — The Track is the only source of run status.** Under every backend a
run's status derives from its Track. DBOS's system tables and Temporal's event
history are checkpoint machinery; nothing reads run status from them.
*(Preserves ADR-0001 D8.)*

**D4 — Runs advance in a run controller.** A *run controller* process runs the
lifecycle runner and owns the executor; it is the only identity with permission
to create workloads. The public API records a run's intent and reads its Track,
and never calls the executor. Under every backend, controllers coordinate through
*run pickup* — an atomic record, naming the controller, that lets exactly one
of them start a submitted run. What differs is ownership after pickup: under
`none` the run belongs to the controller that picked it up and ends
`interrupted` with it, while under `dbos` and `temporal` that controller hands
the run to the engine's queue, and the engine decides which replica resumes it.
*(Preserves ADR-0001 invariant 2; answers #6. Amended by the run controller's
phase, which built pickup: the first wording had `dbos` and `temporal` replace
pickup rather than take ownership after it.)*

**D5 — Workers are harness-neutral.** The hub speaks only the seam — task entry
point, result envelope, health probe, catalog card — and never learns which
harness a Cog uses. An optional *worker SDK* implements the worker side of the
seam once and hands each interaction to a *harness adapter*. The first adapter
speaks ACP (the Agent Client Protocol), an open protocol that Hermes and other
agent harnesses implement, so it covers any ACP agent rather than one product. The hub never imports the SDK. *(Preserves
ADR-0001 D2 and invariant 5.)*

**D6 — Local execution embeds the same package.** Local runs use
`collab-hub-execution` embedded in a local run host on the user's machine, with
`none` by default and `dbos` over SQLite available, so a local run can be
durable without Postgres; the local run host is that machine's run controller.
`temporal` is hub-only, because it needs a service.
Local Tracks stay on the machine. *(Amends ADR-0001 D1: local execution moves
from deferred to phased after the hub path. The seam is unchanged.)*

**D7 — One run API, two hosts.** The hub and the local host serve the same run
API contract. The client chooses a *run target* for each run, and a run bound
to local-only resources never reaches the hub.
*(Preserves ADR-0001 D1: placement-agnostic.)*

**D8 — Gates are declared on steps.** A step declares its Gate — a policy over
the envelope and Guard findings, with the outcomes pass, pass with problems, or
escalate — and who may decide an escalation. A human decision (approve, reject,
or send back with findings) is a signal to the run. A Cog can no longer ask to
pause: `PauseRequest` and the worker's `{"pause": true}` leave the protocol.
*(Preserves the seam rule that Gates decide and Cogs never do.)*

**D9 — Every change is runnable in `dev/` and asserted in CI, in the same PR.**
A change that adds an execution component makes it runnable from `dev/` at the
lowest level that can host it, documents it in `dev/README.md`, and asserts its
contract in CI at that level — no containers at level 1, no large image
downloads, and clusters and heavy engines in their own workflows. *(Serves
ADR-0001 invariant 3: the lifecycle is tested where it is built, not only in a
cluster.)*

**D10 — Every change updates the documents it makes stale, in the same PR.**
That includes deleting the statements it makes false, adding each new term to
the glossary, and updating its issue's row in the
[issue map](../cog-execution/README.md#how-the-open-issues-map-to-the-adr).
*(Extends the glossary's rule — a term lands in the PR that introduces it — from
ADRs to code.)*

**D11 — The lifecycle's states are state machines on the state pattern.** Every
state a Cog's install, a worker, a step attempt and a run can be in is held by
one machine per level. Each state is a class behind its machine's interface,
and an event the current state does not accept is refused, naming both, so an
illegal move fails instead of becoming a status. Transitions are pure — they
return the next state and the records that report it — so every durability
backend moves the same machines, and a run's status is its Track replayed
through the run machine. The states and transitions are in
[states](../cog-execution/states.md), and a test holds that page to the code. A
transition table was the alternative; with the pattern each state owns its
guards and its records, and a state added later is a class added rather than a
table edited in several places. *(Builds ADR-0001 invariant 3, a tested
lifecycle state machine, and keeps D3: status comes from the Track, through one
derivation.)*

**D12 — Agent location is a setting: `local` or `remote`.** Where a Cog's
worker runs, relative to the run controller, is one configuration value. At
`local` the worker is a process on the controller's host, running the Cog
package's `serve` task in the package's own environment, on a loopback port the
executor chooses. At `remote` it is a workload on a cluster. The lifecycle
runner drives both through the same executor interface and the same seam, and
one conformance suite holds every location to the same behaviour. An executor
holds no lifecycle logic. A `local` worker inherits nothing from its
controller but what the binding delivers, is reachable on loopback only, and
cannot outlive its controller: the launcher that starts it kills its process
group when the controller lets go of it or dies. Each worker has one *run
token*, minted when it is materialized and expired when it is torn down; only
its hash is recorded, on the run's Track, so the API and the controller can
both check it. `local` is for development and the desktop: a `local` worker
shares its controller's host and network identity, so the trust boundary of
ADR-0001 D7 does not hold for it, and a Kubernetes hub always runs `remote`.
*(Preserves ADR-0001 invariant 2, materialization behind the pluggable
executor, and D2, the seam as the only contract with a worker; builds on D5.)*

## Backends at a glance

| | `none` | `dbos` | `temporal` |
|---|---|---|---|
| Restart mid-step | the run ends `interrupted` | resumes | resumes |
| Run waiting at a Gate, then a restart | the run ends `interrupted` | resumes at the Gate | resumes at the Gate |
| Ownership across replicas | run pickup | DBOS queues | Temporal task queues |
| A step invoked again | keyed claim | keyed claim | keyed claim |
| Checkpoints | none | DBOS system database — Postgres on the hub, SQLite locally | Temporal persistence |
| Run status from | the Track | the Track | the Track |
| Extra infrastructure | none | none beyond Postgres or SQLite | a Temporal service |
| Local execution | yes, the default | yes, on SQLite | no |

## Invariants (enforce in review)

These add to ADR-0001's seven. Cite them as "ADR-0002 invariant N".

1. No lifecycle logic inside a durability backend. All backends call the same
   step functions, and a test proves it.
2. Run status is read from the Track — never from an engine's tables or
   history, never from process memory.
3. Only the run controller holds workload permissions. The public API never
   calls the executor, and an import-boundary test proves it.
4. The hub depends on the seam, never on the worker SDK or on a harness.
5. A change is runnable from `dev/`, asserted in CI at its level, and
   documented — in the same PR.
6. No lifecycle logic inside an executor, and `location` is the only switch
   between them: nothing imports an executor, and a test proves both.

## Amendments to ADR-0001

- **D1** — local execution is no longer only "not precluded": it is phased after
  the hub path, as D6 and D7 describe.
- **Orchestration & storage** — "a run survives a full restart with no manual
  recovery" holds under a durable backend (`dbos`, `temporal`). Under `none`, a
  restart ends in-flight runs `interrupted` (D2).
- **Out of scope / deferred** — *Local execution implementation* leaves the
  list.

Each amendment is marked in ADR-0001's text. Its decision and invariant numbers
do not change.

## Open decisions

To settle while this ADR is *Proposed*. Each becomes a decision here once
agreed.

1. **Default approvers** when a Gate declares none. *Proposal:* organization
   owners and platform operators.
2. **The hub's production durability backend.** *Proposal:* `dbos` — it reuses
   the Track's Postgres and adds no service. `none` stays the default for
   development, tests and local use.
3. **Gated Ops on `none`.** A run waiting at a Gate cannot survive a restart
   there. *Proposal:* allow it, with the submission response naming the
   backend, and select `dbos` in the chart's production values.
4. **Warm pool bounds** — the pool cap and the default idle timeout.
5. **Encryption at rest for grants' offline tokens** — Kubernetes Secrets with
   envelope encryption, or a KMS.
6. **Where the worker SDK lives.** *Proposal:* this repository, since the
   envelope it implements is the hub's capability list.
7. **Whether local Tracks ever sync to the hub.** *Proposal:* not as part of
   this work.

## Out of scope

- **Sensitivity enforcement** (#10, #12, #13) — still blocked on the rating
  scale; ADR-0001 D10 stands.
- **The registry and catalog** (#81–#87) — ADR-0001 D6 stands.
- **Credential delegation for unattended runs** — stays on ADR-0001's deferred
  list until its grant design is recorded.
