from __future__ import annotations

import asyncio
import base64
import json
import logging
from contextlib import asynccontextmanager

import httpx
import pytest
from httpx import ASGITransport, AsyncClient, Response
from pydantic import ValidationError

from collab_hub_api.config import Config
from collab_hub_api.connectors.connector_text import sanitize_github_api_text
from collab_hub_api.connectors.github_client import (
    GitHubApiRequestError,
    GitHubClient,
    GitHubSearchError,
    GitHubUpstreamError,
    _project_filter_value,
    _raise_for_github_status,
)
from collab_hub_api.connectors.models import (
    GITHUB_READONLY_SCOPES,
    GitHubApiGetRequest,
    GitHubApiGetResponse,
    GitHubSearchRequest,
)
from collab_hub_api.core import make_app

STATIC_TOKEN = "gho_github-token-alice-secret"


@pytest.fixture(autouse=True)
def _allow_unsigned_bearer(monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_BEARER_ALLOW_UNSIGNED", "true")


def _jwt(payload: dict) -> str:
    def encode(part: dict) -> str:
        raw = json.dumps(part, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode(payload)}."


def _auth_header() -> dict[str, str]:
    token = _jwt({"preferred_username": "alice", "org_id": "org-a", "workspace_id": "workspace-a"})
    return {"Authorization": f"Bearer {token}"}


def _config(tmp_path, **github) -> Config:
    github_config = {
        "static_access_token": STATIC_TOKEN,
        "api_base_url": "https://github.test/api",
        # Generic read ships opt-in (default False); the feature tests enable it
        # explicitly. The disabled-path test overrides this back to False.
        "api_get_enabled": True,
        **github,
    }
    return Config.parse(
        {
            "storage": {"frames_path": str(tmp_path / "frames")},
            "frames": {"active_state": {"backend": "memory"}, "mcp_session_manager_enabled": False},
            "connectors": {"github": github_config},
        }
    )


@asynccontextmanager
async def _client(app):
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c


def _install_mock_client(monkeypatch, handler) -> None:
    original_async_client = httpx.AsyncClient

    def mock_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)


def _ok_status_handler(request: httpx.Request) -> Response:
    path = request.url.path
    if path.endswith("/user"):
        return Response(
            200,
            json={"login": "octocat", "id": 1},
            headers={"X-OAuth-Scopes": "repo, read:org, read:project, user:email"},
        )
    if path.endswith("/user/repos"):
        return Response(200, json=[{"full_name": "acme/widgets"}])
    return Response(404, json={"message": "Not Found"})


def _issue_item(number: int, *, body: str = "", title: str = "Example") -> dict:
    return {
        "number": number,
        "title": title,
        "state": "open",
        "user": {"login": "octocat"},
        "assignees": [{"login": "assignee1"}],
        "labels": [{"name": "bug"}],
        "comments": 2,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-02-01T00:00:00Z",
        "repository_url": "https://github.test/api/repos/acme/widgets",
        "body": body,
    }


# --- request model bounds -------------------------------------------------


def test_github_search_request_bounds() -> None:
    request = GitHubSearchRequest(query="bug", limit=25, repo="acme/widgets")
    assert request.limit == 25
    with pytest.raises(ValidationError):
        GitHubSearchRequest(query="", limit=1)
    with pytest.raises(ValidationError):
        GitHubSearchRequest(query="bug", limit=26)


# --- config validation: allowed_orgs ---------------------------------------


def test_github_allowed_orgs_accepts_valid_logins(tmp_path) -> None:
    config = _config(tmp_path, allowed_orgs=["acme", "other-co", "a", "x" * 39])
    assert config.connectors.github.allowed_orgs == ["acme", "other-co", "a", "x" * 39]


@pytest.mark.parametrize(
    "bad_org",
    [
        "-acme",  # leading hyphen
        "acme-",  # trailing hyphen
        "ac--me",  # doubled hyphen
        "acme org",  # whitespace
        "acme OR repo:other/private",  # search-syntax injection
        "x" * 40,  # over GitHub's 39-char login limit
        "",  # blank entry
    ],
)
def test_github_allowed_orgs_rejects_invalid_logins(tmp_path, bad_org) -> None:
    with pytest.raises(ValidationError):
        _config(tmp_path, allowed_orgs=[bad_org])


# --- status ---------------------------------------------------------------


async def test_github_status_connected_reports_account(tmp_path, monkeypatch):
    _install_mock_client(monkeypatch, _ok_status_handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.get("/v1/connectors/github/status", headers=_auth_header())
    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is True
    assert body["state"] == "connected"
    assert body["account"] == "octocat"
    assert set(body["scopes"]) == set(GITHUB_READONLY_SCOPES)


async def test_github_status_reports_actual_token_scopes(tmp_path, monkeypatch):
    # The status must report the token's real X-OAuth-Scopes grant, not the
    # configured intention: a stale link missing read:project shows as missing.
    def handler(request: httpx.Request) -> Response:
        path = request.url.path
        if path.endswith("/user"):
            return Response(200, json={"login": "octocat"}, headers={"X-OAuth-Scopes": "repo, read:org"})
        if path.endswith("/user/repos"):
            return Response(200, json=[{"full_name": "acme/widgets"}])
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.get("/v1/connectors/github/status", headers=_auth_header())
    body = response.json()
    assert body["connected"] is True
    assert body["scopes"] == ["repo", "read:org"]
    assert "read:project" not in body["scopes"]


async def test_github_status_not_connected_without_token_source(tmp_path, monkeypatch):
    _install_mock_client(monkeypatch, _ok_status_handler)
    app = make_app(_config(tmp_path, static_access_token="", broker_token_url=""))
    async with _client(app) as client:
        response = await client.get("/v1/connectors/github/status", headers=_auth_header())
    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is False
    assert body["state"] == "not_connected"


async def test_github_status_reconnect_for_rejected_token(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        return Response(401, json={"message": "Bad credentials"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.get("/v1/connectors/github/status", headers=_auth_header())
    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is False
    assert body["state"] == "reconnect_required"


# --- search ---------------------------------------------------------------


async def test_github_search_returns_hits_and_applies_repo_qualifier(tmp_path, monkeypatch):
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/search/issues"):
            seen["q"] = request.url.params.get("q", "")
            return Response(
                200,
                json={
                    "total_count": 1,
                    "incomplete_results": False,
                    "items": [_issue_item(7, title="Login bug", body="steps to repro")],
                },
            )
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/search",
            headers=_auth_header(),
            json={"query": "login", "limit": 10, "repo": "acme/widgets"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["content_trust"] == "external_untrusted"
    assert len(body["hits"]) == 1
    hit = body["hits"][0]
    assert hit["number"] == 7
    assert hit["repo"] == "acme/widgets"
    assert hit["title"] == "Login bug"
    assert hit["assignees"] == ["assignee1"]
    assert hit["labels"] == ["bug"]
    assert body["next_page_token"] == ""  # single partial page => no continuation
    assert "repo:acme/widgets" in seen["q"]


async def test_github_search_paginates_and_rejects_stale_page_token(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/search/issues"):
            page = request.url.params.get("page", "1")
            return Response(
                200,
                json={
                    "total_count": 5,
                    "incomplete_results": False,
                    "items": [_issue_item(int(page))],
                },
            )
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        first = await client.post(
            "/v1/connectors/github/search",
            headers=_auth_header(),
            json={"query": "bug", "limit": 1},
        )
        token = first.json()["next_page_token"]
        assert token, "a full page under the result cap must yield a continuation token"

        cont = await client.post(
            "/v1/connectors/github/search",
            headers=_auth_header(),
            json={"query": "bug", "limit": 1, "page_token": token},
        )
        assert cont.status_code == 200

        stale = await client.post(
            "/v1/connectors/github/search",
            headers=_auth_header(),
            json={"query": "different", "limit": 1, "page_token": token},
        )
    assert stale.status_code == 422


async def test_github_search_surfaces_incomplete_results(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/search/issues"):
            return Response(200, json={"total_count": 1, "incomplete_results": True, "items": [_issue_item(1)]})
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post("/v1/connectors/github/search", headers=_auth_header(), json={"query": "bug"})
    assert response.json()["incomplete_results"] is True


async def test_github_search_honors_rate_limit_retry_after(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/search/issues"):
            return Response(
                429,
                headers={"Retry-After": "42", "x-ratelimit-remaining": "0"},
                json={"message": "API rate limit exceeded"},
            )
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post("/v1/connectors/github/search", headers=_auth_header(), json={"query": "bug"})
    assert response.status_code == 429
    assert response.headers.get("Retry-After") == "42"


async def test_github_search_sanitizes_link_text(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/search/issues"):
            return Response(
                200,
                json={
                    "total_count": 1,
                    "incomplete_results": False,
                    "items": [_issue_item(1, title="See http://evil.test/x", body="ping http://evil.test")],
                },
            )
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post("/v1/connectors/github/search", headers=_auth_header(), json={"query": "bug"})
    assert "http://evil.test" not in response.text


def test_github_api_text_sanitizer_preserves_code_shapes():
    # The generic read returns diffs, file contents, and refs where the shared
    # sanitizer's bare-domain masking is destructive (config.py -> [link], and
    # every dotted attribute access -- os.path, self.timeout_seconds -- reads as
    # a bare domain too). The GitHub-local variant drops bare-domain masking
    # entirely (the renderer-crash premise, apollo-desktop#365, has expired)
    # while keeping scheme/www./markdown/email links neutralized.
    for value in (
        "config.py",
        "src/components/App.tsx",
        "release/2.0",
        "(a3f5b9c)",
        "a3f5b9c1d2e3f4a5b6c7d8e9f0a1b2c3d4e5f6a7",
        "refs/heads/main",
        "v1.2.3",
        "os.path.join(base, x)",
        "self.timeout_seconds = timeout_seconds",
        "np.linalg.norm(x)",
        "see foo.com for details",
        "example.io test.dev foo.ai",
        "numpy.linalg",
        # The reviewer's own repro case for the old behavior: a domain:tag
        # shape (colon before the tag, not a scheme prefix) must survive too.
        "image: ghcr.io/openteams/collab:8da6475",
    ):
        assert sanitize_github_api_text(value) == value

    # Real links and emails are still neutralized.
    assert sanitize_github_api_text("Go to https://x.test/a") == "Go to [link]"
    assert sanitize_github_api_text("See [the plan](https://x.test/plan).") == "See the plan."
    assert sanitize_github_api_text("Email person@example.com") == "Email person [at] example [dot] com"
    assert sanitize_github_api_text("mailto:bob@x.test") == "[link]"
    assert sanitize_github_api_text("Meet at www.example.com/x") == "Meet at [link]"
    assert sanitize_github_api_text("") == ""

    # Idempotent across code-shaped and link-shaped inputs.
    for value in (
        "config.py",
        "example.io",
        "Go to https://x.test/a",
        "Email person@example.com",
        "See [the plan](https://x.test/plan).",
    ):
        once = sanitize_github_api_text(value)
        assert sanitize_github_api_text(once) == once


def test_shared_sanitizer_still_masks_bare_domains():
    # Guard the intentional divergence: the SHARED sanitizer must keep masking
    # bare domains (Gmail/Calendar/Drive/Slack + curated GitHub depend on it).
    from collab_hub_api.connectors.connector_text import sanitize_connector_text

    assert sanitize_connector_text("config.py") == "[link]"
    assert sanitize_connector_text("see foo.com for details") == "see [link] for details"


async def test_github_endpoints_do_not_expose_access_token(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/user"):
            return Response(200, json={"login": "octocat"})
        if request.url.path.endswith("/user/repos"):
            return Response(200, json=[{"full_name": "acme/widgets"}])
        if request.url.path.endswith("/search/issues"):
            return Response(200, json={"total_count": 1, "incomplete_results": False, "items": [_issue_item(1)]})
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        status_body = (await client.get("/v1/connectors/github/status", headers=_auth_header())).text
        search_body = (
            await client.post("/v1/connectors/github/search", headers=_auth_header(), json={"query": "bug"})
        ).text
    assert STATIC_TOKEN not in status_body
    assert STATIC_TOKEN not in search_body


# --- item read ------------------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


async def test_github_item_read_returns_bounded_body_and_comments(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        path = request.url.path
        if path.endswith("/repos/acme/widgets/issues/7"):
            return Response(
                200,
                json={
                    "number": 7,
                    "title": "Login bug",
                    "state": "open",
                    "user": {"login": "octocat"},
                    "assignees": [{"login": "octocat"}, {"login": "hubot"}],
                    "labels": [{"name": "bug"}, {"name": "priority:high"}],
                    "comments": 1,
                    "body": "The login flow breaks.",
                    "pull_request": {"url": "x"},
                },
            )
        if path.endswith("/repos/acme/widgets/issues/7/comments"):
            return Response(200, json=[{"user": {"login": "hubot"}, "body": "I can repro this."}])
        if path.endswith("/repos/acme/widgets/pulls/7/reviews"):
            return Response(200, json=[{"user": {"login": "hubot"}, "state": "APPROVED"}])
        if path.endswith("/repos/acme/widgets/pulls/7"):
            return Response(200, json={"requested_reviewers": [{"login": "reviewer1"}]})
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/items/7/read",
            headers=_auth_header(),
            json={"repo": "acme/widgets", "max_chars": 5000},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["content_trust"] == "external_untrusted"
    assert body["item"]["number"] == 7
    assert body["item"]["is_pull_request"] is True
    assert body["item"]["assignees"] == ["octocat", "hubot"]
    assert body["item"]["labels"] == ["bug", "priority:high"]
    # PR-only fields sourced from the pulls endpoint, not the issues endpoint.
    assert body["item"]["requested_reviewers"] == ["reviewer1"]
    assert body["item"]["reviews"] == [{"user": "hubot", "state": "APPROVED", "body": ""}]
    assert "login flow breaks" in body["text"]
    assert "repro this" in body["text"]
    assert body["truncated"] is False


async def test_github_item_read_pr_draft_and_merge_state(tmp_path, monkeypatch):
    # is_draft and merge_state come from the pulls payload already fetched for
    # requested_reviewers; state="closed" alone can't tell merged from abandoned.
    pulls = {
        1: {"draft": True, "state": "open", "merged": False},
        2: {"draft": False, "state": "closed", "merged": True},
        3: {"draft": False, "state": "closed", "merged": False},
        4: {"draft": False, "state": "open", "merged": False},
    }

    def handler(request: httpx.Request) -> Response:
        path = request.url.path
        for n in pulls:
            if path.endswith(f"/repos/acme/widgets/issues/{n}"):
                return Response(
                    200,
                    json={
                        "number": n,
                        "title": "A PR",
                        "state": pulls[n]["state"],
                        "user": {"login": "octocat"},
                        "comments": 0,
                        "body": "b",
                        "pull_request": {"url": "x"},
                    },
                )
            if path.endswith(f"/repos/acme/widgets/pulls/{n}/reviews"):
                return Response(200, json=[])
            if path.endswith(f"/repos/acme/widgets/pulls/{n}"):
                return Response(200, json={"requested_reviewers": [], **pulls[n]})
        if path.endswith("/repos/acme/widgets/issues/8"):
            return Response(
                200,
                json={
                    "number": 8,
                    "title": "An issue",
                    "state": "open",
                    "user": {"login": "octocat"},
                    "comments": 0,
                    "body": "b",
                },
            )
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    expected = {1: (True, "open"), 2: (False, "merged"), 3: (False, "closed_unmerged"), 4: (False, "open")}
    async with _client(app) as client:
        for n, (want_draft, want_merge) in expected.items():
            response = await client.post(
                f"/v1/connectors/github/items/{n}/read",
                headers=_auth_header(),
                json={"repo": "acme/widgets", "max_chars": 5000},
            )
            item = response.json()["item"]
            assert item["is_draft"] is want_draft, n
            assert item["merge_state"] == want_merge, n
        # A plain issue leaves both fields defaulted (never fetches pulls).
        response = await client.post(
            "/v1/connectors/github/items/8/read",
            headers=_auth_header(),
            json={"repo": "acme/widgets", "max_chars": 5000},
        )
        item = response.json()["item"]
        assert item["is_draft"] is False
        assert item["merge_state"] == ""


async def test_github_item_read_fetches_most_recent_comments(tmp_path, monkeypatch):
    # Comments come back oldest-first; with 65 comments over 30/page the most
    # recent ones are on page 3. We must request the last page, not page 1.
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> Response:
        path = request.url.path
        if path.endswith("/repos/acme/widgets/issues/7/comments"):
            seen["page"] = request.url.params.get("page")
            seen["per_page"] = request.url.params.get("per_page")
            return Response(200, json=[{"user": {"login": "hubot"}, "body": "the latest comment"}])
        if path.endswith("/repos/acme/widgets/issues/7"):
            return Response(200, json={"number": 7, "title": "x", "state": "open", "comments": 65, "body": "b"})
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/items/7/read",
            headers=_auth_header(),
            json={"repo": "acme/widgets", "max_chars": 5000},
        )
    assert response.status_code == 200
    assert seen["per_page"] == "30"
    assert seen["page"] == "3"  # ceil(65 / 30)
    assert "the latest comment" in response.json()["text"]


async def test_github_item_read_pr_reviews_keep_newest_tail(tmp_path, monkeypatch):
    # Reviews are oldest-first with no sort option; an early CHANGES_REQUESTED
    # must not hide the later APPROVED. per_page=100 + newest tail keeps it.
    reviews = [{"user": {"login": "alice"}, "state": "CHANGES_REQUESTED"}]
    reviews += [{"user": {"login": f"u{i}"}, "state": "COMMENTED"} for i in range(40)]
    reviews += [{"user": {"login": "alice"}, "state": "APPROVED"}]  # newest, last
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> Response:
        path = request.url.path
        if path.endswith("/repos/acme/widgets/pulls/7/reviews"):
            seen["per_page"] = request.url.params.get("per_page")
            return Response(200, json=reviews)
        if path.endswith("/repos/acme/widgets/pulls/7"):
            return Response(200, json={"requested_reviewers": []})
        if path.endswith("/repos/acme/widgets/issues/7"):
            return Response(
                200,
                json={
                    "number": 7,
                    "title": "x",
                    "state": "open",
                    "comments": 0,
                    "body": "b",
                    "pull_request": {"url": "x"},
                },
            )
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/items/7/read",
            headers=_auth_header(),
            json={"repo": "acme/widgets"},
        )
    returned = response.json()["item"]["reviews"]
    assert seen["per_page"] == "100"
    assert len(returned) <= 30
    # The newest review is kept (last); the early stale state fell off the tail.
    assert returned[-1] == {"user": "alice", "state": "APPROVED", "body": ""}


async def test_github_item_read_pr_review_body(tmp_path, monkeypatch):
    # The reviewer's rationale is returned, link-sanitized, and length-capped so a
    # long review can't blow the response, while an empty body stays empty.
    reviews = [
        {"user": {"login": "alice"}, "state": "CHANGES_REQUESTED", "body": "Fix the null check http://evil.test/x"},
        {"user": {"login": "bob"}, "state": "APPROVED", "body": "y" * 900},
        {"user": {"login": "carol"}, "state": "COMMENTED", "body": ""},
    ]

    def handler(request: httpx.Request) -> Response:
        path = request.url.path
        if path.endswith("/repos/acme/widgets/issues/7"):
            return Response(
                200,
                json={
                    "number": 7,
                    "title": "PR",
                    "state": "open",
                    "user": {"login": "octocat"},
                    "comments": 0,
                    "body": "b",
                    "pull_request": {"url": "x"},
                },
            )
        if path.endswith("/repos/acme/widgets/pulls/7/reviews"):
            return Response(200, json=reviews)
        if path.endswith("/repos/acme/widgets/pulls/7"):
            return Response(200, json={"requested_reviewers": []})
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/items/7/read",
            headers=_auth_header(),
            json={"repo": "acme/widgets", "max_chars": 5000},
        )
    out = response.json()["item"]["reviews"]
    assert out[0]["body"].startswith("Fix the null check")
    assert "http://" not in out[0]["body"]  # link-sanitized, no raw URL leak
    assert len(out[1]["body"]) <= 501 and out[1]["body"].endswith("…")  # capped
    assert out[2]["body"] == ""


async def test_github_item_read_pr_requested_teams(tmp_path, monkeypatch):
    # A team requested for review is surfaced alongside individual reviewers, so a
    # reviewer-grouping report doesn't silently drop team requests.
    def handler(request: httpx.Request) -> Response:
        path = request.url.path
        if path.endswith("/repos/acme/widgets/issues/7"):
            return Response(
                200,
                json={
                    "number": 7,
                    "title": "PR",
                    "state": "open",
                    "user": {"login": "octocat"},
                    "comments": 0,
                    "body": "b",
                    "pull_request": {"url": "x"},
                },
            )
        if path.endswith("/repos/acme/widgets/pulls/7/reviews"):
            return Response(200, json=[])
        if path.endswith("/repos/acme/widgets/pulls/7"):
            return Response(
                200,
                json={
                    "requested_reviewers": [{"login": "alice"}],
                    "requested_teams": [
                        {"name": "Platform", "slug": "platform"},
                        {"name": "Security", "slug": "security"},
                    ],
                },
            )
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/items/7/read",
            headers=_auth_header(),
            json={"repo": "acme/widgets", "max_chars": 5000},
        )
    item = response.json()["item"]
    assert item["requested_reviewers"] == ["alice"]
    assert item["requested_teams"] == ["Platform", "Security"]


async def test_github_item_read_truncates_to_max_chars(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        path = request.url.path
        if path.endswith("/repos/acme/widgets/issues/7"):
            return Response(200, json={"number": 7, "title": "x", "state": "open", "body": "A" * 5000})
        if path.endswith("/comments"):
            return Response(200, json=[])
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/items/7/read",
            headers=_auth_header(),
            json={"repo": "acme/widgets", "max_chars": 100},
        )
    body = response.json()
    assert body["truncated"] is True
    assert len(body["text"]) <= 101  # max_chars plus the ellipsis


async def test_github_item_read_rejects_bad_repo(tmp_path, monkeypatch):
    _install_mock_client(monkeypatch, _ok_status_handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/items/7/read",
            headers=_auth_header(),
            json={"repo": "not-a-repo", "max_chars": 100},
        )
    assert response.status_code == 422


# --- file read ------------------------------------------------------------


async def test_github_file_read_decodes_base64(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        if "/contents/" in request.url.path:
            assert request.url.params.get("ref") == "main"
            return Response(
                200,
                json={
                    "type": "file",
                    "encoding": "base64",
                    "size": 11,
                    "content": _b64(b"hello world"),
                },
            )
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/files/read",
            headers=_auth_header(),
            json={"repo": "acme/widgets", "path": "src/app.py", "ref": "main", "max_chars": 5000},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["content"] == "hello world"
    assert body["binary"] is False
    assert body["too_large"] is False


async def test_github_file_read_percent_encodes_path(tmp_path, monkeypatch):
    # '?' and spaces are legal in git paths and pass _validate_github_path; they
    # must be percent-encoded so the remainder cannot leak into the query string
    # and override ref.
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> Response:
        if "/contents/" in request.url.path:
            seen["raw_path"] = request.url.raw_path.decode()
            seen["ref"] = request.url.params.get("ref")
            return Response(
                200,
                json={"type": "file", "encoding": "base64", "size": 2, "content": _b64(b"hi")},
            )
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/files/read",
            headers=_auth_header(),
            json={"repo": "acme/widgets", "path": "docs/a b?ref=evil", "ref": "main", "max_chars": 5000},
        )
    assert response.status_code == 200
    # The '?' and space stay inside the encoded path; ref is the caller's value, not "evil".
    assert "docs/a%20b%3Fref%3Devil" in seen["raw_path"]
    assert seen["ref"] == "main"


async def test_github_file_read_reports_too_large(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        if "/contents/" in request.url.path:
            return Response(200, json={"type": "file", "encoding": "none", "size": 2_000_000, "content": ""})
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/files/read",
            headers=_auth_header(),
            json={"repo": "acme/widgets", "path": "big.bin", "max_chars": 5000},
        )
    body = response.json()
    assert body["too_large"] is True
    assert body["content"] == ""


async def test_github_file_read_flags_binary(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        if "/contents/" in request.url.path:
            return Response(
                200,
                json={"type": "file", "encoding": "base64", "size": 4, "content": _b64(b"\xff\xfe\x00\x01")},
            )
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/files/read",
            headers=_auth_header(),
            json={"repo": "acme/widgets", "path": "logo.png", "max_chars": 5000},
        )
    body = response.json()
    assert body["binary"] is True
    assert body["content"] == ""


async def test_github_file_read_flags_lfs_pointer(tmp_path, monkeypatch):
    pointer = b"version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 12345\n"

    def handler(request: httpx.Request) -> Response:
        if "/contents/" in request.url.path:
            return Response(
                200,
                json={"type": "file", "encoding": "base64", "size": len(pointer), "content": _b64(pointer)},
            )
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/files/read",
            headers=_auth_header(),
            json={"repo": "acme/widgets", "path": "model.bin", "max_chars": 5000},
        )
    body = response.json()
    assert "lfs" in body["unsupported_reason"].lower()
    assert body["content"] == ""


async def test_github_file_read_rejects_path_and_ref_traversal(tmp_path, monkeypatch):
    _install_mock_client(monkeypatch, _ok_status_handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        bad_path = await client.post(
            "/v1/connectors/github/files/read",
            headers=_auth_header(),
            json={"repo": "acme/widgets", "path": "../../etc/passwd", "max_chars": 100},
        )
        bad_ref = await client.post(
            "/v1/connectors/github/files/read",
            headers=_auth_header(),
            json={"repo": "acme/widgets", "path": "src/app.py", "ref": "../secrets", "max_chars": 100},
        )
        abs_path = await client.post(
            "/v1/connectors/github/files/read",
            headers=_auth_header(),
            json={"repo": "acme/widgets", "path": "/etc/passwd", "max_chars": 100},
        )
    assert bad_path.status_code == 422
    assert bad_ref.status_code == 422
    assert abs_path.status_code == 422


async def test_github_reads_do_not_expose_token(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        path = request.url.path
        if path.endswith("/repos/acme/widgets/issues/7"):
            return Response(200, json={"number": 7, "title": "x", "state": "open", "body": "b"})
        if path.endswith("/comments"):
            return Response(200, json=[])
        if "/contents/" in path:
            return Response(200, json={"type": "file", "encoding": "base64", "size": 1, "content": _b64(b"x")})
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        item_body = (
            await client.post(
                "/v1/connectors/github/items/7/read",
                headers=_auth_header(),
                json={"repo": "acme/widgets"},
            )
        ).text
        file_body = (
            await client.post(
                "/v1/connectors/github/files/read",
                headers=_auth_header(),
                json={"repo": "acme/widgets", "path": "a.txt"},
            )
        ).text
    assert STATIC_TOKEN not in item_body
    assert STATIC_TOKEN not in file_body


# --- project boards (Projects V2 / GraphQL) -------------------------------


def _graphql_handler(*, org=None, user=None):
    """A mock that answers POST /graphql with the given organization/user data."""

    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/graphql"):
            return Response(200, json={"data": {"organization": org, "user": user}})
        return Response(404, json={"message": "Not Found"})

    return handler


def _list_node():
    return {
        "projectsV2": {
            "nodes": [
                {
                    "number": 1,
                    "title": "Roadmap",
                    "shortDescription": "Q3 plan",
                    "closed": False,
                    "items": {"totalCount": 12},
                }
            ]
        }
    }


def _read_node():
    return {
        "projectV2": {
            "number": 1,
            "title": "Roadmap",
            "shortDescription": "",
            "closed": False,
            "items": {
                "totalCount": 2,
                "nodes": [
                    {
                        "type": "PULL_REQUEST",
                        "content": {
                            "__typename": "PullRequest",
                            "number": 42,
                            "title": "Add feature",
                            "repository": {"nameWithOwner": "acme/widgets"},
                        },
                        "fieldValues": {
                            "nodes": [
                                {
                                    "__typename": "ProjectV2ItemFieldSingleSelectValue",
                                    "name": "In Progress",
                                    "field": {"name": "Status"},
                                }
                            ]
                        },
                    },
                    {
                        "type": "DRAFT_ISSUE",
                        "content": {"__typename": "DraftIssue", "title": "An idea"},
                        "fieldValues": {"nodes": []},
                    },
                ],
            },
        }
    }


async def test_github_list_projects_org(tmp_path, monkeypatch):
    _install_mock_client(monkeypatch, _graphql_handler(org=_list_node(), user=None))
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/projects/list", headers=_auth_header(), json={"owner": "openteams-ai"}
        )
    assert response.status_code == 200
    projects = response.json()["projects"]
    assert len(projects) == 1
    assert projects[0]["number"] == 1
    assert projects[0]["title"] == "Roadmap"
    assert projects[0]["items_count"] == 12


async def test_github_list_projects_falls_back_to_user(tmp_path, monkeypatch):
    # A personal owner: organization(login) resolves null, user(login) has the boards.
    _install_mock_client(monkeypatch, _graphql_handler(org=None, user=_list_node()))
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/projects/list", headers=_auth_header(), json={"owner": "mcshayla"}
        )
    assert response.status_code == 200
    assert len(response.json()["projects"]) == 1


async def test_github_read_project_items_link_prs_for_chaining(tmp_path, monkeypatch):
    _install_mock_client(monkeypatch, _graphql_handler(org=_read_node(), user=None))
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/projects/1/read",
            headers=_auth_header(),
            json={"owner": "openteams-ai", "max_items": 50},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["content_trust"] == "external_untrusted"
    assert body["project"]["title"] == "Roadmap"
    items = body["items"]
    assert len(items) == 2
    pr = items[0]
    assert pr["type"] == "PULL_REQUEST"
    assert pr["status"] == "In Progress"
    assert pr["repo"] == "acme/widgets"  # so the model can chain read_github_item
    assert pr["number"] == 42
    draft = items[1]
    assert draft["type"] == "DRAFT_ISSUE"
    assert draft["repo"] == "" and draft["number"] == 0
    # Full board fit in the response -> not truncated.
    assert body["total_count"] == 2
    assert body["truncated"] is False


async def test_github_read_project_flags_truncation(tmp_path, monkeypatch):
    # Board reports 91 items but only a page came back -> truncated=True and
    # total_count reflects the real size, so the caller knows it's partial.
    node = _read_node()
    node["projectV2"]["items"]["totalCount"] = 91
    _install_mock_client(monkeypatch, _graphql_handler(org=node, user=None))
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/projects/1/read",
            headers=_auth_header(),
            json={"owner": "openteams-ai"},
        )
    body = response.json()
    assert body["total_count"] == 91
    assert body["truncated"] is True


async def test_github_read_project_auto_paginates(tmp_path, monkeypatch):
    # The read walks pages server-side (GitHub caps a page at 100). Two pages
    # here -> both accumulated into one response, not truncated.
    def _page(nodes, has_next, end_cursor):
        return {
            "projectV2": {
                "number": 1,
                "title": "Roadmap",
                "shortDescription": "",
                "closed": False,
                "items": {
                    "totalCount": 3,
                    "pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor},
                    "nodes": nodes,
                },
            }
        }

    def item(n):
        return {
            "type": "ISSUE",
            "content": {
                "__typename": "Issue",
                "number": n,
                "title": f"i{n}",
                "repository": {"nameWithOwner": "acme/widgets"},
            },
            "fieldValues": {"nodes": []},
        }

    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/graphql"):
            variables = json.loads(request.content.decode("utf-8")).get("variables", {})
            if not variables.get("after"):
                page1 = _page([item(1), item(2)], True, "CURSOR2")
                return Response(200, json={"data": {"organization": page1, "user": None}})
            page2 = _page([item(3)], False, None)
            return Response(200, json={"data": {"organization": page2, "user": None}})
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/projects/1/read", headers=_auth_header(), json={"owner": "openteams-ai"}
        )
    body = response.json()
    assert [i["number"] for i in body["items"]] == [1, 2, 3]  # both pages accumulated
    assert body["total_count"] == 3
    assert body["truncated"] is False


async def test_github_project_owner_not_found(tmp_path, monkeypatch):
    _install_mock_client(monkeypatch, _graphql_handler(org=None, user=None))
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/projects/list", headers=_auth_header(), json={"owner": "ghost-org"}
        )
    assert response.status_code == 404


async def test_github_projects_reject_bad_owner(tmp_path, monkeypatch):
    _install_mock_client(monkeypatch, _graphql_handler(org=_list_node()))
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/projects/list", headers=_auth_header(), json={"owner": "bad/owner"}
        )
    assert response.status_code == 422


def _read_node_states():
    """A board whose items exercise state (open/closed) and a REDACTED node
    (content the viewer cannot access)."""
    return {
        "projectV2": {
            "number": 1,
            "title": "Roadmap",
            "shortDescription": "",
            "closed": False,
            "items": {
                "totalCount": 3,
                "nodes": [
                    {
                        "type": "ISSUE",
                        "content": {
                            "__typename": "Issue",
                            "number": 7,
                            "title": "A bug",
                            "state": "CLOSED",
                            "repository": {"nameWithOwner": "acme/widgets"},
                        },
                        "fieldValues": {"nodes": []},
                    },
                    {
                        "type": "ISSUE",
                        "content": {
                            "__typename": "Issue",
                            "number": 8,
                            "title": "Another bug",
                            "state": "OPEN",
                            "repository": {"nameWithOwner": "acme/widgets"},
                        },
                        "fieldValues": {"nodes": []},
                    },
                    {
                        "type": "REDACTED",
                        "content": None,
                        "fieldValues": {"nodes": []},
                    },
                ],
            },
        }
    }


async def test_github_read_project_item_state_and_redacted(tmp_path, monkeypatch):
    _install_mock_client(monkeypatch, _graphql_handler(org=_read_node_states(), user=None))
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/projects/1/read",
            headers=_auth_header(),
            json={"owner": "openteams-ai"},
        )
    assert response.status_code == 200
    items = response.json()["items"]
    # A linked issue carries its open/closed state.
    assert items[0]["state"] == "closed"
    assert items[1]["state"] == "open"
    # A redacted item is marked as such and leaks no content.
    assert items[2]["type"] == "REDACTED"
    assert items[2]["repo"] == "" and items[2]["number"] == 0
    assert items[2]["state"] == ""


async def test_github_read_project_item_assignees_and_labels(tmp_path, monkeypatch):
    # Linked issues/PRs carry their real assignees and labels, so a caller can
    # triage a board by person or label (the capability the docs advertise).
    node = {
        "projectV2": {
            "number": 1,
            "title": "Roadmap",
            "shortDescription": "",
            "closed": False,
            "items": {
                "totalCount": 2,
                "nodes": [
                    {
                        "type": "ISSUE",
                        "content": {
                            "__typename": "Issue",
                            "number": 7,
                            "title": "Bug",
                            "state": "OPEN",
                            "repository": {"nameWithOwner": "acme/widgets"},
                            "assignees": {"nodes": [{"login": "octocat"}, {"login": "hubot"}]},
                            "labels": {"nodes": [{"name": "bug"}, {"name": "priority:high"}]},
                        },
                        "fieldValues": {"nodes": []},
                    },
                    {
                        "type": "DRAFT_ISSUE",
                        "content": {"__typename": "DraftIssue", "title": "An idea"},
                        "fieldValues": {"nodes": []},
                    },
                ],
            },
        }
    }
    _install_mock_client(monkeypatch, _graphql_handler(org=node, user=None))
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/projects/1/read",
            headers=_auth_header(),
            json={"owner": "openteams-ai"},
        )
    items = response.json()["items"]
    assert items[0]["assignees"] == ["octocat", "hubot"]
    assert items[0]["labels"] == ["bug", "priority:high"]
    # A draft item has no linked issue/PR, so both lists stay empty.
    assert items[1]["assignees"] == [] and items[1]["labels"] == []


def _counts_handler(*, read_node, options, aggregates, status_counts, fail_dsl=False, captured_variables=None):
    """Route POST /graphql three ways by GraphQL operation name: the enumeration
    read (ProjectRead), the aggregate count query (ProjectCounts), and the
    per-status column query (ProjectStatusCounts). fail_dsl simulates the
    board-filter DSL being rejected by the API, so the count path falls back to
    sampling the enumerated items."""

    def handler(request: httpx.Request) -> Response:
        if not request.url.path.endswith("/graphql"):
            return Response(404, json={"message": "Not Found"})
        payload = json.loads(request.content.decode("utf-8"))
        query = payload["query"]
        if "ProjectStatusCounts" in query:
            if captured_variables is not None:
                captured_variables.update(payload["variables"])
            node = {"projectV2": {f"s{i}": {"totalCount": c} for i, c in enumerate(status_counts)}}
            return Response(200, json={"data": {"organization": node, "user": None}})
        if "ProjectCounts" in query:
            if fail_dsl:
                return Response(200, json={"errors": [{"message": "unknown query filter"}]})
            node = {
                "projectV2": {
                    "statusField": {"options": [{"name": o} for o in options]},
                    **{alias: {"totalCount": n} for alias, n in aggregates.items()},
                }
            }
            return Response(200, json={"data": {"organization": node, "user": None}})
        return Response(200, json={"data": {"organization": read_node, "user": None}})

    return handler


async def test_github_read_project_authoritative_counts(tmp_path, monkeypatch):
    # Server-side count queries succeed -> exact, authoritative breakdowns whose
    # status columns reconcile to the total.
    handler = _counts_handler(
        read_node=_read_node(),  # totalCount 2 -> matches the authoritative total
        options=["Backlog", "In progress", "Done"],
        # A board draft matches is:issue too, so is:issue counts the draft (1) and
        # board_draft (is:draft is:issue) isolates it; issue = 1 - 1 = 0 real
        # issues, the draft lands in the draft bucket, and the PR under is:pr.
        aggregates={
            "total": 2,
            "issue": 1,
            "pull_request": 1,
            "board_draft": 1,
            "opened": 1,
            "closed": 1,
            "no_status": 0,
        },
        status_counts=[1, 1, 0],
    )
    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/projects/1/read",
            headers=_auth_header(),
            json={"owner": "openteams-ai"},
        )
    assert response.status_code == 200
    body = response.json()
    counts = body["counts"]
    assert counts["authoritative"] is True
    assert counts["total"] == 2
    assert counts["archived_policy"] == "excluded"
    # The authoritative total agrees with the legacy total_count (same board scalar) —
    # only true because both exclude archived items by default. If archived_policy
    # ever changes to include archived items, this assertion will start failing on
    # boards that actually have archived items.
    assert counts["total"] == body["total_count"]
    assert counts["by_status"] == [
        {"name": "Backlog", "count": 1},
        {"name": "In progress", "count": 1},
        {"name": "Done", "count": 0},
    ]
    assert counts["no_status"] == 0
    # The status columns plus the blank column reconcile to the total.
    assert sum(c["count"] for c in counts["by_status"]) + counts["no_status"] == counts["total"]
    # by_type partitions the total; redacted is derived (total - issue - pr - draft).
    assert counts["by_type"] == {"issue": 0, "pull_request": 1, "draft": 1, "redacted": 0}
    assert sum(counts["by_type"].values()) == counts["total"]
    assert counts["by_state"] == {"open": 1, "closed": 1}


async def test_github_authoritative_by_type_partitions_despite_draft_overlap(tmp_path, monkeypatch):
    # A board of {board draft, draft PR, issue, merged PR}: is:issue counts the
    # real issue AND the board draft (2), is:pr counts both PRs (2), is:draft
    # counts the board draft AND the draft PR. Naively summing is:issue + is:pr +
    # is:draft overcounts (6 > total 4). board_draft (is:draft is:issue) isolates
    # the board draft so the buckets partition the total exactly.
    handler = _counts_handler(
        read_node=_read_node(),
        options=["Todo", "Done"],
        aggregates={
            "total": 4,
            "issue": 2,  # real issue + board draft (a board draft matches is:issue)
            "pull_request": 2,  # merged PR + draft PR
            "board_draft": 1,  # is:draft is:issue -> just the board draft
            "opened": 2,
            "closed": 2,
            "no_status": 0,
        },
        status_counts=[3, 1],
    )
    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/projects/1/read", headers=_auth_header(), json={"owner": "openteams-ai"}
        )
    counts = response.json()["counts"]
    assert counts["authoritative"] is True
    # issue = is:issue(2) - board_draft(1); pr = is:pr(2); draft = board_draft(1).
    assert counts["by_type"] == {"issue": 1, "pull_request": 2, "draft": 1, "redacted": 0}
    assert sum(counts["by_type"].values()) == counts["total"] == 4


async def test_github_authoritative_counts_downgrade_when_status_does_not_reconcile(tmp_path, monkeypatch):
    # Both count queries "succeed", but the status columns + no_status (2) don't
    # reconcile to the total (91) -- exactly what a non-"Status" single-select or
    # a `"`-in-name option can silently produce. The self-check must refuse the
    # authoritative stamp and degrade to the sampled fallback rather than ship
    # wrong numbers with full authority.
    node = _read_node_states()
    node["projectV2"]["items"]["totalCount"] = 91
    handler = _counts_handler(
        read_node=node,
        options=["Todo", "Done"],
        aggregates={
            "total": 91,
            "issue": 91,
            "pull_request": 0,
            "board_draft": 0,
            "opened": 4,
            "closed": 6,
            "no_status": 0,
        },
        status_counts=[1, 1],  # 1 + 1 + no_status(0) = 2, not 91
    )
    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/projects/1/read", headers=_auth_header(), json={"owner": "openteams-ai"}
        )
    counts = response.json()["counts"]
    assert counts["authoritative"] is False
    assert counts["counted_items"] == 3  # bucketed from the enumerated sample, not the bogus server counts


def test_project_filter_value_escapes_status_option_delimiters():
    # Status names are board-controlled. They are GraphQL variables, but their
    # contents are parsed by GitHub's Projects filter DSL before being counted.
    assert _project_filter_value('Ready "for QA"') == r"Ready \"for QA\""
    assert _project_filter_value(r"Needs \ review") == r"Needs \\ review"


async def test_github_project_status_count_escapes_filter_values(tmp_path, monkeypatch):
    captured_variables = {}
    handler = _counts_handler(
        read_node=_read_node(),
        options=['Ready "for QA"', r"Needs \ review"],
        aggregates={
            "total": 2,
            "issue": 1,
            "pull_request": 1,
            "board_draft": 0,
            "opened": 2,
            "closed": 0,
            "no_status": 0,
        },
        status_counts=[1, 1],
        captured_variables=captured_variables,
    )
    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/projects/1/read",
            headers=_auth_header(),
            json={"owner": "openteams-ai"},
        )
    assert response.status_code == 200
    assert captured_variables["q0"] == r'status:"Ready \"for QA\""'
    assert captured_variables["q1"] == r'status:"Needs \\ review"'


async def test_github_status_counts_null_project_falls_back_instead_of_zeroing(tmp_path, monkeypatch):
    # A1 (aggregates) succeeds, but A2 (per-status counts) comes back with
    # projectV2: null -- exactly what GraphQL non-null bubbling produces when a
    # single s{i} alias errors. This must drop the whole count attempt to the
    # sampled fallback, not report every status column as 0 while still claiming
    # authoritative: true.
    # totalCount raised well above what's actually enumerated so a fallback to
    # sampling is unambiguously non-authoritative (mirrors the truncated-board
    # fallback test below), isolating this test to the null-projectV2 defect.
    node = _read_node_states()
    node["projectV2"]["items"]["totalCount"] = 91

    def handler(request: httpx.Request) -> Response:
        if not request.url.path.endswith("/graphql"):
            return Response(404, json={"message": "Not Found"})
        payload = json.loads(request.content.decode("utf-8"))
        query = payload["query"]
        if "ProjectStatusCounts" in query:
            return Response(
                200,
                json={
                    "data": {"organization": {"projectV2": None}, "user": None},
                    "errors": [{"message": "something went wrong resolving Query.organization.projectV2"}],
                },
            )
        if "ProjectCounts" in query:
            agg_node = {
                "projectV2": {
                    "statusField": {"options": [{"name": "Todo"}, {"name": "Done"}]},
                    "total": {"totalCount": 91},
                    "issue": {"totalCount": 91},
                    "pull_request": {"totalCount": 0},
                    "board_draft": {"totalCount": 0},
                    "opened": {"totalCount": 4},
                    "closed": {"totalCount": 6},
                    "no_status": {"totalCount": 0},
                }
            }
            return Response(200, json={"data": {"organization": agg_node, "user": None}})
        return Response(200, json={"data": {"organization": node, "user": None}})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/projects/1/read",
            headers=_auth_header(),
            json={"owner": "openteams-ai"},
        )
    counts = response.json()["counts"]
    assert counts["authoritative"] is False
    assert counts["by_status"] != [
        {"name": "Todo", "count": 0},
        {"name": "Done", "count": 0},
    ]


async def test_github_read_project_counts_fall_back_to_sample(tmp_path, monkeypatch):
    # The board reports 91 items but only a page enumerated, and the count DSL is
    # rejected -> counts fall back to bucketing the returned items, flagged as a
    # non-authoritative sample so the caller never mistakes it for a full count.
    node = _read_node_states()
    node["projectV2"]["items"]["totalCount"] = 91
    handler = _counts_handler(read_node=node, options=[], aggregates={}, status_counts=[], fail_dsl=True)
    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/projects/1/read",
            headers=_auth_header(),
            json={"owner": "openteams-ai"},
        )
    counts = response.json()["counts"]
    assert counts["authoritative"] is False
    assert counts["total"] == 91
    assert counts["counted_items"] == 3
    # Bucketed from the 3 enumerated items: issue(closed), issue(open), redacted.
    assert counts["by_state"] == {"open": 1, "closed": 1}
    assert counts["by_type"]["issue"] == 2
    assert counts["by_type"]["redacted"] == 1


def test_sampled_counts_counts_merged_pr_as_closed():
    # In the sampled fallback a merged PR (state="merged") must count as closed,
    # matching the authoritative is:closed bucket — not fall through to neither.
    from collab_hub_api.connectors.github_client import _sampled_counts
    from collab_hub_api.connectors.models import GitHubProjectItem

    items = [
        GitHubProjectItem(type="PULL_REQUEST", status="Done", state="merged"),
        GitHubProjectItem(type="PULL_REQUEST", status="Review", state="open"),
        GitHubProjectItem(type="ISSUE", status="Done", state="closed"),
    ]
    counts = _sampled_counts(total=3, items=items)
    assert counts.by_state == {"open": 1, "closed": 2}  # merged PR + closed issue
    assert counts.authoritative is True  # whole board enumerated (counted == total)


async def test_github_projects_do_not_expose_token(tmp_path, monkeypatch):
    _install_mock_client(monkeypatch, _graphql_handler(org=_read_node()))
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        list_body = (
            await client.post(
                "/v1/connectors/github/projects/list", headers=_auth_header(), json={"owner": "openteams-ai"}
            )
        ).text
        read_body = (
            await client.post(
                "/v1/connectors/github/projects/1/read",
                headers=_auth_header(),
                json={"owner": "openteams-ai"},
            )
        ).text
    assert STATIC_TOKEN not in list_body
    assert STATIC_TOKEN not in read_body


async def test_github_status_handles_form_encoded_broker_token(tmp_path, monkeypatch):
    # GitHub's OAuth token endpoint returns form-urlencoded, and Keycloak brokers
    # it back that way (unlike the JSON Slack/Google IdPs). The token provider
    # must parse it rather than choke on "invalid JSON".
    def handler(request: httpx.Request) -> Response:
        path = request.url.path
        if path.endswith("/broker/github/token"):
            return Response(
                200,
                text="access_token=gho_brokered_token&scope=repo%2Cread%3Aorg&token_type=bearer",
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
        if path.endswith("/user"):
            return Response(200, json={"login": "octocat"})
        if path.endswith("/user/repos"):
            return Response(200, json=[{"full_name": "acme/widgets"}])
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(
        _config(
            tmp_path,
            static_access_token="",
            broker_token_url="https://keycloak.test/realms/nebari/broker/github/token",
        )
    )
    async with _client(app) as client:
        response = await client.get("/v1/connectors/github/status", headers=_auth_header())
    body = response.json()
    assert body["connected"] is True
    assert body["state"] == "connected"
    assert body["account"] == "octocat"


# --- search ergonomics: clearer 422 on a bad repo/org qualifier ----------


async def test_github_search_bad_repo_qualifier_maps_to_422(tmp_path, monkeypatch):
    # GitHub returns 422 "Validation Failed" when a repo:/org: qualifier points
    # at something that doesn't exist / isn't visible. Surface a caller-fixable
    # 422 with guidance, not a generic 502.
    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/search/issues"):
            return Response(422, json={"message": "Validation Failed"})
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/search",
            headers=_auth_header(),
            json={"query": "is:pr", "repo": "ghost/nope"},
        )
    assert response.status_code == 422
    assert "repo" in response.json()["detail"].lower()


# --- query building: allowed_orgs -----------------------------------------


def test_build_query_empty_allowlist_adds_no_org_qualifiers():
    assert _scoped_api_client([])._build_query("is:open bug", "") == "is:open bug"


def test_build_query_empty_allowlist_imposes_no_repo_restriction():
    # Empty allowed_orgs is the documented "token's full visibility" default:
    # an explicit repo is passed through untouched, whatever org it names.
    client = _scoped_api_client([])
    assert client._build_query("is:open bug", "anyone/anything") == "is:open bug repo:anyone/anything"


def test_build_query_applies_allowed_orgs_when_no_repo_qualifier():
    client = _scoped_api_client(["acme", "other-co"])
    assert client._build_query("is:open bug", "") == "is:open bug org:acme org:other-co"


def test_build_query_explicit_repo_inside_allowlist_passes_without_org_qualifiers():
    # A caller-supplied repo: is a narrower scope than the allowlist and is
    # meant to override it, not stack with it -- as long as the repo's own
    # owner is itself inside the allowlist.
    client = _scoped_api_client(["acme"])
    assert client._build_query("is:open bug", "acme/widgets") == "is:open bug repo:acme/widgets"


def test_build_query_explicit_repo_owner_match_is_case_insensitive():
    # GitHub logins are case-insensitive; the allowlist check must be too.
    client = _scoped_api_client(["Acme"])
    assert client._build_query("is:open bug", "ACME/widgets") == "is:open bug repo:ACME/widgets"


def test_build_query_rejects_explicit_repo_outside_allowlist():
    # The allowlist boundary must hold for a caller-supplied repo too, not
    # just the no-repo case -- otherwise a caller bypasses the allowlist
    # entirely by naming a repo in an org the deployment never approved.
    client = _scoped_api_client(["acme"])
    with pytest.raises(GitHubSearchError):
        client._build_query("is:open bug", "other-org/private-repo")


async def test_github_search_applies_org_allowlist_when_no_repo_given(tmp_path, monkeypatch):
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/search/issues"):
            seen["q"] = request.url.params.get("q", "")
            return Response(200, json={"total_count": 0, "incomplete_results": False, "items": []})
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path, allowed_orgs=["acme", "other-co"]))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/search",
            headers=_auth_header(),
            json={"query": "login", "limit": 10},
        )
    assert response.status_code == 200
    assert "org:acme" in seen["q"]
    assert "org:other-co" in seen["q"]


async def test_github_search_repo_inside_org_allowlist_succeeds(tmp_path, monkeypatch):
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/search/issues"):
            seen["q"] = request.url.params.get("q", "")
            return Response(200, json={"total_count": 0, "incomplete_results": False, "items": []})
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path, allowed_orgs=["acme"]))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/search",
            headers=_auth_header(),
            json={"query": "login", "limit": 10, "repo": "acme/widgets"},
        )
    assert response.status_code == 200
    assert "repo:acme/widgets" in seen["q"]
    assert "org:" not in seen["q"]


async def test_github_search_rejects_repo_outside_org_allowlist(tmp_path, monkeypatch):
    # The allowlist is a data-scope boundary, not a UX default: a caller
    # naming a repo the deployment never approved must be rejected, not
    # silently searched at the token's full visibility.
    _install_mock_client(monkeypatch, _ok_status_handler)
    app = make_app(_config(tmp_path, allowed_orgs=["acme"]))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/search",
            headers=_auth_header(),
            json={"query": "login", "limit": 10, "repo": "other-org/private-repo"},
        )
    assert response.status_code == 422
    detail = response.json()["detail"].lower()
    assert "other-org/private-repo" in detail
    assert "allowlist" in detail


@pytest.mark.parametrize(
    "repo",
    ["acme/x repo:evil/private", "acme/x evil/y", "acme", "acme/x\trepo:evil/y", "acme/x/y"],
)
def test_build_query_rejects_malformed_repo(repo):
    # The owner check reads only the segment before the first "/", so a repo
    # carrying whitespace or a second qualifier would pass it and then widen
    # the search via GitHub's OR of repeated repo: qualifiers.
    client = _scoped_api_client(["acme"])
    with pytest.raises(GitHubSearchError):
        client._build_query("bug", repo)


@pytest.mark.parametrize(
    "query",
    [
        "bug org:evil",
        "bug repo:evil/private",
        "bug user:someone",
        "bug owner:evil",
        "bug ORG:evil",
        "bug -org:evil",
        '"org:evil"',
        "(bug OR feature) repo:evil/private",
    ],
)
def test_build_query_rejects_scope_qualifiers_in_query_with_allowlist(query):
    client = _scoped_api_client(["acme"])
    with pytest.raises(GitHubSearchError):
        client._build_query(query, "")
    with pytest.raises(GitHubSearchError):
        client._build_query(query, "acme/widgets")


def test_build_query_admits_non_scope_qualifiers_with_allowlist():
    client = _scoped_api_client(["acme"])
    assert client._build_query("is:open author:octocat label:bug", "") == "is:open author:octocat label:bug org:acme"


def test_build_query_empty_allowlist_admits_scope_qualifiers_in_query():
    # Empty allowlist == the token's full visibility; the caller may scope freely.
    assert _scoped_api_client([])._build_query("bug org:anyone", "") == "bug org:anyone"


async def test_github_search_route_rejects_malformed_repo(tmp_path, monkeypatch):
    called = {"search": False}

    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/search/issues"):
            called["search"] = True
        return Response(200, json={"total_count": 0, "incomplete_results": False, "items": []})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path, allowed_orgs=["acme"]))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/search",
            headers=_auth_header(),
            json={"query": "bug", "repo": "acme/x repo:evil/private"},
        )
    assert response.status_code == 422
    assert called["search"] is False


async def test_github_search_route_rejects_scope_qualifier_in_query(tmp_path, monkeypatch):
    called = {"search": False}

    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/search/issues"):
            called["search"] = True
        return Response(200, json={"total_count": 0, "incomplete_results": False, "items": []})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path, allowed_orgs=["acme"]))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/search",
            headers=_auth_header(),
            json={"query": "bug org:evil"},
        )
    assert response.status_code == 422
    assert "org:" in response.json()["detail"]
    assert called["search"] is False


async def test_github_search_drops_hits_outside_allowlist(tmp_path, monkeypatch):
    # Defense in depth: even if GitHub returned an out-of-allowlist hit, the
    # client drops it rather than relying on query parsing alone.
    outside = _issue_item(2)
    outside["repository_url"] = "https://github.test/api/repos/evil/private"

    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/search/issues"):
            return Response(
                200, json={"total_count": 2, "incomplete_results": False, "items": [_issue_item(1), outside]}
            )
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path, allowed_orgs=["ACME"]))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/search",
            headers=_auth_header(),
            json={"query": "bug", "limit": 10},
        )
    assert response.status_code == 200
    assert [hit["repo"] for hit in response.json()["hits"]] == ["acme/widgets"]


# --- repo discovery: list_github_repos -----------------------------------


def _repo_node(name):
    return {
        "full_name": f"acme/{name}",
        "description": f"{name} service",
        "open_issues_count": 3,
        "updated_at": "2026-08-01T00:00:00Z",
        "private": True,
        "archived": False,
    }


async def test_github_list_repos_org(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/orgs/acme/repos"):
            return Response(200, json=[_repo_node("widgets"), _repo_node("gadgets")])
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post("/v1/connectors/github/repos/list", headers=_auth_header(), json={"owner": "acme"})
    assert response.status_code == 200
    repos = response.json()["repos"]
    assert [r["full_name"] for r in repos] == ["acme/widgets", "acme/gadgets"]
    assert repos[0]["open_issues"] == 3


async def test_github_list_repos_falls_back_to_user(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        if request.url.path.endswith("/orgs/octocat/repos"):
            return Response(404, json={"message": "Not Found"})
        if request.url.path.endswith("/users/octocat/repos"):
            return Response(200, json=[_repo_node("dotfiles")])
        return Response(404, json={"message": "Not Found"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/repos/list", headers=_auth_header(), json={"owner": "octocat"}
        )
    assert response.status_code == 200
    assert len(response.json()["repos"]) == 1


async def test_github_list_repos_rejects_bad_owner(tmp_path, monkeypatch):
    _install_mock_client(monkeypatch, _ok_status_handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/connectors/github/repos/list", headers=_auth_header(), json={"owner": "bad/owner"}
        )
    assert response.status_code == 422


# --- generic read: GitHubClient.api_get -----------------------------------

_API_BASE = "https://github.test/api"


def _api_client() -> GitHubClient:
    return GitHubClient(access_token=STATIC_TOKEN, api_base_url=_API_BASE)


def _json_response(payload, *, headers=None, links=None) -> Response:
    hdrs = {"content-type": "application/json; charset=utf-8"}
    if links:
        hdrs["Link"] = links
    if headers:
        hdrs.update(headers)
    return Response(200, headers=hdrs, json=payload)


async def test_api_get_happy_json(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> Response:
        captured["path"] = request.url.path
        captured["params"] = request.url.params
        captured["accept"] = request.headers.get("accept")
        captured["api_version"] = request.headers.get("x-github-api-version")
        captured["auth"] = request.headers.get("authorization")
        return _json_response({"number": 1, "title": "hi"})

    _install_mock_client(monkeypatch, handler)
    result = await _api_client().api_get(path="/repos/acme/widgets/pulls/1", params={"per_page": 3})
    assert result.body == {"number": 1, "title": "hi"}
    assert result.body_text == ""
    assert result.truncated is False
    assert result.has_more is False
    assert result.content_type == "application/json"
    assert result.status == 200
    assert captured["path"] == "/api/repos/acme/widgets/pulls/1"
    assert captured["params"].get("per_page") == "3"
    assert captured["accept"] == "application/vnd.github+json"
    assert captured["api_version"] == "2022-11-28"
    assert captured["auth"] == f"Bearer {STATIC_TOKEN}"


async def test_api_get_coerces_param_scalars(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> Response:
        captured["params"] = request.url.params
        return _json_response([])

    _install_mock_client(monkeypatch, handler)
    await _api_client().api_get(path="/repos/acme/widgets/pulls", params={"per_page": 100, "page": 2, "all": True})
    params = captured["params"]
    assert params.get("per_page") == "100"
    assert params.get("page") == "2"
    assert params.get("all") == "true"  # bool -> lowercase, not "True"


async def test_api_get_sanitizes_nested_string_values(monkeypatch):
    def handler(request: httpx.Request) -> Response:
        return _json_response(
            {
                "body": "see https://evil.test/x",
                "path": "config.py",
                "nested": {"url": "http://evil.test", "n": 5},
                "list": ["visit www.evil.test/a", "release/2.0"],
            }
        )

    _install_mock_client(monkeypatch, handler)
    result = await _api_client().api_get(path="/repos/a/b/issues/1")
    assert result.body["body"] == "see [link]"
    assert result.body["path"] == "config.py"  # code-shaped value preserved
    assert result.body["nested"]["url"] == "[link]"
    assert result.body["nested"]["n"] == 5  # non-string untouched
    assert result.body["list"] == ["visit [link]", "release/2.0"]
    assert set(result.body.keys()) == {"body", "path", "nested", "list"}  # non-link keys unchanged


async def test_api_get_sanitizes_link_shaped_object_keys(monkeypatch):
    # Gists key their `files` object by attacker-chosen filename, so an object KEY
    # is as untrusted a channel as a value: a link-shaped key must be masked too.
    def handler(request: httpx.Request) -> Response:
        return _json_response({"files": {"see https://evil.test/x.txt": {"content": "ok"}}})

    _install_mock_client(monkeypatch, handler)
    result = await _api_client().api_get(path="/gists/abc123")
    files = result.body["files"]
    assert "https://evil.test" not in str(files)  # link-shaped key masked
    assert list(files.values()) == [{"content": "ok"}]  # value under it preserved
    assert any("[link]" in key for key in files)


async def test_api_get_error_message_is_sanitized(monkeypatch):
    # A 4xx message reflects request input; a link in it must be masked before it
    # reaches the model through the error detail (the body path already sanitizes).
    def handler(request: httpx.Request) -> Response:
        return Response(422, json={"message": "Invalid ref: see http://evil.test/x for help"})

    _install_mock_client(monkeypatch, handler)
    with pytest.raises(GitHubUpstreamError) as excinfo:
        await _api_client().api_get(path="/repos/a/b/issues/1")
    assert "http://evil.test" not in str(excinfo.value)
    assert "[link]" in str(excinfo.value)


async def test_api_get_diff_media(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> Response:
        captured["accept"] = request.headers.get("accept")
        return Response(
            200,
            headers={"content-type": "application/vnd.github.diff; charset=utf-8"},
            content=b"diff --git a/config.py b/config.py\n+see https://evil.test\n",
        )

    _install_mock_client(monkeypatch, handler)
    result = await _api_client().api_get(path="/repos/a/b/pulls/1", media_type="diff")
    assert captured["accept"] == "application/vnd.github.diff"
    assert result.content_type == "application/vnd.github.diff"
    assert result.body is None
    assert "config.py" in result.body_text  # code preserved
    assert "[link]" in result.body_text  # url masked
    assert "https://evil.test" not in result.body_text
    assert result.status == 200


async def test_api_get_diff_requested_json_returned_degrades(monkeypatch):
    # GitHub silently returns JSON when a diff Accept is unsupported (e.g. issues);
    # dispatch is keyed on Content-Type, and content_type echoes the degrade.
    def handler(request: httpx.Request) -> Response:
        return _json_response({"number": 1})

    _install_mock_client(monkeypatch, handler)
    result = await _api_client().api_get(path="/repos/a/b/issues/1", media_type="diff")
    assert result.body == {"number": 1}
    assert result.content_type == "application/json"


@pytest.mark.parametrize(
    "bad_path",
    [
        "no-slash",
        "/a?q=1",
        "/a#frag",
        "/a/../b",
        "//evil/x",
        "https://evil.com",
        "/a\x01b",
        "/a\x7fb",
        "/a\u200bb",
        "/" + "x" * 500,
        "",
        "/a b",
        "/%2e%2e/x",
        "/a%2fb",
        "/a%20b",
        "/graphql",
        "/GRAPHQL",
        "/graphql/",
    ],
)
async def test_api_get_rejects_bad_paths(monkeypatch, bad_path):
    def handler(request: httpx.Request) -> Response:  # pragma: no cover - must not be reached
        return _json_response({})

    _install_mock_client(monkeypatch, handler)
    with pytest.raises(GitHubApiRequestError):
        await _api_client().api_get(path=bad_path)


def _scoped_api_client(orgs: list[str]) -> GitHubClient:
    return GitHubClient(access_token=STATIC_TOKEN, api_base_url=_API_BASE, allowed_orgs=orgs)


async def test_api_get_allowlist_admits_owner_qualified_paths(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> Response:
        captured["path"] = request.url.path
        return _json_response({"ok": True})

    _install_mock_client(monkeypatch, handler)
    client = _scoped_api_client(["acme"])
    for path in ("/repos/acme/widgets/pulls/1", "/orgs/acme/members", "/users/acme/repos"):
        result = await client.api_get(path=path)
        assert result.body == {"ok": True}


async def test_api_get_allowlist_is_case_insensitive(monkeypatch):
    def handler(request: httpx.Request) -> Response:
        return _json_response({"ok": True})

    _install_mock_client(monkeypatch, handler)
    # Config value cased one way, path cased the other -- GitHub logins are
    # case-insensitive, so this must be admitted.
    result = await _scoped_api_client(["Acme"]).api_get(path="/repos/ACME/widgets")
    assert result.body == {"ok": True}


@pytest.mark.parametrize(
    "path",
    [
        "/repos/other/widgets/pulls/1",
        "/orgs/other/members",
        "/users/other/repos",
    ],
)
async def test_api_get_rejects_owner_outside_allowlist(monkeypatch, path):
    def handler(request: httpx.Request) -> Response:  # pragma: no cover - must not be reached
        return _json_response({})

    _install_mock_client(monkeypatch, handler)
    with pytest.raises(GitHubApiRequestError, match="allowlist"):
        await _scoped_api_client(["acme"]).api_get(path=path)


@pytest.mark.parametrize(
    "path",
    [
        "/search/issues",
        "/search/code",
        "/search/repositories",
        "/user",
        "/user/repos",
        "/user/emails",
        "/issues",
        "/gists",
        "/notifications",
        "/repositories",
    ],
)
async def test_api_get_allowlist_refuses_non_owner_scoped_paths(monkeypatch, path):
    def handler(request: httpx.Request) -> Response:  # pragma: no cover - must not be reached
        return _json_response({})

    _install_mock_client(monkeypatch, handler)
    with pytest.raises(GitHubApiRequestError, match="allowed org"):
        await _scoped_api_client(["acme"]).api_get(path=path)


async def test_api_get_empty_allowlist_admits_any_path(monkeypatch):
    def handler(request: httpx.Request) -> Response:
        return _json_response({"ok": True})

    _install_mock_client(monkeypatch, handler)
    # Empty allowlist == the token's full visibility (existing behavior): a
    # cross-org path and a search path both go through unchanged.
    client = _api_client()
    for path in ("/repos/anyone/x", "/search/issues", "/user"):
        result = await client.api_get(path=path)
        assert result.body == {"ok": True}


async def test_api_get_follows_same_host_redirect(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> Response:
        seen.append((str(request.url), request.headers.get("authorization")))
        if request.url.path == "/api/repos/old/name":
            return Response(301, headers={"Location": "https://github.test/api/repos/new/name"})
        return _json_response({"ok": True})

    _install_mock_client(monkeypatch, handler)
    result = await _api_client().api_get(path="/repos/old/name")
    assert result.body == {"ok": True}
    assert len(seen) == 2
    assert seen[1][0] == "https://github.test/api/repos/new/name"
    assert seen[1][1] == f"Bearer {STATIC_TOKEN}"  # auth kept across the hop
    for url, _auth in seen:
        assert httpx.URL(url).host == "github.test"


async def test_api_get_reenforces_allowlist_on_redirect_to_disallowed_owner(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> Response:
        seen.append(request.url.path)
        if request.url.path == "/api/repos/acme/old":
            # A same-origin 301 onto an owner OUTSIDE the allowlist (e.g. a repo
            # transferred to a new owner) — must be re-checked, not followed.
            return Response(301, headers={"Location": "https://github.test/api/repos/other/new"})
        return _json_response({"ok": True})  # pragma: no cover - must not be reached

    _install_mock_client(monkeypatch, handler)
    with pytest.raises(GitHubApiRequestError, match="allowlist"):
        await _scoped_api_client(["acme"]).api_get(path="/repos/acme/old")
    assert seen == ["/api/repos/acme/old"]  # the disallowed hop was never issued


async def test_api_get_follows_redirect_to_allowed_owner(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> Response:
        seen.append(request.url.path)
        if request.url.path == "/api/repos/acme/old":
            return Response(301, headers={"Location": "https://github.test/api/repos/acme2/new"})
        return _json_response({"ok": True})

    _install_mock_client(monkeypatch, handler)
    result = await _scoped_api_client(["acme", "acme2"]).api_get(path="/repos/acme/old")
    assert result.body == {"ok": True}
    assert seen == ["/api/repos/acme/old", "/api/repos/acme2/new"]  # both owners allowed


async def test_api_get_redirect_hop_cap(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> Response:
        seen.append(str(request.url))
        # Always redirect on-host to a fresh path so only the hop cap can stop it.
        return Response(301, headers={"Location": f"https://github.test/api/x{len(seen)}"})

    _install_mock_client(monkeypatch, handler)
    with pytest.raises(GitHubApiRequestError):
        await _api_client().api_get(path="/repos/a/b")
    assert len(seen) == 4  # initial + 3 followed hops, then refuse


async def test_api_get_refuses_cross_host_redirect(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> Response:
        seen.append(str(request.url))
        return Response(302, headers={"Location": "https://codeload.github.test/acme/widgets/tarball"})

    _install_mock_client(monkeypatch, handler)
    with pytest.raises(GitHubApiRequestError) as excinfo:
        await _api_client().api_get(path="/repos/acme/widgets/tarball")
    assert len(seen) == 1  # never issued the cross-host hop
    assert httpx.URL(seen[0]).host == "github.test"
    message = str(excinfo.value).lower()
    assert "archive" in message or "binary" in message


async def test_api_get_refuses_downgrade_redirect(monkeypatch):
    def handler(request: httpx.Request) -> Response:
        return Response(301, headers={"Location": "http://github.test/api/x"})

    _install_mock_client(monkeypatch, handler)
    with pytest.raises(GitHubApiRequestError):
        await _api_client().api_get(path="/repos/a/b")


async def test_api_get_refuses_userinfo_spoof_redirect(monkeypatch):
    def handler(request: httpx.Request) -> Response:
        return Response(301, headers={"Location": "https://github.test@evil.com/x"})

    _install_mock_client(monkeypatch, handler)
    with pytest.raises(GitHubApiRequestError):
        await _api_client().api_get(path="/repos/a/b")


async def test_api_get_refuses_malformed_redirect_location(monkeypatch):
    # A malformed upstream Location (unterminated IPv6 bracket) raises
    # httpx.InvalidURL, which is NOT an httpx.HTTPError — it must degrade to the
    # 422 refusal, not surface as an uncaught raw 500.
    def handler(request: httpx.Request) -> Response:
        return Response(301, headers={"Location": "https://[bad"})

    _install_mock_client(monkeypatch, handler)
    with pytest.raises(GitHubApiRequestError):
        await _api_client().api_get(path="/repos/a/b")


async def test_api_get_aborts_oversized_body(monkeypatch):
    pulled = {"chunks": 0}

    def handler(request: httpx.Request) -> Response:
        async def gen():
            for _ in range(1000):
                pulled["chunks"] += 1
                yield b"a" * 10_000  # 10 MB if fully drained

        return Response(200, headers={"content-type": "application/vnd.github.diff"}, content=gen())

    _install_mock_client(monkeypatch, handler)
    result = await _api_client().api_get(path="/repos/a/b/pulls/1", media_type="diff", max_chars=1000)
    assert result.truncated is True
    assert pulled["chunks"] < 100  # aborted at the byte cap, not fully buffered
    assert len(result.body_text) <= 1000


async def test_api_get_text_expansion_does_not_falsely_truncate(monkeypatch):
    # Masking inflates length (a@b.co -> a [at] b [dot] co), so a diff that fit the
    # raw byte budget must NOT be flagged truncated just because the masked form is
    # longer than max_chars — that would set has_more and send the model paging for
    # content it already holds in full.
    raw = b"a@b.co c@d.co e@f.co"  # 20 bytes; masks to 53 chars
    expected = "a [at] b [dot] co c [at] d [dot] co e [at] f [dot] co"

    def handler(request: httpx.Request) -> Response:
        return Response(200, headers={"content-type": "application/vnd.github.diff"}, content=raw)

    _install_mock_client(monkeypatch, handler)
    result = await _api_client().api_get(path="/repos/a/b/pulls/1", media_type="diff", max_chars=30)
    assert result.truncated is False
    assert result.has_more is False
    assert result.body_text == expected  # returned whole despite exceeding max_chars
    assert len(result.body_text) > 30


async def test_api_get_json_expansion_keeps_structure(monkeypatch):
    # A JSON body that fit raw must stay STRUCTURED even when value masking inflates
    # its serialized length past max_chars — don't downgrade a complete object to a
    # truncated text prefix.
    def handler(request: httpx.Request) -> Response:
        return _json_response({"e": "a@b.co c@d.co e@f.co"})

    _install_mock_client(monkeypatch, handler)
    result = await _api_client().api_get(path="/repos/a/b/issues/1", max_chars=30)
    assert result.truncated is False
    assert result.body == {"e": "a [at] b [dot] co c [at] d [dot] co e [at] f [dot] co"}
    assert result.body_text == ""


async def test_api_get_json_size_ignores_upstream_whitespace(monkeypatch):
    # The size decision is made on the COMPACT parsed size, so a body padded with
    # incidental upstream whitespace (indentation) that fits compact must NOT be
    # flagged truncated — only the real content counts against max_chars.
    raw = b'{\n    "a": 1,\n    "b": 2\n}'  # 26 bytes raw; compact {"a":1,"b":2} is 13

    def handler(request: httpx.Request) -> Response:
        return Response(200, headers={"content-type": "application/json"}, content=raw)

    _install_mock_client(monkeypatch, handler)
    result = await _api_client().api_get(path="/repos/a/b", max_chars=20)
    assert result.truncated is False
    assert result.body == {"a": 1, "b": 2}


async def test_api_get_partial_line_trim_keeps_long_partial_on_small_budget(monkeypatch):
    # Shed threshold is min(1000-char cap, max_chars // divisor). On a small budget the
    # max_chars//divisor floor takes over, so a LONG partial line is kept (trimming back
    # to an early newline would discard most of the returned content) — unlike a bare
    # 1000-char cap, which exceeds the whole body at this budget and would always fire.
    content = b"xxxxx\n" + b"y" * 100

    def handler(request: httpx.Request) -> Response:
        return Response(200, headers={"content-type": "application/vnd.github.diff"}, content=content)

    _install_mock_client(monkeypatch, handler)
    result = await _api_client().api_get(path="/repos/a/b/pulls/1", media_type="diff", max_chars=20)
    assert result.truncated is True
    assert len(result.body_text) == 20  # long partial line kept, budget not thrown away
    assert result.body_text.startswith("xxxxx\ny")


async def test_api_get_partial_line_trim_sheds_short_tail_on_small_budget(monkeypatch):
    # ...but a SHORT trailing partial (within the threshold) IS still shed on the same
    # small budget.
    content = b"line1\nline2\nline3\n" + b"z" * 100

    def handler(request: httpx.Request) -> Response:
        return Response(200, headers={"content-type": "application/vnd.github.diff"}, content=content)

    _install_mock_client(monkeypatch, handler)
    result = await _api_client().api_get(path="/repos/a/b/pulls/1", media_type="diff", max_chars=20)
    assert result.truncated is True
    assert result.body_text == "line1\nline2\nline3"  # partial "zz" tail dropped


async def test_api_get_honors_timeout_override(monkeypatch):
    # A caller can hand api_get a reduced deadline (the route charges the read only
    # the budget left after the concurrency-permit wait); a slow upstream then times
    # out within that override.
    async def slow_body():
        await asyncio.sleep(0.5)
        yield b"{}"

    def handler(request: httpx.Request) -> Response:
        return Response(200, headers={"content-type": "application/json"}, content=slow_body())

    _install_mock_client(monkeypatch, handler)
    with pytest.raises(GitHubUpstreamError) as excinfo:
        await _api_client().api_get(path="/repos/a/b", timeout_seconds=0.05)
    assert "timed out" in str(excinfo.value)


async def test_api_get_clamps_non_positive_timeout_override(monkeypatch):
    # Defensive backstop (mirrors the max_chars clamp): a non-positive override from a
    # direct caller must be ignored (fall back to the client deadline), NOT passed to
    # asyncio.timeout — where 0/negative would fire immediately and fail the read.
    def handler(request: httpx.Request) -> Response:
        return _json_response({"ok": 1})

    _install_mock_client(monkeypatch, handler)
    for bad in (0, -5):
        result = await _api_client().api_get(path="/repos/a/b", timeout_seconds=bad)
        assert result.body == {"ok": 1}  # succeeded on the fallback deadline, not an instant timeout


async def test_api_get_refuses_binary_content(monkeypatch):
    def handler(request: httpx.Request) -> Response:
        return Response(200, headers={"content-type": "application/octet-stream"}, content=b"\x00\x01\x02")

    _install_mock_client(monkeypatch, handler)
    with pytest.raises(GitHubApiRequestError):
        await _api_client().api_get(path="/repos/a/b/contents/x")


async def test_api_get_rate_limit_primary(monkeypatch):
    def handler(request: httpx.Request) -> Response:
        return Response(
            403,
            headers={"x-ratelimit-remaining": "0", "retry-after": "60"},
            json={"message": "API rate limit exceeded"},
        )

    _install_mock_client(monkeypatch, handler)
    with pytest.raises(GitHubUpstreamError) as excinfo:
        await _api_client().api_get(path="/repos/a/b")
    assert excinfo.value.status_code == 429
    assert excinfo.value.retry_after == "60"


async def test_api_get_rate_limit_secondary_header(monkeypatch):
    # 403 + Retry-After with remaining>0 is the secondary limit: NEW header-based
    # normalization to 429 (would otherwise escape to 502 and drop the hint).
    def handler(request: httpx.Request) -> Response:
        return Response(
            403,
            headers={"x-ratelimit-remaining": "42", "retry-after": "30"},
            json={"message": "You have exceeded a secondary rate limit"},
        )

    _install_mock_client(monkeypatch, handler)
    with pytest.raises(GitHubUpstreamError) as excinfo:
        await _api_client().api_get(path="/repos/a/b")
    assert excinfo.value.status_code == 429
    assert excinfo.value.retry_after == "30"
    assert "30" in str(excinfo.value)  # retry_after surfaced in the detail string


async def test_api_get_forbidden_without_rate_signal_is_upstream(monkeypatch):
    # 403 with neither remaining:0 nor Retry-After stays a 403 upstream error
    # (-> 502 at the route): we refuse to string-match GitHub's error prose.
    def handler(request: httpx.Request) -> Response:
        return Response(403, json={"message": "Forbidden"})

    _install_mock_client(monkeypatch, handler)
    with pytest.raises(GitHubUpstreamError) as excinfo:
        await _api_client().api_get(path="/repos/a/b")
    assert excinfo.value.status_code == 403


# _raise_for_github_status is the SHARED classifier (every curated GitHub tool
# routes 4xx/5xx through it), so pin its 403->429 behavior directly, not only via
# api_get, which can be feature-flagged off.
def test_raise_for_github_status_403_retry_after_keeps_upstream_detail():
    # A 403 carrying Retry-After can be a permanent WAF/abuse block, not a rate
    # limit: normalize to 429 (keep the backoff hint) WITHOUT discarding GitHub's
    # own structured reason behind the generic message.
    response = httpx.Response(
        403,
        headers={"retry-after": "45", "x-ratelimit-remaining": "99"},
        json={"message": "Access blocked by our WAF"},
    )
    with pytest.raises(GitHubUpstreamError) as excinfo:
        _raise_for_github_status(response, operation="curated read")
    exc = excinfo.value
    assert exc.status_code == 429
    assert exc.retry_after == "45"
    assert "retry after 45 seconds" in str(exc)
    assert "Access blocked by our WAF" in str(exc)  # upstream detail not dropped


def test_raise_for_github_status_http_date_retry_after_not_labeled_seconds():
    # Retry-After is legally an HTTP-date (RFC 9110), not just delta-seconds. It
    # must NOT be interpolated as "<date> seconds" into the model-visible detail,
    # though the raw value still rides the retry_after field for the header.
    response = httpx.Response(
        403,
        headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT", "x-ratelimit-remaining": "0"},
        json={"message": "API rate limit exceeded"},
    )
    with pytest.raises(GitHubUpstreamError) as excinfo:
        _raise_for_github_status(response, operation="curated read")
    exc = excinfo.value
    assert exc.status_code == 429
    assert "seconds" not in str(exc)  # a date was never mislabeled as seconds
    assert exc.retry_after == "Wed, 21 Oct 2026 07:28:00 GMT"
    assert "API rate limit exceeded" in str(exc)


async def test_api_get_diff_406_friendly_message(monkeypatch):
    def handler(request: httpx.Request) -> Response:
        return Response(406, json={"message": "Not Acceptable"})

    _install_mock_client(monkeypatch, handler)
    with pytest.raises(GitHubApiRequestError) as excinfo:
        await _api_client().api_get(path="/repos/a/b/pulls/1", media_type="diff")
    message = str(excinfo.value).lower()
    assert "pulls" in message and "files" in message


async def test_api_get_202_empty_body(monkeypatch):
    def handler(request: httpx.Request) -> Response:
        return Response(202, headers={"content-type": "application/json"})

    _install_mock_client(monkeypatch, handler)
    result = await _api_client().api_get(path="/repos/a/b/stats/contributors")
    assert result.status == 202
    assert result.body is None
    assert result.body_text == ""
    assert result.truncated is False


async def test_api_get_204_empty_body(monkeypatch):
    # Several GETs answer no-body-means-yes with a bare 204 and no Content-Type
    # at all (e.g. /repos/{o}/{r}/collaborators/{user}, /user/following/{user},
    # /repos/{o}/{r}/vulnerability-alerts). Must not fall through to the
    # binary/unknown-content-type refusal.
    def handler(request: httpx.Request) -> Response:
        return Response(204)

    _install_mock_client(monkeypatch, handler)
    result = await _api_client().api_get(path="/repos/a/b/collaborators/carol")
    assert result.status == 204
    assert result.body is None
    assert result.body_text == ""
    assert result.truncated is False


async def test_api_get_empty_200_body_is_not_a_size_cap_error(monkeypatch):
    # A literal empty 200 body (Content-Length: 0, Content-Type: json) must not
    # be reported as "exceeded the size cap before it could be parsed" -- that
    # message is for a body that overflowed max_chars mid-parse, and would send
    # someone debugging a legitimately-empty response looking at the wrong
    # knob entirely. Nothing here was truncated: byte_truncated is False.
    def handler(request: httpx.Request) -> Response:
        return Response(200, headers={"content-type": "application/json"}, content=b"")

    _install_mock_client(monkeypatch, handler)
    result = await _api_client().api_get(path="/repos/a/b/some/empty/json/endpoint")
    assert result.status == 200
    assert result.body is None
    assert result.body_text == ""
    assert result.truncated is False


async def test_api_get_has_more_from_link_header(monkeypatch):
    def handler(request: httpx.Request) -> Response:
        return _json_response([{"n": 1}], links='<https://github.test/api/x?page=2>; rel="next"')

    _install_mock_client(monkeypatch, handler)
    result = await _api_client().api_get(path="/repos/a/b/pulls")
    assert result.has_more is True


async def test_api_get_json_over_max_chars_becomes_text_prefix(monkeypatch):
    big = {"items": ["x" * 100 for _ in range(50)]}  # serialized well over 200 chars

    def handler(request: httpx.Request) -> Response:
        return _json_response(big)

    _install_mock_client(monkeypatch, handler)
    result = await _api_client().api_get(path="/repos/a/b/pulls", max_chars=200)
    assert result.body is None
    assert result.truncated is True
    assert len(result.body_text) <= 200


async def test_api_get_truncated_unparseable_json_is_sanitized(monkeypatch):
    # A JSON body that overflows the byte cap and fails to parse still has its
    # link-shaped values masked — the truncated-prefix path must not leak raw
    # URLs to the renderer (parity with the parsed and diff paths).
    body = '{"u": "visit https://evil.test/x", "pad": "' + "y" * 5000 + '"}'

    def handler(request: httpx.Request) -> Response:
        return Response(200, headers={"content-type": "application/json"}, content=body.encode())

    _install_mock_client(monkeypatch, handler)
    result = await _api_client().api_get(path="/repos/a/b/commits", max_chars=200)
    assert result.body is None
    assert result.truncated is True
    assert "https://evil.test" not in result.body_text  # link masked, not leaked
    assert "[link]" in result.body_text


async def test_api_get_diff_trimmed_to_last_line(monkeypatch):
    text = ("line one is here\n" * 100).encode()

    def handler(request: httpx.Request) -> Response:
        return Response(200, headers={"content-type": "application/vnd.github.diff"}, content=text)

    _install_mock_client(monkeypatch, handler)
    result = await _api_client().api_get(path="/repos/a/b/pulls/1", media_type="diff", max_chars=50)
    assert result.truncated is True
    assert len(result.body_text) <= 50
    assert result.body_text.endswith("here")  # cut at a line boundary, not mid-line


async def test_api_get_exact_fit_not_truncated(monkeypatch):
    payload = {"a": "bb"}

    def handler(request: httpx.Request) -> Response:
        return _json_response(payload)

    _install_mock_client(monkeypatch, handler)
    result = await _api_client().api_get(path="/repos/a/b", max_chars=10_000)
    assert result.truncated is False
    assert result.body == payload


async def test_api_get_json_default_max_chars(monkeypatch):
    payload = {"items": ["y" * 100 for _ in range(300)]}  # serialized well over 20k

    def handler(request: httpx.Request) -> Response:
        return _json_response(payload)

    _install_mock_client(monkeypatch, handler)
    result = await _api_client().api_get(path="/repos/a/b/pulls")  # omitted -> json default 20k
    assert result.truncated is True
    assert result.body is None


async def test_api_get_diff_default_max_chars(monkeypatch):
    text = b"a" * 30_000  # under the diff default of 50k

    def handler(request: httpx.Request) -> Response:
        return Response(200, headers={"content-type": "application/vnd.github.diff"}, content=text)

    _install_mock_client(monkeypatch, handler)
    result = await _api_client().api_get(path="/repos/a/b/pulls/1", media_type="diff")  # -> 50k default
    assert result.truncated is False
    assert len(result.body_text) == 30_000


async def test_api_get_never_exposes_token(monkeypatch):
    def handler(request: httpx.Request) -> Response:
        path = request.url.path
        if path.endswith("/diff"):
            return Response(
                200,
                headers={"content-type": "application/vnd.github.diff"},
                content=b"diff config.py\n",
            )
        if path.endswith("/err"):
            return Response(500, json={"message": "boom"})
        if path.endswith("/binary"):
            return Response(200, headers={"content-type": "application/octet-stream"}, content=b"\x00")
        return _json_response({"t": "ok"})

    _install_mock_client(monkeypatch, handler)
    client = _api_client()

    async def blob_of(path, **kwargs) -> str:
        try:
            result = await client.api_get(path=path, **kwargs)
        except (GitHubApiRequestError, GitHubUpstreamError) as exc:
            return str(exc)
        return repr(result.body) + result.body_text + result.content_type + str(result.status)

    assert STATIC_TOKEN not in await blob_of("/repos/a/b/json")
    assert STATIC_TOKEN not in await blob_of("/repos/a/b/diff", media_type="diff")
    assert STATIC_TOKEN not in await blob_of("/repos/a/b/err")
    assert STATIC_TOKEN not in await blob_of("/repos/a/b/binary")
    assert STATIC_TOKEN not in await blob_of("no-slash-refusal")  # validation refusal path


async def test_api_get_enforces_time_budget(monkeypatch):
    def handler(request: httpx.Request) -> Response:
        async def slow():
            await asyncio.sleep(5)
            yield b"{}"

        return Response(200, headers={"content-type": "application/json"}, content=slow())

    _install_mock_client(monkeypatch, handler)
    client = GitHubClient(access_token=STATIC_TOKEN, api_base_url=_API_BASE, timeout_seconds=0.15)
    with pytest.raises((GitHubUpstreamError, httpx.TimeoutException)):
        await client.api_get(path="/repos/a/b")


# --- generic read: latest-review hardening (batch, TDD) -------------------


async def test_api_get_diff_single_long_line_keeps_budget(monkeypatch):
    # Regression: a minified / single-long-line diff is ~40 chars of headers then
    # one enormous line. Trimming to the last newline shed the whole budget back
    # to the header (49.9k dropped, no offset to recover). At the 50k default the
    # shed ceiling is the 1000-char cap (~2% of budget), so only a SHORT trailing
    # partial line is trimmed; this long one is kept.
    header = "diff --git a/min.js b/min.js\n@@ -1 +1 @@\n"
    text = (header + "+" + "a" * 200_000).encode()

    def handler(request: httpx.Request) -> Response:
        return Response(200, headers={"content-type": "application/vnd.github.diff"}, content=text)

    _install_mock_client(monkeypatch, handler)
    result = await _api_client().api_get(path="/repos/a/b/pulls/1", media_type="diff", max_chars=50_000)
    assert result.truncated is True
    assert len(result.body_text) > 40_000  # budget preserved, not trimmed to the header


async def test_api_get_error_body_is_capped(monkeypatch):
    # Hardening: the >=400 error-body read was the one uncapped read left. A
    # misbehaving GHE/proxy streaming a huge error page must not be fully buffered.
    pulled = {"chunks": 0}

    def handler(request: httpx.Request) -> Response:
        async def gen():
            for _ in range(1000):
                pulled["chunks"] += 1
                yield b"E" * 100_000  # 100 MB if fully drained

        return Response(500, headers={"content-type": "application/json"}, content=gen())

    _install_mock_client(monkeypatch, handler)
    with pytest.raises(GitHubUpstreamError):
        await _api_client().api_get(path="/repos/a/b")
    assert pulled["chunks"] < 50  # error body capped, not fully buffered


async def test_read_capped_single_oversized_chunk_bounded():
    # gzip-decompressed bodies yield decoded chunks that can be tens of MB in one
    # go; read_capped must bound the retained/returned bytes to the cap regardless.
    from collab_hub_api.connectors.http_stream import read_capped

    async def gen():
        yield b"z" * 10_000_000

    response = httpx.Response(200, content=gen())
    raw, truncated = await read_capped(response, 1000)
    assert truncated is True
    assert len(raw) <= 1000


async def test_api_get_invalid_json_not_capped_gives_honest_message(monkeypatch):
    # A whitespace-only / proxy-mangled 200 JSON body the cap did NOT truncate must
    # not tell the model to shrink per_page — that guidance is untrue and wastes turns.
    def handler(request: httpx.Request) -> Response:
        return Response(200, headers={"content-type": "application/json"}, content=b"   ")

    _install_mock_client(monkeypatch, handler)
    with pytest.raises(GitHubApiRequestError) as excinfo:
        await _api_client().api_get(path="/repos/a/b/commits")
    msg = str(excinfo.value).lower()
    assert "per_page" not in msg
    assert "not valid json" in msg


async def test_api_get_hop_cap_message_names_redirects_not_binary(monkeypatch):
    # After >max_redirects SAME-origin hops the refusal is a redirect-loop, not an
    # off-origin archive/binary download — the message must not misdirect.
    seen = []

    def handler(request: httpx.Request) -> Response:
        seen.append(1)
        return Response(301, headers={"Location": f"https://github.test/api/x{len(seen)}"})

    _install_mock_client(monkeypatch, handler)
    with pytest.raises(GitHubApiRequestError) as excinfo:
        await _api_client().api_get(path="/repos/a/b")
    msg = str(excinfo.value).lower()
    assert "redirect" in msg
    assert "archive" not in msg and "binary" not in msg


# --- generic read: request model + route ----------------------------------

_API_GET_ROUTE = "/v1/connectors/github/api/get"


def test_api_get_request_coerces_and_bounds() -> None:
    request = GitHubApiGetRequest(path="/repos/a/b/pulls", params={"per_page": 100, "draft": True})
    assert request.params == {"per_page": "100", "draft": "true"}  # int/bool -> str
    with pytest.raises(ValidationError):
        GitHubApiGetRequest(path="/x", media_type="xml")
    with pytest.raises(ValidationError):
        GitHubApiGetRequest(path="/x", params={"k" * 65: "v"})
    with pytest.raises(ValidationError):
        GitHubApiGetRequest(path="/x", params={"k": "v" * 257})
    with pytest.raises(ValidationError):
        GitHubApiGetRequest(path="/x", params={f"k{i}": "v" for i in range(21)})


async def test_api_get_route_happy(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        return _json_response({"number": 7})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            _API_GET_ROUTE, headers=_auth_header(), json={"path": "/repos/acme/widgets/pulls/7"}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["body"] == {"number": 7}
    assert body["content_trust"] == "external_untrusted"
    assert body["security_notice"]
    assert body["status"] == 200


async def test_api_get_route_enforces_allowed_orgs(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        return _json_response({"ok": True})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path, allowed_orgs=["acme"]))
    async with _client(app) as client:
        # An owner inside the allowlist goes through.
        ok = await client.post(_API_GET_ROUTE, headers=_auth_header(), json={"path": "/repos/acme/widgets/pulls/7"})
        assert ok.status_code == 200
        # An owner outside the allowlist is refused as a 422 (not silently run at
        # full token visibility) -- the wiring from config -> client -> enforcement.
        blocked = await client.post(
            _API_GET_ROUTE, headers=_auth_header(), json={"path": "/repos/other/secret/pulls/1"}
        )
        assert blocked.status_code == 422
        assert "allowlist" in blocked.json()["detail"]
        # A search path can read across orgs, so it is refused under an allowlist.
        search = await client.post(_API_GET_ROUTE, headers=_auth_header(), json={"path": "/search/issues"})
        assert search.status_code == 422


async def test_api_get_route_coerces_params(tmp_path, monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> Response:
        captured["params"] = request.url.params
        return _json_response([])

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            _API_GET_ROUTE,
            headers=_auth_header(),
            json={"path": "/repos/a/b/pulls", "params": {"per_page": 5, "draft": True}},
        )
    assert response.status_code == 200
    assert captured["params"].get("per_page") == "5"
    assert captured["params"].get("draft") == "true"


@pytest.mark.parametrize(
    "payload",
    [
        {"path": "/x", "media_type": "xml"},
        {"path": "/x", "max_chars": 0},
        {"path": "/x", "max_chars": 50_001},
        {"path": ""},
        {"path": "/x", "params": {f"k{i}": "v" for i in range(21)}},
        {"path": "/x", "params": {"k" * 65: "v"}},
        {"path": "/x", "params": {"k": "v" * 257}},
    ],
)
async def test_api_get_route_model_validation_422(tmp_path, monkeypatch, payload):
    _install_mock_client(monkeypatch, lambda r: _json_response({}))
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(_API_GET_ROUTE, headers=_auth_header(), json=payload)
    assert response.status_code == 422


async def test_api_get_route_bad_path_shape_is_422(tmp_path, monkeypatch):
    # Passes the Field length bound but fails client path validation -> 422 via
    # the GitHubApiRequestError except clause (not 404/502).
    _install_mock_client(monkeypatch, lambda r: _json_response({}))
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(_API_GET_ROUTE, headers=_auth_header(), json={"path": "no-leading-slash"})
    assert response.status_code == 422


async def test_api_get_route_rate_limit_propagates_retry_after(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        return Response(
            429,
            headers={"Retry-After": "42", "x-ratelimit-remaining": "0"},
            json={"message": "API rate limit exceeded"},
        )

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(_API_GET_ROUTE, headers=_auth_header(), json={"path": "/repos/a/b"})
    assert response.status_code == 429
    assert response.headers.get("Retry-After") == "42"
    assert "42" in response.json()["detail"]  # duration surfaced in the detail string


async def test_api_get_route_upstream_5xx_is_502(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        return Response(500, json={"message": "boom"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(_API_GET_ROUTE, headers=_auth_header(), json={"path": "/repos/a/b"})
    assert response.status_code == 502


async def test_api_get_route_upstream_422_is_422(tmp_path, monkeypatch):
    # A GitHub 422 (e.g. paging a search-backed endpoint past its 1000-result
    # cap) is model-correctable, so the generic read surfaces it as 422 with
    # GitHub's message — not a blind-retry 502.
    def handler(request: httpx.Request) -> Response:
        return Response(422, json={"message": "Only the first 1000 search results are available"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(_API_GET_ROUTE, headers=_auth_header(), json={"path": "/search/issues"})
    assert response.status_code == 422
    assert "1000" in response.json()["detail"]


async def test_api_get_route_throttles_when_saturated(tmp_path, monkeypatch, caplog):
    # 7b: when every permit is held, a request waits up to the timeout then sheds
    # itself as a 429 with a Retry-After (not a silent unbounded queue) and logs a
    # throttle event.
    _install_mock_client(monkeypatch, lambda r: _json_response({"x": 1}))
    app = make_app(_config(tmp_path, api_get_max_concurrency=1, request_timeout_seconds=0.2))
    with caplog.at_level(logging.INFO, logger="frames_server.connectors"):
        async with _client(app) as client:
            # Exhaust the single permit so the request has to wait — and time out.
            await app.state.github_api_get_semaphore.acquire()
            response = await client.post(_API_GET_ROUTE, headers=_auth_header(), json={"path": "/repos/a/b"})
    assert response.status_code == 429
    assert "busy" in response.json()["detail"].lower()
    assert response.headers.get("retry-after") == "1"  # ceil(0.2s wait budget), floored at 1
    throttle = next(r for r in caplog.records if r.msg == "github_api_get_throttled")
    assert throttle.waiters == 1  # queue depth at shed time includes this request


async def test_api_get_route_charges_read_the_remaining_budget(tmp_path, monkeypatch):
    # 7b/perf: the permit wait and the upstream read share ONE budget, so the route
    # hands the read only the time left after the wait — its deadline never stacks on
    # top of the wait budget (which would double worst-case latency).
    captured: dict = {}

    async def fake_api_get(self, **kwargs):
        captured.update(kwargs)
        return GitHubApiGetResponse(
            body={"x": 1},
            body_text="",
            truncated=False,
            has_more=False,
            content_type="application/json",
            status=200,
        )

    monkeypatch.setattr(GitHubClient, "api_get", fake_api_get)
    app = make_app(_config(tmp_path, request_timeout_seconds=5.0))
    async with _client(app) as client:
        response = await client.post(_API_GET_ROUTE, headers=_auth_header(), json={"path": "/repos/a/b"})
    assert response.status_code == 200
    assert 0 < captured["timeout_seconds"] <= 5.0  # bounded by the shared budget, not on top of it


async def test_api_get_route_broker_error_not_blocked_by_saturation(tmp_path, monkeypatch):
    # 7a: token brokering happens OUTSIDE the concurrency permit, so a token failure
    # surfaces its own status (409 not-connected) even when the read pool is fully
    # saturated — it never queues behind the semaphore or throttles to 503.
    _install_mock_client(monkeypatch, lambda r: _json_response({"x": 1}))
    # No static token and no broker URL -> ConnectorNotConnected -> 409, and it must
    # be reached before the (exhausted) permit is ever awaited.
    app = make_app(_config(tmp_path, static_access_token="", api_get_max_concurrency=1, request_timeout_seconds=0.2))
    async with _client(app) as client:
        await app.state.github_api_get_semaphore.acquire()  # exhaust the pool
        response = await client.post(_API_GET_ROUTE, headers=_auth_header(), json={"path": "/repos/a/b"})
    assert response.status_code == 409  # broker error surfaced, NOT a 503 throttle


async def test_api_get_route_upstream_400_is_422(tmp_path, monkeypatch):
    # A GitHub 400 on malformed GET params is model-correctable (bad query), so the
    # generic read surfaces 422 with GitHub's message rather than a blind-retry 502.
    def handler(request: httpx.Request) -> Response:
        return Response(400, json={"message": "Problems parsing query"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            _API_GET_ROUTE, headers=_auth_header(), json={"path": "/search/issues", "params": {"q": "bad"}}
        )
    assert response.status_code == 422
    assert "parsing" in response.json()["detail"].lower()


async def test_api_get_route_disabled_is_403(tmp_path, monkeypatch):
    _install_mock_client(monkeypatch, lambda r: _json_response({"x": 1}))
    app = make_app(_config(tmp_path, api_get_enabled=False))
    async with _client(app) as client:
        response = await client.post(_API_GET_ROUTE, headers=_auth_header(), json={"path": "/repos/a/b"})
    assert response.status_code == 403
    assert "disabled" in response.json()["detail"].lower()


def test_api_get_disabled_by_default(tmp_path):
    # Generic read ships opt-in: a hub that never sets api_get_enabled must not
    # gain full-token-visibility read on upgrade. (_config forces it on for the
    # feature tests, so build the config directly here.)
    config = Config.parse(
        {
            "storage": {"frames_path": str(tmp_path / "frames")},
            "frames": {"active_state": {"backend": "memory"}, "mcp_session_manager_enabled": False},
            "connectors": {
                "github": {
                    "static_access_token": STATIC_TOKEN,
                    "api_base_url": "https://github.test/api",
                }
            },
        }
    )
    assert config.connectors.github.api_get_enabled is False


async def test_api_get_route_requires_auth(tmp_path, monkeypatch):
    _install_mock_client(monkeypatch, lambda r: _json_response({}))
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(_API_GET_ROUTE, json={"path": "/repos/a/b"})
    assert response.status_code in (401, 403)


async def test_api_get_route_does_not_expose_token(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> Response:
        return _json_response({"x": "ok"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    async with _client(app) as client:
        response = await client.post(_API_GET_ROUTE, headers=_auth_header(), json={"path": "/repos/a/b"})
    assert STATIC_TOKEN not in response.text


async def test_api_get_route_logs_request_and_upstream_error(tmp_path, monkeypatch, caplog):
    def handler(request: httpx.Request) -> Response:
        return Response(500, json={"message": "boom"})

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    with caplog.at_level(logging.INFO, logger="frames_server.connectors"):
        async with _client(app) as client:
            await client.post(_API_GET_ROUTE, headers=_auth_header(), json={"path": "/repos/a/b"})
    requests = [r for r in caplog.records if r.msg == "github_api_get_request"]
    assert requests and requests[0].path == "/repos/a/b" and requests[0].user
    errors = [r for r in caplog.records if r.msg == "github_api_get_upstream_error"]
    assert errors
    assert errors[0].operation == "api get"
    assert errors[0].status_code == 500
    assert errors[0].path == "/repos/a/b"


async def test_api_get_route_logs_truncation(tmp_path, monkeypatch, caplog):
    big = {"items": ["y" * 100 for _ in range(300)]}

    def handler(request: httpx.Request) -> Response:
        return _json_response(big)

    _install_mock_client(monkeypatch, handler)
    app = make_app(_config(tmp_path))
    with caplog.at_level(logging.INFO, logger="frames_server.connectors"):
        async with _client(app) as client:
            response = await client.post(
                _API_GET_ROUTE, headers=_auth_header(), json={"path": "/repos/a/b", "max_chars": 200}
            )
    assert response.json()["truncated"] is True
    assert any(r.msg == "github_api_get_truncation" for r in caplog.records)


async def test_api_get_route_logs_refusal(tmp_path, monkeypatch, caplog):
    _install_mock_client(monkeypatch, lambda r: _json_response({}))
    app = make_app(_config(tmp_path))
    with caplog.at_level(logging.INFO, logger="frames_server.connectors"):
        async with _client(app) as client:
            response = await client.post(_API_GET_ROUTE, headers=_auth_header(), json={"path": "no-slash"})
    assert response.status_code == 422
    assert any(r.msg == "github_api_get_refusal" for r in caplog.records)
