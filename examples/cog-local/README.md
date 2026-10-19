# Hermes as a Cog, on your machine

Start a hub, sign in, launch a [Hermes](https://hermes-agent.nousresearch.com) agent as a Cog, see it running, talk to it from [Toad](https://github.com/batrachianai/toad), and stop it. Everything runs on your machine: the hub's API and its run controller are two processes, the sign-in is a real one against a local Keycloak, and Hermes runs as a process of its own, in its own environment.

Each step is one `make` target in this directory. Run `make help` to list them.

## What you need

- [Docker](https://docs.docker.com/get-docker/), for Postgres and Keycloak.
- [uv](https://docs.astral.sh/uv/getting-started/installation/), for the Python environments.

Then, once, from this directory:

```sh
make env      # the CLI, the hub, Toad, pixi, and Hermes's environment
make tools    # checks each one is there
```

`make env` installs [pixi](https://pixi.sh) if it is missing (it gives each Cog its own environment), [Toad](https://github.com/batrachianai/toad) with `uv tool install`, and Hermes Agent into the Hermes Cog's environment, about 400 MB the first time. Open a new shell afterwards if pixi or Toad was new.

## The quickest way: `make toad`

With `ANTHROPIC_API_KEY` set, one command takes you from nothing to chatting with Hermes, running as a Cog and talking to Claude, in Toad:

```sh
export ANTHROPIC_API_KEY=...   # from https://platform.claude.com
make toad
```

It goes through the steps below one by one, each explained under a banner as it runs: it checks the tools and your key, starts Postgres, Keycloak and the hub with Claude as Hermes's model, signs in, launches Hermes, and opens Toad on it. Type a message and press Enter; Hermes answers through the hub. Leave Toad with `ctrl+q`: Hermes keeps running, and the last lines say how to come back (`make connect`), stop it (`make stop`) or stop the hub (`make shutdown`). Without a key, it says so and stops before starting anything; `make demo` walks through the same steps against the fake model.

The rest of this page is the same thing one step at a time.

## 1. Start the hub

```sh
make hub           # Postgres and Keycloak, in containers
make credentials   # who to sign in as: user dev, password dev
```

Keycloak comes with a realm, `nebari`, and its users. `make credentials` shows the one this example signs in as and checks the realm issues a token for it.

Now the processes, each in a terminal of its own:

```sh
make model         # terminal 1: the model Hermes calls (a fake one, see below)
make api           # terminal 2: the hub's API, on http://127.0.0.1:8000
make controller    # terminal 3: the run controller
```

Or all three in the background with `make start` (logs in `.local/`, stopped with `make shutdown`). The API accepts what you ask for and records it. The controller is the one that starts Cogs. They share a record of every run, the Track, and never call each other.

### Which model

The steps decide it once, from your environment:

| If | Hermes chats with | `make model` |
|---|---|---|
| `ANTHROPIC_API_KEY` is set | **Claude**, for real: `claude-opus-5-5`, through Hermes's own Anthropic provider | not needed |
| `COLLAB_MODEL_BASE_URL` is set | your OpenAI-compatible endpoint, with `COLLAB_MODEL_NAME` and `COLLAB_MODEL_API_KEY` | not needed |
| neither | a fake model on port 8090 that answers every prompt with `The fake model heard: ...`, with no account and no network | needed, or `make start` |

To chat with Claude:

```sh
export ANTHROPIC_API_KEY=...   # from https://platform.claude.com; read from your environment, never from make's command line
make claude                    # checks the key with Anthropic, and confirms Hermes will use Claude
```

```text
  ANTHROPIC_API_KEY is set, and Anthropic accepts it: Claude Opus 5.5 (claude-opus-5-5) is available.
  Hermes will chat with Claude for real: start (or restart) the controller from here,
  with make controller or make start, and launch Hermes.
```

The model is the controller's to hand out, so set the key before starting it. `CLAUDE_MODEL=...` picks another Claude model. The controller hands the model to the Hermes Cog's worker and to no other Cog, and Hermes sees only that model's key: none of the other provider keys in your environment, and none of your own Claude Code or Hermes sign-ins. `make demo` always uses the fake model, so it costs nothing and runs anywhere.

## 2. Sign in with the CLI

```sh
make login         # opens Keycloak in your browser: sign in as dev / dev
make whoami
```

```text
user          3b8ec34f-eaa3-4056-897e-c2179651bc69
name          Dev User <dev@example.com>
organization  dev-org
signed in     yes, token expires 2026-10-03T07:24:43Z
```

No browser on this machine? `make login-token` signs in with a token the realm issues for the same user.

The example keeps its sign-in in `.local/cli`, so it does not touch your own `collab-hub` profile.

## 3. Launch Hermes

```sh
make cogs          # the Cogs this hub can launch: hermes among them
make launch        # collab-hub cog launch hermes --entry session --name hermes-on-...
```

```text
Launched hermes as run-d30f9866404b (hermes-on-fake-model) on the none backend, workers local.
```

The controller starts the Hermes Cog as a process in its own environment, and the Cog starts Hermes and opens a session with it. The run is named after the Cog and its model, `hermes-on-claude-opus-5-5` with Claude (`NAME=...` to choose your own), and its id is kept in `.local/run` for the next steps; pass `RUN=...` to act on another run.

## 4. See it running

```sh
make list          # collab-hub run list
```

```text
RUN               NAME                  COG     STATUS   AGE  BY        CONNECT
run-d30f9866404b  hermes-on-fake-model  hermes  RUNNING  0s   Dev User  env COLLAB_HUB_CONFIG_DIR=.../cog-local/.local/cli .../cli/.venv/bin/collab-hub --hub http://127.0.0.1:8000 run connect run-d30f9866404b
```

`CONNECT` is what an ACP client needs to reach the run. ACP has no URL: the client starts an agent as a command and speaks to it on its stdin and stdout, so this is the command to give it, `toad acp "<CONNECT>"` for Toad, which is what `make connect` does. It names the CLI by its path, the directory where this example signed in, and the hub, so it works from any shell (paths shortened here). A run that has ended takes no more turns and shows none.

## 5. Talk to Hermes from Toad

```sh
make connect    # toad acp "collab-hub run connect RUN"
```

Toad opens with the running Hermes as its agent. Ask it a few things: `hello`, `what can you do?`, anything you would ask an assistant. With Claude or another real model, it is Hermes answering; with the fake model each answer is `The fake model heard: ...`. Leave Toad with `ctrl+q`; Hermes keeps running.

The same from the CLI, one turn at a time:

```sh
make say TEXT="what can you do?"    # collab-hub run say RUN what can you do?
```

How a prompt travels: Toad speaks the [Agent Client Protocol](https://agentclientprotocol.com) (ACP) to `collab-hub run connect`, which sends it to the hub as one turn of the run. The controller delivers the turn to the Hermes Cog, which speaks ACP to Hermes, and the answer comes back the same way. Toad never reaches Hermes directly, and every turn and its answer are recorded with the run.

## 6. Stop Hermes

```sh
make stop          # collab-hub run terminate RUN
make list
```

```text
run-d30f9866404b ended CANCELLED.
RUN               NAME                  COG     STATUS     AGE  BY        CONNECT
run-d30f9866404b  hermes-on-fake-model  hermes  CANCELLED  4s   Dev User
```

The controller stops the Cog's process and Hermes with it, and the run takes no more turns. Saying `bye` to it ends the session too, and the run then ends `COMPLETED` instead.

## 7. Shut down

```sh
make shutdown      # what `make start` started
make down          # the containers; their data is kept
make clean         # this example's sign-in, run id and logs
```

## All of it, in one command

```sh
make demo
```

Every step above in order, each under a banner that says what it does and the command it stands for, then checked: it stops at the first answer that is wrong. It signs in with `login-token` and plays Toad's part with a scripted ACP client, `acp_check.py`, so it needs no browser and no terminal interface; CI runs it. The walk-through uses the fake model, so it costs nothing. When `ANTHROPIC_API_KEY` is set, one more step restarts the controller with Claude, launches Hermes again and chats with it for real (a few Claude tokens); without the key, the step says it is skipped and why. Docker's output and the processes' logs go to `.local/`.

```text
━━━ Step 9 of 13 · Chat with Hermes the way Toad does ━━━━━━━━━━━━━━━━━━━━━━━━━━
  Toad is a terminal chat for agents that speak ACP, the Agent Client Protocol; make connect opens it.
  ...
  $ make connect   (scripted here)

  > hello hermes
  The fake model heard: hello hermes

  ✔ Hermes answered, through the hub
```

## How it fits together

```text
make login     ──▶ Keycloak (dev / dev) ──token──▶ collab-hub
make launch    ──▶ POST /v1/runs ──▶ API ──writes──▶ Track ◀──watches── controller ──starts──▶ Hermes Cog ──▶ hermes acp
make connect ──▶ Toad ──ACP──▶ collab-hub run connect ──POST /v1/runs/{id}/turns──▶ API ──▶ Track
                       controller ──POST /turn──▶ Hermes Cog ──ACP──▶ Hermes ──▶ model
make stop      ──▶ POST /v1/runs/{id}/cancel ──▶ API ──▶ Track ──▶ controller stops the Cog and Hermes
```

The Track is the hub's own Postgres, the database the API runs on, as on a deployed hub: the API's migrations create its tables, and `make controller` waits for the API before it starts watching it.

The Hermes Cog is [`cogs/hermes`](../../cogs/hermes/README.md) at the root of the repository: a worker that serves the hub's seam and drives Hermes over ACP, and a pixi package that pins Hermes Agent. Its README says what it does with tools, its model and its home directory.

This directory also has [`cogs/hello`](cogs/hello), the smallest Cog that holds a session, with no model at all. Launch it with `make launch COG=hello`; it answers `help`, `sum 1 2 3`, `whoami` and a few more. It is the one to copy to write a Cog of your own: change `Session.answer` in `serve.py`, run `pixi lock`, and launch it by its directory's name. The controller finds Cogs in `cogs/` at the root, here, and in `dev/cogs`.

## When something is off

| What you see | Why | What to do |
|---|---|---|
| `make api` stops with Postgres or Keycloak errors | The containers are not up | `make hub` |
| `make login` says the hub runs dev auth | Another API, `make -C ../../dev api`, holds the port | Stop it, or `make ... PORT=8010` on every step |
| The run stays `SUBMITTED` | No controller is running | `make controller`, or `make start` |
| Hermes answers `API call failed ... Connection error` | Nothing listens at the model's address | `make model`, or check `COLLAB_MODEL_BASE_URL` |
| The run fails with `model-unavailable` | The controller had no model to hand Hermes | Start the controller from this directory, or set the three variables above |
| The first launch takes minutes | pixi is installing Hermes's environment | `make env` installs it ahead of time |
| `make connect` or `make say` says the run takes no turns | The run has ended | `make launch` again |
| `toad: command not found` | Toad was installed into `~/.local/bin` | Add it to `PATH`, or open a new shell |

The Cog's own output, Hermes's included, is under `../../dev/.local/runs/`, and the processes' in `.local/` when `make start` started them.

## What this does not show yet

- **Hermes's tools.** Hermes runs with none here: it answers, and does nothing else, not even read a file. Giving it tools through the hub, and asking you in Toad before it uses one, is later work.
- **A restart.** The controller runs on the `none` backend, which keeps nothing: stop it while Hermes runs and the run is recorded `INTERRUPTED` when a controller starts again.
- **A deployed hub.** Its API serves the run API, but the chart runs no controller and has no Cog packages to launch, and Hermes here is a process on the controller's machine. On a cluster a Cog runs as a pod, a later phase of [`COG_EXECUTION.md`](../../COG_EXECUTION.md).

More: [`docs/cog-execution/runs.md`](../../docs/cog-execution/runs.md) for the run API and the controller, [`cli/README.md`](../../cli/README.md) for every CLI command.
