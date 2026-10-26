"""A fake ACP agent: an agent on stdio that answers without a harness or a model. Standard library only.

    python dev/fake-agent/fake_acp_agent.py [--embedded-context] [--slow-start] [--exit-on-initialize]

A stand-in for `hermes acp`, or any agent that speaks the Agent Client Protocol
(https://agentclientprotocol.com): it echoes each prompt as `agent heard: ...`,
so the worker SDK's ACP adapter and the Hermes Cog's worker can be run, and
checked, with no agent installed. A few prompts make it misbehave the way an
agent can:

    fail         answers the prompt with a JSON-RPC error
    overflow     answers with an error saying the context length was exceeded
    silence      ends the turn having said nothing
    slow         takes two seconds, and honours session/cancel meanwhile
    use a tool   asks the client's permission for a tool, and reports the answer
    exit         exits mid-session, as a crashing agent would
    env          reports its environment: the Hermes Cog's tests read it

With `--embedded-context` it advertises `promptCapabilities.embeddedContext`
and echoes each resource block it receives; without it, only text blocks.
`--exit-on-initialize` exits on the first request, as an agent that fails on
import does; `--slow-start` sleeps a minute first, as a cold start can.
"""

import json
import os
import sys
import time
from pathlib import Path

EMBEDDED = "--embedded-context" in sys.argv
SESSION = "s-1"


def send(message):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
    sys.stdout.flush()


def say(text):
    send({"method": "session/update", "params": {"sessionId": SESSION, "update": {
        "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}}})


def environment():
    report = {"cwd": os.getcwd(), "home": os.environ.get("HOME"), "run_token": "COLLAB_RUN_TOKEN" in os.environ,
              "api_key_env": "COLLAB_MODEL_API_KEY" in os.environ, "anthropic_key": os.environ.get("ANTHROPIC_API_KEY"),
              "gemini_key": "GEMINI_API_KEY" in os.environ,
              "ssl_cert_file": os.environ.get("SSL_CERT_FILE"), "requests_ca_bundle": os.environ.get("REQUESTS_CA_BUNDLE")}
    home = os.environ.get("HERMES_HOME")
    if home and (Path(home) / "config.yaml").is_file():
        config = json.loads((Path(home) / "config.yaml").read_text())
        report.update(model=config.get("model"), security=config.get("security"))
    return report


def prompt(message, params):
    """One session/prompt: what the agent says, and how the turn stops."""
    texts = [block.get("text", "") for block in params["prompt"] if block.get("type") == "text"]
    resources = [block["resource"] for block in params["prompt"] if block.get("type") == "resource"]
    text = " ".join(texts).strip()
    first = texts[0].strip() if texts else ""
    if first == "exit":
        sys.exit(3)
    if first == "fail":
        return send({"id": message["id"], "error": {"code": -32603, "message": "the agent could not answer"}})
    if first == "overflow":
        return send({"id": message["id"], "error": {"code": -32603,
                                                    "message": "context_length_exceeded: the prompt is too long"}})
    if first == "silence":
        return send({"id": message["id"], "result": {"stopReason": "end_turn"}})
    if first == "slow":
        for _ in range(20):
            time.sleep(0.1)
            if CANCELLED.get(SESSION):
                CANCELLED.pop(SESSION)
                return send({"id": message["id"], "result": {"stopReason": "cancelled"}})
        say("slowly: done")
    elif first == "use a tool":
        send({"id": "permission-1", "method": "session/request_permission", "params": {
            "sessionId": SESSION, "toolCall": {"toolCallId": "t", "title": "run a command", "kind": "execute"},
            "options": [{"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                        {"optionId": "reject", "name": "Reject", "kind": "reject_once"}]}})
        answer = json.loads(sys.stdin.readline())
        outcome = answer["result"]["outcome"]
        say(f"permission: {outcome['outcome']}" + (f" {outcome['optionId']}" if "optionId" in outcome else ""))
    elif first == "env":
        say(json.dumps(environment()))
    else:
        say("agent heard: ")
        say(text)
        for resource in resources:
            say(f" [resource {resource.get('uri')}: {resource.get('text', '')}]")
    send({"id": message["id"], "result": {"stopReason": "end_turn", "usage": {"inputTokens": 7, "outputTokens": 3}}})


CANCELLED = {}

if __name__ == "__main__":
    if "--slow-start" in sys.argv:
        time.sleep(60)  # as Hermes can be, on a first start: the session is still opening
    for line in sys.stdin:
        message = json.loads(line)
        method, params = message.get("method"), message.get("params") or {}
        if method == "initialize":
            if "--exit-on-initialize" in sys.argv:
                sys.exit(4)
            capabilities = {"promptCapabilities": {"embeddedContext": EMBEDDED, "image": False, "audio": False}}
            send({"id": message["id"], "result": {"protocolVersion": 1, "agentCapabilities": capabilities,
                                                  "authMethods": [], "agentInfo": {"name": "fake-acp-agent"}}})
        elif method == "session/new":
            send({"id": message["id"], "result": {"sessionId": SESSION}})
        elif method == "session/prompt":
            prompt(message, params)
        elif method == "session/cancel":
            CANCELLED[params.get("sessionId")] = True
