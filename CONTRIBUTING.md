# Contributing to Collab Hub API

Thanks for your interest in contributing. This pack is developed in the open
under the [Apache-2.0 license](LICENSE).

## Development setup

Run the pack from [`dev/`](dev/), which starts whatever a given level needs and
sets the environment for you:

```sh
cd dev
make help
make api      # the API alone: no containers, no token needed
make test     # the API test suite
make lint     # helm lint + kubeconform on the rendered manifests
```

There are four levels, from a bare process up to the chart on a kind cluster;
[`dev/README.md`](dev/README.md) walks through them and through the Keycloak
setup each connector needs. CI exercises all four —
[`.github/workflows/dev-env.yaml`](.github/workflows/dev-env.yaml).

The API itself lives in [`api/`](api/) and uses
[uv](https://docs.astral.sh/uv/), if you would rather drive it directly:

```sh
cd api
uv sync --group test        # install runtime + test deps
uv run pytest               # run the test suite
```

Tests that need a live Postgres skip unless `COLLAB_HUB_TEST_POSTGRES_URL`
points at a throwaway database (they drop and recreate every `collab_`
table). CI's `test` job starts a Postgres service and sets it, so those tests
run there.

The admin panel is a Vite and React app in [`api/admin-ui/`](api/admin-ui/)
with its own Vitest suite. It needs Node (CI uses Node 24):

```sh
cd api/admin-ui
npm ci --ignore-scripts     # install exactly what package-lock.json pins
npm test                    # run the Vitest suite
npm run typecheck           # tsc, as CI runs it
```

The API supports **Python 3.13 and later**. `api/.python-version` pins 3.14,
the version the image runs, and CI runs the suite on 3.13 as well
(`Test (Python 3.13)`). To reproduce that job, prefix the commands with
`UV_PYTHON=3.13`. Keep code valid on 3.13: ruff targets `py313` and reports
newer syntax.

Running it by hand needs **all three** dev-auth switches —
`FRAMES_UNSAFE_AUTH_ENABLED=true`, `DEV_AUTH_ENABLED=true` and
`DEV_AUTH_USER=<name>`. Setting only `DEV_AUTH_USER` authenticates nothing and
every route answers 401, which is why `make api` is the easier path.

The Helm chart is in [`helm/collab-hub/`](helm/collab-hub/):

```sh
helm lint helm/collab-hub
helm template helm/collab-hub | kubeconform -strict -ignore-missing-schemas -
```

## Pull requests

- Open an issue first for anything non-trivial, and link it from the PR with a
  closing keyword (`Fixes #123`).
- CI (`lint`, `test`, including the `admin-ui` job) must pass. Add tests with your change: unit tests for new
  functionality, regression tests for bug fixes.
- A [code owner](.github/CODEOWNERS) must approve before merge. Take PRs out of
  draft before requesting code-owner review.
- Keep the change focused; describe *how* it addresses the issue.
- Work that lands on `main` before it's ready to expose goes behind a feature
  flag: declare the flag, gate the code on it, and retire it once the feature
  ships. [Feature flags](docs/feature-flags.md) covers how.
- Cog execution changes follow two same-PR rules from
  [ADR-0002](docs/adr/0002-lifecycle-runner-durability-and-placement.md):
  make what you add runnable from [`dev/`](dev/README.md#running-cogs-and-ops)
  at the lowest level that can host it, with a CI assertion at that level
  (D9); and update every document the change makes stale, naming them in the
  PR description (D10).

## PR titles and releases

PRs are squash-merged, and the squash commit takes the PR title as its
subject and the PR description as its body. Leave both as they are in the
merge dialog: the squash commit is what the release reads. Every merge to
`main` whose title calls for a release is released automatically, so the
title decides the version.

Titles follow [Conventional Commits 1.0.0](https://www.conventionalcommits.org/en/v1.0.0/):
`type(scope): description`, for example `fix(connectors): retry a Slack
token refresh once`. The `PR Title` check enforces the format. The scope is
optional and names the area the change touches, such as `connectors`,
`config` or `deps`.

| Type | Release |
| --- | --- |
| `feat` | minor (0.2.0 to 0.3.0) |
| `fix`, `perf` | patch (0.2.0 to 0.2.1) |
| `refactor`, `docs`, `test`, `build`, `ci`, `chore`, `style` | none |
| `revert` | none, unless the title has no scope (`revert: ...`) and the description keeps git's `This reverts commit <sha>.` line, which is a patch |

A `!` after the type or scope (`feat!: ...`), or a line in the PR description
that starts with `BREAKING CHANGE:`, marks a breaking change. Before 1.0 it is
a minor release, so neither can move the pack to 1.0.0 by accident; that step
is a deliberate change to `.releaserc.json`. A title the check would reject
releases nothing, so a fix merged under one waits for the next releasing merge.

Fix forward rather than reverting. GitHub's Revert button titles its PR
`Revert "<original title>"`, which the check rejects.

Versions follow [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html).
The chart, the API and the CLI share one version. On `main`, it's a
development placeholder in `helm/collab-hub/Chart.yaml` (`0.0.0-dev`) and in
`api/pyproject.toml`, `cli/pyproject.toml` and the CLI's `__version__`
(`0.0.0.dev0`, as Python spells it), and a build from `main` reports it. A release never writes to `main`:
[`semantic-release.yml`](.github/workflows/semantic-release.yml) tags the
merged commit `v<version>`, pins the version into those files in a commit
that exists only under the tag `collab-hub-<version>`, and builds the image
and the chart from that tag. To work with a released version, check out
its `collab-hub-<version>` tag.

## Reporting security issues

Do not open a public issue for vulnerabilities. Use GitHub's
[Report a vulnerability](https://github.com/openteams-ai/collab-hub-pack/security/advisories/new)
to open a private advisory with the maintainers.
