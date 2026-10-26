"""The Hermes harness Cog's worker: Hermes Agent behind the hub's seam, on the worker SDK.

The run controller starts this with the package's `serve` task, in the
package's pixi environment, where Hermes Agent and the SDK are installed. The
SDK serves the seam (`GET /healthz`, `POST /invoke`, `POST /turn` while a
session is open) and drives Hermes through its ACP adapter: `hermes acp` as a
child process, one JSON-RPC message per line. What is Hermes's own is here:
a home for the run, its configuration from the binding, and its environment.

Entry points:

- `session` opens a Hermes session and holds it: each turn the hub delivers is
  one prompt, answered with what Hermes said. It ends on `bye`, or when the run
  is terminated and this process with it.
- `ask` answers one prompt, `input.prompt`, and returns `{"answer": ...}`.

The model is the binding the hub delivered (`COLLAB_MODEL_*`): `provider`
`openai-compatible` (the default) for any chat-completions endpoint, or
`anthropic` for Claude through Hermes's own Anthropic provider. Hermes runs with
no tools (`hermes_acp.py`): it answers, and does nothing else, and a permission
it asks for is refused, since nobody is there to answer it.
"""

import json
import os
import shlex
import shutil
import sys
import tempfile
from pathlib import Path

from collab_hub_cog_worker import AcpHarnessAdapter, Context, Harness, Task, Worker, WorkerError

DEFAULT_CLAUDE = "claude-opus-5-5"
# What Hermes's process inherits from the worker's: enough to run, and no credential.
HERMES_INHERITS = ("PATH", "LANG", "LC_ALL", "TZ", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE")
# The CA bundle Hermes verifies TLS against inside the package's pixi environment: conda-forge's
# ca-certificates, the same Mozilla bundle certifi ships (the feedstock builds it from certifi's own
# cacert.pem), through a package pixi.lock controls and `pixi update ca-certificates` bumps. certifi
# itself is pinned exactly by hermes-agent, so a root-store fix there waits on a hermes-agent release.
# Hermes reads these variables ahead of certifi's bundle: SSL_CERT_FILE for httpx and the Anthropic
# SDK, REQUESTS_CA_BUNDLE for requests. Only inside the pixi environment (pixi sets
# PIXI_ENVIRONMENT_NAME on activation): in the `host` environment sys.prefix is whatever interpreter
# runs this, whose bundle may be older than certifi's. This stays: the certifi override in pixi.toml
# is the incident-only part.
CA_BUNDLE = Path(sys.prefix) / "ssl" / "cacert.pem"
# Overridable so the worker can be tested against a stand-in agent; Hermes itself by default.
AGENT = shlex.split(os.environ.get("COLLAB_HERMES_COMMAND", "")) or [
    sys.executable, str(Path(__file__).resolve().parent / "hermes_acp.py")]


def log(message):
    print(f"hermes cog: {message}", file=sys.stderr, flush=True)


def own_ca_bundle():
    """The pixi environment's CA bundle, or None outside one or when the environment has no bundle."""
    if os.environ.get("PIXI_ENVIRONMENT_NAME") and CA_BUNDLE.is_file():
        return str(CA_BUNDLE)
    return None


def hermes_environment():
    """What Hermes's process gets from the worker's environment: the inherited names, and the CA bundles.

    The controller's own bundles, when it delivered any, win, and any one delivered disables the
    defaults rather than being merged with them: Hermes resolves SSL_CERT_FILE or REQUESTS_CA_BUNDLE
    itself and otherwise lets httpx read SSL_CERT_FILE, then SSL_CERT_DIR, so a default set beside a
    delivered variable would take precedence over it (a default file over a delivered directory, say).
    """
    env = {name: os.environ[name] for name in HERMES_INHERITS if name in os.environ}
    bundle = own_ca_bundle()
    if bundle and not any(env.get(name) for name in ("SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE")):
        env["SSL_CERT_FILE"] = env["REQUESTS_CA_BUNDLE"] = bundle
    return env


def hermes_config(binding):
    """Hermes's `config.yaml` for this run: the model the hub delivered, and nothing else."""
    if binding.provider == "anthropic":
        # Claude, through Hermes's own Anthropic provider (the Anthropic SDK), with the delivered key.
        if not binding.api_key:
            raise WorkerError("model-unavailable", "no model: the anthropic provider needs COLLAB_MODEL_API_KEY")
        # Hermes 0.19 reads an Anthropic key from its environment only: the adapter passes it there.
        model = {"provider": "anthropic", "default": binding.model or DEFAULT_CLAUDE}
    elif binding.provider == "openai-compatible":
        if not binding.base_url:
            raise WorkerError("model-unavailable", "no model: the controller delivers COLLAB_MODEL_BASE_URL, or "
                                                   "COLLAB_MODEL_PROVIDER=anthropic, to this Cog")
        # Chat completions, always: Hermes would otherwise pick another API from the URL's host (Bedrock's
        # Converse through boto3 for bedrock-runtime.*.amazonaws.com), which this provider does not promise.
        model = {"provider": "custom", "base_url": binding.base_url, "default": binding.model,
                 "api_key": binding.api_key or "none", "api_mode": "chat_completions"}
    else:
        raise WorkerError("model-unavailable", f"unknown COLLAB_MODEL_PROVIDER {binding.provider!r}: "
                                               "it is openai-compatible or anthropic")
    # No lazy installs: Hermes would otherwise install a provider's SDK into the package's locked
    # environment, from the network, mid-session.
    return {"model": model, "security": {"allow_lazy_installs": False}}


class Hermes(AcpHarnessAdapter):
    """Hermes over ACP, in a home of its own for the run, configured from the binding."""

    name = "Hermes"

    def __init__(self, context: Context) -> None:
        super().__init__(AGENT, inherits=HERMES_INHERITS)
        self.config = hermes_config(context.binding)  # a model Hermes cannot use fails the step before it starts
        self.workspace = Path(tempfile.mkdtemp(prefix=f"hermes-{context.run_id or 'run'}-")).resolve()
        self.binding = context.binding

    def working_directory(self, context: Context) -> str:
        return str(self.workspace)

    def environment(self, context: Context) -> dict:
        # Hermes picks up any provider's key it finds in its environment, and credentials of its own
        # under HOME (Claude Code's among them): it gets neither, only what it needs to run, a HOME of
        # its own, and its config, which names the model it was delivered.
        env = hermes_environment()
        home = self.workspace / "hermes-home"
        home.mkdir(parents=True, exist_ok=True)
        # JSON is YAML: Hermes reads this file as its config.yaml. Readable by its owner alone.
        (home / "config.yaml").write_text(json.dumps(self.config, indent=2))
        (home / "config.yaml").chmod(0o600)
        env.update(HERMES_HOME=str(home), HOME=str(self.workspace))
        if self.binding.provider == "anthropic":
            env["ANTHROPIC_API_KEY"] = self.binding.api_key
        return env

    def start(self, context: Context) -> None:
        try:
            super().start(context)
        except BaseException:
            self.close()  # a session that could not open leaves nothing behind either
            raise
        log(f"session {self.session} open in {self.workspace}")

    def close(self) -> None:
        """Stop Hermes, and remove the run's workspace: its config names the model, and its key."""
        super().close()
        shutil.rmtree(self.workspace, ignore_errors=True)


worker = Worker("hermes", "0.2.0", harness=Hermes)


@worker.task("ask")
def ask(task: Task, harness: Harness):
    """One prompt, `input.prompt`, answered."""
    prompt = (task.input or {}).get("prompt", "") if isinstance(task.input, dict) else ""
    return {"answer": harness.interact(str(prompt)).text}


worker.session("session")

if __name__ == "__main__":
    worker.serve()
