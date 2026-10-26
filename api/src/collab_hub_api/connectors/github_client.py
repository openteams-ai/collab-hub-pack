from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from .connector_text import sanitize_connector_text, sanitize_github_api_text
from .http_stream import read_capped
from .models import (
    GitHubApiGetResponse,
    GitHubItem,
    GitHubProject,
    GitHubProjectCounts,
    GitHubProjectItem,
    GitHubRepo,
    GitHubReview,
    GitHubSearchHit,
    GitHubStatusCount,
    coerce_github_param_value,
)
from .validation import has_control_or_nonprintable

# GitHub search returns at most 1000 results across all pages, regardless of
# total_count, and caps per_page at 100. We never page past the 1000th result.
GITHUB_SEARCH_RESULT_CAP = 1000
GITHUB_MAX_PER_PAGE = 100
# Cap the free-text snippet carried in a search hit or a PR review body; a full
# issue/PR/review body belongs to a dedicated read, not a summary snippet. A PR
# keeps the newest MAX_ITEM_COMMENTS reviews, so this also bounds a PR's review
# text at MAX_ITEM_COMMENTS * TEXT_SNIPPET_CHARS regardless of body length.
TEXT_SNIPPET_CHARS = 500
# The contents API only returns files up to 1 MB; larger ones come back with
# encoding "none" and no content, and must be fetched via the git blobs API.
CONTENTS_MAX_BYTES = 1_000_000
# Bound how many comments a single item read pulls, independent of max_chars.
MAX_ITEM_COMMENTS = 30
# PR reviews come back oldest-first with no sort option; fetch a wide page and
# keep the newest tail so a later APPROVED isn't hidden by an early CHANGES_REQUESTED.
_PR_REVIEW_PAGE_SIZE = 100
_LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec"

logger = logging.getLogger("frames_server.connectors")

# Shape of a curated-search ``repo``: exactly ``owner/name``, nothing else. The
# repo is appended as a ``repo:`` qualifier, so anything looser (whitespace, a
# second qualifier) would let a caller widen the search past the owner check.
_SEARCH_REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
# Search qualifiers that set which owners/repos a search spans. GitHub ORs
# repeated scope qualifiers, so one in caller text would widen an allowlisted
# search; with an allowlist set, the hub owns the scope and refuses these.
_SCOPE_QUALIFIER_RE = re.compile(r"\b(?:org|user|owner|repo):", re.IGNORECASE)

# Projects V2 are GraphQL-only. Query the org and user owners in one request and
# use whichever resolves, so one tool handles org and personal boards.
_PROJECT_ITEM_FIELDS = """
        totalCount
        pageInfo { hasNextPage endCursor }
        nodes {
          type
          content {
            __typename
            ... on Issue {
              number title state repository { nameWithOwner }
              assignees(first: 10) { nodes { login } }
              labels(first: 20) { nodes { name } }
            }
            ... on PullRequest {
              number title state repository { nameWithOwner }
              assignees(first: 10) { nodes { login } }
              labels(first: 20) { nodes { name } }
            }
            ... on DraftIssue { title }
          }
          fieldValues(first: 8) {
            nodes {
              __typename
              ... on ProjectV2ItemFieldSingleSelectValue { name field { ... on ProjectV2FieldCommon { name } } }
              ... on ProjectV2ItemFieldTextValue { text field { ... on ProjectV2FieldCommon { name } } }
              ... on ProjectV2ItemFieldDateValue { date field { ... on ProjectV2FieldCommon { name } } }
              ... on ProjectV2ItemFieldNumberValue { number field { ... on ProjectV2FieldCommon { name } } }
            }
          }
        }
"""

_PROJECT_SUMMARY = "number title shortDescription closed items { totalCount }"

_PROJECTS_LIST_QUERY = f"""
query ProjectsList($login: String!, $first: Int!) {{
  organization(login: $login) {{ projectsV2(first: $first) {{ nodes {{ {_PROJECT_SUMMARY} }} }} }}
  user(login: $login) {{ projectsV2(first: $first) {{ nodes {{ {_PROJECT_SUMMARY} }} }} }}
}}
"""

_PROJECT_READ_BODY = f"""projectV2(number: $number) {{
      number title shortDescription closed
      items(first: $first, after: $after) {{ {_PROJECT_ITEM_FIELDS} }}
    }}"""

_PROJECT_READ_QUERY = f"""
query ProjectRead($login: String!, $number: Int!, $first: Int!, $after: String) {{
  organization(login: $login) {{ {_PROJECT_READ_BODY} }}
  user(login: $login) {{ {_PROJECT_READ_BODY} }}
}}
"""

# Project reads auto-paginate server-side: GitHub's GraphQL caps a page at 100,
# so we walk up to MAX_PROJECT_PAGES pages (a bounded whole-board read) and set
# truncated=True only for boards larger than that ceiling.
_PROJECT_PAGE_SIZE = 100
_MAX_PROJECT_ITEMS = 500
_MAX_PROJECT_PAGES = _MAX_PROJECT_ITEMS // _PROJECT_PAGE_SIZE

# --- Generic read (api_get) -------------------------------------------------
# The media enum maps to a FIXED Accept table — the model never controls Accept,
# so the verb (GET) and the negotiated representation are both non-model-driven.
_ACCEPT_BY_MEDIA = {
    "json": "application/vnd.github+json",
    "diff": "application/vnd.github.diff",
    "patch": "application/vnd.github.patch",
}
# Media-aware char defaults (safe-by-default): a single raw PR is ~16.6k chars so
# JSON needs headroom above that; a truncated diff is useless so text gets more.
# 50k is the hard ceiling either way (see request-model bound).
_API_GET_JSON_DEFAULT_MAX_CHARS = 20_000
_API_GET_TEXT_DEFAULT_MAX_CHARS = 50_000
_API_GET_MAX_CHARS_CEILING = 50_000
# ≤3 hops keeps a renamed-repo 301 working while refusing archive/binary chains.
_API_GET_MAX_REDIRECTS = 3
_API_GET_JSON_ERROR = (
    "the JSON response exceeded the size cap before it could be parsed — narrow "
    "the request (e.g. a smaller per_page) or fetch a smaller resource"
)
# The cap did NOT fire: a proxy-mangled or whitespace-only 200 body. Telling the
# model to shrink per_page here is untrue and uncorrectable, so keep the guidance
# honest — the response simply was not valid JSON.
_API_GET_JSON_INVALID = "the upstream returned a body that is not valid JSON"
_API_GET_REDIRECT_REFUSAL = (
    "refused to follow a redirect off the GitHub API origin — the target is "
    "likely an archive or binary download, which this read tool does not support"
)
# Distinct from the off-origin refusal above: this fires only after >max_redirects
# SAME-origin hops (a redirect loop or an unusually long renamed-repo chain), so
# the "archive or binary download" framing would misdirect the model and operator.
_API_GET_TOO_MANY_REDIRECTS = (
    f"too many redirects on the GitHub API origin — did not resolve within "
    f"{_API_GET_MAX_REDIRECTS} hops"
)
# A trailing partial line is worth shedding from a truncated text/diff body only
# when it's a small fraction of the budget; beyond that (a single-long-line minified
# file) cutting back to the last newline would discard most of the returned content,
# so we keep the partial line instead. The absolute cap is the shed ceiling at every
# realistic budget (1000 of a 50k default read ~= 2%); the max_chars//DIVISOR floor
# only takes over at tiny budgets, where a fixed 1000 would exceed the whole body and
# always fire. Shed threshold = min(cap, max_chars // divisor).
_API_GET_MAX_PARTIAL_LINE_TRIM = 1_000
_API_GET_PARTIAL_LINE_TRIM_DIVISOR = 4
# Locally-owned recovery guidance only — do NOT restate GitHub's numeric diff
# limits (lines / MB / files). Those drift and this code can't observe them, so
# we append this to GitHub's own 406 message rather than stating stale numbers.
_API_GET_406_GUIDANCE = (
    "the diff or patch is too large to return — fetch changes per file via "
    "/pulls/{number}/files (paginated), which also lets very large or binary "
    "files omit their patch field"
)
_API_GET_DEPTH_GUARD = 30
# The COMPLETE set of GitHub REST collections whose second path segment is an
# owner (org or user) login. A configured org allowlist admits only these three
# under an allowed owner and refuses everything else — search, the /user self-
# endpoints, /issues, /gists, /notifications, … — by construction, so a new
# cross-org endpoint is refused without a code change (the deliberate trade: a
# new legitimately owner-scoped prefix would need adding here). GitHub's org and
# user login namespace is shared, so /users/{login} is admitted when {login} is
# in the (org-named) allowlist.
_API_GET_OWNER_SCOPED_COLLECTIONS = ("repos", "orgs", "users")

# --- Project board counts ---------------------------------------------------
# Projects V2 GraphQL has NO group-by aggregate; the only server-side counter is
# totalCount on the items connection. But items() accepts a board-filter `query`
# and an `archivedStates` argument, so one totalCount-only query per bucket gives
# an EXACT count regardless of the item-enumeration cap above. Every count query
# pins archivedStates:[NOT_ARCHIVED] so the buckets share one policy and the
# status columns provably reconcile to the total (archived_policy="excluded").
_COUNTS_ARCHIVED = "archivedStates: [NOT_ARCHIVED]"
# A1: options + total + by-type + by-state + blank-status, all in one request.
# The by_type/by_state aliases lean on the `query` filter DSL, so a rejection of
# that DSL surfaces here and drops the whole count path to the sampled fallback.
# `is:draft` matches BOTH board drafts (DraftIssue) and draft PRs, and a board
# draft ALSO matches `is:issue` -- so the type partition is built from the raw
# buckets, not by summing overlapping filters (see _authoritative_counts):
# `board_draft` (is:draft is:issue) isolates board drafts, letting issue/pr/draft
# form an exact, non-overlapping partition of the total.
_PROJECT_COUNTS_BODY = f"""projectV2(number: $number) {{
      statusField: field(name: "Status") {{ ... on ProjectV2SingleSelectField {{ options {{ name }} }} }}
      total: items(first: 0, {_COUNTS_ARCHIVED}) {{ totalCount }}
      issue: items(first: 0, {_COUNTS_ARCHIVED}, query: "is:issue") {{ totalCount }}
      pull_request: items(first: 0, {_COUNTS_ARCHIVED}, query: "is:pr") {{ totalCount }}
      board_draft: items(first: 0, {_COUNTS_ARCHIVED}, query: "is:draft is:issue") {{ totalCount }}
      opened: items(first: 0, {_COUNTS_ARCHIVED}, query: "is:open") {{ totalCount }}
      closed: items(first: 0, {_COUNTS_ARCHIVED}, query: "is:closed") {{ totalCount }}
      no_status: items(first: 0, {_COUNTS_ARCHIVED}, query: "no:status") {{ totalCount }}
    }}"""

_PROJECT_COUNTS_QUERY = f"""
query ProjectCounts($login: String!, $number: Int!) {{
  organization(login: $login) {{ {_PROJECT_COUNTS_BODY} }}
  user(login: $login) {{ {_PROJECT_COUNTS_BODY} }}
}}
"""


def _status_counts_query(option_count: int) -> str:
    """Build one aliased query that counts every Status column in a single
    request. Column names are passed as GraphQL variables ($q0..$qN), never
    interpolated, so an option name cannot inject query text."""
    var_decls = "".join(f", $q{i}: String!" for i in range(option_count))
    aliases = "\n      ".join(
        f"s{i}: items(first: 0, {_COUNTS_ARCHIVED}, query: $q{i}) {{ totalCount }}"
        for i in range(option_count)
    )
    body = f"projectV2(number: $number) {{\n      {aliases}\n    }}"
    return (
        f"query ProjectStatusCounts($login: String!, $number: Int!{var_decls}) {{\n"
        f"  organization(login: $login) {{ {body} }}\n"
        f"  user(login: $login) {{ {body} }}\n"
        f"}}\n"
    )


def _project_filter_value(value: str) -> str:
    """Escape a value embedded in a quoted Projects filter expression.

    GraphQL variables keep option names out of the GraphQL document, but the
    variable itself is interpreted by GitHub's Projects filter DSL. Escape its
    string delimiters so a board-controlled Status option cannot change the
    query that is counted.

    Unverified upstream: live testing shows a quoted value with these escapes
    returns `totalCount: 0` with no error, so whether the Projects filter DSL
    actually honors backslash escapes (vs. silently never matching) could not
    be confirmed. A status option containing `"` in its name may therefore
    silently count as 0 rather than raising.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


@dataclass
class GitHubFileContent:
    content: str = ""
    truncated: bool = False
    too_large: bool = False
    binary: bool = False
    unsupported_reason: str = ""


@dataclass
class GitHubAccessCheck:
    """Result of validating a token against GitHub's read endpoints."""

    login: str = ""
    scopes: list[str] = field(default_factory=list)


class GitHubUpstreamError(RuntimeError):
    def __init__(
        self,
        *,
        operation: str,
        status_code: int | None = None,
        message: str = "",
        retry_after: str = "",
    ) -> None:
        self.operation = operation
        self.status_code = status_code
        self.message = message
        self.retry_after = retry_after
        detail = f"GitHub {operation} failed"
        if status_code is not None and status_code != 200:
            detail = f"{detail} with HTTP {status_code}"
        if message:
            detail = f"{detail}: {message}"
        super().__init__(detail)


class GitHubSearchError(ValueError):
    """Caller-correctable search parameters (e.g. a stale page_token) -> 422."""


class GitHubApiRequestError(ValueError):
    """A generic-read failure the model can correct by changing its request.

    Bad path shape, a refused cross-origin redirect, the redirect hop cap, or an
    unsupported (binary) content type. The route maps this to 422 — distinct from
    GitHubUpstreamError (transient/upstream -> 502) so the model does not blindly
    retry an unfixable request.
    """


class GitHubClient:
    """Read-only GitHub API client. Only read methods are implemented.

    Read-only is a code-level guarantee, not a token-level one: the brokered
    ``repo`` scope is write-capable (GitHub has no read-only private-repo scope),
    so this class deliberately exposes no method that issues a non-GET request.
    """

    def __init__(
        self,
        *,
        access_token: str,
        api_base_url: str,
        timeout_seconds: float = 10.0,
        allowed_orgs: list[str] | None = None,
    ):
        self.access_token = access_token
        self.api_base_url = api_base_url.rstrip("/")
        # Empty == the token's full visibility. When set, BOTH surfaces are
        # confined to these org logins: the curated search (_build_query) and the
        # generic api_get read (_enforce_api_get_org_scope).
        self.allowed_orgs = allowed_orgs or []
        # Case-folded lookup set, computed once (GitHub logins are case-
        # insensitive); allowed_orgs is kept raw for display in refusal messages.
        self._allowed_orgs_normalized = {org.strip().lower() for org in self.allowed_orgs}
        self.timeout = httpx.Timeout(timeout_seconds)
        # api_get bounds the WHOLE call (validate + ≤3 hops) with this as an
        # overall deadline, so a slow redirect chain can't blow the apollo cap.
        self.timeout_seconds = timeout_seconds

    def _headers(self, accept: str, *, api_version: bool = True) -> dict[str, str]:
        """Auth + Accept header dict, one authority so a future API-version bump
        can't miss a call site. GraphQL omits X-GitHub-Api-Version (api_version=False)."""
        headers = {"Authorization": f"Bearer {self.access_token}", "Accept": accept}
        if api_version:
            headers["X-GitHub-Api-Version"] = "2022-11-28"
        return headers

    async def verify_access(self) -> GitHubAccessCheck:
        """Confirm the token can read, and resolve the connected login.

        ``/user`` validates the token and yields the login; ``/user/repos`` is
        the capability probe that fails meaningfully (401/403) when the token
        cannot enumerate repositories (bad token, or org SSO not authorized).
        """
        response = await self._get_response("/user", operation="auth check")
        await self._get("/user/repos", params={"per_page": "1"}, operation="repo access check")
        try:
            user = response.json()
        except ValueError as exc:
            raise GitHubUpstreamError(
                operation="auth check", status_code=response.status_code, message="invalid JSON"
            ) from exc
        login = str(user.get("login", "") or "") if isinstance(user, dict) else ""
        # Report the token's real grant (X-OAuth-Scopes header), not the configured
        # intention: a stale link silently missing e.g. read:project must show as such.
        scopes = _parse_oauth_scopes(response.headers.get("x-oauth-scopes", ""))
        return GitHubAccessCheck(login=login, scopes=scopes)

    async def search(
        self,
        *,
        query: str,
        limit: int,
        repo: str = "",
        page_token: str = "",
    ) -> tuple[list[GitHubSearchHit], str, bool]:
        fingerprint = _fingerprint(query=query, repo=repo, limit=limit)
        page = _decode_page_token(page_token, fingerprint)
        per_page = min(max(limit, 1), GITHUB_MAX_PER_PAGE)

        try:
            payload = await self._get(
                "/search/issues",
                params={
                    "q": self._build_query(query, repo),
                    "per_page": str(per_page),
                    "page": str(page),
                    "sort": "updated",
                    "order": "desc",
                },
                operation="search",
            )
        except GitHubUpstreamError as exc:
            # GitHub answers 422 when a qualifier is invalid — most often a
            # repo:/org: that doesn't exist or isn't visible. Surface it as a
            # caller-fixable error so the model corrects the repo, not a 502.
            if exc.status_code == 422:
                raise GitHubSearchError(
                    "GitHub rejected the search. Check the query and that any repo/org "
                    "exists and is accessible (use owner/name for repo)."
                ) from exc
            raise
        if not isinstance(payload, dict):
            raise GitHubUpstreamError(operation="search", message="invalid response")
        items = payload.get("items")
        if not isinstance(items, list):
            items = []
        total = int(payload.get("total_count", 0) or 0)
        incomplete = bool(payload.get("incomplete_results", False))
        hits = [_search_hit(item) for item in items if isinstance(item, dict)]
        if self._allowed_orgs_normalized:
            # Belt and braces: the query is already scoped, but drop any hit
            # outside the allowlist so the boundary does not rest on GitHub's
            # query parsing alone. Pagination below still counts raw items.
            kept = [hit for hit in hits if self._owner_allowed(hit.repo.split("/", 1)[0])]
            if len(kept) != len(hits):
                logger.warning("github_search_dropped_out_of_allowlist_hits", extra={"dropped": len(hits) - len(kept)})
            hits = kept

        next_token = ""
        reachable = min(total, GITHUB_SEARCH_RESULT_CAP)
        if len(items) >= per_page and page * per_page < reachable:
            next_token = _encode_page_token(page + 1, fingerprint)
        return hits, next_token, incomplete

    async def read_item(
        self,
        *,
        repo: str,
        number: int,
        max_chars: int,
    ) -> tuple[GitHubItem, str, bool]:
        owner_repo = repo.strip()
        issue = await self._get(f"/repos/{owner_repo}/issues/{number}", operation="item read")
        if not isinstance(issue, dict):
            raise GitHubUpstreamError(operation="item read", message="invalid response")
        user = issue.get("user") or {}
        item = GitHubItem(
            repo=owner_repo,
            number=int(issue.get("number", number) or number),
            title=sanitize_connector_text(str(issue.get("title", "") or "")),
            state=str(issue.get("state", "") or ""),
            is_pull_request="pull_request" in issue,
            author=str(user.get("login", "") or "") if isinstance(user, dict) else "",
            assignees=_logins(issue.get("assignees")),
            labels=_label_names(issue.get("labels")),
            comments=int(issue.get("comments", 0) or 0),
            created_at=issue.get("created_at"),
            updated_at=issue.get("updated_at"),
        )

        # The issues endpoint returns a PR's body and comments but not its
        # requested reviewers or review states; fetch those from the pulls API.
        if item.is_pull_request:
            pull = await self._get(f"/repos/{owner_repo}/pulls/{number}", operation="pull read")
            if isinstance(pull, dict):
                item.requested_reviewers = _logins(pull.get("requested_reviewers"))
                # requested_teams entries always carry a "name", so the label
                # extractor (name-or-str) handles them without a slug fallback.
                item.requested_teams = _label_names(pull.get("requested_teams"))
                item.is_draft = bool(pull.get("draft"))
                item.merge_state = _merge_state(pull)
            pull_reviews = await self._get(
                f"/repos/{owner_repo}/pulls/{number}/reviews",
                params={"per_page": str(_PR_REVIEW_PAGE_SIZE)},
                operation="pull reviews",
            )
            # Keep the newest tail: reviews are oldest-first, so a later APPROVED
            # must not be dropped in favor of an earlier CHANGES_REQUESTED.
            item.reviews = _reviews(pull_reviews)[-MAX_ITEM_COMMENTS:]

        segments: list[str] = []
        body = sanitize_connector_text(str(issue.get("body", "") or ""))
        if body:
            segments.append(body)
        if item.comments:
            # Comments come back oldest-first; the count is in the issue payload,
            # so jump to the last page to return the most recent comments, not the
            # oldest 30 (which is what "recent comments" is supposed to mean).
            last_page = (item.comments + MAX_ITEM_COMMENTS - 1) // MAX_ITEM_COMMENTS
            comments = await self._get(
                f"/repos/{owner_repo}/issues/{number}/comments",
                params={"per_page": str(MAX_ITEM_COMMENTS), "page": str(last_page)},
                operation="item comments",
            )
            if isinstance(comments, list):
                for comment in comments:
                    if not isinstance(comment, dict):
                        continue
                    commenter = comment.get("user") or {}
                    login = str(commenter.get("login", "") or "") if isinstance(commenter, dict) else ""
                    text = sanitize_connector_text(str(comment.get("body", "") or ""))
                    segments.append(f"@{login}: {text}" if login else text)

        full = "\n\n".join(segment for segment in segments if segment)
        truncated = len(full) > max_chars
        return item, _snippet(full, max_chars), truncated

    async def read_file(
        self,
        *,
        repo: str,
        path: str,
        ref: str,
        max_chars: int,
    ) -> GitHubFileContent:
        owner_repo = repo.strip()
        params = {"ref": ref.strip()} if ref.strip() else None
        data = await self._get(
            f"/repos/{owner_repo}/contents/{quote(path.strip(), safe='/')}",
            params=params,
            operation="file read",
        )
        if isinstance(data, list):
            return GitHubFileContent(unsupported_reason="path is a directory, not a file")
        if not isinstance(data, dict):
            raise GitHubUpstreamError(operation="file read", message="invalid response")
        if str(data.get("type", "")) != "file":
            return GitHubFileContent(unsupported_reason=f"path is a {data.get('type', 'non-file')}, not a file")

        size = int(data.get("size", 0) or 0)
        encoding = str(data.get("encoding", "") or "")
        raw_content = data.get("content", "") or ""
        # The contents API omits content (encoding "none") for files over 1 MB.
        if size > CONTENTS_MAX_BYTES or encoding == "none" or (encoding != "base64" and not raw_content):
            return GitHubFileContent(too_large=True)

        try:
            decoded = base64.b64decode(raw_content)
        except (ValueError, TypeError):
            return GitHubFileContent(binary=True)
        if decoded.startswith(_LFS_POINTER_PREFIX):
            return GitHubFileContent(unsupported_reason="file is a git-lfs pointer, not stored inline")
        try:
            text = decoded.decode("utf-8")
        except UnicodeDecodeError:
            return GitHubFileContent(binary=True)

        text = sanitize_connector_text(text)
        truncated = len(text) > max_chars
        return GitHubFileContent(content=_snippet(text, max_chars), truncated=truncated)

    async def list_projects(self, *, owner: str, first: int = 20) -> list[GitHubProject]:
        data = await self._graphql(
            _PROJECTS_LIST_QUERY, {"login": owner.strip(), "first": first}, operation="project list"
        )
        node = _owner_node(data)
        if node is None:
            raise GitHubUpstreamError(operation="project list", status_code=404, message="owner not found")
        nodes = ((node.get("projectsV2") or {}).get("nodes")) or []
        return [_project(p) for p in nodes if isinstance(p, dict)]

    async def read_project(
        self,
        *,
        owner: str,
        number: int,
        max_items: int,
    ) -> tuple[GitHubProject, list[GitHubProjectItem]]:
        """Read a board's items, auto-paginating server-side up to a bounded cap
        so the caller gets the whole board in one call (GitHub caps a GraphQL
        page at 100). project.items_count is the board's real size; the router
        marks the response truncated when we returned fewer (board > cap)."""
        cap = min(max(max_items, 1), _MAX_PROJECT_ITEMS)
        login = owner.strip()
        project: dict | None = None
        collected: list[GitHubProjectItem] = []
        cursor: str | None = None
        for _page in range(_MAX_PROJECT_PAGES):
            data = await self._graphql(
                _PROJECT_READ_QUERY,
                {"login": login, "number": number, "first": _PROJECT_PAGE_SIZE, "after": cursor},
                operation="project read",
            )
            project = _owner_project(
                data, operation="project read", status_code=404, message="project not found"
            )
            items_field = project.get("items") or {}
            for item in (items_field.get("nodes") or []):
                if isinstance(item, dict):
                    collected.append(_project_item(item))
            if len(collected) >= cap:
                break
            page_info = items_field.get("pageInfo") or {}
            if page_info.get("hasNextPage") and page_info.get("endCursor"):
                cursor = str(page_info["endCursor"])
            else:
                break
        return _project(project or {}), collected[:cap]

    async def read_project_with_counts(
        self, *, owner: str, number: int, max_items: int
    ) -> tuple[GitHubProject, list[GitHubProjectItem], GitHubProjectCounts]:
        """Read a board and compute its aggregate counts concurrently.

        The exact server-side count queries don't depend on the item
        enumeration, so they run alongside read_project instead of strictly
        after it -- overlapping the two count round-trips with the 1-5
        enumeration pages rather than paying them in series.

        Counts never break the read: on any count failure (rejected filter DSL,
        non-reconciling self-check, upstream/network error, or a coding bug) we
        fall back to bucketing the enumerated items, flagged non-authoritative.
        A read_project failure, by contrast, propagates -- it's the caller's
        error to map -- after the counts task is cancelled so it can't leak."""
        counts_task = asyncio.ensure_future(
            self._authoritative_counts(login=owner.strip(), number=number)
        )
        try:
            project, items = await self.read_project(owner=owner, number=number, max_items=max_items)
        except BaseException:
            counts_task.cancel()
            with contextlib.suppress(BaseException):
                await counts_task
            raise
        total = project.items_count
        try:
            counts = await counts_task
        except (GitHubUpstreamError, httpx.HTTPError):
            # Expected degrade: upstream/network failure, a rejected filter DSL,
            # or a self-check that refused to stamp non-reconciling counts
            # authoritative (see _authoritative_counts). Sample at WARNING.
            logger.warning("github_project_counts_fallback", exc_info=True)
            counts = _sampled_counts(total=total, items=items)
        except Exception:
            # Unexpected: a coding bug (bad query edit, upstream shape change,
            # TypeError in parsing) would otherwise become permanent silent
            # degradation -- and a small fully-enumerated board still returns
            # authoritative=True from the fallback, masking it. Log at ERROR so a
            # shipped regression is loud, but still honor "must not break the
            # read" (asyncio.CancelledError is a BaseException and still surfaces).
            logger.error("github_project_counts_unexpected_error", exc_info=True)
            counts = _sampled_counts(total=total, items=items)
        return project, items, counts

    async def _authoritative_counts(self, *, login: str, number: int) -> GitHubProjectCounts:
        data = await self._graphql(
            _PROJECT_COUNTS_QUERY, {"login": login, "number": number}, operation="project counts"
        )
        # Existence was already confirmed by read_project moments earlier in the
        # same request, so a null projectV2 here is a partial GraphQL error on
        # this specific query (rate limit, transient upstream issue, ...), not
        # evidence the project doesn't exist -- no status_code, matching the
        # "invalid response" convention for an unexpected shape (vs. a real HTTP
        # error). project_counts turns this into the sampled fallback.
        project = _owner_project(
            data, operation="project counts", status_code=None, message="project data missing from response"
        )

        def _tc(key: str) -> int:
            return int((project.get(key) or {}).get("totalCount", 0) or 0)

        total = _tc("total")
        # is:draft matches BOTH board drafts and draft PRs, and a board draft also
        # matches is:issue, so the raw filters overlap. board_draft (is:draft
        # is:issue) isolates board drafts; subtracting it from is:issue leaves
        # real issues, draft PRs stay under is:pr, and the three buckets then
        # partition the total exactly -- redacted absorbs items matching none.
        board_draft = _tc("board_draft")
        issue = _tc("issue") - board_draft
        pull_request = _tc("pull_request")
        redacted = total - (issue + pull_request + board_draft)
        status_field = project.get("statusField") or {}
        options = [
            str((o or {}).get("name", "") or "")
            for o in (status_field.get("options") or [])
            if isinstance(o, dict)
        ]
        options = [o for o in options if o]
        by_status = await self._status_column_counts(login=login, number=number, options=options)
        no_status = _tc("no_status")
        # Verify the invariant before advertising authoritative=True. Two disclosed
        # edges can silently miscount while the query still "succeeds": a Status
        # option whose name contains `"` (see _project_filter_value), and a board
        # whose single-select isn't named "Status" (statusField -> null, so
        # by_status is empty while no:status counts something unrelated). If the
        # buckets don't reconcile to the total, refuse the authoritative stamp --
        # raise so project_counts degrades to the honest sampled fallback.
        if sum(c.count for c in by_status) + no_status != total or issue < 0 or redacted < 0:
            raise GitHubUpstreamError(
                operation="project counts",
                message="server-side counts did not reconcile to the board total",
            )
        return GitHubProjectCounts(
            total=total,
            archived_policy="excluded",
            by_status=by_status,
            no_status=no_status,
            by_type={
                "issue": issue,
                "pull_request": pull_request,
                "draft": board_draft,
                # Derived: items matching none of the type filters (redacted content).
                "redacted": redacted,
            },
            by_state={"open": _tc("opened"), "closed": _tc("closed")},
            authoritative=True,
            counted_items=total,
        )

    async def _status_column_counts(
        self, *, login: str, number: int, options: list[str]
    ) -> list[GitHubStatusCount]:
        if not options:
            return []
        variables: dict[str, str | int] = {"login": login, "number": number}
        for index, option in enumerate(options):
            variables[f"q{index}"] = f'status:"{_project_filter_value(option)}"'
        data = await self._graphql(
            _status_counts_query(len(options)), variables, operation="project status counts"
        )
        # Same reasoning as _authoritative_counts: existence was already confirmed
        # by read_project, so a null projectV2 here is a partial GraphQL error on
        # this specific query, not a real not-found -- no status_code, so the
        # fallback's logged exception doesn't read as "HTTP 404" during, say, a
        # rate-limit incident.
        project = _owner_project(
            data,
            operation="project status counts",
            status_code=None,
            message="project data missing from response",
        )
        return [
            GitHubStatusCount(
                name=sanitize_connector_text(option),
                count=int((project.get(f"s{index}") or {}).get("totalCount", 0) or 0),
            )
            for index, option in enumerate(options)
        ]

    async def list_repos(self, *, owner: str, first: int = 30) -> list[GitHubRepo]:
        """List repos for an org or user (whichever the owner is), most recently
        updated first — so an agent can resolve a name to an actual repo instead
        of guessing owner/name."""
        owner = owner.strip()
        params = {"per_page": str(first), "sort": "updated", "direction": "desc"}
        try:
            data = await self._get(f"/orgs/{owner}/repos", params=params, operation="repo list")
        except GitHubUpstreamError as exc:
            if exc.status_code == 404:  # not an org (or not visible as one) — try the user
                data = await self._get(f"/users/{owner}/repos", params=params, operation="repo list")
            else:
                raise
        if not isinstance(data, list):
            return []
        return [_repo(node) for node in data if isinstance(node, dict)]

    async def _graphql(self, query: str, variables: dict, *, operation: str) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                self.api_base_url + "/graphql",
                headers=self._headers("application/vnd.github+json", api_version=False),
                json={"query": query, "variables": variables},
            )
        _raise_for_github_status(response, operation=operation)
        try:
            payload = response.json()
        except ValueError as exc:
            raise GitHubUpstreamError(
                operation=operation, status_code=response.status_code, message="invalid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise GitHubUpstreamError(operation=operation, message="invalid response")
        data = payload.get("data")
        # GraphQL returns HTTP 200 with a top-level errors[] for auth-scope and
        # other failures; surface only the structured message, never a raw body.
        if data is None:
            message = ""
            errors = payload.get("errors")
            if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                message = str(errors[0].get("message", "") or "")[:240]
            raise GitHubUpstreamError(operation=operation, message=message or "GraphQL query failed")
        return data if isinstance(data, dict) else {}

    def _build_query(self, query: str, repo: str) -> str:
        query = query.strip()
        repo = repo.strip()
        if repo and not _SEARCH_REPO_RE.fullmatch(repo):
            raise GitHubSearchError("repo must be of the form 'owner/name'")
        if self._allowed_orgs_normalized:
            # With an allowlist the hub owns the search scope: caller text must
            # not carry its own org:/user:/owner:/repo: qualifier, which GitHub
            # would OR with ours. This also refuses a quoted phrase containing
            # such a token -- an acceptable cost at a security boundary.
            if _SCOPE_QUALIFIER_RE.search(query):
                raise GitHubSearchError(
                    "the search query may not contain org:, user:, owner: or repo: "
                    "qualifiers on this hub; use the repo field to narrow the search"
                )
            # An explicit repo is narrower than the allowlist, but it is still
            # caller-supplied, so its owner must itself be in the allowlist.
            if repo and not self._owner_allowed(repo.split("/", 1)[0]):
                raise GitHubSearchError(
                    f"repo {repo!r} is outside the configured GitHub org allowlist "
                    f"({', '.join(self.allowed_orgs)})"
                )
        parts = [query]
        if repo:
            parts.append(f"repo:{repo}")
        else:
            # Repeating ``org:`` qualifiers ORs them in GitHub issue search, so
            # results stay confined to the configured org allowlist.
            parts.extend(f"org:{org}" for org in self.allowed_orgs)
        return " ".join(part for part in parts if part)

    def _owner_allowed(self, owner: str) -> bool:
        """Whether ``owner`` is in the allowlist (GitHub logins fold case)."""
        return owner.strip().lower() in self._allowed_orgs_normalized

    async def _get_response(
        self, path: str, *, operation: str, params: dict[str, str] | None = None
    ) -> httpx.Response:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(
                self.api_base_url + path,
                headers=self._headers("application/vnd.github+json"),
                params=params,
            )
        _raise_for_github_status(response, operation=operation)
        return response

    async def _get(self, path: str, *, operation: str, params: dict[str, str] | None = None) -> Any:
        response = await self._get_response(path, operation=operation, params=params)
        try:
            return response.json()
        except ValueError as exc:
            raise GitHubUpstreamError(
                operation=operation, status_code=response.status_code, message="invalid JSON"
            ) from exc

    async def api_get(
        self,
        *,
        path: str,
        params: dict[str, Any] | None = None,
        media_type: str = "json",
        max_chars: int | None = None,
        timeout_seconds: float | None = None,
    ) -> GitHubApiGetResponse:
        """GET an arbitrary GitHub REST endpoint: origin-locked, size-capped, sanitized.

        The verb is hardcoded GET and Accept comes from a fixed table, so neither
        is model-controllable (GraphQL, which is POST-only, is excluded by that
        verb lock, not by any denylist). The host cannot be escaped: httpx resolves
        ``base + path`` within the base authority, and redirects are followed only
        back to the same origin. The body is streamed with a hard byte cap so an
        unbounded upstream (e.g. ``/git/trees?recursive=1``) can't exhaust memory.
        """
        if media_type not in _ACCEPT_BY_MEDIA:
            raise GitHubApiRequestError(f"unsupported media_type {media_type!r}")
        self._validate_api_path(path)
        self._enforce_api_get_org_scope(path)
        base = httpx.URL(self.api_base_url)
        try:
            start = httpx.URL(self.api_base_url + path)
        except httpx.InvalidURL as exc:
            # A malformed path that slips past the string rules must degrade to a
            # clean 422 refusal, not an uncaught httpx.InvalidURL -> raw 500.
            raise GitHubApiRequestError("path is not a valid GitHub API URL") from exc
        # Re-check the CONSTRUCTED URL's authority: a malformed base+path could in
        # principle join to a different scheme/host, so verify both are unchanged.
        # (httpx normalizes any ".." dot-segments before ``.path`` is readable, so
        # the segment test below is belt-and-suspenders — the load-bearing
        # traversal guard is _validate_api_path on the raw string, not this.)
        if start.host != base.host or start.scheme != base.scheme or ".." in start.path.split("/"):
            raise GitHubApiRequestError("path resolves outside the GitHub API base")

        resolved_max = max_chars
        if resolved_max is None:
            resolved_max = (
                _API_GET_JSON_DEFAULT_MAX_CHARS
                if media_type == "json"
                else _API_GET_TEXT_DEFAULT_MAX_CHARS
            )
        # Defensive backstop: the route's pydantic bound already enforces the
        # range, but api_get must not trust an out-of-range max_chars from any
        # caller (direct callers, tests) — clamp to [1, ceiling].
        resolved_max = max(1, min(resolved_max, _API_GET_MAX_CHARS_CEILING))
        headers = self._headers(_ACCEPT_BY_MEDIA[media_type])
        coerced = {key: coerce_github_param_value(value) for key, value in (params or {}).items()}
        current = start.copy_merge_params(coerced) if coerced else start

        # A caller that already spent part of a shared budget (e.g. the route's
        # concurrency-permit wait) can hand the read only the time left, so the total
        # stays bounded to one budget instead of ~2x. Defensive backstop (mirrors the
        # max_chars clamp above): don't trust an out-of-range override from a direct
        # caller — a non-positive value is ignored (falls back to the client deadline
        # rather than reaching asyncio.timeout/httpx.Timeout), and the override can
        # never exceed the client-wide deadline. The route already passes a floored,
        # in-range value.
        deadline = self.timeout_seconds
        if timeout_seconds is not None and timeout_seconds > 0:
            deadline = min(timeout_seconds, self.timeout_seconds)
        try:
            async with asyncio.timeout(deadline):
                async with httpx.AsyncClient(timeout=httpx.Timeout(deadline)) as client:
                    for _hop in range(_API_GET_MAX_REDIRECTS + 1):
                        async with client.stream("GET", current, headers=headers) as response:
                            if response.is_redirect:
                                current = self._resolve_api_redirect(
                                    current, response.headers.get("location", "")
                                )
                                # Re-apply the org allowlist to the redirect TARGET.
                                # _resolve_api_redirect checks only origin, so a
                                # same-origin 301 (a repo renamed or transferred to a
                                # new owner) could otherwise land on an owner the
                                # initial-path check at line 790 never saw.
                                self._enforce_api_get_org_scope(self._base_relative_path(current))
                                continue
                            return await self._read_api_result(response, media_type, resolved_max)
        except TimeoutError as exc:
            raise GitHubUpstreamError(operation="api get", message="request timed out") from exc
        # Loop exhausted: every off-origin hop already raised inside
        # _resolve_api_redirect, so reaching here means too many SAME-origin hops.
        raise GitHubApiRequestError(_API_GET_TOO_MANY_REDIRECTS)

    def _validate_api_path(self, path: str) -> None:
        """Reject anything that isn't a plain, on-host GitHub API path (raw string)."""
        if not path or not path.startswith("/"):
            raise GitHubApiRequestError("path must start with '/'")
        if len(path) > 500:
            raise GitHubApiRequestError("path must be at most 500 characters")
        if path.startswith("//"):
            raise GitHubApiRequestError("path must not start with '//'")
        if "?" in path or "#" in path:
            raise GitHubApiRequestError("path must not contain '?' or '#' — pass query args via params")
        if "%" in path:
            # Percent-encoding buys nothing on this API and closes the encoded-bypass class.
            raise GitHubApiRequestError("percent-encoding is not allowed in path")
        if any(ch.isspace() for ch in path) or has_control_or_nonprintable(path):
            # Whitespace plus the shared control/DEL/zero-width/non-printable class
            # that would otherwise reach httpx.URL and 500 the request.
            raise GitHubApiRequestError(
                "path must not contain whitespace, control, or non-printable characters"
            )
        if ".." in path.split("/"):
            raise GitHubApiRequestError("path must not contain a '..' segment")
        # Courtesy only (NOT the exclusion mechanism — GraphQL is excluded by the
        # GET-only verb lock): return a clearer error than upstream would, and cut
        # the noise of harmless GET schema-introspection.
        normalized = path.rstrip("/").lower()
        if normalized == "/graphql" or normalized.startswith("/graphql/"):
            raise GitHubApiRequestError("GraphQL is not available through this read tool (REST GET only)")

    def _enforce_api_get_org_scope(self, path: str) -> None:
        """Confine the generic read to the configured org allowlist.

        api_get never touches the curated search's ``_build_query``, so without
        this an injected agent could reach ``/search/*``, the ``/user/*``
        self-endpoints, or ``/repos/{any-owner}/...`` at the token's FULL
        visibility -- walking around the very allowlist the curated search
        enforces (issue #64). An empty allowlist == the token's full visibility;
        a nonempty one admits ONLY an owner-qualified collection
        (:data:`_API_GET_OWNER_SCOPED_COLLECTIONS`) under an allowed owner.
        """
        if not self._allowed_orgs_normalized:
            return
        # _validate_api_path already guaranteed a leading "/", no "//", and no
        # ".." segment, so a simple split yields clean, non-empty segments.
        segments = [segment for segment in path.split("/") if segment]
        owner_scoped = len(segments) >= 2 and segments[0] in _API_GET_OWNER_SCOPED_COLLECTIONS
        if owner_scoped and segments[1].lower() in self._allowed_orgs_normalized:
            return
        allowlist = ", ".join(self.allowed_orgs)
        if owner_scoped:
            raise GitHubApiRequestError(
                f"path targets {segments[1]!r}, which is outside the configured "
                f"GitHub org allowlist ({allowlist})"
            )
        raise GitHubApiRequestError(
            "this path is not scoped to an allowed org — the configured allowlist "
            f"({allowlist}) permits only /repos, /orgs, and /users paths under those owners"
        )

    def _base_relative_path(self, url: httpx.URL) -> str:
        """Strip the API base's own path prefix (e.g. a GHE/proxy ``/api``) off a
        same-origin URL, yielding the leading-``/`` path the request-time
        validators expect. For the default ``https://api.github.com`` base (no
        prefix) the path is returned unchanged."""
        prefix = httpx.URL(self.api_base_url).path.rstrip("/")
        path = url.path
        if prefix and path.startswith(prefix):
            path = path[len(prefix):]
        return path if path.startswith("/") else "/" + path

    def _resolve_api_redirect(self, current: httpx.URL, location: str) -> httpx.URL:
        """Follow a redirect ONLY back to the same origin; refuse anything else.

        The target is resolved through httpx (never a string prefix match), then
        its full origin is compared: https, same host, same port, no userinfo. A
        renamed-repo 301 stays on the API host and is followed; a codeload/storage
        hop, an https->http downgrade, or a ``host@evil`` userinfo spoof is refused.
        """
        if not location:
            raise GitHubApiRequestError(_API_GET_REDIRECT_REFUSAL)
        base = httpx.URL(self.api_base_url)
        try:
            target = current.join(location)
        except httpx.InvalidURL as exc:
            # httpx.InvalidURL is NOT an httpx.HTTPError, so a malformed upstream
            # Location (e.g. "https://[bad") would slip past the caller's
            # except-HTTPError and surface as a raw 500. Degrade to the same
            # refusal the initial-URL guard uses.
            raise GitHubApiRequestError(_API_GET_REDIRECT_REFUSAL) from exc
        # https-only by policy: a redirect target must stay on the https origin.
        # NOTE this is intentionally not `base.scheme` — a hub configured with an
        # http proxy base therefore can't follow ANY redirect (incl. the renamed-repo
        # 301). That's the deliberate side: don't "fix" it to `base.scheme`, which
        # would let an http->https same-host hop pass since both ports normalize to
        # None. http bases simply don't support redirects.
        if (
            target.scheme != "https"
            or target.host != base.host
            or target.port != base.port
            or target.userinfo
        ):
            raise GitHubApiRequestError(_API_GET_REDIRECT_REFUSAL)
        return target

    async def _read_api_result(
        self, response: httpx.Response, media_type: str, max_chars: int
    ) -> GitHubApiGetResponse:
        status_code = response.status_code
        # Parse the Content-Type PARAMETER off: live GitHub always appends
        # "; charset=utf-8", so bare-string equality would misroute every real
        # response to the binary-refusal branch while clean-header mocks pass.
        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        has_more = "next" in response.links

        if status_code >= 400:
            # Cap the error-body read too — it was the one remaining uncapped read.
            # api_base_url is configurable, so a misbehaving GHE/proxy could stream
            # an unbounded error page; a few KB is plenty for the structured message.
            error_body, _ = await read_capped(response, 65_536)
            if status_code == 406 and media_type in ("diff", "patch"):
                # Surface GitHub's own message and append our locally-owned recovery
                # guidance, rather than restating GitHub's (drift-prone) numeric
                # limits ourselves — this code can't observe if they change.
                upstream = _github_error_message(response, body=error_body)
                message = f"{upstream}: {_API_GET_406_GUIDANCE}" if upstream else _API_GET_406_GUIDANCE
                raise GitHubApiRequestError(message)
            _raise_for_github_status(response, operation="api get", body=error_body)

        if status_code in (202, 204):
            # 202: repo-statistics endpoints return empty body while GitHub
            # computes; that is a retry signal, not an error. 204: several GETs
            # answer no-body-means-yes (e.g. collaborator/following membership
            # checks, vulnerability-alerts) with no Content-Type at all, which
            # would otherwise fall through to the binary-refusal branch below.
            return GitHubApiGetResponse(
                body=None, body_text="", truncated=False, has_more=has_more,
                content_type=content_type, status=status_code,
            )

        raw, byte_truncated = await read_capped(response, max_chars * 4 + 1)

        if _is_json_content(content_type):
            return self._json_result(raw, byte_truncated, has_more, content_type, status_code, max_chars)
        if _is_text_content(content_type):
            return self._text_result(raw, byte_truncated, has_more, content_type, status_code, max_chars)
        raise GitHubApiRequestError(
            f"unsupported content type {content_type or 'unknown'!r} — this read tool returns "
            "JSON, diff, or patch text only, not binary or archive payloads"
        )

    def _json_result(
        self,
        raw: bytes,
        byte_truncated: bool,
        has_more: bool,
        content_type: str,
        status: int,
        max_chars: int,
    ) -> GitHubApiGetResponse:
        if not raw:
            # A literal empty 200 body (e.g. Content-Length: 0) is a valid empty
            # result, not a truncation failure -- distinguish it from the
            # size-cap error below, which would otherwise send a caller looking
            # at max_chars for a body that was simply never there.
            return GitHubApiGetResponse(
                body=None, body_text="", truncated=False, has_more=has_more,
                content_type=content_type, status=status,
            )
        text = raw.decode("utf-8", errors="replace")
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            # Overflowed the byte cap mid-object (or otherwise invalid): keep the
            # readable prefix as text, or refuse if nothing parseable survives.
            # Sanitize it like any other untrusted body — an unparseable JSON
            # prefix still carries link-shaped values that must not reach the
            # renderer unmasked (the parsed and diff paths both sanitize).
            prefix = sanitize_github_api_text(text)[:max_chars]
            if not prefix.strip():
                # Only claim the size cap fired when it actually did; otherwise the
                # body was simply invalid (proxy-mangled / whitespace) and "shrink
                # per_page" would send the model chasing an uncorrectable error.
                message = _API_GET_JSON_ERROR if byte_truncated else _API_GET_JSON_INVALID
                raise GitHubApiRequestError(message) from exc
            return GitHubApiGetResponse(
                body=None, body_text=prefix, truncated=True, has_more=has_more,
                content_type=content_type, status=status,
            )
        sanitized = self._sanitize_json_values(parsed)
        # Judge truncation on the COMPACT size of the PARSED object — not the raw
        # decoded text (which counts incidental upstream whitespace) and not the
        # sanitized serialization (which value masking inflates: a@b.co -> a [at] b
        # [dot] co). Both would flag a body that actually fit as truncated and strip
        # its structure for content that was already complete. Computed once here
        # purely for the size decision; the returned body is still the sanitized
        # object. read_capped hard-capped the input to max_chars*4+1 bytes, so this
        # serialize stays bounded.
        compact = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
        if not byte_truncated and len(compact) <= max_chars:
            return GitHubApiGetResponse(
                body=sanitized, body_text="", truncated=False, has_more=has_more,
                content_type=content_type, status=status,
            )
        # Genuinely over budget (or byte-truncated): no complete structure worth
        # returning, so serialize the SANITIZED value to a masked text prefix and
        # flag it.
        serialized = json.dumps(sanitized, ensure_ascii=False, separators=(",", ":"))
        return GitHubApiGetResponse(
            body=None, body_text=serialized[:max_chars], truncated=True, has_more=has_more,
            content_type=content_type, status=status,
        )

    def _text_result(
        self,
        raw: bytes,
        byte_truncated: bool,
        has_more: bool,
        content_type: str,
        status: int,
        max_chars: int,
    ) -> GitHubApiGetResponse:
        decoded = raw.decode("utf-8", errors="replace")
        sanitized = sanitize_github_api_text(decoded)
        # Judge truncation by the DECODED length, not the sanitized one: link/email
        # masking inflates length (a@b.co -> a [at] b [dot] co), so a body that fit
        # raw must not be flagged truncated — that would set has_more and send the
        # model paging for content it already has in full. When nothing was actually
        # cut, return the masked text whole (the expansion is our transform, not data).
        truncated = byte_truncated or len(decoded) > max_chars
        if not truncated:
            return GitHubApiGetResponse(
                body=None, body_text=sanitized, truncated=False, has_more=has_more,
                content_type=content_type, status=status,
            )
        body_text = sanitized[:max_chars]
        cut = body_text.rfind("\n")
        # Only shed a SHORT trailing partial line. rfind finds the last newline
        # anywhere, so on a single-long-line diff (a minified file) it sits near the
        # start — trimming to it would silently drop most of the budget with no offset
        # to recover. Keep the partial line when the shed tail exceeds the threshold,
        # and when there's no newline at all (cut == -1, keep the prefix — without this
        # guard a short no-newline body would drop its last character).
        shed_ceiling = min(_API_GET_MAX_PARTIAL_LINE_TRIM, max(1, max_chars // _API_GET_PARTIAL_LINE_TRIM_DIVISOR))
        if cut != -1 and len(body_text) - cut <= shed_ceiling:
            body_text = body_text[:cut]
        return GitHubApiGetResponse(
            body=None, body_text=body_text, truncated=truncated, has_more=has_more,
            content_type=content_type, status=status,
        )

    def _sanitize_json_values(self, value: Any, depth: int = 0) -> Any:
        """Sanitize string keys and values recursively; non-string scalars pass through.

        Keys are sanitized too: ``github_api_get`` reaches arbitrary paths, and some
        (e.g. the Gists API) key objects by attacker-chosen strings (``files`` is
        keyed by filename), so a key is as untrusted a channel as a value. Strings
        are always masked, even past the depth guard; only nested containers are
        dropped there, since unbounded depth can only arrive through them.
        """
        if isinstance(value, str):
            return sanitize_github_api_text(value)
        if isinstance(value, list):
            if depth > _API_GET_DEPTH_GUARD:
                return None
            return [self._sanitize_json_values(item, depth + 1) for item in value]
        if isinstance(value, dict):
            if depth > _API_GET_DEPTH_GUARD:
                return None
            sanitized: dict[Any, Any] = {}
            for key, item in value.items():
                safe_key = sanitize_github_api_text(key) if isinstance(key, str) else key
                # Two distinct keys can mask to the same string (e.g. two gist
                # filenames that both reduce to "[link]"); disambiguate with a
                # suffix instead of silently dropping the earlier entry.
                if safe_key in sanitized:
                    suffix = 2
                    while f"{safe_key} ({suffix})" in sanitized:
                        suffix += 1
                    safe_key = f"{safe_key} ({suffix})"
                sanitized[safe_key] = self._sanitize_json_values(item, depth + 1)
            return sanitized
        return value


def _is_json_content(content_type: str) -> bool:
    return (
        content_type == "application/json"
        or content_type.endswith("+json")
        or (content_type.startswith("application/vnd.github") and content_type.endswith("json"))
    )


def _is_text_content(content_type: str) -> bool:
    return content_type in ("application/vnd.github.diff", "application/vnd.github.patch") or (
        content_type.startswith("text/")
    )


def _snippet(text: str, limit: int) -> str:
    """Truncate to `limit` chars on an overflow, trimming trailing space and
    marking the cut with an ellipsis; return the text unchanged when it fits."""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _parse_oauth_scopes(header: str) -> list[str]:
    # GitHub returns the token's granted scopes as a comma-separated X-OAuth-Scopes header.
    return [scope.strip() for scope in header.split(",") if scope.strip()]


def _raise_for_github_status(response: httpx.Response, *, operation: str, body: bytes | None = None) -> None:
    # `body` lets a streamed caller (api_get) pass the already-read, size-capped
    # bytes so the message is parsed from those rather than a second full read.
    if response.status_code < 400:
        return
    retry_after = response.headers.get("retry-after", "")
    rate_limited = response.status_code == 429 or (
        response.status_code == 403
        # remaining:0 is the primary limit; a bare Retry-After on a 403 is the
        # secondary limit, which can carry remaining>0 and would otherwise escape
        # to 502 and drop the backoff hint. We deliberately do NOT match GitHub's
        # "secondary rate limit" prose — that copy changes without notice.
        and (response.headers.get("x-ratelimit-remaining") == "0" or retry_after)
    )
    if rate_limited:
        # Normalize primary and secondary rate limits to 429 so the router maps
        # them consistently and can echo Retry-After.
        message = "GitHub rate limit exceeded"
        # Retry-After is legally an HTTP-date OR delta-seconds (RFC 9110). Only
        # fold it into the model-visible prose when it's plain integer seconds, so
        # we never emit "retry after <date> seconds". The raw header still rides
        # the retry_after field for the response header either way. Both apollo
        # tool paths surface only the detail string, so the hint must live here.
        if retry_after.strip().isdigit():
            message = f"{message}; retry after {retry_after.strip()} seconds"
        # A 403 can be a permanent WAF/abuse block that merely carries Retry-After
        # (api_base_url is configurable for GHE/proxied hubs). Keep GitHub's own
        # structured reason instead of discarding it behind the generic 429 text.
        detail = _github_error_message(response, body=body)
        if detail:
            message = f"{message}: {detail}"
        raise GitHubUpstreamError(
            operation=operation,
            status_code=429,
            message=message,
            retry_after=retry_after,
        )
    raise GitHubUpstreamError(
        operation=operation,
        status_code=response.status_code,
        message=_github_error_message(response, body=body),
        retry_after=retry_after,
    )


def _github_error_message(response: httpx.Response, *, body: bytes | None = None) -> str:
    """Return only GitHub's structured error message, never a raw body.

    A non-JSON body (e.g. a proxy's HTML error page) yields ``""`` so infra
    detail cannot leak into a 502. ``body`` lets a streamed caller pass the
    already-read, size-capped bytes instead of a second ``response.json()`` read.
    """
    try:
        payload = json.loads(body) if body is not None else response.json()
    except ValueError:
        return ""
    if isinstance(payload, dict):
        message = payload.get("message")
        if isinstance(message, str):
            # A 4xx message reflects request input, so an attacker-shaped link or
            # domain could otherwise reach the model unmasked via the error channel.
            # Use the STRICT sanitizer (bare domains masked too): this is the shared
            # classifier, so curated-tool errors flow through here and must stay as
            # bare-domain-free as the rest of the curated surface. These are short
            # GitHub status strings, not code, so nothing is lost by masking.
            return sanitize_connector_text(message[:240]).strip()
    return ""


def _fingerprint(*, query: str, repo: str, limit: int) -> str:
    raw = f"{query}\x1f{repo}\x1f{limit}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def _encode_page_token(page: int, fingerprint: str) -> str:
    return f"{page}.{fingerprint}"


def _decode_page_token(page_token: str, fingerprint: str) -> int:
    if not page_token:
        return 1
    page_str, _, token_fingerprint = page_token.partition(".")
    if not token_fingerprint or token_fingerprint != fingerprint:
        raise GitHubSearchError(
            "page_token does not match the current search parameters; restart from the first page"
        )
    try:
        page = int(page_str)
    except ValueError as exc:
        raise GitHubSearchError("page_token is malformed") from exc
    if page < 1:
        raise GitHubSearchError("page_token is malformed")
    return page


def _repo_from_repository_url(repository_url: str) -> str:
    marker = "/repos/"
    index = repository_url.find(marker)
    if index == -1:
        return ""
    return repository_url[index + len(marker):].strip("/")


def _owner_node(data: dict) -> dict | None:
    """Return the organization or user node, whichever the GraphQL query resolved."""
    for key in ("organization", "user"):
        node = data.get(key)
        if isinstance(node, dict):
            return node
    return None


def _owner_project(data: dict, *, operation: str, status_code: int | None, message: str) -> dict:
    """Resolve the projectV2 node from an org-or-user GraphQL response, raising a
    GitHubUpstreamError with the caller's message/status when it is absent."""
    node = _owner_node(data)
    project = node.get("projectV2") if node else None
    if not isinstance(project, dict):
        raise GitHubUpstreamError(operation=operation, status_code=status_code, message=message)
    return project


def _repo(node: dict) -> GitHubRepo:
    return GitHubRepo(
        full_name=str(node.get("full_name", "") or ""),
        description=sanitize_connector_text(str(node.get("description", "") or "")),
        open_issues=int(node.get("open_issues_count", 0) or 0),
        private=bool(node.get("private", False)),
        archived=bool(node.get("archived", False)),
        updated_at=node.get("updated_at"),
    )


def _project(node: dict) -> GitHubProject:
    return GitHubProject(
        number=int(node.get("number", 0) or 0),
        title=sanitize_connector_text(str(node.get("title", "") or "")),
        description=sanitize_connector_text(str(node.get("shortDescription", "") or "")),
        closed=bool(node.get("closed", False)),
        items_count=int((node.get("items") or {}).get("totalCount", 0) or 0),
    )


def _project_item(node: dict) -> GitHubProjectItem:
    content = node.get("content") if isinstance(node.get("content"), dict) else {}
    repository = content.get("repository") or {}
    repo = str(repository.get("nameWithOwner", "") or "") if isinstance(repository, dict) else ""
    status = ""
    fields: dict[str, str] = {}
    for value in ((node.get("fieldValues") or {}).get("nodes")) or []:
        if not isinstance(value, dict):
            continue
        field = value.get("field") if isinstance(value.get("field"), dict) else {}
        field_name = str(field.get("name", "") or "")
        raw = value.get("name")
        if raw is None:
            raw = value.get("text")
        if raw is None:
            raw = value.get("date")
        if raw is None and value.get("number") is not None:
            raw = value.get("number")
        if not field_name or raw in (None, ""):
            continue
        clean_name = sanitize_connector_text(field_name)
        clean_value = sanitize_connector_text(str(raw))
        if clean_name.lower() == "status":
            status = clean_value
        else:
            fields[clean_name] = clean_value
    return GitHubProjectItem(
        title=sanitize_connector_text(str(content.get("title", "") or "")),
        type=str(node.get("type", "") or ""),
        status=status,
        state=str(content.get("state", "") or "").lower(),
        repo=repo,
        number=int(content.get("number", 0) or 0),
        assignees=_logins((content.get("assignees") or {}).get("nodes")),
        labels=_label_names((content.get("labels") or {}).get("nodes")),
        fields=fields,
    )


_PROJECT_TYPE_BUCKETS = {
    "ISSUE": "issue",
    "PULL_REQUEST": "pull_request",
    "DRAFT_ISSUE": "draft",
    "REDACTED": "redacted",
}


def _sampled_counts(*, total: int, items: list[GitHubProjectItem]) -> GitHubProjectCounts:
    """Bucket the already-enumerated items when server-side counts are
    unavailable. Exact only when the whole board was enumerated
    (counted == total); otherwise flagged non-authoritative so a caller never
    reads a truncated sample as a full count."""
    by_status: dict[str, int] = {}
    order: list[str] = []
    no_status = 0
    by_type = {"issue": 0, "pull_request": 0, "draft": 0, "redacted": 0}
    by_state = {"open": 0, "closed": 0}
    for item in items:
        bucket = _PROJECT_TYPE_BUCKETS.get(item.type)
        if bucket:
            by_type[bucket] += 1
        if item.status:
            if item.status not in by_status:
                by_status[item.status] = 0
                order.append(item.status)
            by_status[item.status] += 1
        else:
            no_status += 1
        if item.state == "merged":
            # A merged PR is closed; the authoritative path counts it under
            # is:closed, so sample the same way rather than dropping it.
            by_state["closed"] += 1
        elif item.state in ("open", "closed"):
            by_state[item.state] += 1
    counted = len(items)
    return GitHubProjectCounts(
        total=total,
        archived_policy="excluded",
        by_status=[GitHubStatusCount(name=name, count=by_status[name]) for name in order],
        no_status=no_status,
        by_type=by_type,
        by_state=by_state,
        authoritative=counted == total,
        counted_items=counted,
    )


def _logins(values: Any) -> list[str]:
    result: list[str] = []
    if isinstance(values, list):
        for entry in values:
            if isinstance(entry, dict):
                login = str(entry.get("login", "") or "")
                if login:
                    result.append(sanitize_connector_text(login))
    return result


def _label_names(values: Any) -> list[str]:
    result: list[str] = []
    if isinstance(values, list):
        for entry in values:
            name = str(entry.get("name", "") or "") if isinstance(entry, dict) else str(entry or "")
            if name:
                result.append(sanitize_connector_text(name))
    return result


def _merge_state(pull: dict) -> str:
    """Collapse a PR's outcome to merged | closed_unmerged | open, so a closed
    PR is never ambiguous about whether it actually landed."""
    if pull.get("merged"):
        return "merged"
    if str(pull.get("state", "") or "").lower() == "closed":
        return "closed_unmerged"
    return "open"


def _reviews(values: Any) -> list[GitHubReview]:
    result: list[GitHubReview] = []
    if isinstance(values, list):
        for entry in values:
            if not isinstance(entry, dict):
                continue
            user = entry.get("user") or {}
            login = str(user.get("login", "") or "") if isinstance(user, dict) else ""
            state = str(entry.get("state", "") or "")
            body = _snippet(sanitize_connector_text(str(entry.get("body", "") or "")), TEXT_SNIPPET_CHARS)
            if login or state or body:
                result.append(GitHubReview(user=sanitize_connector_text(login), state=state, body=body))
    return result


def _search_hit(item: dict) -> GitHubSearchHit:
    body = _snippet(sanitize_connector_text(str(item.get("body", "") or "")), TEXT_SNIPPET_CHARS)
    user = item.get("user") or {}
    return GitHubSearchHit(
        repo=_repo_from_repository_url(str(item.get("repository_url", "") or "")),
        number=int(item.get("number", 0) or 0),
        title=sanitize_connector_text(str(item.get("title", "") or "")),
        state=str(item.get("state", "") or ""),
        is_pull_request="pull_request" in item,
        author=str(user.get("login", "") or "") if isinstance(user, dict) else "",
        assignees=_logins(item.get("assignees")),
        labels=_label_names(item.get("labels")),
        comments=int(item.get("comments", 0) or 0),
        created_at=item.get("created_at"),
        updated_at=item.get("updated_at"),
        text=body,
    )
