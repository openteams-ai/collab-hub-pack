# Running a Cog on a hub, from the `collab-hub` CLI

How to install the `collab-hub` CLI from this repository's `main` branch, sign
in to a hub, and take a Cog through its whole life: list what the hub can
launch, launch it, see it running, talk to it, and stop it. These are the
commands behind the `make` targets of
[`examples/cog-local`](../examples/cog-local/README.md), which runs the same
steps against a hub on your own machine.

`<HUB_URL>` below is the hub's address, such as `https://hub.example.com`; keep
the quotes around it in a command, or the shell reads `<` and `>` as
redirections.

## What you need

- Python 3.11 or later, and `pip` (or [uv](https://docs.astral.sh/uv/)).
- A hub whose run API has Cogs to launch, and a run controller beside it
  ([`cog-execution/runs.md`](cog-execution/runs.md#the-run-controller-and-the-run-api)).
  Every hub serves the run API, `/v1/runs`, on its own database. What it can
  launch are the Cog packages its controller is given, which `cog list
  --launchable` names; without a controller, a launched run stays `SUBMITTED`.

  **A hub deployed with the Helm chart launches nothing yet.** The chart runs
  no run controller and gives the API no Cog packages, so `cog list
  --launchable` is empty there and a launch is refused naming none. The Cogs
  `cog list` shows are the hub's catalog, which is not launchable by name
  until a Cog is installed by its digest. These steps work against a hub run
  from a checkout of this repository: [`dev/`](../dev/README.md#running-cogs-and-ops),
  or [`examples/cog-local`](../examples/cog-local/README.md). The chart gains
  a controller, and Cogs that run as pods, in later phases of
  [`COG_EXECUTION.md`](../COG_EXECUTION.md).
- An account in the hub's realm.
- To chat with a Cog from a terminal interface:
  [Toad](https://github.com/batrachianai/toad), an ACP client
  (`uv tool install -U batrachian-toad --python 3.14`). Optional: `run say`
  talks to a Cog without it.

## 1. Install the CLI from `main`

```sh
pip install "git+https://github.com/nebari-dev/collab-hub-pack.git@main#subdirectory=cli"
collab-hub --help
```

The package is `collab-hub-cli`, in [`cli/`](../cli/README.md), and its
command is `collab-hub`. Install it in a virtual environment, or as a tool of
its own:

```sh
uv tool install "git+https://github.com/nebari-dev/collab-hub-pack.git@main#subdirectory=cli"
pipx install "git+https://github.com/nebari-dev/collab-hub-pack.git@main#subdirectory=cli"
```

To pick up later changes on `main`, install again with `--force-reinstall`
(pip), `--reinstall` (uv), or `pipx reinstall collab-hub-cli`.

### From a clone of the repository

To install from a checkout instead, for a branch other than `main` or to
change the CLI:

```sh
git clone https://github.com/nebari-dev/collab-hub-pack.git
cd collab-hub-pack
pip install -e cli
collab-hub --help
```

`-e` installs it in editable mode, so the installed CLI is the clone's
`cli/` itself: `git pull`, or `git switch` to another branch, and the next
`collab-hub` runs that code without installing again. Install again only when
`cli/pyproject.toml` changes its dependencies. The same as a tool of its own:

```sh
uv tool install --editable ./cli
```

Or run it from the clone without installing it, as `examples/cog-local`
does; uv builds the environment on first use:

```sh
uv run --project cli collab-hub --help
```

## 2. Sign in

```sh
collab-hub --hub "<HUB_URL>" login
collab-hub whoami
```

`login` opens your browser at the hub's realm, the way the Collab desktop
signs in, and stores the session for the profile; the hub's URL is remembered
with it, so the commands below need no `--hub`. `--hub`, like `--profile`, is
an option of `collab-hub` itself, so it comes before the command. Other ways to sign in:

| Situation | Command |
|---|---|
| No browser on this machine | `collab-hub --hub "<HUB_URL>" login --no-browser` prints the URL to open elsewhere |
| Signing in over SSH | `collab-hub --hub "<HUB_URL>" login --no-browser --port 8765`, with that port forwarded (`ssh -L 8765:127.0.0.1:8765 ...`) |
| A script, with a token from the realm | `... \| collab-hub --hub "<HUB_URL>" login --with-token` |
| Another hub beside this one | `collab-hub --profile NAME ...` on every command, or `COLLAB_HUB_PROFILE=NAME` |

`collab-hub logout` ends a browser sign-in's session at the realm, revokes its
refresh token, and forgets it here. A token given to `login --with-token` is
only forgotten here: the CLI cannot revoke it, and it stays valid until it
expires.

## 3. Run a Cog

Each step is one of `examples/cog-local`'s `make` targets.

| Step | `make` target | Command |
|---|---|---|
| The Cogs the hub can launch | `make cogs` | `collab-hub cog list --launchable` |
| Launch one, holding a session | `make launch` | `collab-hub cog launch hermes --entry session --gate never --name hermes-demo` |
| See it running | `make list` | `collab-hub run list` |
| One run, each step | | `collab-hub run show RUN` |
| Say something to it | `make say TEXT="..."` | `collab-hub run say RUN "what can you do?"` |
| Chat with it from Toad | `make connect` | `toad acp "collab-hub run connect RUN"` |
| Stop it | `make stop` | `collab-hub run terminate RUN` |

`cog launch` prints the run's id, `run-…`: that is `RUN` above.

```sh
collab-hub cog list --launchable
collab-hub cog launch hermes --entry session --gate never --name hermes-demo
# Launched hermes as run-d30f9866404b (hermes-demo) on the none backend, workers local.
collab-hub run list
collab-hub run say run-d30f9866404b "what can you do?"
toad acp "collab-hub run connect run-d30f9866404b"
collab-hub run terminate run-d30f9866404b
```

- **`--entry session`** asks for the entry point that stays open and answers
  turns, which is what `run say` and `run connect` talk to. **`--gate never`**
  keeps the step's Gate from holding the run for a decision that cannot be
  made from the CLI yet. **`--name`** is the run's label in `run list`.
- **`run list`** shows each run's status, age and who launched it, and, for a
  run that has not ended, in its `CONNECT` column, the exact command an ACP
  client starts to talk to it. ACP is spoken on a command's stdin and stdout,
  so that command, not a URL, is what a client such as Toad is given.
- **`run connect`** serves the run as an ACP agent: each prompt becomes one
  turn of the run, delivered through the hub to the Cog, and every turn is
  recorded with the run. Start it from the client, not by hand. Leave Toad
  with `ctrl+q`; the Cog keeps running.
- **`run terminate`** asks the hub to cancel the run and waits until it has
  ended; `--no-wait` returns once the request is recorded. Saying `bye` to a
  session Cog ends it too, and the run completes.

A Cog that answers once rather than holding a session, followed to its end:

```sh
collab-hub cog launch hello --input '{"name": "Ada"}' --watch
```

`hermes` and `hello` are the Cogs of `examples/cog-local`; a hub launches the
packages its controller is given, which `cog list --launchable` shows. A name
it cannot launch is refused with the names it can.

Every command but `run connect` takes `--json`, for scripts; `run connect`'s
stdout carries the ACP protocol and nothing else. `run watch` and `cog launch --watch`
exit with the run's outcome: 0 completed, 1 failed, 3 interrupted, 4 waiting
at a Gate, 6 cancelled ([`cli/README.md`](../cli/README.md)).

## Everything on your own machine

[`examples/cog-local`](../examples/cog-local/README.md) starts a hub on your
machine — Postgres and Keycloak in containers, the API and a run controller as
processes — and runs these steps against it, one `make` target each; `make
demo` runs them all, and `make toad` ends with Toad open on Hermes, on Claude
when `ANTHROPIC_API_KEY` is set. How runs work underneath is in
[`cog-execution/`](cog-execution/README.md).
