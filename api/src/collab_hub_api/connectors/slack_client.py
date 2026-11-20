from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

import httpx

from .models import SlackChannel, SlackMessage, SlackSearchHit
from .slack_text import sanitize_slack_text

DM_TYPES = ["im", "mpim"]

MAX_LIST_PAGE_SIZE = 200
MAX_LIST_PAGES = 5
MAX_CHANNEL_AUTHORIZATION_PAGES = 25
# Search results only show the first 500 characters of each message.
# The full message can still be fetched with a read.
SEARCH_SNIPPET_CHARS = 500

# Default read size budget, matching SlackReadRequest/SlackThreadReadRequest.
DEFAULT_READ_BUDGET_CHARS = 12_000

# Prefix marking a cursor as our own read-budget continuation point (a Slack
# message ts) rather than Slack's opaque pagination cursor. A channel read that
# had a lower time bound carries it too, as "ts:<resume ts>:<oldest>", so the
# next page stays inside the requested window.
_BUDGET_CURSOR_PREFIX = "ts:"

# Slack's own cursor only records a position, not the time window. When a channel
# read has a lower time bound, its Slack cursor is wrapped as
# "slack:<oldest>:<slack cursor>" so the bound survives that page too.
_WINDOWED_SLACK_CURSOR_PREFIX = "slack:"

# A Slack message timestamp ("1790000000.000100"), and a time-window bound, which
# can be shorter (a pre-2001 date) or "0.000000". Our own cursors are only
# recognised when they have these shapes. The request models reject anything else
# with a 422; a direct client call sends it to Slack unchanged, which rejects it,
# instead of crashing or restarting the read.
_SLACK_TS = re.compile(r"\d{10,}\.\d{3,}")
_SLACK_BOUND = re.compile(r"\d+\.\d+")

# ``auth.test`` errors that mean the brokered token is not a usable Slack Web API
# user token -- e.g. Keycloak brokered an OpenID sign-in/identity token instead of an
# ``xoxp`` user token. These map the connector status to "reconnect required" so a
# linked-but-unusable token surfaces before every read fails.
SLACK_TOKEN_INVALID_ERRORS = frozenset(
    {
        "not_authed",
        "no_authed",
        "invalid_auth",
        "account_inactive",
        "token_revoked",
        "token_expired",
        "not_allowed_token_type",
    }
)


@dataclass
class SlackAccessCheck:
    """Result of validating a token against Slack's ``auth.test`` read endpoint."""

    user_id: str = ""
    team: str = ""
    granted_scopes: list[str] = field(default_factory=list)


class SlackUpstreamError(RuntimeError):
    def __init__(self, *, operation: str, status_code: int | None = None, message: str = "") -> None:
        self.operation = operation
        self.status_code = status_code
        self.message = message
        detail = f"Slack {operation} failed"
        if status_code is not None and status_code != 200:
            detail = f"{detail} with HTTP {status_code}"
        if message:
            detail = f"{detail}: {message}"
        super().__init__(detail)


class SlackConversationNotAllowed(RuntimeError):
    """Raised when a raw conversation id is not an ordinary Slack channel."""


class SlackClient:
    """Read-only Slack Web API client. Only GET-style read methods are implemented."""

    def __init__(self, *, access_token: str, api_base_url: str, timeout_seconds: float = 10.0):
        self.access_token = access_token
        self.api_base_url = api_base_url.rstrip("/")
        self.timeout = httpx.Timeout(timeout_seconds)

    async def list_channels(
        self,
        *,
        limit: int,
        cursor: str = "",
        max_pages: int = MAX_LIST_PAGES,
    ) -> tuple[list[SlackChannel], str]:
        phase, upstream_cursor = _decode_channel_cursor(cursor)

        if phase == "public":
            public, next_cursor = await self._list_conversations(
                types=["public_channel"],
                limit=limit,
                cursor=upstream_cursor,
                operation="public channel list",
                max_pages=max_pages,
            )
            if next_cursor:
                return public, _encode_channel_cursor("public", next_cursor)
            return public, _encode_channel_cursor("private", "")

        private, next_cursor = await self._list_conversations(
            types=["private_channel"],
            limit=limit,
            cursor=upstream_cursor,
            operation="private channel list",
            max_pages=max_pages,
        )
        if next_cursor:
            return private, _encode_channel_cursor("private", next_cursor)
        return private, ""

    async def list_dms(self, *, limit: int, cursor: str = "") -> tuple[list[SlackChannel], str]:
        return await self._list_conversations(
            types=DM_TYPES,
            limit=limit,
            cursor=cursor,
            operation="dm list",
        )

    async def search(self, *, query: str, limit: int) -> list[SlackSearchHit]:
        hits, _ = await self.search_page(query=query, limit=limit, page=1)
        return hits

    async def search_page(self, *, query: str, limit: int, page: int = 1) -> tuple[list[SlackSearchHit], int | None]:
        payload = await self._get_json(
            "/search.messages",
            params={
                "query": query,
                "count": str(limit),
                "page": str(page),
                "sort": "timestamp",
                "sort_dir": "desc",
                "highlight": "false",
            },
            operation="search",
        )
        matches = payload.get("messages", {}).get("matches", [])
        if not isinstance(matches, list):
            return [], None
        paging = payload.get("messages", {}).get("paging", {})
        pages = int(paging.get("pages", page) or page) if isinstance(paging, dict) else page
        next_page = page + 1 if page < pages else None
        hits = [_search_hit(item) for item in matches[:limit]]
        return [hit for hit in hits if not (hit.is_im or hit.is_mpim)], next_page

    async def read_conversation(
        self,
        *,
        channel_id: str,
        limit: int,
        oldest: str = "",
        latest: str = "",
        cursor: str = "",
        max_chars: int = DEFAULT_READ_BUDGET_CHARS,
    ) -> tuple[list[SlackMessage], bool, str]:
        await self._require_channel(channel_id)
        params = {
            "channel": channel_id,
            "limit": str(limit),
            "inclusive": "true",
        }
        if oldest:
            params["oldest"] = oldest
        if latest:
            params["latest"] = latest
        budget = _decode_budget_cursor(cursor)
        windowed = _decode_windowed_slack_cursor(cursor)
        window_oldest = ""
        if budget is not None:
            budget_ts, window_oldest = budget
            # A budget cursor picks up where the last page's budget stopped:
            # everything at or before that ts, i.e. the next (older) page. If the
            # caller also sent its own latest, keep whichever is older.
            params["latest"] = min(budget_ts, latest, key=float) if latest else budget_ts
        elif windowed is not None:
            slack_cursor, window_oldest = windowed
            params["cursor"] = slack_cursor
        elif cursor:
            params["cursor"] = cursor
        # Keep the original lower time bound (from oldest, days_back or
        # since_date) so later pages don't drift outside the window.
        if window_oldest and not oldest:
            params["oldest"] = window_oldest
        payload = await self._get_json("/conversations.history", params=params, operation="conversation read")
        messages, has_more, next_cursor = _messages_page(payload)
        window = params.get("oldest", "")
        messages, has_more, next_cursor = _apply_read_budget(messages, has_more, next_cursor, max_chars, oldest=window)
        if window and next_cursor and not next_cursor.startswith(_BUDGET_CURSOR_PREFIX):
            # Slack's cursor would lose the window, so carry it along.
            next_cursor = _encode_windowed_slack_cursor(next_cursor, window)
        return messages, has_more, next_cursor

    async def read_thread(
        self,
        *,
        channel_id: str,
        message_ts: str,
        limit: int,
        cursor: str = "",
        max_chars: int = DEFAULT_READ_BUDGET_CHARS,
    ) -> tuple[list[SlackMessage], bool, str]:
        await self._require_channel(channel_id)
        params = {
            "channel": channel_id,
            "ts": message_ts,
            "limit": str(limit),
            "inclusive": "true",
        }
        budget = _decode_budget_cursor(cursor)
        budget_ts = budget[0] if budget is not None else None
        if budget_ts is not None:
            # Threads are read oldest-first, so the budget cursor drops the
            # already-read older messages and continues from where we stopped.
            params["oldest"] = budget_ts
        elif cursor:
            params["cursor"] = cursor
        payload = await self._get_json("/conversations.replies", params=params, operation="thread read")
        messages, has_more, next_cursor = _messages_page(payload)
        if cursor:
            # Slack can put the thread's first message (thread_ts == ts) at the top
            # of every page. Past the first page it was already returned, so drop
            # it whichever cursor got us here.
            messages = [message for message in messages if not (message.thread_ts and message.ts == message.thread_ts)]
        if budget_ts is not None:
            # Also drop anything older than where the budget cursor resumes.
            messages = [message for message in messages if float(message.ts) >= float(budget_ts)]
        return _apply_read_budget(messages, has_more, next_cursor, max_chars)

    async def _require_channel(self, channel_id: str) -> None:
        try:
            payload = await self._get_json(
                "/conversations.info",
                params={"channel": channel_id},
                operation="conversation metadata",
            )
        except SlackUpstreamError as exc:
            if exc.message != "missing_scope":
                raise
            cursor = ""
            seen: set[str] = set()
            for _page in range(MAX_CHANNEL_AUTHORIZATION_PAGES):
                channels, next_cursor = await self.list_channels(
                    limit=MAX_LIST_PAGE_SIZE,
                    cursor=cursor,
                    max_pages=1,
                )
                if any(channel.id == channel_id for channel in channels):
                    return
                if not next_cursor or next_cursor in seen:
                    break
                seen.add(next_cursor)
                cursor = next_cursor
            raise SlackConversationNotAllowed("Only Slack channels are allowed") from exc
        item = payload.get("channel")
        if not isinstance(item, dict):
            raise SlackConversationNotAllowed("Only Slack channels are allowed")
        if item.get("is_im") or item.get("is_mpim"):
            raise SlackConversationNotAllowed("Direct-message conversations are not allowed")
        if not (item.get("is_channel") or item.get("is_group")):
            raise SlackConversationNotAllowed("Only Slack channels are allowed")

    async def verify_access(self) -> SlackAccessCheck:
        """Confirm the token is a usable Slack Web API user token via ``auth.test``.

        Raises :class:`SlackUpstreamError` when Slack rejects the token; an OpenID
        sign-in/identity token surfaces as ``invalid_auth`` / ``not_authed`` (see
        :data:`SLACK_TOKEN_INVALID_ERRORS`). On success the granted user scopes are
        read from the ``x-oauth-scopes`` response header when Slack provides it.
        """
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(
                self.api_base_url + "/auth.test",
                headers={"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"},
            )
        _raise_for_slack_status(response, operation="auth check")
        payload = response.json()
        if not isinstance(payload, dict):
            raise SlackUpstreamError(
                operation="auth check", status_code=response.status_code, message="invalid response"
            )
        if not payload.get("ok", False):
            raise SlackUpstreamError(
                operation="auth check",
                status_code=response.status_code,
                message=str(payload.get("error", "unknown_error")),
            )
        granted = response.headers.get("x-oauth-scopes", "")
        scopes = [scope.strip() for scope in granted.split(",") if scope.strip()]
        return SlackAccessCheck(
            user_id=str(payload.get("user_id", "") or ""),
            team=str(payload.get("team", "") or ""),
            granted_scopes=scopes,
        )

    async def _list_conversations(
        self,
        *,
        types: list[str],
        limit: int,
        cursor: str,
        operation: str,
        max_pages: int = MAX_LIST_PAGES,
    ) -> tuple[list[SlackChannel], str]:
        channels: list[SlackChannel] = []
        next_cursor = cursor
        for _page in range(max_pages):
            params = {
                "types": ",".join(types),
                "exclude_archived": "true",
                "limit": str(min(max(limit, 1), MAX_LIST_PAGE_SIZE)),
            }
            if next_cursor:
                params["cursor"] = next_cursor
            payload = await self._get_json("/conversations.list", params=params, operation=operation)
            items = payload.get("channels", [])
            if isinstance(items, list):
                channels.extend(_channel(item) for item in items)
            next_cursor = payload.get("response_metadata", {}).get("next_cursor", "") or ""
            if not next_cursor or len(channels) >= limit:
                break
        return channels[:limit], next_cursor

    async def _get_json(self, path: str, params: dict[str, str], *, operation: str) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(
                self.api_base_url + path,
                headers={"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"},
                params=params,
            )
        _raise_for_slack_status(response, operation=operation)
        payload = response.json()
        if not isinstance(payload, dict):
            raise SlackUpstreamError(operation=operation, status_code=response.status_code, message="invalid response")
        if not payload.get("ok", False):
            raise SlackUpstreamError(
                operation=operation,
                status_code=response.status_code,
                message=str(payload.get("error", "unknown_error")),
            )
        return payload


def _messages_page(payload: dict) -> tuple[list[SlackMessage], bool, str]:
    messages = payload.get("messages", [])
    if not isinstance(messages, list):
        messages = []
    next_cursor = payload.get("response_metadata", {}).get("next_cursor", "") or ""
    return (
        [_message(item) for item in messages],
        bool(payload.get("has_more", False)),
        next_cursor,
    )


def _apply_read_budget(
    messages: list[SlackMessage],
    has_more: bool,
    next_cursor: str,
    max_chars: int,
    oldest: str = "",
) -> tuple[list[SlackMessage], bool, str]:
    """Stop adding messages once their text would go over ``max_chars``.

    Never cuts a message, and always keeps the first one even if it alone is
    over budget. Stopping early takes precedence over Slack's own pagination:
    the cursor points at the first message left out, so the next call picks up
    exactly there. ``oldest`` is the read's lower time bound, if any; it rides
    along in the cursor so the next page keeps it.
    """
    kept: list[SlackMessage] = []
    total = 0
    for message in messages:
        size = len(message.text)
        if kept and total + size > max_chars:
            return kept, True, _encode_budget_cursor(message.ts, oldest)
        kept.append(message)
        total += size
    return kept, has_more, next_cursor


def _encode_budget_cursor(ts: str, oldest: str = "") -> str:
    cursor = f"{_BUDGET_CURSOR_PREFIX}{ts}"
    return f"{cursor}:{oldest}" if oldest else cursor


def _decode_budget_cursor(cursor: str) -> tuple[str, str] | None:
    """Return (resume ts, lower time bound or "") for a budget cursor, else None."""
    if not cursor.startswith(_BUDGET_CURSOR_PREFIX):
        return None
    ts, _, oldest = cursor[len(_BUDGET_CURSOR_PREFIX) :].partition(":")
    if not _SLACK_TS.fullmatch(ts) or (oldest and not _SLACK_BOUND.fullmatch(oldest)):
        return None
    return ts, oldest


def _encode_windowed_slack_cursor(slack_cursor: str, oldest: str) -> str:
    return f"{_WINDOWED_SLACK_CURSOR_PREFIX}{oldest}:{slack_cursor}"


def _decode_windowed_slack_cursor(cursor: str) -> tuple[str, str] | None:
    """Return (Slack cursor, lower time bound) for a windowed Slack cursor, else None."""
    if not cursor.startswith(_WINDOWED_SLACK_CURSOR_PREFIX):
        return None
    oldest, _, slack_cursor = cursor[len(_WINDOWED_SLACK_CURSOR_PREFIX) :].partition(":")
    if not _SLACK_BOUND.fullmatch(oldest) or not slack_cursor:
        return None
    return slack_cursor, oldest


def _encode_channel_cursor(phase: str, upstream_cursor: str) -> str:
    return f"{phase}:{upstream_cursor}"


def _decode_channel_cursor(cursor: str) -> tuple[str, str]:
    phase, separator, upstream_cursor = cursor.partition(":")
    if separator and phase in {"public", "private"}:
        return phase, upstream_cursor
    return "public", cursor


def _channel(item: dict) -> SlackChannel:
    return SlackChannel(
        id=str(item.get("id", "")),
        name=str(item.get("name", "") or _dm_name(item)),
        is_private=bool(item.get("is_private", False)),
        is_im=bool(item.get("is_im", False)),
        is_mpim=bool(item.get("is_mpim", False)),
        user_id=str(item.get("user", "") or ""),
        topic=sanitize_slack_text(str((item.get("topic") or {}).get("value", "") or "")),
        num_members=item.get("num_members"),
    )


def _dm_name(item: dict) -> str:
    if item.get("is_im"):
        user = item.get("user", "")
        return f"dm-{user}" if user else "dm"
    return ""


def _message(item: dict) -> SlackMessage:
    return SlackMessage(
        ts=str(item.get("ts", "")),
        user_id=str(item.get("user") or item.get("bot_id") or ""),
        text=sanitize_slack_text(str(item.get("text", "") or "")),
        thread_ts=str(item.get("thread_ts", "") or ""),
        reply_count=int(item.get("reply_count", 0) or 0),
    )


def _snippet(text: str, limit: int) -> tuple[str, bool]:
    """Cut the text down to `limit` characters and say whether anything was cut."""
    if len(text) <= limit:
        return text, False
    return text[:limit].rstrip() + "…", True


def _search_hit(item: dict) -> SlackSearchHit:
    channel = item.get("channel") or {}
    # Clean the text first, then shorten it, so we never cut a Slack link in half.
    text, truncated = _snippet(sanitize_slack_text(str(item.get("text", "") or "")), SEARCH_SNIPPET_CHARS)
    return SlackSearchHit(
        channel_id=str(channel.get("id", "") or ""),
        channel_name=str(channel.get("name", "") or ""),
        is_im=bool(channel.get("is_im", False)),
        is_mpim=bool(channel.get("is_mpim", False)),
        ts=str(item.get("ts", "")),
        user_id=str(item.get("user", "") or ""),
        author_name=str(item.get("username", "") or ""),
        text=text,
        truncated=truncated,
        thread_ts=str(item.get("thread_ts", "") or "") or _permalink_thread_ts(item),
    )


def _permalink_thread_ts(item: dict) -> str:
    """Slack search matches may only carry a reply's thread in its permalink."""
    # The permalink itself is never sent to the model (see SlackSearchHit).
    thread_ts = parse_qs(urlsplit(str(item.get("permalink", "") or "")).query).get("thread_ts", [""])[0]
    return thread_ts if _SLACK_TS.fullmatch(thread_ts) else ""


def _raise_for_slack_status(response: httpx.Response, *, operation: str) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise SlackUpstreamError(
            operation=operation,
            status_code=response.status_code,
            message=_slack_error_message(response),
        ) from exc


def _slack_error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:240].strip()
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, str):
            return error[:240].strip()
    return ""
