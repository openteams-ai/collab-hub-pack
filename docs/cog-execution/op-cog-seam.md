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
the first form of talking to a running Cog. The [Worker SDK](../../worker/README.md)
maps a turn onto a harness as one interaction: each turn is one prompt to the
harness, answered with what it said, and a farewell ends the session.

## Frames in the task bundle

A task bundle may carry the step's Frame snapshots beside its input:
`POST /invoke` with `{"entry_point", "input", "idempotency_key", "frames":
[{"id", "text", "title"?, "uri"?, "digest"?}]}`. The hub snapshots a step's
Frames when the run is submitted and sends them here; a worker never calls the
Frames API. A worker on the SDK places them beside the first prompt, the way
its harness takes them — embedded resources for an ACP agent that advertises
`embeddedContext`, text otherwise, a context message for a direct model call —
and an input that cannot fit the bound model's context window with its Frames
fails the step `context-exhausted` ([the envelope](result-envelope.md)).

## The keyed claim

Every task bundle carries an `idempotency_key`, stable for one step attempt
(`{run}:{step}:{attempt}`, each component percent-escaped). A step can be
invoked again under that key after it already acted: a durable backend
resuming after a crash, or a person retrying an interrupted run. A worker
honours the key through the hub's claim routes, with its run token as the
bearer. The key travels in the request body, or the query, never the path, so
any step name fits:

| Route | What it does |
|---|---|
| `POST /v1/claims/reserve` `{"key": ...}` | Before acting. 201 with `yours: true`: the key is this worker's, act and then commit or release. 200 with `state: committed` and the `envelope`: answer with that envelope, and act on nothing. 200 with `state: outcome_unknown`: another worker reserved the key and never committed it, so its side effect may have happened; answer `ok: false` with `error.code: outcome-unknown` (HTTP 409), and act on nothing |
| `POST /v1/claims/commit` `{"key": ..., "envelope": ...}` | After acting, with the envelope `/invoke` is about to answer with. 200; 409 `claim_refused` for a key this worker does not hold; 422 for a body that is no envelope |
| `POST /v1/claims/release` `{"key": ...}` | When nothing acted under a key this worker reserved: the key is free again, and a replay reserves it afresh and runs the step. 204; 409 `claim_refused` for a key this worker does not hold, a committed one included |
| `GET /v1/claims?key=` | What became of the worker's own key; 404 for one never reserved |

**What is committed, and what is released.** The worker commits the envelope
when the entry point was called: it answered, or it failed on its own (a
`WorkerError` it raised, an exception). It commits nothing, and releases the
key, when nothing acted: the task was refused before the entry point was
called (no such entry point, a session already held, a harness that could
not start: `invalid-input`, `model-unavailable`, `binding-invalid`), or the
interaction was cancelled before it answered (the run was terminated, or the
worker stopped, while it ran). A replay of a released key runs the step; a
replay of a committed failure is answered with that failure, since the step
acted, and the run's retry runs it as a new attempt. A key the hub answered
`committed` or `outcome_unknown` to is neither committed nor released by the
worker that asked: it is not that worker's.

**Who reaches which key.** In order, and nothing is read before the first
three pass: the bearer is there (else 401 `run_token_required`); it has the
shape of a run token (else 401 `run_token_invalid`); the key has the shape of
an idempotency key naming a run (else 422 `claim_key_invalid`). The hub then
reads that run's Track directly and verifies the token against the hash
`worker_started` recorded, never asking the controller. A reserve takes a
worker that is up, on a run that has not ended. A commit, a release and a
read take only a worker whose step attempt has no outcome on the Track yet,
whether or not the worker is still up or the run was since recorded
`interrupted` or `cancelled`: a worker whose controller died keeps acting and
commits what it produced, and the retry of the interrupted run is answered
with it. Last, the key must be the one the worker's `worker_started` admits
it to, its own step attempt's: another step's key, or another attempt's, is
403 `claim_not_yours`, and a key of another run, or of no run, is 401 like an
unknown token, so the answer never says whether such a run exists. The routes
are for workers, which reach the API inside the cluster by its Service; an
installation need not expose `/v1/claims` on its ingress.

The hub is `COLLAB_HUB_URL` in the worker's environment; a worker given none
claims nothing and acts on every invocation. A hub that cannot be reached for
the reservation fails the step `hub-unavailable` (HTTP 503) before anything
acts; a commit or a release the hub does not take is logged, and the envelope
answered all the same, since the controller records it. The claim narrows the
window in which a lost worker's side effect is unaccounted for to the gap
between reserve and commit, and makes it visible as `outcome-unknown`; it
does not pretend to close it. Such a step is never retried by the run API: a
platform operator settles what happened and records it (`POST
/v1/runs/{id}/steps/{step}/reconcile`, `collab-hub run reconcile`; the
`outcome_reconciled` event on the Track, and the claim `recorded`), before the
run is retried as a new attempt.

The [Worker SDK](../../worker/README.md) does all of this for every Cog built
on it, and `dev/cogs/fake_worker.py` is the reference implementation for a
worker that is not (its `handle` always acts, so it never releases). The
hub's side is `collab_hub_execution.claims`: three stores, in memory, SQLite
beside the SQLite Track and Postgres beside the hub's, under one conformance
suite, and the step attempt machine of [states.md](states.md) deciding every
move.

## The two sides already fit

| Op factory concept | Cog-side implementation |
|---|---|
| Frames (org context in) | Task-bundle context; notes; system context files |
| Guards (checks on output) | System-side verifiers consuming the envelope; the Cog's own contract checks (grounding, schema, injection canaries) arrive as `problems` |
| Gates (human sign-off on flagged work) | The three-state result (ok / ok-with-problems / error) is exactly what a gate consumes |
| Tracks (durable evidence) | Binding record + activity journal: which pinned model, which weights, which evidence, which problems, per run |
| Data sources per step | Cog-declared requirements + environment connectors |
| Catalog of workers | `cog.yaml` → catalog card |

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
