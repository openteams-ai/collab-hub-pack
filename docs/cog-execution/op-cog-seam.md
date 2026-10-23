# The Op–Cog seam

Focus the hub's build on the Op factory, and couple it to Cogs through a
deliberately narrow interface. This note records why the boundary sits
where it does; [ADR-0001](../adr/0001-cog-execution.md) records the
decisions that depend on it.

## What the evidence says

Three artifacts, built independently, agree about the boundary:

- **The Ops Factory prototype.** Its entire Cog model is `{id, name,
  function, description, defaultFrames, skills}` — an opaque named worker.
  All the richness is Op-side: steps with an expected outcome, frames, data
  sources, and gates (guards + reviewer Cogs + a named human). The Op layer
  never looks inside a Cog.
- **The Nebi-installable Cog.** A pixi-packaged Cog carrying its own
  lightweight harness. `nebi pull` copies files; `nebi run` executes a task.
  Running one task of one Cog with a prompt *is* an Op — the smallest one.
- **The reference Cogs and the workbench.** Packages that run themselves,
  expose declared entry points, verify model identity, ground their outputs
  in quoted evidence, and journal every run.

The constraints from the spec sync bound the design space: the harness
lives *in* the Cog and is not specified by the hub ("never say a Cog must
use this harness"); a Cog is "not much more than the package spec of what's
needed to run this"; hosting environments publish their own compatibility
requirements (the capability list); Cogs won't run everywhere, and that's
fine.

## Bundled ops

A Cog's entry points have two audiences:

- **Usage entry points** (`ask`, `chat`, build-a-bundle-and-ask): what end
  users and the Op layer invoke. These are the Cog's **bundled ops** —
  single-Cog, zero-gate ops shipped in the package — and they appear in the
  catalog card.
- **Lifecycle entry points** (`resolve`, `serve`, `check`, `eval`): how a
  hosting environment installs, binds, starts, and verifies the Cog. These
  are not ops — nothing a person kicks off or composes into steps — and the
  Op layer never sees them.

This makes the Op factory's job precise: **compose bundled ops into steps,
and wrap Frames, Guards, Gates, and Tracks around them.** `nebi run cog`
and a five-step proposal Op with human sign-off are the same concept at two
scales.

## The seam: four items

Everything an Op step needs from a Cog:

1. **An invokable task entry point** — POST a task bundle (context in), get
   a result. This is the usage entry point, the bundled op.
2. **A result envelope** — a checkable payload plus a problems list. Guards
   check the payload; Gates read the problems; "passed with integrity
   problems" is rendered for the human, not swallowed. See
   [the envelope](result-envelope.md).
3. **A health probe** — is this Cog able to take work right now.
4. **A catalog card** — name, function, io, and its usage entry points,
   derivable from the manifest. This is how Cogs appear in the Op builder's picker.

Nothing else crosses the seam. Harness, model, weights, pixi environments,
binding machinery — all stay inside the Cog, and all remain swappable
without the Op layer noticing.

## Sessions: an entry point that stays open

An entry point may hold a session instead of answering at once. Its
`/invoke` stays open for as long as the session lasts, and while it does the
worker answers **turns**: `POST /turn` with `{"turn": id, "text": ...}`,
bearing the run token like every request from the controller, answered with
`{"text": ...}`. The session ends when the entry point's `/invoke` returns its
envelope — the Cog decides when, `hello` on `bye` — or when the run is
terminated and the worker torn down. A worker with no session open answers
`/turn` with 404; the controller retries a turn so answered for a short grace,
since a worker may be invoked and not yet have opened its session.

Turns come only from the run controller, which takes them from the run's
Track, so a client never reaches a worker and every turn is recorded. This is
the first form of talking to a running Cog; how a harness Cog maps turns onto
its own agent loop is the Worker SDK's to define.

## The two sides already fit

| Op factory concept | Cog-side implementation |
|---|---|
| Frames (org context in) | Task-bundle context; notes; system context files |
| Guards (checks on output) | System-side verifiers consuming the envelope; the Cog's own contract checks (grounding, schema, injection canaries) arrive as `problems` |
| Gates (human sign-off on flagged work) | The three-state result (ok / ok-with-problems / error) is exactly what a gate consumes |
| Tracks (durable evidence) | Binding record + activity journal: which pinned model, which weights, which evidence, which problems, per run |
| Data sources per step | Cog-declared requirements + environment connectors |
| Catalog of workers | Manifest (`[tool.cog]` in `pixi.toml`) → catalog card |

## The principle: loose coupling, strict semantics

Loose coupling: the Op layer sees only the four-item seam, so the hub can
build the Op factory now, against Cogs it treats as services — no shared
runtime, no harness spec, no reaching into packages.

Strict semantics: what makes a Gate signature worth anything is that the
Track can say *which* model, *which* weights, *which* evidence produced the
thing the human signed. That rigor (identity verification, pinning,
grounded quotes) lives inside the Cog package and is invisible at the seam
— but it is why the seam can stay so small. A Cog that is "just a prompt
with a name" would make the gates theater. Loose interface, never loose
identity.

## Who builds what

- **The hub:** the Op builder (natural-language drafting, human sign-off
  required per gate), the durable Op orchestrator, and the Guard/Gate/Track
  surfaces. Integrates with Cogs only through the seam.
- **Cogs:** provide the four items. The remaining work is packaging so Cogs
  are installable where the orchestrator can reach them.
- **The hosting environment:** owns the lifecycle entry points and publishes
  its capability list — the shape a Cog must have to be runnable *there*.
  Environment-owned, so it needs no universal ratification.
- **The registry/catalog:** distribution and discovery; its catalog feeds
  the Op builder's picker.

## What this defers, deliberately

No harness spec. No universal envelope ratification — the envelope's
contents are *requirements-driven*: it must carry what Guards check and
Tracks record, which is a finite list. No local-vs-hub decision baked into
packages — a Cog never encodes where it runs; local execution stays
possible for free as long as the orchestrator only ever touches the seam.

## Rules for orchestrator code

- The Op orchestrator integrates with Cogs exclusively via their declared
  task entry points; it never executes Cog internals and never manages a
  Cog's environment beyond invoking its lifecycle entry points.
- Cogs run themselves: the hub *hosts* Cogs (lifecycle entry points) and
  *invokes* them (usage entry points). "Run a Cog" in orchestrator code
  should always mean one of those two verbs.
- A step's gate consumes the Cog's result envelope (payload + problems);
  Guard failures and integrity problems are surfaced to the gate, never
  retried silently. The gate is declared on the step; the Cog does not
  implement approval loops, and it cannot pause a run — an answer asking to
  pause is not an envelope, and fails the step.
- Hub-specific requirements on Cogs (envelope shape, io values, entry-point
  forms) live in the hub's published capability list, not in the Cog spec.
- Model Cogs are shared services that context Cogs bind to via resolution;
  substituting the hub's model for a Cog's carried default is dependency
  re-satisfaction, recorded in the Cog's binding record.
