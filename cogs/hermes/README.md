# The Hermes harness Cog

[Hermes Agent](https://hermes-agent.nousresearch.com) as a Cog: the hub launches it, keeps a session open with it, and relays turns to it, without knowing it is Hermes. [`examples/cog-local`](../../examples/cog-local/README.md) walks through it end to end.

| File | What it is |
|---|---|
| `serve.py` | The worker the run controller starts, on the [worker SDK](../../worker/README.md): what is Hermes's own, and nothing of the seam |
| `hermes_acp.py` | How the worker starts Hermes: `hermes acp`, with no tools |
| `pixi.toml` | The package: Python 3.12, `hermes-agent[acp,anthropic]`, the SDK by path, the `serve` task, and the pixi it needs |
| `pixi.lock` | That environment, pinned for Linux and macOS |

## What the worker does

The SDK serves the seam every Cog worker serves (`GET /healthz`, `POST /invoke`, and `POST /turn` while a session is open, [`op-cog-seam.md`](../../docs/cog-execution/op-cog-seam.md)) and drives Hermes through its [ACP](https://agentclientprotocol.com) adapter: `hermes acp` as a child process, JSON-RPC one message per line. The Cog's own code is the `Hermes` adapter, a subclass that gives Hermes a home for the run, its configuration from the binding, and its environment.

| Entry point | Input | Does |
|---|---|---|
| `session` | none | Opens a Hermes session and holds it. Each turn the hub delivers is one prompt, answered with what Hermes said. It ends on `bye`, or when the run is terminated and the worker with it |
| `ask` | `{"prompt": "..."}` | Answers one prompt and returns `{"answer": "..."}` |

A turn that arrives while Hermes is still starting waits for the session, up to five minutes. If Hermes exits during a session, the session ends at once and the step fails `model-call-failed`, saying so, rather than holding the run open. A turn Hermes ends having said nothing fails `model-response-malformed`; one it answers with an error, `model-call-failed`, or `context-exhausted` when the error says the input did not fit.

## Its model

The binding the controller delivers, to this Cog and no other:

| Variable | What |
|---|---|
| `COLLAB_MODEL_PROVIDER` | `openai-compatible` (the default), or `anthropic` for Claude through Hermes's own Anthropic provider, which uses the Anthropic SDK |
| `COLLAB_MODEL_BASE_URL` | The OpenAI-compatible endpoint. Not used with `anthropic` |
| `COLLAB_MODEL_NAME` | The model's id. With `anthropic`, `claude-opus-5-5` when unset |
| `COLLAB_MODEL_API_KEY` | Its key. Required with `anthropic` |
| `COLLAB_MODEL_CONTEXT_WINDOW`, `COLLAB_MODEL_MAX_OUTPUT_TOKENS` | The model's limits, when the hub knows them: an input that cannot fit fails `context-exhausted` before Hermes is asked |

A model the worker cannot use fails the step `model-unavailable`, saying why. `make -C dev controller` delivers them, with [`dev/fake-model`](../../dev/fake-model/fake_model.py) as the default: an endpoint that answers `The fake model heard: ...` with no account and no network. [`examples/cog-local`](../../examples/cog-local/README.md) switches to Claude when `ANTHROPIC_API_KEY` is set. A binding the Cog's own `resolve` produces replaces this in a later phase of the plan; the SDK reads that file the same way.

## What Hermes sees

- **A home of its own.** Each session gets a temporary workspace, which is also Hermes's `HOME`, and `HERMES_HOME` inside it holds only a `config.yaml` naming the delivered model. Nothing of the machine's own `~/.hermes` is read or changed, and Hermes finds no credentials of its own: its Anthropic provider would otherwise prefer Claude Code's sign-in to any key. The workspace is removed when the session ends or the worker is stopped.
- **Only its model's key.** Hermes's environment is an allowlist (`PATH`, `LANG` and the like), so the run token, and any provider key the worker's environment happens to hold, never reach it: Hermes picks up whatever key it finds. Its model's key reaches it in its config file, readable by its owner alone, or, for Claude, as `ANTHROPIC_API_KEY`, the only way Hermes 0.19's Anthropic provider takes one.
- **The environment's trust store.** Inside its pixi environment Hermes verifies TLS against conda-forge's `ca-certificates`, pinned by `pixi.lock` and bumped with `pixi update ca-certificates`, rather than the certifi hermes-agent pins exactly: the same Mozilla bundle, through a package the lock controls, so a root-store fix never waits on a hermes-agent release. The worker hands it to Hermes as `SSL_CERT_FILE` and `REQUESTS_CA_BUNDLE`; bundles the controller delivers win. This stays; the certifi override in `pixi.toml` is the incident-only part.
- **The API its provider names, and nothing installed.** With `openai-compatible`, Hermes speaks chat completions whatever the URL's host: Hermes 0.19 would otherwise pick an API from the host, Bedrock's Converse through boto3 for `bedrock-runtime.*.amazonaws.com`. Its lazy installs are off, so it never installs a provider's SDK into the locked environment, from the network, mid-session; a feature that needs one is unavailable instead.
- **No tools.** The first Hermes run is prompt in, answer out (decision 14 of the plan): no command, no file, no browser, no web. Hermes 0.19's ACP adapter enables its whole toolset for every session and reads no setting that narrows it, so the worker starts Hermes through [`hermes_acp.py`](hermes_acp.py), which gives each session no toolsets at all. Its model is then offered no tools, and a tool call it makes anyway fails with `Tool '...' does not exist`; a test checks it with a model that orders a shell command. Should Hermes ask its client's permission for anything, the SDK refuses: the Cog declares no allowed tools.
- **The step's Frames**, when the hub sends them, arrive in Hermes's prompt through the SDK: as embedded resources when Hermes advertises `embeddedContext`, as text otherwise. The Cog has no code for it.

The Cog contains nothing that knows whether it is a process or a pod: the controller tells the worker where to listen, and the SDK does the rest.

## Running it

From the hub, which is the point: `collab-hub cog launch hermes --entry session`, then `collab-hub run say RUN ...` or `toad acp "collab-hub run connect RUN"`. Without the hub, `make -C dev op OP=hermes LOCATION=local` runs its `ask` once, against `make -C dev fake-model` or the model `COLLAB_MODEL_*` names.

Installing this package needs pixi 0.82.1 or later, which `pixi.toml` states with `requires-pixi`: earlier releases trust a compromised CA chain. A runner that bundles its own pixi must update it before it can install the package.

Its tests run the real worker against the fake ACP agent, and Hermes itself against the fake model when this environment is installed: `execution/tests/test_hermes_cog.py`.
