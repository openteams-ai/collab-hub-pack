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
[uv](https://docs.astral.sh/uv/), if you would rather drive it directly. Use
uv 0.13.4 or later: earlier releases still trust the compromised eMudhra CA
chain, so upgrade with `uv self update` (standalone installer) or your package
manager before running anything that reaches the network:

```sh
cd api
uv sync --group test        # install runtime + test deps
uv run pytest               # run the test suite
```

Tests that need a live Postgres skip unless `COLLAB_HUB_TEST_POSTGRES_URL`
points at a throwaway database (they drop and recreate every `collab_`
table). CI's `test` job starts a Postgres service and sets it, so those tests
run there.

The Cog packages ([`cogs/`](cogs/), [`dev/cogs/`](dev/cogs/) and the
[`examples/`](examples/)) are [pixi](https://pixi.sh/) projects and need pixi
0.82.1 or later, which their manifests state with `requires-pixi`; earlier
releases trust the same compromised CA chain. Update via `pixi self-update` or
the package manager you installed it with.

The tool versions CI installs each live in one file, so a bump is a one-line
change: uv in [`.github/uv.toml`](.github/uv.toml), pixi in
[`.github/pixi-version`](.github/pixi-version), and Node in the `.node-version`
beside each package that needs it ([`api/admin-ui/`](api/admin-ui/) and
[`scripts/release/semantic-release/`](scripts/release/semantic-release/)).
Dependabot bumps action and image versions, not these.

The admin panel is a Vite and React app in [`api/admin-ui/`](api/admin-ui/)
with its own Vitest suite. It needs Node 24.22.1 or later (CI pins 24.22.1):

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

Follow [the PR style guide](docs/pr-style-guide.md) for descriptions and squash
commit messages. Include applicable validation and review evidence below the
reviewer-context marker. Complete the applicable template checklist items
and explain any that do not apply.

- Open an issue first for anything non-trivial, and link it from the PR with a
  closing keyword (`Fixes #123`).
- CI (`lint`, `test`, including the `admin-ui` job) must pass. Add tests with your change: unit tests for new
  functionality, regression tests for bug fixes.
- A [code owner](.github/CODEOWNERS) must approve before merge. Take PRs out of
  draft before requesting code-owner review.
- User-facing UI changes include useful before/after screenshots.
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

PRs are squash-merged. Keep the Conventional Commits PR title as the subject
in the merge dialog. The merger must run `python3 scripts/strip_pr_body.py`
on the PR description and use its output as the squash commit body;
CLI mergers must pass the output with `--body-file`.
Preserve issue references, `BREAKING CHANGE:` footers, and revert metadata
as described in the [PR style guide](docs/pr-style-guide.md).

The squash commit is what the release reads. Every merge to
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
