# Cog execution — read this first

This directory is the basis for the hub's Cog and Op execution work: the
vocabulary, the boundary between the hub and a Cog, and the contracts that
cross it. Read it before picking up any issue labeled `cog-execution`, and
check work against it in review. The decisions themselves are recorded in
[ADR-0001](../adr/0001-cog-execution.md) and
[ADR-0002](../adr/0002-lifecycle-runner-durability-and-placement.md); this
directory explains the terms and the seam the ADRs assume.

Contents:

- **This page** — the seam in one paragraph, what stays inside a Cog, a
  review checklist, and how the open issues map to the ADR.
- [The glossary](../GLOSSARY.md) — every term used in the issues, the ADR,
  and these docs, defined in one place with citations.
- [The Op–Cog seam](op-cog-seam.md) — the four things an Op step needs from
  a Cog, and why nothing else crosses.
- [The result envelope](result-envelope.md) — the shape a Cog's entry point
  returns; what Guards check, Gates read, and Tracks record.
- [The Track](track.md) — event schema v1: what each event carries, payloads
  by reference, the three stores, and how a Track written before v1 is read.
- [States](states.md) — the four state machines (install, worker, step
  attempt, run): every state, every transition, and what each records.
- [Running Ops](runs.md) — the durability backends, what each run status
  means, what a restart does under `none`, and cancelling a run.
- [Running a Cog from the CLI](../cog-execution.md) — installing the
  `collab-hub` CLI from `main`, signing in to a hub, and launching, listing,
  talking to and stopping a Cog.
- [Sensitivity](sensitivity.md) — how data-sensitivity labels are born,
  propagated, and enforced (the basis of ADR-0001 D10).

## Vocabulary

These terms are used precisely throughout the issues, the ADR, and the
code, and they all live in **[the glossary](../GLOSSARY.md)** — one page,
every term, each entry citing the whitepaper section or ADR decision that
defines it. When a word there has a narrower meaning than its everyday
one, the narrower meaning is the one intended. Read the glossary before
the rest of this directory; the notes below assume its vocabulary
(seam, envelope, worker, binding record, Guard vs contract check).

## The seam in one paragraph

An Op step needs exactly four things from a Cog: an invokable task entry
point, a result envelope, a health probe, and a catalog card. Nothing else
crosses. Harness, model, weights, environment, binding machinery — all stay
inside the Cog and remain swappable without the Op layer noticing. This is
what lets the hub build the Op factory against Cogs it treats as services,
and what lets the same Cog run locally later (ADR-0001 D1). The full
argument is in [the seam note](op-cog-seam.md).

## What stays inside a Cog

The hub must not build or own any of these, and a design that needs the
hub to is drifting:

- the Cog's runtime and pinned environment (the package *is* the runtime;
  the hub runs it, it does not build a generic image and inject the Cog's
  context into it);
- the harness — the render → call → parse → check loop around the model;
- the model client and how the Cog authenticates to its bound model;
- resolution logic — the hub supplies the satisfier *inventory* and records
  the *result*; the Cog's `resolve` entry point selects;
- contract checks on the Cog's own output;
- any notion of approval, pause, or revise loop — those are Op-layer
  policy, declared on the step and evaluated by the engine from the
  envelope.

## Review checklist

Ask these of every `cog-execution` change; each maps to an ADR invariant or
decision, or a seam rule. A bare *Invariant N* is ADR-0001's.

1. Does the orchestrator touch the Cog only through declared entry points?
   (Invariant 5.) A step that reaches into a package, builds a prompt for
   the Cog, or supplies its model client has flattened the Cog into a DAG
   step.
2. Are cluster and engine primitives confined to the executor and engine
   implementations? (Invariant 2.)
3. Who decides a pause? If the answer is "the Cog," the gate has moved to
   the wrong side of the seam.
4. Does the Track entry for a step carry the binding identity and, for gate
   decisions, the actor? If not, the Track cannot answer "what produced
   this?" or "who signed?"
5. Is the worker's return an envelope (`ok`, `payload`, `problems`,
   `binding`) rather than an ad hoc `{output, …}`?
6. Do credentials appear only by reference? Does the worker carry no
   user credential and no cluster credential it does not need?
7. Are `requires`/connector declarations still structured objects?
   (D9.)
8. Does an unknown or unavailable capability degrade uniformly rather than
   being special-cased? (Invariant 4.)
9. Does any lifecycle logic live in a durability backend? Would the change
   behave differently under `none`, `dbos` or `temporal` in anything but
   what survives a restart? (ADR-0002 invariant 1.)
10. Is run status read from anywhere but the Track? (ADR-0002 invariant 2.)
11. Can the public API reach the executor, or does anything besides the run
    controller hold workload permissions? (ADR-0002 invariant 3.)
12. Is it harness-neutral — does the hub side depend only on the seam,
    never on the worker SDK or a particular harness? (ADR-0002 invariant 4.)
13. Is what it adds runnable from `dev/` at the lowest level that can host
    it, and asserted in CI at that level? (ADR-0002 invariant 5, D9.)
14. Which documents did it make stale, and are they updated in the same PR?
    (ADR-0002 invariant 5, D10.)
15. Does every change of state go through its machine, and does
    [states](states.md) still match the code? (ADR-0002 D11.)

## How the open issues map to the ADR

Bare decision and invariant numbers are ADR-0001's.

| Issue | Decision / invariant | Note |
|---|---|---|
| #1 worker lifecycle on the hub | D5, invariants 1–3; ADR-0002 D1, D4 | *Install* vs *materialize* — see vocabulary. Depends on #7 for the artifact to materialize from. Runs advance in the run controller, which alone owns the executor; a step executed again within the same attempt does not repeat its side effect. |
| #2 durable Op engine | D2, D5, D8, invariant 5; ADR-0002 D1–D3, D8 | One lifecycle runner with a durability backend (`none`, `dbos`, `temporal`). Gates are declared on the step; recovery must not depend on a caller re-submitting. |
| #3 model binding | D3, invariant 4 | The hub offers the inventory; the Cog's `resolve` selects; the binding record is the output. |
| #4 budgets and idle workers | invariant 3 | Duration is a hard pre-check; token/cost is post-interaction accounting. |
| #5 durable Track | D8; ADR-0002 D3 | Event schema v1 ([track](track.md)): binding identity per step, actor per gate decision, a bounded failure record with the key and worker; payloads by reference; in-memory, SQLite and Postgres stores under one conformance suite; the hub's tables from migration 12. Catalog persistence belongs to #7. |
| #6 the remote location and least-privilege RBAC | invariant 2; ADR-0002 D4, invariant 3 | `location: remote` puts the Kubernetes executor behind the switch #109 adds; a namespace-scoped grant to the controller only; workers carry no ServiceAccount token. |
| #7 registry and catalog | D6, D9 | The catalog card derives from the manifest; index the full profile as declared. |
| #8 delegated connector access | "deferred" list | The hub's brokered connectors act as the user; no credential enters the worker. |
| #9 Guards | seam item 2 | Guards consume the envelope; a Cog's contract checks are inputs to Guards, not Guards. Gates decide. |
| #10–#13 sensitivity | D9, D10, invariants 6–7 | See [sensitivity](sensitivity.md). Blocked on the rating scale. |
| #98 result envelope from workers | seam item 2 | Workers return the envelope; the engine stops reading `output`. |
| #99 Gates on Op steps | ADR-0002 D8 | A Cog can no longer pause a run. |
| #100 extract the lifecycle runner | ADR-0002 D1 | A refactor with no behaviour change. |
| #101 the `none` backend | ADR-0002 D1–D3, invariants 1–2 | In-flight runs end `interrupted` after a restart and are never resumed. |
| #102 keyed claim | ADR-0002 D1 | One attempt acts once; a recorded failure retries under a new key. |
| #103 run API | ADR-0002 D4, D7, invariant 3 | The API records intent and reads the Track; it never calls the executor. |
| #177 a Cog launched from the CLI on a local dev hub | ADR-0002 D4, D9, invariant 3 | `examples/cog-local`, on the first form of the run controller, the run API (behind `cog_runs`) and the CLI's launch and run commands; #121, #103 and #126 build each out. |
| #121 run controller and run pickup | ADR-0002 D4, invariant 3 | Runs advance in the controller, which alone constructs an executor; pickup under `none`. |
| #104 DBOS backend | ADR-0002 D1, D3 | Resumes after a restart; status still comes from the Track. |
| #105 materialize from the artifact | D5, invariant 2 | The worker runs the Cog's own `serve`. |
| #106 install by digest | D5 | Install runs `check` once; materialize happens per run. |
| #107 worker SDK | ADR-0002 D5, invariant 4 | Optional for Cogs; the hub never imports it. |
| #108 Hermes harness Cog | ADR-0002 D5 | The first harness Cog, built on the worker SDK. |
| #109 agent location | ADR-0002 D12, invariant 6 | `location`, `local` or `remote`, beside `backend`: a process on the controller's host first, built; a pod (#6) behind the same switch. A worker's run token is minted here. |
| #110 Temporal backend | ADR-0002 D1, D3 | The same conformance suites as the other backends. |
| #125 the `collab-hub` CLI | ADR-0002 D4, D7 | A client over the REST API, authentication first; it imports no hub package. |
| #126 CLI run commands | ADR-0002 D4, D7 | Every run API endpoint behind a command; later work adds its own commands, not another client. |
| #130 state machines | ADR-0001 invariant 3; ADR-0002 D3, D11 | One machine per level (install, worker, step attempt, run) on the state pattern; run status is the Track replayed through the run machine. |
