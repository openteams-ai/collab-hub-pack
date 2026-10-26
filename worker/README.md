# The worker SDK: `collab-hub-cog-worker`

The worker side of the hub's seam, written once. A Cog built on it declares its entry points, names the adapter that drives its harness, and calls `serve()`; the SDK serves what the run controller speaks to (`GET /healthz`, `POST /invoke`, `POST /turn` while a session is open; [`op-cog-seam.md`](../docs/cog-execution/op-cog-seam.md)), reads the binding the hub delivered, places the step's Frames beside the prompt, keeps the model's limits, counts usage, and answers with the [result envelope](../docs/cog-execution/result-envelope.md). Wrapping a harness is writing an adapter, not a server.

The hub never imports this package: it depends on the seam alone (ADR-0002 D5, invariant 4), and a test in the execution suite holds it to that. The SDK depends on nothing beyond the standard library, so a Cog's environment stays the harness's own; Python 3.11 and later.

## Writing a Cog on it

```python
from collab_hub_cog_worker import AcpHarnessAdapter, Harness, Task, Worker

worker = Worker("my-cog", "0.1.0", harness=lambda context: AcpHarnessAdapter(["my-agent", "acp"]))

@worker.task("ask")
def ask(task: Task, harness: Harness):
    return {"answer": harness.interact(task.input["prompt"]).text}

worker.session("session")   # holds a session; each turn is one interaction, `bye` ends it

if __name__ == "__main__":
    worker.serve()
```

That file is the package's `serve` task (`pixi.toml`: `serve = "python serve.py"`), and the package pins the SDK: by path for a Cog in this repository (`collab-hub-cog-worker = { path = "../../worker" }`), by a commit of this repository otherwise (`git = "https://github.com/openteams-ai/collab-hub-pack", rev = "<commit>", subdirectory = "worker"`). [`cogs/hermes`](../cogs/hermes/README.md) is the reference: Hermes Agent over ACP, with what is Hermes's own (its home, its configuration from the binding, its environment) in forty lines of adapter subclass.

**Entry points.** `@worker.task(name)` registers `fn(task, harness) -> payload`: the harness is started before the call and closed after it; returning an `Interaction` answers `{"answer": text}`; raising `WorkerError(code, detail)` fails the step with one of the envelope's codes. Every `/invoke` is answered with an envelope: an exception that is neither, from the Cog's own code or its harness, fails the step `model-call-failed` naming the exception, and never drops the connection. `worker.session(name)` registers an entry point that holds a session: its `/invoke` stays open, each `/turn` is one interaction with the harness answered with what it said, a farewell (`bye`) ends it and completes the step with `{"turns": n}`, and a harness that exits ends it `model-call-failed`. A Cog whose entry points need no harness passes none.

**What an entry point gets.** `task.input`, `task.idempotency_key`, `task.frames` (the step's Frame snapshots, when the hub sent them) and `task.signal` (what the hub sent with the task, a Gate's send-back findings). `harness.interact(text)` is one prompt to the harness: the step's Frames ride with the first interaction unless `frames=` says otherwise; `harness.binding` is the model the hub bound the Cog to; `harness.problem(check, detail, severity)` reports a contract-check finding of the Cog's own on the envelope, for Guards and Gates.

**The binding.** The run controller delivers a Cog's model as `COLLAB_MODEL_*` variables (`PROVIDER`, `BASE_URL`, `NAME`, `API_KEY`, `CONTEXT_WINDOW`, `MAX_OUTPUT_TOKENS`), from the hub's `models:` block; or as a file named by `COLLAB_BINDING_FILE`, the format the Cog's own `resolve` fills in later, with its key by reference (`auth_ref: "env:NAME"`). The SDK reads either into one `Binding`; the envelope's `binding` carries its identity and limits, never the key. A limit that is not a whole number, or a file that cannot be read, fails the step `binding-invalid` before anything is spent.

**The model's limits, and `context-exhausted`.** The SDK hands the adapter the binding's context window and maximum output tokens, so a harness reserves output headroom from a number rather than a guess. What a harness keeps from turn to turn — compaction, what enters each prompt — is the harness's own, inside the Cog; the SDK does not run its turns (decision 19 of [the plan](../COG_EXECUTION.md#10-decisions)). The SDK refuses only what could never fit: a prompt whose estimated tokens, with its Frames, exceed the context window less the output headroom fails `context-exhausted` before the harness is asked, and a harness or model that refuses the input as too long is reported the same way. Retrying on the same binding would not help; a Gate or a person chooses a larger model or fewer Frames.

**Frames, the same way to every harness.** When the task carries Frames, `AcpHarnessAdapter` sends each in `session/prompt` as an embedded `resource` block (`text/markdown`, a URI naming the Frame) when the agent advertised `promptCapabilities.embeddedContext` at `initialize`, and as a text block when it did not; `OpenAICompatAdapter` sends them as a context message before the prompt. A harness Cog never calls the Frames API.

**Authentication.** `/invoke` and `/turn` admit the bearer of `COLLAB_RUN_TOKEN`, the run token the controller presents. `Worker(authenticate=fn)` replaces that check with `fn(headers) -> bool`: the hook an authenticated `/invoke` fills in when worker egress is restricted.

**Cancellation.** The controller stops a worker with `SIGTERM`: the SDK cancels the interaction in flight (`session/cancel` for an ACP agent), closes every harness, started or still starting, and exits. An entry point that waits on something else reads `harness.cancel.is_set()`.

## The adapter protocol

```python
class HarnessAdapter(Protocol):
    def start(self, context: Context) -> None: ...                                  # once, before the first interaction
    def interact(self, prompt: Prompt, context: Context, signal: Signal) -> Interaction: ...
    def cancel(self) -> None: ...                                                   # interrupt the interaction in flight
    def close(self) -> None: ...                                                    # stop it, leave nothing behind
    def alive(self) -> bool: ...                                                    # can it still answer
```

`Context` carries the binding, the Cog and run ids, and where the adapter may write. `Prompt` is text and Frames. `Interaction` is what the harness answered: its text, the verbatim exchange for the envelope's `raw`, its usage, and how it stopped. An adapter raises `WorkerError(code, detail)` for a failure and `Cancelled` when the signal was set.

Two adapters ship:

- **`AcpHarnessAdapter(command, env=None, cwd=None, allowed_tools=())`** runs any agent that speaks the [Agent Client Protocol](https://agentclientprotocol.com) over stdio: it starts the agent, runs `initialize` and `session/new`, sends each prompt as `session/prompt`, and folds the `session/update` notifications into the answer (`agent_message_chunk`) and `raw` (all of them). A permission the agent asks for is granted only for a tool kind named in `allowed_tools` (the protocol's `toolCall.kind`, never the agent's free-text title), once per request; the rest are refused, since nobody is there to answer. The agent inherits an allowlist of the worker's environment (`PATH`, locale, `TMPDIR`, certificates) and nothing that carries a credential; a subclass shapes `environment()` and `working_directory()`, as the Hermes Cog does.
- **`OpenAICompatAdapter(system=None)`** makes one chat-completions call to the binding's endpoint with no tools. It is what a plain context Cog needs, and the proof that the seam is harness-neutral.

**The `end_turn` hazard.** An ACP agent can end a failed turn with an ordinary stop reason: Hermes reports one as `end_turn`. So the adapter never reads success from `stopReason`. An interaction fails on an explicit signal — the agent answering the prompt with a JSON-RPC error (`model-call-failed`, or `context-exhausted` when the error says the context did not fit), the agent exiting (`model-call-failed`, with its exit code), or a turn that ends having said nothing (`model-response-malformed`). A turn that stops for `max_tokens` or `refusal` with something said is an answer, and `Interaction.stop` says how it stopped.

## Testing a Cog

Two stand-ins under `dev/` let a Cog's worker and the adapters run with nothing installed and no network: [`dev/fake-agent/fake_acp_agent.py`](../dev/fake-agent/fake_acp_agent.py), an ACP agent that echoes and misbehaves on request (`fail`, `overflow`, `silence`, `slow`, `exit`, `use a tool`), and [`dev/fake-model/fake_model.py`](../dev/fake-model/fake_model.py), an OpenAI-compatible endpoint (`make -C dev fake-model`). The SDK's own suite runs both adapters through one conformance suite against them, and the seam over HTTP:

```sh
cd worker && uv run --group test pytest
```

CI runs it on Python 3.11 and 3.14 (`test-worker.yaml`). A Cog's tests run its worker the way the execution suite runs the Hermes Cog's: through the local executor with the package's `serve`, against the fake agent, and against the harness itself where its environment is installed.
