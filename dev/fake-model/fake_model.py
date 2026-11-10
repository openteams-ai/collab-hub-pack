"""A fake model: an OpenAI-compatible endpoint that answers without one. Standard library only.

    python dev/fake-model/fake_model.py [--host 127.0.0.1] [--port 8090]

Serves `GET /v1/models` and `POST /v1/chat/completions`, streamed or not. Its
answer is a fixed sentence quoting the last thing the user said, so a Cog that
calls a model (the Hermes Cog) can be run, and checked, with no account and no
network. Point a Cog at it with `COLLAB_MODEL_BASE_URL=http://127.0.0.1:8090/v1`.
"""

import argparse
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "fake-model"


def answer(messages):
    """What the fake model says: the user's last words, quoted."""
    last = next((message for message in reversed(messages) if message.get("role") == "user"), {})
    content = last.get("content") or ""
    if isinstance(content, list):
        content = " ".join(part.get("text", "") for part in content if isinstance(part, dict))
    said = " ".join(str(content).split())
    heard = f"The fake model heard: {said[-300:]}"
    # Context messages a worker sent beside the prompt (the step's Frames) are named, so a test sees they arrived.
    frames = [line[3:].strip() for message in messages if message.get("role") == "system"
              for line in str(message.get("content") or "").splitlines() if line.startswith("## ")]
    return heard + (f" [frames: {', '.join(frames)}]" if frames else "")


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/").endswith("/models"):
            return self._json(200, {"object": "list", "data": [{"id": MODEL, "object": "model", "owned_by": "dev"}]})
        if self.path == "/health":
            return self._json(200, {"ok": True})
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):  # noqa: N802
        if not self.path.rstrip("/").endswith("/chat/completions"):
            return self._json(404, {"error": {"message": "not found"}})
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if "overflow" in json.dumps(request.get("messages") or []):
            # As a real endpoint refuses a prompt its model cannot fit.
            return self._json(400, {"error": {"code": "context_length_exceeded",
                                              "message": "This model's maximum context length is exceeded"}})
        text, created = answer(request.get("messages") or []), int(time.time())
        usage = {"prompt_tokens": 10, "completion_tokens": len(text.split()), "total_tokens": 10 + len(text.split())}
        if not request.get("stream"):
            return self._json(200, {
                "id": "chatcmpl-fake", "object": "chat.completion", "created": created, "model": MODEL, "usage": usage,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            })
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        chunk = {"id": "chatcmpl-fake", "object": "chat.completion.chunk", "created": created, "model": MODEL}

        def send(choice, **extra):
            self.wfile.write(f"data: {json.dumps({**chunk, 'choices': [choice], **extra})}\n\n".encode())
            self.wfile.flush()

        send({"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None})
        for word in text.split(" "):
            send({"index": 0, "delta": {"content": word + " "}, "finish_reason": None})
        send({"index": 0, "delta": {}, "finish_reason": "stop"}, usage=usage)
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def log_message(self, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    print(f"fake model on http://{args.host}:{args.port}/v1 (model {MODEL})", file=sys.stderr, flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
