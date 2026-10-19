# Collab Hub

![Status: Beta](https://img.shields.io/badge/status-beta-orange)
[![Lint](https://github.com/nebari-dev/collab-hub-pack/actions/workflows/lint.yaml/badge.svg)](https://github.com/nebari-dev/collab-hub-pack/actions/workflows/lint.yaml)
[![Test](https://github.com/nebari-dev/collab-hub-pack/actions/workflows/test.yaml/badge.svg)](https://github.com/nebari-dev/collab-hub-pack/actions/workflows/test.yaml)
[![Dev Environment](https://github.com/nebari-dev/collab-hub-pack/actions/workflows/dev-env.yaml/badge.svg)](https://github.com/nebari-dev/collab-hub-pack/actions/workflows/dev-env.yaml)

> **Beta** — stable enough to deploy in your own environment with engineering
> support. APIs and chart values may still change between releases. See the
> [maturity levels](docs/release-readiness-checklist.md).

The intelligence and integration backend for AI-assisted collaboration on
[Nebari](https://nebari.dev). Collab Hub is a [Nebari software
pack](https://github.com/nebari-dev/software-pack-template): a Kubernetes
application that installs on a Nebari cluster with routing, TLS, and OIDC
authentication.

The API is a FastAPI service exposing:

- **Frames** — workspace-scoped saved Markdown context, suggestions, and
  active-frame selection.
- **Connectors** — read access to workspace tools: Slack, Gmail, Google
  Calendar, and Google Drive.
- **User directory** — org/workspace identity resolved from Keycloak.
- **Scheduled tasks**, **usage**, and an **MCP server** exposing the above to
  MCP-speaking clients.
- **Admin panel**: a browser app at `/admin` where operators manage model
  access, operator roles, connectors and invitations, and read hub usage and
  the audit log.

## Prerequisites

- A [Nebari](https://nebari.dev) cluster with the
  [nebari-operator](https://github.com/nebari-dev/nebari-operator) (for the
  `NebariApp` routing/TLS/OIDC integration), or a plain Kubernetes cluster if
  you wire routing yourself.
- [Helm 3](https://helm.sh/docs/intro/install/) and `kubectl`.
- A Keycloak realm for OIDC auth.
- Optional, for persistence: Frame content storage defaults to the local
  filesystem but can use an S3-compatible object store; state/history/tasks
  default to in-memory and can be backed by a Postgres database.

## Quickstart

Install the versioned chart from the latest [release](../../releases/latest)
(pinned to an immutable image):

```sh
helm install collab-hub-pack \
  https://github.com/nebari-dev/collab-hub-pack/releases/download/v0.1.0/collab-hub-0.1.0.tgz \
  --namespace nebari-nexus --create-namespace \
  --values values-example.yaml
```

Or track `main` from a checkout: `helm install collab-hub-pack ./helm/collab-hub …`.

Start from [`values-example.yaml`](values-example.yaml); the full value
reference is [`helm/collab-hub/values.yaml`](helm/collab-hub/values.yaml) and
setup guides are in [`docs/`](docs/) (connectors, standalone deployment, the
web surface).

## Architecture

```mermaid
flowchart LR
  client[Apollo desktop / MCP client] -->|OIDC bearer| api[Collab Hub API]
  api --> frames[Frames store<br/>S3 / Postgres]
  api --> conn[Connectors<br/>Slack / Google]
  api --> dir[User directory<br/>Keycloak]
  api -->|records intent,<br/>reads status| track[(Track<br/>Postgres / SQLite)]
  controller[Run controllers<br/>one or more] -->|picks up runs,<br/>records progress| track
  controller -->|starts and tears down| workers[Cog workers<br/>local processes]
```

The API never calls a run controller and never starts a worker: it records what
a client asked for on the Track, and the controllers act on it. Of several
controllers, one picks up each run
([`docs/cog-execution/runs.md`](docs/cog-execution/runs.md#the-run-controller-and-the-run-api)).

## Known limitations

- **The `none` durability backend does not survive a restart.** Cog execution
  runs Ops on `none` today: nothing is checkpointed, so a run in flight — or
  waiting at a Gate — when its host stops is recorded `interrupted` when a host
  starts again, and continues only when someone retries it. `dbos` (#104) is
  the backend that resumes runs; see
  [`docs/cog-execution/runs.md`](docs/cog-execution/runs.md).
- **A deployed hub launches no Cog yet.** Every hub serves the run API
  (`/v1/runs`) on its own database, and the image ships the run controller
  (`collab-hub-run-controller`), but the chart runs no controller and gives the
  API no Cog packages: workers run as local processes beside a controller, which
  is for development. See [`examples/cog-local`](examples/cog-local/README.md).

## Local development

To see a Cog run before anything else, [`examples/cog-local`](examples/cog-local/README.md)
launches one from the `collab-hub` CLI on your own machine, with no container.

Everything is driven from [`dev/`](dev/), which runs the pack on your machine
in four levels — from a bare process with no dependencies, up to real
datastores, a real identity provider, and the chart on a kind cluster:

```sh
cd dev
make help
make api      # the API alone: no containers, no token needed
```

The full walkthrough, including the Keycloak setup each connector needs, is
[`dev/README.md`](dev/README.md).

The dev-auth shortcut needs **all three** of `FRAMES_UNSAFE_AUTH_ENABLED`,
`DEV_AUTH_ENABLED` and `DEV_AUTH_USER`; setting only the last authenticates
nothing and every route answers 401. `make api` sets them for you.

To run the test suites:

```sh
cd api
uv sync --group test
uv run pytest                                # the API (Python)

cd admin-ui
npm ci --ignore-scripts && npm test          # the admin panel (Vitest)
```

CI runs both: the `test` job runs pytest against a real Postgres service, and
the `admin-ui` job runs `npm ci`, `npm audit`, the typecheck, Vitest and the
production build.

## Documentation

Setup and reference docs — connector setup, Frames, operations, the
[Cog registry](docs/cog-registry.md), [feature flags](docs/feature-flags.md) — live in [`docs/`](docs/).

The basis for Cog and Op execution — vocabulary, the Op–Cog seam, the
result envelope, the sensitivity model — is in
[`docs/cog-execution/`](docs/cog-execution/); the decisions behind it are
recorded in [`docs/adr/`](docs/adr/).

The [`collab-hub` CLI](cli/README.md) is a terminal client for the hub's REST
API: it signs in the way the Collab desktop does, and lists the Cogs the hub
offers.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Contributions are accepted under the
[Apache-2.0 license](LICENSE). Report
security issues privately via GitHub's
[Report a vulnerability](https://github.com/nebari-dev/collab-hub-pack/security/advisories/new).
