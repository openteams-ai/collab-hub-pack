"""`collab-hub run connect RUN`: a running Cog as an ACP agent, for any ACP client.

The Agent Client Protocol (https://agentclientprotocol.com) is JSON-RPC over the
agent's stdin and stdout, one message per line. A client such as Toad starts
this command as its agent, and each prompt it sends becomes one turn of the
run: posted to the hub (``POST /v1/runs/{id}/turns``), delivered by the run
controller to the Cog's worker, and read back once answered. The answer is
streamed to the client as the agent's message.

The client talks to the hub only through this process, with the profile's
session: it never reaches the worker, and every turn is on the run's Track.
stdout carries the protocol and nothing else; messages for a person go to
stderr.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from typing import IO, Any

from . import __version__
from .hub import Hub, HubError
from .oidc import AuthError, RealmError

PROTOCOL_VERSION = 1
METHOD_NOT_FOUND, INVALID_PARAMS, HUB_FAILED = -32601, -32602, -32000


class Bridge:
    """One ACP connection: the client on the other end of stdin and stdout, the run on the hub."""

    def __init__(self, hub: Hub, run_id: str, stdin: IO[str], stdout: IO[str], *, poll_seconds: float = 0.25,
                 answer_timeout: float = 300.0):
        self.hub = hub
        self.run_id = run_id
        self.stdin = stdin
        self.stdout = stdout
        self.poll_seconds = poll_seconds
        self.answer_timeout = answer_timeout
        self._write = threading.Lock()
        self._cancelled: set[str] = set()
        self._sessions: set[str] = set()
        self._prompts: list[threading.Thread] = []

    # --- the wire -----------------------------------------------------------------------------

    def _send(self, message: dict[str, Any]) -> None:
        with self._write:
            self.stdout.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
            self.stdout.flush()

    def _result(self, request_id: Any, result: dict[str, Any]) -> None:
        self._send({"id": request_id, "result": result})

    def _error(self, request_id: Any, code: int, message: str) -> None:
        self._send({"id": request_id, "error": {"code": code, "message": message}})

    def _say(self, session_id: str, text: str) -> None:
        self._send({"method": "session/update", "params": {
            "sessionId": session_id,
            "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}},
        }})

    def serve(self) -> None:
        """Answer the client until it closes stdin."""
        for line in self.stdin:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except ValueError:
                print(f"collab-hub: not JSON from the client: {line[:200]!r}", file=sys.stderr)
                continue
            if isinstance(message, dict) and "method" in message:
                self._dispatch(message)
            # Anything else is a response to a request this agent never makes.
        for prompt in self._prompts:
            prompt.join()

    def _dispatch(self, message: dict[str, Any]) -> None:
        method, params, request_id = message["method"], message.get("params") or {}, message.get("id")
        if method == "session/cancel":
            self._cancelled.add(params.get("sessionId"))
            return
        if request_id is None:
            return  # a notification this agent has no use for
        try:
            if method == "initialize":
                self._result(request_id, self._initialize())
            elif method == "session/new":
                self._result(request_id, self._new_session())
            elif method == "session/prompt":
                session_id = params.get("sessionId")
                if session_id not in self._sessions:
                    self._error(request_id, INVALID_PARAMS, f"no session {session_id!r}")
                    return
                # A prompt waits on the hub; the loop keeps reading, so a cancel can reach it. A cancel
                # left from an earlier prompt is cleared here, before reading on, so one sent right
                # behind this prompt is never lost.
                self._cancelled.discard(session_id)
                prompt = threading.Thread(target=self._prompt, args=(request_id, session_id, params), daemon=True)
                self._prompts.append(prompt)
                prompt.start()
            else:
                self._error(request_id, METHOD_NOT_FOUND, f"{method} is not offered by this agent")
        except (HubError, AuthError, RealmError) as exc:
            self._error(request_id, HUB_FAILED, str(exc))

    # --- the methods --------------------------------------------------------------------------

    def _initialize(self) -> dict[str, Any]:
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "agentCapabilities": {
                "loadSession": False,
                "promptCapabilities": {"image": False, "audio": False, "embeddedContext": False},
            },
            "authMethods": [],
            "agentInfo": {"name": "collab-hub", "title": f"Collab Hub · {self.run_id}", "version": __version__},
        }

    def _new_session(self) -> dict[str, Any]:
        run = self.hub.get_json(f"/v1/runs/{self.run_id}")
        if run["ended"]:
            raise HubError(f"run {self.run_id} has ended {run['status']}: launch another to talk to")
        session_id = f"{self.run_id}-{len(self._sessions) + 1}"
        self._sessions.add(session_id)
        print(f"collab-hub: connected to {self.run_id} ({', '.join(s['cog'] for s in run['steps'])}) on {self.hub.url}",
              file=sys.stderr)
        return {"sessionId": session_id}

    def _prompt(self, request_id: Any, session_id: str, params: dict[str, Any]) -> None:
        text = "\n".join(block.get("text", "") for block in params.get("prompt") or []
                         if isinstance(block, dict) and block.get("type") == "text").strip()
        if not text:
            self._say(session_id, "Say something in words: this Cog reads text only.")
            self._result(request_id, {"sessionId": session_id, "stopReason": "end_turn"})
            return
        try:
            turn = self.hub.request_json("POST", f"/v1/runs/{self.run_id}/turns", json={"text": text})
            deadline = time.monotonic() + self.answer_timeout
            while turn["state"] == "pending" and time.monotonic() < deadline:
                if session_id in self._cancelled:
                    # The turn stays on the Track and may still be answered there; this prompt stops waiting.
                    self._result(request_id, {"sessionId": session_id, "stopReason": "cancelled"})
                    return
                time.sleep(self.poll_seconds)
                turn = self.hub.get_json(f"/v1/runs/{self.run_id}/turns/{turn['turn']}")
            if turn["state"] == "pending":
                status = self.hub.get_json(f"/v1/runs/{self.run_id}")["status"]
        except (HubError, AuthError, RealmError) as exc:
            self._say(session_id, f"The hub could not deliver this: {exc}")
        else:
            if turn["state"] == "answered":
                self._say(session_id, turn["answer"])
            elif turn["state"] == "pending":
                self._say(session_id, f"No answer within {self.answer_timeout:g} seconds: the run is {status}"
                          + (", and no run controller has picked it up" if status == "SUBMITTED" else "")
                          + ". The message stays on the run, and is answered if its Cog comes up.")
            else:
                self._say(session_id, f"The Cog did not answer: {turn['error']}")
        self._result(request_id, {"sessionId": session_id, "stopReason": "end_turn"})


def connect(hub: Hub, run_id: str, *, stdin: IO[str] = sys.stdin, stdout: IO[str] = sys.stdout) -> None:
    Bridge(hub, run_id, stdin, stdout).serve()
