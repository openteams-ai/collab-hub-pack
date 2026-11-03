# Sensitivity: labels born on data, declared rarely, enforced at the seam

The model behind [ADR-0001](../adr/0001-cog-execution.md) D10 and
invariants 6–7, and the basis for issues #10–#13. Nothing here changes the
Cog manifest profile; it describes where sensitivity lives and who enforces
it.

## The question, and the shape of the answer

Where does "how sensitive is this Cog's work" fit in the flow? Two places,
and only two:

- **Bind time.** A Cog working with sensitive material constrains provider
  resolution: every satisfier bound to it must be rated to handle at least
  that level. An in-cluster model (weights on-prem, in-cluster auth) can
  carry a rating an external passthrough provider cannot.
- **Flow time.** The Op layer must know that a step's output carries a
  level, and match it as the process moves along: a downstream step may
  consume a label only if its own resolution is rated for it.

This is **locality generalized**. Locality — where a satisfier runs and
where data goes when it is used: in-cluster, on-prem, or an external
provider — is already a bind-time constraint that resolution enforces so an
organization's data stays where its policy says it may go. Sensitivity is a
second axis through exactly the same machinery — one more predicate in the
resolve step. Capability negotiation stays the only glue (ADR-0001
invariant 4); constraints are never special-cased into the spec.

## Labels originate in the data catalog, not in Cogs

The system needs a **data catalog**: the environment service that rates
data sources on the shared sensitivity scale. Labels are born on *data*.
The hub's brokered connectors are the natural first customers — already
enumerable, and exactly the things an organization would rate.

Consequence: **most Cogs never declare a level.** A general-purpose Cog is
sensitivity-polymorphic — it processes whatever flows in, and the
environment computes the effective label of a run from the catalog ratings
of the sources actually bound. The Cog states facts; the system computes
the label. A Cog never gets to call its output "public" when the system
knows the inputs came in confidential (the same principle as
ok-may-carry-problems: the Cog declares, Gates decide).

Optionality comes free under the existing extension discipline (every
extension degrades): a Cog with no sensitivity declarations behaves exactly
as today, and an environment with no catalog ignores labels entirely.
General-purpose Cogs get no heavier. The machinery engages only when a
labeled source enters a run — in practice, org-specific Cogs built against
particular systems. Those Cogs already say which sources they are designed
for: the `requires`/connector declarations, with the catalog supplying the
sources' ratings at bind time. That is why ADR-0001 D9 requires those
declarations to stay structured objects.

## What the manifest declares, when it declares anything

Only what the environment cannot infer. Three items, additive:

1. **Intended data sources** — already exists (`requires`, connectors).
   The catalog attaches ratings to these declarations.
2. **Declared downgrades on `produces`** — the load-bearing exception. An
   anonymizer or aggregator whose whole job is "confidential in, public
   out" declares the downgrade explicitly. A system-side Guard verifies the
   downgrade actually happened (for example PII detection on the output);
   a Gate puts a human signature on the declassification. Without this
   rule labels only ratchet up and every long Op ends at maximum
   sensitivity; with it, downgrading is visible, verified, and signed. The
   declaration is a *request*; it takes effect only after verification and
   signature.
3. **Optional per-requirement protection constraint** — a Cog that wants
   to insist on a satisfier tier regardless of what data shows up (a
   defensive floor for a Cog built to handle sensitive work).

## Enforcement at the seam

- **Bind time:** the hosting environment filters its satisfier inventory by
  rating before the Cog's `resolve` sees it; descriptors already carry the
  facts a rating derives from (locality, endpoint, provider, credential
  reference, retention terms). The **binding record** shows which rated
  satisfier met the constraint — the audit line that keeps a Gate
  signature meaningful. The plug point is built:
  `collab_hub_execution.binding.InventoryFilter`, which the installer
  applies to the inventory with the install being bound (its reference, its
  card) before the Cog's `resolve` sees it; `no_filter` is the only filter
  there is, and a rating filter (#10) replaces it without touching
  resolution, the record or delivery.
- **Catalog card:** carries the declared level and intended sources, so the
  Op builder's picker checks compositions *statically* — authoring time,
  not run time, is when you learn step 3 routes confidential output to a
  public-rated satisfier.
- **Result envelope:** carries the *effective* label of this run's output,
  beside the binding identity already in every result. The v1 envelope
  defines no label field yet; it arrives as an additive optional field with
  the flow-time work (#13), which the envelope's versioning rule allows
  without a version bump.
- **Propagation (the Op orchestrator's rule):** a step's output label is at
  least the strictest of its input labels on each axis, absent a declared,
  Guard-verified, Gate-signed downgrade; a step may consume a label only if
  its resolution is rated for it. **Tracks** record labels per step — the
  run's sensitivity lineage next to its binding lineage.
- **Declared equals actual (invariant 7):** none of the above holds if a
  Cog can dial a source or provider directly. Rated sources and providers
  are reached only through hub-mediated paths, and worker runtimes are
  egress-restricted.

## Who owns what

The spec **does not set values — it says how to declare them**
(declarations state; they never grant). The scale's ordering is shared
vocabulary so declarations can match; everything with meaning is
environment-owned.

| Layer | Owns |
|---|---|
| Manifest profile | Declaration shapes only: source declarations, downgrades, constraints |
| Data catalog | Ratings of actual sources, on the shared scale |
| Hub capability list | Tier semantics — what infrastructure qualifies at each tier |
| Resolution + binding record | Enforcement and evidence at bind time |
| Op orchestrator + Tracks | Propagation and lineage at flow time |
| Guards / Gates | Downgrade verification / human sign-off |

## Sequencing

Bind-time enforcement against a hand-maintained rating list comes first
(#10), as an `InventoryFilter`; the catalog service replaces the list (#12);
flow-time propagation and the downgrade rule follow (#13); egress
restriction (#11) is what makes any of it enforceable rather than advisory.
The rating scale itself is an open decision and should be settled before
#10 is built.
