"""A stand-in for `hermes acp`: an ACP agent on stdio that echoes, to test the Hermes Cog's worker without Hermes."""

import json
import os
import sys
from pathlib import Path


def send(message):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
    sys.stdout.flush()


def say(session, text):
    send({"method": "session/update", "params": {"sessionId": session, "update": {
        "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}}})


if "--slow-start" in sys.argv:
    import time

    time.sleep(60)  # as Hermes can be, on a first start: the session is still opening

for line in sys.stdin:
    message = json.loads(line)
    method, params = message.get("method"), message.get("params") or {}
    if method == "initialize":
        send({"id": message["id"], "result": {"protocolVersion": 1, "agentCapabilities": {}, "authMethods": []}})
    elif method == "session/new":
        send({"id": message["id"], "result": {"sessionId": "s-1"}})
    elif method == "session/prompt":
        text = " ".join(block.get("text", "") for block in params["prompt"])
        if text == "exit":
            sys.exit(3)  # as Hermes would, crashing in the middle of a session
        if text == "use a tool":
            # Ask the client for permission, as Hermes does before a tool, and report its answer.
            send({"id": "permission-1", "method": "session/request_permission", "params": {
                "sessionId": "s-1", "toolCall": {"toolCallId": "t", "title": "run a command"},
                "options": [{"optionId": "allow", "name": "Allow", "kind": "allow_once"}]}})
            answer = json.loads(sys.stdin.readline())
            say("s-1", f"permission: {answer['result']['outcome']['outcome']}")
        elif text == "env":
            home = Path(os.environ["HERMES_HOME"])
            config = json.loads((home / "config.yaml").read_text())
            say("s-1", json.dumps({"model": config["model"], "security": config.get("security"),
                                   "cwd": os.getcwd(), "home": os.environ.get("HOME"),
                                   "run_token": "COLLAB_RUN_TOKEN" in os.environ,
                                   "api_key_env": "COLLAB_MODEL_API_KEY" in os.environ,
                                   "anthropic_key": os.environ.get("ANTHROPIC_API_KEY"),
                                   "gemini_key": "GEMINI_API_KEY" in os.environ,
                                   "ssl_cert_file": os.environ.get("SSL_CERT_FILE"),
                                   "requests_ca_bundle": os.environ.get("REQUESTS_CA_BUNDLE")}))
        else:
            say("s-1", "agent heard: ")
            say("s-1", text)
        send({"id": message["id"], "result": {"stopReason": "end_turn"}})
