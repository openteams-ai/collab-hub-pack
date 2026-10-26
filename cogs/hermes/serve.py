"""The Hermes harness Cog's worker: Hermes Agent behind the hub's seam. Standard library only.

The run controller starts this with the package's `serve` task, in the
package's pixi environment, where Hermes Agent is installed. It serves the
seam every Cog worker serves:

    GET  /healthz   200 once it can answer
    POST /invoke    {entry_point, input, idempotency_key} -> a result envelope
    POST /turn      {turn, text} -> {text}, while a `session` is open

and drives Hermes over the Agent Client Protocol: it starts `hermes acp` as a
child process and is its ACP client, one JSON-RPC message per line on the
child's stdin and stdout. The hub never sees ACP, and Hermes never sees the hub.
Hermes runs with no tools (`hermes_acp.py`): it answers, and does nothing else.

Entry points:

- `session` opens a Hermes session and holds it: each turn the hub delivers
  is one prompt, answered with what Hermes said. It ends on `bye`, or when the
  run is terminated and this process with it.
- `ask` answers one prompt, `input.prompt`, and returns.

The model comes from the controller, in the environment: `COLLAB_MODEL_PROVIDER`
(`openai-compatible`, the default, or `anthropic` for Claude through Hermes's
Anthropic provider), `COLLAB_MODEL_BASE_URL` (the OpenAI-compatible endpoint),
`COLLAB_MODEL_NAME` and `COLLAB_MODEL_API_KEY`.
Hermes gets a home of its own for the run, so nothing of the machine's own
Hermes setup is read or changed. Should Hermes ask permission for anything, the
worker refuses, since nobody is there to answer it.
"""

import hmac
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

COG = os.environ.get("COLLAB_COG_ID", "hermes")
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
RUN = os.environ.get("COLLAB_RUN_ID", "")
TOKEN = os.environ.get("COLLAB_RUN_TOKEN", "")
# Overridable so the worker can be tested against a stand-in agent; Hermes itself by default.
AGENT = shlex.split(os.environ.get("COLLAB_HERMES_COMMAND", "")) or [
    sys.executable, str(Path(__file__).resolve().parent / "hermes_acp.py")]


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


def log(message):
    print(f"hermes cog: {message}", file=sys.stderr, flush=True)


def hermes_home(root):
    """A Hermes home for this run: the model the controller delivered, and nothing else."""
    home = Path(root) / "hermes-home"
    home.mkdir(parents=True, exist_ok=True)
    provider = os.environ.get("COLLAB_MODEL_PROVIDER", "") or "openai-compatible"
    name, key = os.environ.get("COLLAB_MODEL_NAME", ""), os.environ.get("COLLAB_MODEL_API_KEY", "")
    if provider == "anthropic":
        # Claude, through Hermes's own Anthropic provider (the Anthropic SDK), with the delivered key.
        if not key:
            raise RuntimeError("no model: the anthropic provider needs COLLAB_MODEL_API_KEY")
        # Hermes 0.19 reads an Anthropic key from its environment only: Agent passes it there.
        model = {"provider": "anthropic", "default": name or DEFAULT_CLAUDE}
    elif provider == "openai-compatible":
        base_url = os.environ.get("COLLAB_MODEL_BASE_URL", "")
        if not base_url:
            raise RuntimeError("no model: the controller delivers COLLAB_MODEL_BASE_URL, or "
                               "COLLAB_MODEL_PROVIDER=anthropic, to this Cog")
        # Chat completions, always: Hermes would otherwise pick another API from the URL's host (Bedrock's
        # Converse through boto3 for bedrock-runtime.*.amazonaws.com), which this provider does not promise.
        model = {"provider": "custom", "base_url": base_url, "default": name, "api_key": key or "none",
                 "api_mode": "chat_completions"}
    else:
        raise RuntimeError(f"unknown COLLAB_MODEL_PROVIDER {provider!r}: it is openai-compatible or anthropic")
    # JSON is YAML: Hermes reads this file as its config.yaml. No lazy installs: Hermes would otherwise
    # install a provider's SDK into the package's locked environment, from the network, mid-session.
    config = {"model": model, "security": {"allow_lazy_installs": False}}
    (home / "config.yaml").write_text(json.dumps(config, indent=2))
    (home / "config.yaml").chmod(0o600)
    return home


class Agent:
    """Hermes over ACP: one child process, one session, one prompt at a time."""

    def __init__(self):
        self.workspace = Path(tempfile.mkdtemp(prefix=f"hermes-{RUN or 'run'}-"))
        self.process = None
        LIVE.add(self)  # from here on, a stop removes the workspace, even while the session is opening
        try:
            self._start()
        except BaseException:
            self.close()  # a session that could not open leaves nothing behind either
            raise

    def _start(self):
        # Hermes picks up any provider's key it finds in its environment, and credentials of its own
        # under HOME (Claude Code's among them): it gets neither, only what it needs to run, a HOME
        # of its own, and its config, which names the model it was delivered.
        env = hermes_environment()
        env.update(HERMES_HOME=str(hermes_home(self.workspace)), HOME=str(self.workspace))
        if os.environ.get("COLLAB_MODEL_PROVIDER") == "anthropic":
            env["ANTHROPIC_API_KEY"] = os.environ["COLLAB_MODEL_API_KEY"]
        self.process = subprocess.Popen(AGENT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sys.stderr,
                                        text=True, env=env, cwd=self.workspace)
        self.lock = threading.Lock()
        self.next_id = 0
        self.turns = []
        self.call("initialize", {"protocolVersion": 1, "clientCapabilities": {},
                                 "clientInfo": {"name": "collab-hub-cog-hermes", "version": "0.1.0"}})
        self.session = self.call("session/new", {"cwd": str(self.workspace), "mcpServers": []})[0]["sessionId"]
        log(f"session {self.session} open in {self.workspace}")

    def _send(self, message):
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
        self.process.stdin.flush()

    def call(self, method, params):
        """One request to Hermes; returns its result and what Hermes said on the way."""
        self.next_id += 1
        request_id = self.next_id
        self._send({"id": request_id, "method": method, "params": params})
        said = []
        for line in self.process.stdout:
            if not line.strip():
                continue
            message = json.loads(line)
            if message.get("method") == "session/update":
                update = message["params"]["update"]
                if update.get("sessionUpdate") == "agent_message_chunk":
                    said.append(update.get("content", {}).get("text", ""))
            elif "method" in message and "id" in message:
                self._refuse(message)
            elif message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError(f"Hermes refused {method}: {message['error'].get('message')}")
                return message["result"], "".join(said)
        raise RuntimeError(f"Hermes stopped before answering {method}")

    def _refuse(self, request):
        """A request from Hermes to its client: permission for a tool is refused, anything else unknown."""
        if request["method"] == "session/request_permission":
            log(f"refused permission: {request['params'].get('toolCall', {}).get('title', '?')}")
            self._send({"id": request["id"], "result": {"outcome": {"outcome": "cancelled"}}})
        else:
            self._send({"id": request["id"], "error": {"code": -32601, "message": "not offered by this client"}})

    def prompt(self, text):
        with self.lock:
            result, said = self.call("session/prompt", {"sessionId": self.session,
                                                        "prompt": [{"type": "text", "text": text}]})
        if result.get("stopReason") not in (None, "end_turn"):
            said += f"\n\n(Hermes stopped: {result['stopReason']})"
        return said.strip() or "(Hermes said nothing)"

    def close(self):
        """Stop Hermes, and remove the session's workspace: its config names the model, and its key."""
        if self.process is not None and self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
        shutil.rmtree(self.workspace, ignore_errors=True)
        LIVE.discard(self)


session = None
LIVE = set()  # every Agent whose workspace exists: what a stop must clean up
starting, opened, ended = threading.Event(), threading.Event(), threading.Event()
SESSION_START_SECONDS = 300  # Hermes's first start in a fresh environment can take a while


def envelope(entry_point, *, payload=None, error=None):
    """A version-1 result envelope: what every Cog answers `/invoke` with."""
    return {
        "envelope": 1, "cog": {"id": COG, "version": "0.1.0"}, "task": entry_point,
        "ok": error is None, "error": error, "payload": payload,
        "raw": None, "problems": [], "binding": None, "usage": None,
    }


def invoke(entry_point, value):
    global session
    value = value or {}
    try:
        if entry_point == "ask":
            agent = Agent()
            try:
                return 200, envelope(entry_point, payload={"answer": agent.prompt(str(value.get("prompt", "")))})
            finally:
                agent.close()
        if entry_point == "session":
            starting.set()
            try:
                session = Agent()
            finally:
                opened.set()  # a turn waiting on the session is answered now: by it, or by its absence
            # Until `bye`, or until Hermes exits on its own: a session whose Hermes is gone can never
            # answer again, so it ends, failed, rather than holding the run open. A terminated run
            # kills this process instead.
            while not ended.wait(1):
                if session.process.poll() is not None:
                    break
            code = session.process.poll() if not ended.is_set() else None
            ended.set()
            session.close()
            if code is not None:
                detail = f"Hermes exited (code {code}) during the session, after {len(session.turns)} turns"
                log(detail)
                return 502, envelope(entry_point, error={"code": "model-call-failed", "detail": detail})
            return 200, envelope(entry_point, payload={"turns": len(session.turns)})
    except (OSError, RuntimeError) as exc:
        log(f"{entry_point} failed: {exc}")
        error = {"code": "model-unavailable", "detail": str(exc)[:500]}
        return 503, envelope(entry_point, error=error)
    error = {"code": "invalid-input", "detail": f"hermes has no entry point {entry_point!r}: it has ask and session"}
    return 422, envelope(entry_point, error=error)


def turn(text):
    if text.strip().lower() in ("bye", "/bye"):
        ended.set()
        return "Bye! The Hermes session ends, and the run completes."
    session.turns.append(text)
    return session.prompt(text)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        self._send(200, {"ok": True}) if self.path == "/healthz" else self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        presented = self.headers.get("Authorization", "").removeprefix("Bearer ")
        if not TOKEN or not hmac.compare_digest(presented.encode(), TOKEN.encode()):
            return self._send(401, {"error": "not the run token of this worker"})
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path == "/invoke":
            return self._send(*invoke(request.get("entry_point"), request.get("input")))
        if self.path == "/turn":
            # A turn may come while Hermes is still starting the session: it waits for it.
            if not starting.is_set() or not opened.wait(SESSION_START_SECONDS) or ended.is_set():
                return self._send(404, {"error": "no session is open"})
            if session is None:
                return self._send(503, {"error": "the Hermes session could not be opened"})
            try:
                return self._send(200, {"text": turn(str(request.get("text", "")))})
            except (OSError, RuntimeError) as exc:
                return self._send(502, {"error": str(exc)[:500]})
        self._send(404, {"error": "not found"})

    def log_message(self, *args):
        pass


def stop(*_):
    """The controller stops a worker with SIGTERM first: leave nothing of any session behind."""
    for agent in list(LIVE):
        agent.close()
    os._exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, stop)
    host, port = os.environ.get("COLLAB_COG_HOST", "127.0.0.1"), int(os.environ["COLLAB_COG_PORT"])
    log(f"serving run {RUN} on {host}:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()
