# ADR-0001 — Cog & Op execution architecture

**Status:** Proposed
**Date:** 2026-08-20

> Companion reading: [`docs/cog-execution/`](../cog-execution/README.md) — the
> vocabulary, the Op–Cog seam, the result envelope, and the sensitivity model
> this ADR relies on. "A4" below is the current delivery milestone.

## Context

A4 needs to execute Cogs and Ops. The open tension was local vs. hub execution under constrained resourcing. The primary market is organizations running an Intelligence Hub, with Collab Desktop as the gateway. Two things must hold regardless: the architecture stays placement-agnostic even though we ship hub-first, and Cogs are treated as self-running workers, not opaque pipeline steps.

## Decisions

**D1 — Hub-first, placement-agnostic.** Ship hub-side execution — durable, multi-user, history-preserving. Do not bake in hub-only assumptions; the same Cog/Op must be able to run locally later. Local is deferred, not precluded. *Amended by [ADR-0002](0002-lifecycle-runner-durability-and-placement.md) D6–D7: local execution is phased after the hub path, embedding the same package in Collab's local host.*

**D2 — Interact, don't execute.** The runtime interacts with a Cog through its declared entry points; it never runs a Cog as an opaque step. Model and harness Cogs are materialized and reached; context Cogs bind to them. *(Invariant.)*

**D3 — Compose via Cog classes.** Classes — model, harness, context, complete — compose through `provides`/`requires` capability resolution. A non-technical author writes a context Cog that references default model/harness Cogs; the hub ships the defaults, so authors don't fill in environment or model detail.

**D4 — Everything-as-Cogs in the runtime, bounded for A4.** Harness Cogs, context Cogs, and complete Cogs all flow through one contract. The full desktop rearchitecture around Cogs is endorsed in principle but phased behind a flag — it does not gate A4.

**D5 — One lifecycle contract, pluggable executor.** Every class flows through one lifecycle: install → materialize → advertise capability → resolve → interact → observe → manage lifetime. Materialization sits behind a pluggable executor (Kubernetes is the default implementation); the orchestration engine sits behind its own interface. No raw cluster or engine primitives leak into orchestration.

**D6 — Nebi is the v1 distribution mechanism.** Publish Cogs via Nebi to an OCI registry, and index that registry into the catalog. Not every Cog must use Nebi, but v1 Cogs do.

**D6 amendment — Hub-owned discovery and install authorization (2026-10-08).**
For Hub-managed Cogs, the client-facing registry endpoint is the Hub host.
Discovery and installation use one Hub sign-in and one membership model;
users do not need a second account or a personal credential on the backing
registry. Nebi remains the distribution client, and an existing OCI registry
remains the backing store that the Hub indexes. Clients that persist a
registry login exchange their Hub session for a limited Hub-issued registry
credential, rather than storing the Hub access or refresh token.

The pull surface is read-only in [#181](https://github.com/nebari-dev/collab-hub-pack/pull/181).
Publishing through the same Hub endpoint is separate work in
[#182](https://github.com/nebari-dev/collab-hub-pack/pull/182), with its own
publish permission and repository-ownership decisions. The client contract
is the Hub host and Hub-issued authorization; transferring bytes through
registry software behind that endpoint later can preserve this contract.
That refactor is not a prerequisite for the pull implementation. Cross-Hub
sharing and public-registry federation remain separate decisions.

**D7 — Collab edits Ops; it does not build Cogs.** End users compose Ops from existing Cogs. Cog authoring is technical and out of A4 scope. The spec stays open so other clients could author Cogs and enter a future marketplace.

**D8 — One durable Track per run.** The hub preserves history, paused steps, and artifacts across an Op's lifetime; the desktop observes through signals (state, logs, artifacts, failures, timings, who started it). Run status derives from the Track, not from in-memory state.

**D9 — Manifests follow cog-spec as a compatible profile.** `COG.md` identity envelope → `cog.yaml` (the OpenTeams profile: entry points, io, model/harness `requires`, frames, connectors) → `pixi.toml` (environment) → `context/`. Our profile extends cog-spec[^3] rather than forking it; extensions are upstreamed. The `requires`/connector declarations stay structured objects — never flattened to strings — so the data catalog can attach source ratings to them later (D10).

**D10 — Sensitivity labels come from the data, not from Cogs.** An org sets a source's sensitivity by rating it in a data catalog (a hub service); a Cog declares only the sources it uses (D9), and a run inherits its effective label from the rated sources it binds. Sensitivity is multi-axis, combined as the strictest value on each axis. Resolution won't bind a provider below the run's label, and a label only drops through a declared downgrade that a Guard verifies and a Gate signs. A Cog with no declared sources, or an environment with no catalog, runs as it does today. The rating scheme and enforcement details are the environment's to define.[^1][^2]

## Invariants (enforce in review)

1. One lifecycle contract; Cog classes are declared data, never divergent code paths.
2. Materialization stays behind the pluggable executor — no raw cluster/engine calls in orchestration.
3. A tested lifecycle state machine with per-run budgets, timeouts, and warm/idle/teardown. This is the reliability core.
4. Capability negotiation (`provides`/`requires`) is the only glue; unavailability is signalled uniformly, never special-cased into the spec.
5. Interact, don't execute — the orchestrator never flattens a Cog into a generic DAG step. This is the single biggest risk to guard against.
6. The label is the data's, not the Cog's — computed from catalog ratings of the sources actually bound; resolution never binds a provider below a run's effective label, and every boundary decision is recorded on the Track.
7. Declared equals actual — rated sources and providers are reached only through hub-mediated paths and Cog runtimes are egress-restricted, so a Cog cannot bypass the label by dialing a source or provider directly.

## Orchestration & storage

The workflow engine and the executor are pluggable behind interfaces; the specific engine and registry are implementation choices recorded with the code, not fixed here. Durable Op state and the Track share one datastore so a run survives a full restart with no manual recovery. *Amended by [ADR-0002](0002-lifecycle-runner-durability-and-placement.md) D2: this holds under a durable backend (`dbos`, `temporal`); under `none`, a restart ends in-flight runs `interrupted`.*

## Out of scope / deferred

- **The data catalog & full sensitivity enforcement** — for A4, source ratings are maintained by hand until the catalog service exists (D10). The catalog service, flow-time label propagation, and the downgrade rule are deferred.
- **Usage & cost metering** — observability (token and cost attribution) that rides on Langfuse, tracked separately from Cog execution.
- **Credential delegation for unattended runs** — request-scoped auth does not survive the request; standing, revocable grants are needed. Top open technical risk.
- **Full desktop rearchitecture** around Cogs — phased behind a flag.
- ~~**Local execution implementation** — the architecture stays open (D1); implementation comes later.~~ *Removed by [ADR-0002](0002-lifecycle-runner-durability-and-placement.md) D6: phased after the hub path.*

[^1]: Hub planning decisions, 2026-08-25 — sensitivity levels agreed as an A4 goal; full enforcement and data-source registration deferred.
[^2]: [Sensitivity: labels born on data, declared rarely, enforced at the seam](../cog-execution/sensitivity.md) — source of the data-catalog model, the bind/flow-time seams, and the declared-downgrade rule.
[^3]: [cog-spec](https://github.com/openteams-ai/cog-spec/blob/main/SPEC.md) (private at time of writing) — the shared Cog specification our profile extends (the `COG.md` identity envelope, `kind` vocabulary, and capability strings).
