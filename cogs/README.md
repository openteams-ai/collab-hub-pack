# The harness Cogs

A harness Cog wraps an agent harness, so the hub can launch it, hold a session with it and relay turns to it without knowing which harness it is. Each one is a directory here: a pixi package whose `serve` task starts a worker that serves the hub's seam (`GET /healthz`, `POST /invoke`, `POST /turn` while a session is open; [`op-cog-seam.md`](../docs/cog-execution/op-cog-seam.md)) and drives the harness behind it. The worker is written on the [worker SDK](../worker/README.md): the Cog declares its entry points and the adapter that drives its harness, and the SDK serves the seam.

| Cog | Harness | How it drives it | Model |
|---|---|---|---|
| [`hermes`](hermes/README.md) | [Hermes Agent](https://hermes-agent.nousresearch.com) 0.19 | the SDK's ACP adapter, running `hermes acp` with no tools | any OpenAI-compatible endpoint, or Claude through Hermes's Anthropic provider |

## Running one

The run controller finds packages here by name (`make -C dev controller` reads this directory), so `collab-hub cog launch hermes --entry session` launches the Hermes Cog. [`examples/cog-local`](../examples/cog-local/README.md) does it end to end; `make toad` there leaves you chatting with Hermes in Toad.

## Conventions

- **A package of its own.** `pixi.toml` pins the harness and declares the `serve` task, and `pixi.lock` pins its environment; the controller refuses a package without its lock. The harness never shares the hub's environment. A Cog in this repository pins the SDK by path (`collab-hub-cog-worker = { path = "../../worker" }`); one outside it pins a commit of this repository (`#subdirectory=worker`).
- **Its model comes from the controller.** The SDK reads the binding the controller delivered (`COLLAB_MODEL_*`), and the Cog hands the harness that model alone: no other key, and no credentials of the machine's own.
- **Only what is the harness's own is in the Cog.** The seam, sessions, turns, Frames, the binding's limits and the envelope are the SDK's; a Cog here holds the harness's command, its home and configuration for the run, and its environment.
- **Tested without the harness, and with it.** Each Cog's tests run its worker against the fake ACP agent (`dev/fake-agent/`) in the execution suite, and the harness itself where its environment is installed.
