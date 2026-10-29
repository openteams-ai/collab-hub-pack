from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ACCESS_TOKEN = "fake-slack-access-token"
READ_SCOPES = "channels:read,channels:history,groups:read,groups:history,search:read"
CHANNEL = {
    "id": "C0001",
    "name": "proposals",
    "is_channel": True,
    "is_private": False,
    "topic": {"value": "Proposal work"},
    "num_members": 12,
}
DM = {"id": "D0001", "is_im": True, "user": "U0002"}
GROUP_DM = {"id": "G0002", "name": "mpdm-alice--bob", "is_mpim": True}
CHANNEL_MESSAGES = [
    # Carries a Slack link entity and a plain URL to prove link handling end to end
    # (#143): the read keeps both http(s) targets exactly, as "label (url)" and bare.
    {
        "ts": "1783041900.111000",
        "user": "U0002",
        "text": "Kickoff notes in <https://intranet.test/kickoff|the kickoff doc>; mirror at https://example.test/raw",
        "reply_count": 0,
    },
    {"ts": "1783041866.494000", "user": "U0001", "text": "Do we have healthcare kickoff notes?", "reply_count": 0},
]
SEARCH_MATCH = {
    "ts": "1783041866.494000",
    "user": "U0001",
    "username": "alice",
    "text": "Do we have healthcare kickoff notes?",
    "permalink": "https://slack.test/archives/C0001/p1783041866494000",
    "channel": {"id": "C0001", "name": "proposals", "is_im": False},
}
DM_SEARCH_MATCH = {
    **SEARCH_MATCH,
    "text": "Private healthcare kickoff note",
    "channel": {"id": "D0001", "name": "", "is_im": True},
}
GROUP_DM_SEARCH_MATCH = {
    **SEARCH_MATCH,
    "text": "Private group healthcare kickoff note",
    "channel": {"id": "G0002", "name": "mpdm-alice--bob", "is_mpim": True},
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self.respond_json({"ok": True})
            return
        if parsed.path == "/broker/token":
            if not self.headers.get("Authorization", "").startswith("Bearer "):
                self.respond_json({"error": "missing bearer"}, status=401)
                return
            self.respond_json({"access_token": ACCESS_TOKEN, "expires_in": 3600})
            return
        if not self.headers.get("Authorization", "") == f"Bearer {ACCESS_TOKEN}":
            self.respond_json({"ok": False, "error": "invalid_auth"}, status=401)
            return
        query = parse_qs(parsed.query)
        if parsed.path == "/api/auth.test":
            # A real Web API user token answers auth.test with ok:true and advertises
            # its granted scopes; an OpenID sign-in token would fail here instead.
            self.respond_json(
                {"ok": True, "url": "https://slack.test/", "team": "Acme", "user": "alice", "user_id": "U0001"},
                extra_headers={"X-OAuth-Scopes": READ_SCOPES},
            )
            return
        if parsed.path == "/api/conversations.list":
            types = query.get("types", [""])[0]
            if "im" in types.split(","):
                self.respond_json({"ok": True, "channels": [DM, GROUP_DM], "response_metadata": {"next_cursor": ""}})
                return
            self.respond_json({"ok": True, "channels": [CHANNEL], "response_metadata": {"next_cursor": ""}})
            return
        if parsed.path == "/api/conversations.info":
            channel_id = query.get("channel", [""])[0]
            conversations = {item["id"]: item for item in (CHANNEL, DM, GROUP_DM)}
            if channel_id not in conversations:
                self.respond_json({"ok": False, "error": "channel_not_found"})
                return
            self.respond_json({"ok": True, "channel": conversations[channel_id]})
            return
        if parsed.path == "/api/search.messages":
            if "healthcare" not in query.get("query", [""])[0]:
                self.respond_json({"ok": True, "messages": {"matches": []}})
                return
            self.respond_json(
                {
                    "ok": True,
                    "messages": {"matches": [DM_SEARCH_MATCH, SEARCH_MATCH, GROUP_DM_SEARCH_MATCH]},
                }
            )
            return
        if parsed.path == "/api/conversations.history":
            if query.get("channel", [""])[0] != "C0001":
                self.respond_json({"ok": False, "error": "channel_not_found"})
                return
            self.respond_json({"ok": True, "messages": CHANNEL_MESSAGES, "has_more": False})
            return
        if parsed.path == "/api/conversations.replies":
            self.respond_json({"ok": True, "messages": CHANNEL_MESSAGES, "has_more": False})
            return
        self.respond_json({"ok": False, "error": "unknown_method"}, status=404)

    def log_message(self, format: str, *args) -> None:
        return

    def respond_json(self, payload: dict, *, status: int = 200, extra_headers: dict | None = None) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
