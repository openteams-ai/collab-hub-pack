"""A stub realm and a stub hub behind one ``httpx.MockTransport``, and a browser that follows the redirect."""

from __future__ import annotations

import base64
import hashlib
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import pytest
from typer.testing import CliRunner

from collab_hub_cli import hub, main, oidc

HUB = "https://hub.test"
OTHER_HUB = "https://hub-two.test"  # a second hub on the same realm
ISSUER = "https://id.test/realms/nebari"
CLIENT = "apollo-desktop"


def jwt(claims: dict) -> str:
    def part(value: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


@dataclass
class Stub:
    """The realm and the hub, and a record of what was asked of them."""

    dev_auth: bool = False
    issuer: str | None = ISSUER
    users: dict[str, str] = field(default_factory=dict)  # access token -> user
    refreshable: dict[str, str] = field(default_factory=dict)  # refresh token -> user
    requests: list[httpx.Request] = field(default_factory=list)
    authorizations: list[dict] = field(default_factory=list)
    exchanges: list[dict] = field(default_factory=list)
    ended: list[dict] = field(default_factory=list)
    revoked: list[dict] = field(default_factory=list)
    publishes_revocation: bool = True
    cogs: list[dict] = field(default_factory=list)
    runs: list[dict] = field(default_factory=list)  # newest first, as the hub lists them
    launchable: tuple[str, ...] = ("echo", "slow")
    turns: list[dict] = field(default_factory=list)
    hold_turns: bool = False  # leave every turn pending, as a worker that has not answered yet
    # What a run's status becomes on each later read: a list consumed one status per GET.
    progress: dict[str, list[str]] = field(default_factory=dict)
    counter: int = 0
    issued: list[dict] = field(default_factory=list)  # every token response, in order
    sso: bool = False  # a browser still signed in to the realm: every sign-in joins the same realm session
    sessions: dict[str, bool] = field(default_factory=dict)  # realm session id -> open
    # Failures to inject: an HTTP status, or an exception to raise, per endpoint.
    fail: dict[str, int | Exception] = field(default_factory=dict)
    discovery: dict = field(default_factory=dict)  # overrides of the discovery document

    def issue(self, user: str, sid: str) -> dict:
        self.counter += 1
        access = jwt({"sid": sid, "typ": "Bearer", "n": self.counter})
        refresh = jwt({"sid": sid, "typ": "Offline", "n": self.counter})
        self.users[access], self.refreshable[refresh] = user, user
        body = {"access_token": access, "refresh_token": refresh, "expires_in": 300, "token_type": "Bearer"}
        self.issued.append(body)
        return body

    def _sign_in_session(self) -> str:
        if self.sso and self.sessions:
            return next(iter(self.sessions))
        sid = f"session-{len(self.sessions) + 1}"
        self.sessions[sid] = True
        return sid

    def end(self, token: str | None) -> None:
        """End the realm session a token belongs to: none of its refresh tokens renews again."""

        from collab_hub_cli.oidc import session_id

        sid = session_id(token)
        if sid in self.sessions:
            self.sessions[sid] = False
        self.refreshable = {t: u for t, u in self.refreshable.items() if session_id(t) != sid}

    def alive(self, refresh_token: str) -> bool:
        from collab_hub_cli.oidc import session_id

        return refresh_token in self.refreshable and self.sessions.get(session_id(refresh_token), False)

    def _failure(self, name: str) -> httpx.Response | None:
        failure = self.fail.get(name)
        if isinstance(failure, Exception):
            raise failure
        if isinstance(failure, int):
            return httpx.Response(failure, text="upstream unavailable")
        return None

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = request.url
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()} if request.method == "POST" else {}
        base = f"{url.scheme}://{url.host}"
        if base == "https://id.test":
            return self._realm(url.path, form)
        if base in (HUB, OTHER_HUB):
            return self._hub(request)
        return httpx.Response(599)

    def _realm(self, path: str, form: dict) -> httpx.Response:
        realm = "/realms/nebari/protocol/openid-connect"
        if path == "/realms/nebari/.well-known/openid-configuration":
            return self._failure("discovery") or httpx.Response(200, json={
                "issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/protocol/openid-connect/auth",
                "token_endpoint": f"{ISSUER}/protocol/openid-connect/token",
                "end_session_endpoint": f"{ISSUER}/protocol/openid-connect/logout",
                **({"revocation_endpoint": f"{ISSUER}/protocol/openid-connect/revoke"}
                   if self.publishes_revocation else {}),
                **self.discovery,
            })
        if path == f"{realm}/token" and form.get("grant_type") == "authorization_code":
            self.exchanges.append(form)
            [asked] = [a for a in self.authorizations if a["code"] == form["code"]]
            challenge = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest())
            if (challenge.decode().rstrip("=") != asked["code_challenge"]
                    or form["redirect_uri"] != asked["redirect_uri"] or form["client_id"] != CLIENT):
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json=self.issue("alice", self._sign_in_session()))
        if path == f"{realm}/token" and form.get("grant_type") == "refresh_token":
            if failed := self._failure("refresh"):
                return failed
            if not self.alive(form["refresh_token"]):
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Token is not active"})
            from collab_hub_cli.oidc import session_id

            user = self.refreshable.pop(form["refresh_token"])
            return httpx.Response(200, json=self.issue(user, session_id(form["refresh_token"])))
        if path == f"{realm}/logout":
            if failed := self._failure("logout"):
                return failed
            self.ended.append(form)
            self.end(form.get("refresh_token"))
            return httpx.Response(204)
        if path == f"{realm}/revoke":
            if failed := self._failure("revoke"):
                return failed
            # RFC 7009 §2.2: an invalid or already revoked token is still a 200.
            self.revoked.append(form)
            self.end(form.get("token"))
            return httpx.Response(200)
        return httpx.Response(404)

    def _hub(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/auth/cli":
            body = {"issuer": self.issuer, "client_id": CLIENT, "dev_auth": self.dev_auth}
            return self._failure("auth_cli") or httpx.Response(200, json=body)
        header = request.headers.get("authorization", "")
        token = header.removeprefix("Bearer ") if header else None
        user = self.users.get(token) if token else ("dev-user" if self.dev_auth else None)
        if user is None:
            return httpx.Response(401, json={"detail": "Invalid bearer token" if token else "Authentication required"})
        if path == "/v1/me":
            return self._failure("me") or httpx.Response(200, json={
                "user": user, "org_id": "org-a", "workspace_id": "default", "org_role": "owner",
                "platform_role": None, "name": None, "email": None,
                "authenticated_by": "token" if token else "dev",
            })
        if path == "/v1/cogs":
            limit, offset = int(request.url.params["limit"]), int(request.url.params["offset"])
            items = [c for c in self.cogs if request.url.params.get("kind") in (None, c["card"].get("kind"))]
            page = items[offset:offset + limit]
            more = offset + limit < len(items)
            return httpx.Response(200, json={"items": page, "limit": limit, "offset": offset,
                                             "next_offset": offset + limit if more else None})
        if path.startswith("/v1/cogs/"):
            cog_id = path.removeprefix("/v1/cogs/")
            for cog in self.cogs:
                if cog["cog_id"] == cog_id:
                    return httpx.Response(200, json={**cog, "versions": [cog]})
            return httpx.Response(404, json={"error": {"code": "cog_not_found", "message": f"No Cog {cog_id}"}})
        if path == "/v1/runs" and request.method == "POST":
            submitted = json.loads(request.content)
            [step] = submitted["steps"]
            if step["cog"] not in self.launchable:
                return httpx.Response(422, json={"error": {
                    "code": "cog_not_launchable",
                    "message": f"Cannot launch {step['cog']}; the Cogs this hub launches are: "
                               f"{', '.join(self.launchable)}"}})
            run = {"id": f"run-{len(self.runs) + 1:012d}", "status": "SUBMITTED", "ended": False,
                   "steps": [{"name": step["name"], "cog": step["cog"], "entry_point": step["entry_point"],
                              "state": "pending", "attempt": None, "error": None}],
                   "submitted_by": user, "submitted_at": "2026-10-02T08:00:00+00:00",
                   "updated_at": "2026-10-02T08:00:00+00:00", "cancel_requested_by": None, "error": None,
                   "reason": None, "backend": "none", "location": "local", "_input": step["input"],
                   "name": submitted.get("name"),
                   "_gate": step["gate"]}
            self.runs.insert(0, run)
            return httpx.Response(201, json=run)
        if path == "/v1/runs":
            wanted = request.url.params.get("status")
            limit, offset = int(request.url.params["limit"]), int(request.url.params["offset"])
            items = [r for r in self.runs if wanted is None or r["status"] == wanted.upper()]
            more = offset + limit < len(items)
            return httpx.Response(200, json={"items": items[offset:offset + limit],
                                             "next_offset": offset + limit if more else None})
        if path == "/v1/runs/launchable":
            return httpx.Response(200, json={"items": list(self.launchable)})
        if path.startswith("/v1/runs/"):
            run_id, _, action = path.removeprefix("/v1/runs/").partition("/")
            run = next((r for r in self.runs if r["id"] == run_id), None)
            if run is None:
                return httpx.Response(404, json={"error": {"code": "run_not_found", "message": f"No run {run_id}"}})
            if action == "turns" and request.method == "POST":
                if run["ended"]:
                    return httpx.Response(409, json={"error": {
                        "code": "run_ended",
                        "message": f"Run {run_id} takes no turns: the run has ended {run['status']}"}})
                text = json.loads(request.content)["text"]
                turn = {"turn": f"{len(self.turns) + 1:012x}", "text": text, "state": "pending", "answer": None,
                        "error": None, "asked_by": user}
                self.turns.append(turn)
                return httpx.Response(202, json=turn)
            if action.startswith("turns/"):
                turn = next(t for t in self.turns if t["turn"] == action.removeprefix("turns/"))
                if turn["state"] == "pending" and not self.hold_turns:
                    failing = turn["text"] == "fail"
                    turn.update(state="failed" if failing else "answered",
                                answer=None if failing else f"you said: {turn['text']}",
                                error="the worker answered HTTP 500" if failing else None)
                return httpx.Response(200, json=turn)
            if action == "cancel":
                if run["ended"]:
                    return httpx.Response(409, json={"error": {
                        "code": "run_ended",
                        "message": f"Run {run_id} cannot be cancelled: the run has ended {run['status']}"}})
                run["cancel_requested_by"] = user
                return httpx.Response(202, json=run)
            if self.progress.get(run_id):
                run["status"] = self.progress[run_id].pop(0)
                run["ended"] = run["status"] not in ("SUBMITTED", "RUNNING", "WAITING_AT_GATE")
                if run["status"] == "COMPLETED":
                    run["steps"][0].update(state="completed", output={"greeting": "hi"})
            return httpx.Response(200, json=run)
        return httpx.Response(404)

    def browser(self, approve: bool = True):
        """What a browser does with the sign-in URL: the realm signs the user in and redirects back."""

        def open_url(url: str) -> bool:
            query = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
            code = f"code-{len(self.authorizations)}"
            self.authorizations.append({**query, "code": code})
            answer = {"code": code} if approve else {"error": "access_denied", "error_description": "denied"}
            try:
                urllib.request.urlopen(f"{query['redirect_uri']}?{urlencode({**answer, 'state': query['state']})}")
            except urllib.error.HTTPError:
                pass
            return True

        return open_url


@pytest.fixture
def stub(monkeypatch, tmp_path) -> Stub:
    stub = Stub()
    monkeypatch.setattr(hub, "transport", httpx.MockTransport(stub.handle))
    monkeypatch.setattr(main.webbrowser, "open", stub.browser())
    monkeypatch.setenv("COLLAB_HUB_CONFIG_DIR", str(tmp_path / "config"))
    for name in ("COLLAB_HUB_URL", "COLLAB_HUB_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(oidc, "clock", lambda: 1_000_000.0)
    return stub


@pytest.fixture
def cli(monkeypatch, tmp_path):
    # A configuration directory of the test's own, with or without the stub: the machine's own
    # profile may well name a hub and hold a session.
    monkeypatch.setenv("COLLAB_HUB_CONFIG_DIR", str(tmp_path / "config"))
    for name in ("COLLAB_HUB_URL", "COLLAB_HUB_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    runner = CliRunner()

    def invoke(*args: str, input: str | None = None):
        return runner.invoke(main.app, list(args), input=input)

    return invoke
