# Software Pack Maturity & Release Readiness

This document defines the maturity levels for Nebari **first-party** software packs and the requirements for promoting a pack between levels. It applies only to packs maintained by the Nebari core team. Community-contributed packs are out of scope.

Pack state is declared in a `pack-metadata.yaml` file at the root of each pack repo. The Nebari pack dashboard ([`nebari-dev/software-pack-dashboard`](https://github.com/nebari-dev/software-pack-dashboard)) aggregates these metadata files hourly and renders a single view of every tracked pack. That dashboard is the canonical place pre-sales engineers consult before demos.

## Maturity Levels

A pack moves through four sequential active levels. **Deprecated** is an orthogonal status that can apply to a pack at any level.

### Experimental
- **Audience:** Contributors only.
- **Promise:** None. May not install. May not work. May disappear.
- **Pre-sales behavior:** Do not demo. Do not mention to customers.

### Alpha
- **Audience:** Internal demos.
- **Promise:** Installs and runs the happy path on a current NIC dev cluster. Known limitations are documented.
- **Pre-sales behavior:** Can demo in customer meetings; must explicitly flag as early-stage and must not commit to availability or feature timelines.

### Beta
- **Audience:** Customer pilots.
- **Promise:** Stable enough for a customer to deploy in their own environment with engineering support. APIs and values may still change between releases.
- **Pre-sales behavior:** Can demo without caveat. Customer pilots may be offered with engineering involvement.

### GA (v1.0+)
- **Audience:** Production customers.
- **Promise:** Fully supported. Documented upgrade path between releases. We own the bug-fix and security-fix story.
- **Pre-sales behavior:** Pitch freely.

### Deprecated (orthogonal status)
A pack at any level can be marked Deprecated. A deprecated pack must:
- Set `deprecated: true` and `sunset_date: YYYY-MM-DD` in `pack-metadata.yaml`
- Have a `DEPRECATED.md` at the repo root with the migration path
- Show a deprecation banner at the top of `README.md`

The dashboard automatically moves deprecated packs to a separate table.

**Pre-sales behavior:** Do not pitch to new customers. Do not include in demos. Existing customers using the pack should be referred to the documented migration path.

## Pack Metadata File

Every tracked pack has a `pack-metadata.yaml` at its repo root. It is the source of truth for the pack's declared maturity level, ownership, and integration metadata. (Other dashboard columns - Description, latest release, last commit - are pulled from GitHub directly, not from this file. The Description column specifically uses the GitHub repo description.) The schema is owned by the dashboard repo (`nebari-dev/software-pack-dashboard/schema/pack-metadata.schema.json`) and may be validated locally:

```sh
check-jsonschema \
  --schemafile https://raw.githubusercontent.com/nebari-dev/software-pack-dashboard/main/schema/pack-metadata.schema.json \
  pack-metadata.yaml
```

Key fields the checklist depends on:

- `level` — the declared maturity level (`experimental` | `alpha` | `beta` | `ga`)
- `owner` — accountable engineer's GitHub username
- `product_owner` — required when `level: ga`
- `deprecated` + `sunset_date` — see Deprecated status above
- `nebariapp_integration` — `none` | `partial` | `full` | `na`
- `scope.standalone-supported` — `yes` | `no`
- `last_promoted_at` / `last_promoted_pr` — updated on every promotion
- `demo_notes` — current known gotchas, surfaced in the dashboard Notes column (first ~100 chars)

See the full schema in the dashboard repo for required fields, validation rules, and the canonical example.

### Scope Flags

Scope flags are declared in `pack-metadata.yaml` under the `scope:` key. Each flag affects which checklist items apply to a pack at each level.

- **`standalone-supported: yes | no`** — Whether the pack is intended to install and function without the Nebari Operator (i.e., as a plain Helm chart). If `no`, standalone-related items in the checklist do not apply at any level.

Additional scope flags will be added here as they emerge.

## Version Tagging Convention

Packs version releases with [EffVer](https://jacobtomlinson.dev/effver/) (Effort Versioning): `vMACRO.MESO.MICRO`. The three numbers describe how much work an upgrade costs the people consuming the pack, not how large the change was to build:

- **MICRO**: a drop-in. Bug fixes and additive features that need no action from existing users.
- **MESO**: some effort. A larger fix or a small breaking change that needs a little adoption work.
- **MACRO**: significant effort. A breaking overhaul; users should plan time to upgrade.

While a pack is pre-1.0, EffVer collapses to `0.MACRO.MICRO`: the leading `0` marks the in-development phase, a breaking or significant change bumps the middle number (`0.2.0`), and a drop-in fix bumps the last (`0.1.1`).

Tag rules:

- Always prefix the tag with `v` (`v0.1.0`, `v1.2.0`). Do not prefix with the repo name (`nebari-foo-pack-0.1.0`) or any other string. One repo, one tag scheme.
- Tags are three numeric segments; prereleases use a SemVer-style suffix (`-alpha.N`).

Maturity maps to the tag shape as follows. The declared `level` in `pack-metadata.yaml` remains the source of truth; the tag is expected to line up with it but does not define it.

| Level | Tag shape | Example |
|---|---|---|
| Experimental | usually unreleased; if tagged, a prerelease | `v0.1.0-alpha.1` |
| Alpha | prerelease `v0.1.0-alpha.N` | `v0.1.0-alpha.3` |
| Beta | bare `v0.x.y` (the whole `0.x` line) | `v0.2.1` |
| GA | `v1.0.0` and up | `v1.0.0` |

Beta is the entire `v0.x.y` line, not a single tag: once `v0.1.0` ships you keep versioning within `0.x` (a fix is `v0.1.1`, a breaking change is `v0.2.0`), and leaving `0.x` for `v1.0.0` is the deliberate "this is stable now" signal. Do not renumber an already-published higher version downward to match a label; correct the `level` field instead, or cut the next release at a corrected number.

## How to Use This Checklist

Each item below is tagged with the maturity level at which it becomes a **blocker** for promotion:

- `[E]` — Experimental
- `[A]` — Alpha
- `[B]` — Beta
- `[GA]` — GA / v1.0

To promote a pack from one level to the next, every item tagged with that level or earlier must be checked. Items tagged at later levels are encouraged but not required. Items that don't apply (e.g., auth items for a pack with no auth) should be marked **N/A** with a brief justification in the promotion PR.

## Promotion Process

A promotion is a PR in the pack repo that:

1. Updates `pack-metadata.yaml`:
   - `level` set to the new target
   - `last_promoted_at` set to the PR merge date
   - `last_promoted_pr` set to the PR number
   - `product_owner` set (required when promoting to `ga`)
2. Updates the pack's `README.md` with the new declared level
3. Has the required reviewers listed below

The dashboard regenerates hourly; no manual dashboard update is needed.

Required reviewers per promotion:

| From → To | Required reviewers |
|---|---|
| (new repo) → Experimental | None — default state |
| Experimental → Alpha | Pack owner + pre-sales rep |
| Alpha → Beta | Pack owner + pre-sales rep + tech lead |
| Beta → GA | Pack owner + pre-sales lead + tech lead + product owner |
| Active → Deprecated | Tech lead |

If "product owner" has not been named for a pack, the tech lead acts as product owner for promotion purposes, but this should be resolved before the next promotion attempt.

---

## The Checklist

### Ownership & Identity
- `[E]` Repo is created from the software pack template
- `[E]` `CODEOWNERS` names at least one accountable engineer
- `[E]` `pack-metadata.yaml` exists at the repo root, validates against the schema, and declares level + owner + scope flags
- `[E]` Pack is listed in `nebari-dev/software-pack-dashboard/tracked-packs.yaml`
- `[E]` README explains what the pack does and who it's for
- `[B]` `product_owner` field is populated in `pack-metadata.yaml` (may be the tech lead by default)

### Installation
- `[A]` Installs cleanly on a fresh current-release NIC dev cluster following only the README instructions
- `[A]` Prerequisites documented (NIC version, cluster sizing, namespace labels, external dependencies)
- `[B]` `helm lint` passes in CI
- `[B]` `helm template` renders correctly with NebariApp enabled and disabled
- `[B]` Schema validation passes (kubeconform or equivalent) in CI
- `[GA]` Integration test in CI against the full NIC stack (nebari-operator, Envoy Gateway, cert-manager, Keycloak)
- `[GA]` *If `standalone-supported: yes`:* Standalone install test in CI (`nebariapp.enabled=false`)

### NebariApp Integration
- `[A]` `NebariApp` reaches Ready condition with all applicable sub-conditions healthy (RoutingReady, TLSReady, AuthReady)
- `[A]` All configurable NebariApp fields used by the pack are documented in the pack's values reference
- `[A]` `nebariapp_integration` field in `pack-metadata.yaml` accurately reflects the integration depth (`none` | `partial` | `full` | `na`)
- `[B]` Auth-protected routes reject unauthenticated requests (if auth enabled)
- `[B]` Auth-protected routes allow authenticated users with correct group membership (if auth enabled)
- `[B]` Health/readiness probes configured and verified

### Documentation
- `[E]` README exists with: what the pack does, who it's for, deploy command
- `[A]` README includes prerequisites and a "Known Limitations" section
- `[B]` Authentication setup is documented (if applicable)
- `[B]` Troubleshooting section covers common failure modes
- `[B]` Upstream chart values that users need to customize are documented
- `[GA]` Performance and sizing guidance documented
- `[GA]` Documented upgrade path from the latest pre-1.0 release to 1.0
- `[GA]` `CHANGELOG.md` exists with notable changes summarized
- `[GA]` Docs site compliant with the [pack docs requirements](pack-docs-requirements.md), published at `packs.nebari.dev/<slug>/`

### Examples
- `[A]` At least one example values file that deploys without modification (other than hostname)
- `[B]` Example values file for full Nebari deployment (`nebari-values.yaml`)
- `[B]` *If `standalone-supported: yes`:* Example values file for standalone deployment (`standalone-values.yaml`)
- `[B]` ArgoCD Application example that references the published Helm repo

### Telemetry
- `[B]` `ServiceMonitor` or `PodMonitor` exposed, or documented justification for not exposing metrics
- `[B]` Application logs are written to stdout/stderr in a structured format (JSON preferred)
- `[GA]` Example dashboard or Grafana panel definition for the LGTM stack (if applicable)

### Security
- `[B]` Containers do not run as root (or have documented justification if they must)
- `[B]` No secrets hardcoded in templates or default values
- `[B]` OIDC scopes minimally scoped to what the app needs
- `[B]` Upstream container images pinned to a specific tag or digest (never `latest`)
- `[GA]` `securityContext` sets `readOnlyRootFilesystem`, `runAsNonRoot`, `allowPrivilegeEscalation: false` where possible
- `[GA]` `NetworkPolicy` or equivalent restricts unnecessary pod-to-pod communication (if applicable)

### Release Engineering
- `[B]` Chart is publishable to `oci://ghcr.io/openteams-ai/charts`
- `[B]` Release workflow is configured and at least one pre-1.0 release has been published
- `[B]` `appVersion` in `Chart.yaml` matches the upstream application version being wrapped
- `[GA]` Chart version is set to `1.0.0` and follows the [version tagging convention](#version-tagging-convention)
- `[GA]` Custom container images (if any) are published and accessible
- `[GA]` Chart publishes to `oci://ghcr.io/openteams-ai/charts` and is attached to a GitHub Release after release
- `[GA]` Upgrade smoke test in CI: `helm install` + `helm upgrade` succeeds without errors

### Pre-sales Verification
- `[A]` Pre-sales engineer has run the demo end-to-end and signed off on the happy path
- `[B]` Pre-sales engineer has confirmed the pack can be demoed without engineering on the call
- `[B]` `demo_notes` in `pack-metadata.yaml` reflects current known demo gotchas (or is empty if none)
- `[GA]` Pre-sales engineer has verified the demo flow on the GA release commit after release

### Sign-off
- `[A]` Pack owner approves the promotion PR
- `[A]` Pre-sales rep approves the promotion PR
- `[B]` Tech lead approves the promotion PR
- `[GA]` Product owner has documented and verified acceptance criteria
- `[GA]` Product owner approves the promotion PR

---

## What the Dashboard Surfaces Automatically

Once `pack-metadata.yaml` is populated and the pack is in `tracked-packs.yaml`, the dashboard will automatically flag:

- **`stale`** — no commits in 90 days (and not deprecated)
- **`no-product-owner`** — at GA but `product_owner` is null
- **`metadata-missing`** / **`metadata-invalid`** — the file is missing or fails schema validation
- **`repo-not-found`** — the pack repo could not be reached at all
- **`deprecated`** — pack is marked `deprecated: true`; it also moves to the Deprecated packs table

These are visibility flags, not formal blockers, but a pack with persistent flags is signaling that something in this checklist has decayed. Tech lead reviews flags weekly.

---

This checklist is the single source of truth for first-party pack maturity. The dashboard reflects each pack's currently declared level by reading `pack-metadata.yaml` from the pack's default branch.
