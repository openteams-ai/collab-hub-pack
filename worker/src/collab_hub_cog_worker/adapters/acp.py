"""Any agent that speaks the Agent Client Protocol (https://agentclientprotocol.com), over stdio.

The adapter is the agent's ACP client: it starts the agent as a child process,
speaks JSON-RPC to it one message per line, opens one session, and answers each
prompt with what the agent said. The SDK folds the session's updates into the
envelope's ``raw`` and the agent's message chunks into the answer.

What makes an interaction fail is an explicit signal — the agent refusing the
request with a JSON-RPC error, the agent exiting, or a turn that ends with
nothing said — and never the stop reason alone: Hermes reports a failed turn
as ``end_turn`` like any other. A permission the agent asks for is granted only
for a tool the Cog declared; the rest are refused, since nobody is there to
answer.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ..envelope import Cancelled, WorkerError
from ..limits import looks_like_overflow
from ..task import Context, Frame, Interaction, Prompt, Signal

PROTOCOL_VERSION = 1

# What the agent's process inherits from the worker's: enough to run, and no credential.
INHERITS = ("PATH", "LANG", "LC_ALL", "TZ", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR", "PYTHONPATH")


class _Pending:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: Any = None
        self.error: Mapping[str, Any] | None = None


class AcpHarnessAdapter:
    """One ACP agent process, one session, one prompt at a time.

    ``command`` starts the agent; ``env`` is its whole environment (``inherits``
    from the worker's own when ``None``); ``cwd`` is the session's working
    directory. ``allowed_tools`` names the tools whose permission requests are
    granted, by the tool call's ``kind`` or ``title``; everything else is refused.
    """

    client_name = "collab-hub-cog-worker"
    client_version = "0.1.0"

    def __init__(self, command: Sequence[str], *, env: Mapping[str, str] | None = None, cwd: str | None = None,
                 inherits: Iterable[str] = INHERITS, allowed_tools: Iterable[str] = (),
                 stderr: Any = None) -> None:
        self.command = list(command)
        self.env = dict(env) if env is not None else None
        self.inherits = tuple(inherits)
        self.cwd = cwd
        self.allowed_tools = frozenset(allowed_tools)
        self.stderr = stderr
        self.process: subprocess.Popen[str] | None = None
        self.session: str | None = None
        self.capabilities: dict[str, Any] = {}
        self._next_id = 0
        self._pending: dict[int, _Pending] = {}
        self._updates: list[dict[str, Any]] = []
        self._lock = threading.Lock()  # one prompt at a time; writes to the agent are serialized
        self._write_lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._in_flight: int | None = None
        self._closed = False

    # --- the harness's environment: what a subclass shapes --------------------------------------

    def environment(self, context: Context) -> dict[str, str]:
        """The agent's environment: given outright, or the allowlisted part of the worker's."""
        if self.env is not None:
            return dict(self.env)
        return {name: os.environ[name] for name in self.inherits if name in os.environ}

    def working_directory(self, context: Context) -> str:
        return self.cwd or context.work_dir or os.getcwd()

    # --- lifecycle ---------------------------------------------------------------------------

    def start(self, context: Context) -> None:
        if self._closed:
            raise WorkerError("model-unavailable", f"{self.name} was closed before it started")
        cwd = self.working_directory(context)
        try:
            self.process = subprocess.Popen(  # noqa: S603 - the Cog's own agent command
                self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=self.stderr if self.stderr is not None else sys.stderr, text=True,
                env=self.environment(context), cwd=cwd)
        except OSError as exc:
            raise WorkerError("model-unavailable", f"the agent could not be started: {exc}") from None
        self._reader = threading.Thread(target=self._read, name="acp-reader", daemon=True)
        self._reader.start()
        try:
            answer = self.call("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
                "clientInfo": {"name": self.client_name, "version": self.client_version}})
            self.capabilities = dict(answer.get("agentCapabilities") or {})
            self.session = self.call("session/new", {"cwd": cwd, "mcpServers": []})["sessionId"]
        except BaseException:
            self.close()
            raise

    @property
    def name(self) -> str:
        """What the agent is called in what the worker reports: ``the agent`` unless a Cog says otherwise."""
        return "the agent"

    def alive(self) -> bool:
        return not self._closed and self.process is not None and self.process.poll() is None

    def close(self) -> None:
        self._closed = True  # first, so a start or a call under way sees it is over
        process, self.process = self.process, None
        if process is None:
            return
        if process.poll() is None:
            try:
                process.stdin.close()
                process.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
                process.wait(timeout=5)
        for pending in list(self._pending.values()):
            pending.error = {"message": "the agent was closed"}
            pending.done.set()

    def cancel(self) -> None:
        """``session/cancel``: the agent ends the prompt in flight with ``stopReason: cancelled``."""
        if self.session is not None and self._in_flight is not None and self.alive():
            self.notify("session/cancel", {"sessionId": self.session})

    # --- the interaction ---------------------------------------------------------------------

    def interact(self, prompt: Prompt, context: Context, signal: Signal) -> Interaction:
        if signal.is_set():
            raise Cancelled()
        with self._lock:
            self._updates = []
            blocks = [{"type": "text", "text": prompt.text}] + [self.frame_block(frame) for frame in prompt.frames]
            self._next_id += 1
            request_id = self._next_id
            self._in_flight = request_id
            try:
                pending = self._send(request_id, "session/prompt", {"sessionId": self.session, "prompt": blocks})
                while not pending.done.wait(0.1):
                    if signal.is_set():
                        self.cancel()
                        pending.done.wait(5)
                        raise Cancelled()
                    if not self.alive() and not pending.done.is_set():
                        raise WorkerError("model-call-failed",
                                          f"{self.name} exited (code {self._exit_code()}) during the prompt")
            finally:
                self._in_flight = None
            if pending.error is None and pending.result is None:
                # The reader let go of the request without an answer: the agent closed its stdout.
                raise WorkerError("model-call-failed",
                                  f"{self.name} exited (code {self._exit_code()}) during the prompt")
            if pending.error is not None:
                message = str(pending.error.get("message", "the agent refused the prompt"))
                code = "context-exhausted" if looks_like_overflow(message) else "model-call-failed"
                raise WorkerError(code, f"the agent answered an error: {message}")
            stop = (pending.result or {}).get("stopReason")
            said = "".join(self._texts(self._updates))
            raw = "\n".join(json.dumps(update) for update in self._updates) or None
            if stop == "cancelled" or signal.is_set():
                raise Cancelled()
            if not said.strip():
                raise WorkerError("model-response-malformed",
                                  f"the agent ended its turn ({stop or 'no stop reason'}) without saying anything")
            return Interaction(text=said.strip(), raw=raw, usage=self._usage(pending.result), stop=stop)

    def frame_block(self, frame: Frame) -> dict[str, Any]:
        """A Frame as a prompt block: an embedded resource when the agent takes them, text otherwise."""
        embedded = bool((self.capabilities.get("promptCapabilities") or {}).get("embeddedContext"))
        if embedded:
            return {"type": "resource", "resource": {"uri": frame.location, "mimeType": "text/markdown",
                                                     "text": frame.text}}
        return {"type": "text", "text": f"<frame id={frame.id!r} title={frame.name!r}>\n{frame.text}\n</frame>"}

    @staticmethod
    def _texts(updates: Iterable[Mapping[str, Any]]) -> Iterable[str]:
        for update in updates:
            if update.get("sessionUpdate") == "agent_message_chunk":
                content = update.get("content") or {}
                if content.get("type") == "text":
                    yield str(content.get("text", ""))

    @staticmethod
    def _usage(result: Any) -> Mapping[str, Any] | None:
        """Usage an agent reports on its prompt result (``usage`` or ``_meta.usage``), as the envelope counts it."""
        if not isinstance(result, Mapping):
            return None
        usage = result.get("usage") or (result.get("_meta") or {}).get("usage")
        if not isinstance(usage, Mapping):
            return None
        tokens = usage.get("totalTokens", usage.get("total_tokens"))
        report = {"tokens": tokens if isinstance(tokens, int) else 0,
                  "prompt_tokens": usage.get("inputTokens", usage.get("prompt_tokens", 0)),
                  "completion_tokens": usage.get("outputTokens", usage.get("completion_tokens", 0))}
        if not report["tokens"]:
            report["tokens"] = int(report["prompt_tokens"] or 0) + int(report["completion_tokens"] or 0)
        return report

    # --- JSON-RPC over stdio -----------------------------------------------------------------

    def call(self, method: str, params: Mapping[str, Any], timeout: float = 300.0) -> Any:
        """One request to the agent; its result, or ``WorkerError`` when it refused or is gone."""
        self._next_id += 1
        pending = self._send(self._next_id, method, params)
        while not pending.done.wait(0.1):
            if not self.alive():
                raise WorkerError("model-unavailable",
                                  f"{self.name} exited (code {self._exit_code()}) before answering {method}")
            timeout -= 0.1
            if timeout <= 0:
                raise WorkerError("model-unavailable", f"the agent did not answer {method} in time")
        if pending.error is not None:
            raise WorkerError("model-unavailable", f"the agent refused {method}: {pending.error.get('message')}")
        return pending.result

    def _exit_code(self) -> int | None:
        process = self.process
        if process is None:
            return None
        try:
            return process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            return None

    def notify(self, method: str, params: Mapping[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": dict(params)})

    def _send(self, request_id: int, method: str, params: Mapping[str, Any]) -> _Pending:
        pending = _Pending()
        self._pending[request_id] = pending
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)})
        return pending

    def _write(self, message: Mapping[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None:
            return
        with self._write_lock:
            try:
                process.stdin.write(json.dumps(message) + "\n")
                process.stdin.flush()
            except (OSError, ValueError):
                pass  # the agent is gone: the caller sees it through alive()

    def _read(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue  # not JSON-RPC: an agent's stray print
            if not isinstance(message, Mapping):
                continue
            if "method" in message and "id" in message:
                self._answer_request(message)
            elif message.get("method") == "session/update":
                update = (message.get("params") or {}).get("update")
                if isinstance(update, Mapping):
                    self._updates.append(dict(update))
            elif "id" in message:
                pending = self._pending.pop(message["id"], None)
                if pending is not None:
                    pending.result, pending.error = message.get("result"), message.get("error")
                    pending.done.set()
        for pending in list(self._pending.values()):
            pending.done.set()  # the agent closed its stdout: whoever waits sees it is gone

    def _answer_request(self, request: Mapping[str, Any]) -> None:
        """A request from the agent to its client: a permission is granted only for a declared tool."""
        if request["method"] == "session/request_permission":
            params = request.get("params") or {}
            tool = params.get("toolCall") or {}
            names = {str(tool.get("kind") or ""), str(tool.get("title") or "")}
            options = params.get("options") or []
            allow = next((o for o in options if str(o.get("kind", "")).startswith("allow")), None)
            if names & self.allowed_tools and allow is not None:
                outcome = {"outcome": "selected", "optionId": allow.get("optionId")}
            else:
                outcome = {"outcome": "cancelled"}
            self._write({"jsonrpc": "2.0", "id": request["id"], "result": {"outcome": outcome}})
        else:
            self._write({"jsonrpc": "2.0", "id": request["id"],
                         "error": {"code": -32601, "message": "not offered by this client"}})
