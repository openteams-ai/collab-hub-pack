"""Usage-statistics endpoints.

Read endpoints aggregate three sources that already live in the shared frames
Postgres: the seen-user roster captured on every authenticated request, the
client-reported usage events (``POST /usage/events``), and the existing frame
history and active-state tables.

All endpoints are scoped to the caller's own ``(org, workspace)`` tenant and
require only authentication — the hub has no role model yet, so any workspace
member can read their workspace's aggregates. Revisit if roles land.

The JSON endpoints accept a repeatable ``user`` query parameter to narrow the
per-user figures to specific users; ``GET /usage/me`` is the self-scoped
convenience used by the desktop app. ``page_router`` additionally serves a
server-rendered HTML dashboard for browsers coming from the hub landing page.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response, status
from fastapi.responses import HTMLResponse
from pydantic import AwareDatetime, BaseModel

from ..dependencies import get_active_frame_store, get_history_store, get_usage_store
from ..frames.active_state import ActiveFrameStore, ActiveStateUnavailableError
from ..frames.auth import AuthContext, get_auth_context
from ..frames.history import FrameHistoryStore, HistoryEventCount
from ..frames.observability import USAGE_EVENTS
from ..frames.usage import UsageStore, UsageUnavailableError, UsageUser
from .frames import error_response

router = APIRouter(tags=["usage"])

# Browser-facing HTML pages, included in the app without the /v1 prefix only.
page_router = APIRouter(include_in_schema=False)

AuthDep = Annotated[AuthContext, Depends(get_auth_context)]
UsageStoreDep = Annotated[UsageStore, Depends(get_usage_store)]
HistoryStoreDep = Annotated[FrameHistoryStore, Depends(get_history_store)]
ActiveStoreDep = Annotated[ActiveFrameStore, Depends(get_active_frame_store)]

UserFilterDep = Annotated[
    list[str] | None,
    Query(
        alias="user",
        description="Limit per-user figures to these user ids (repeatable).",
    ),
]


class UsageEventCreate(BaseModel):
    """A client-reported usage event.

    The event vocabulary is a closed set: clients report activity the hub
    cannot observe itself, and each kind must be deliberately added here so the
    events table never becomes a free-form sink.

    Counts derived from these events are **approximate telemetry from honest
    clients**: there is no replay/idempotency key or abuse control, so a
    misbehaving authenticated client can inflate its own numbers. Do not treat
    them as an audited record.
    """

    event: Literal["chat_created"]
    detail: dict | None = None


class UserCount(BaseModel):
    """A per-user count for one metric."""

    user: str
    count: int


class UsersSummary(BaseModel):
    """Seen-user aggregates."""

    total: int
    active: int
    """Users whose ``last_seen`` falls inside the requested window."""


class ChatsSummary(BaseModel):
    """Client-reported chat aggregates."""

    created: int
    created_by_user: list[UserCount]


class FramesSummary(BaseModel):
    """Frame mutation aggregates derived from the change-history log."""

    created: int
    updated: int
    created_by_user: list[UserCount]
    updated_by_user: list[UserCount]


class ActiveFramesSummary(BaseModel):
    """Current active-Frame aggregates."""

    frames: int
    users: int


class UsageSummaryResponse(BaseModel):
    """The workspace usage roll-up."""

    org_id: str
    workspace_id: str
    since: datetime | None
    until: datetime | None
    users: UsersSummary
    chats: ChatsSummary
    frames: FramesSummary
    active_frames: ActiveFramesSummary | None
    """``null`` when the deployment has no active-state backend configured."""


class UsageUserEntry(BaseModel):
    """One seen user."""

    user: str
    email: str | None
    first_seen: datetime
    last_seen: datetime


class UsageUsersResponse(BaseModel):
    """The workspace's seen-user roster."""

    users: list[UsageUserEntry]


def _ensure_aware(value: datetime | None) -> datetime | None:
    """Interpret a naive query datetime as UTC so store comparisons never mix
    naive and aware values."""

    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _in_window(value: datetime, since: datetime | None, until: datetime | None) -> bool:
    if since is not None and value < since:
        return False
    if until is not None and value >= until:
        return False
    return True


def _user_counts(counts: list[HistoryEventCount], event: str) -> list[UserCount]:
    return [UserCount(user=item.actor, count=item.count) for item in counts if item.event == event]


@dataclass(frozen=True)
class UsageReport:
    """A computed usage roll-up plus the roster rows behind it.

    One computation feeds three consumers: the JSON summary endpoint, the
    self-scoped ``/usage/me``, and the server-rendered HTML dashboard (which
    also renders the roster).
    """

    summary: UsageSummaryResponse
    roster: list[UsageUser]
    chats_by_user: dict[str, int]
    frames_created_by_user: dict[str, int]
    frames_updated_by_user: dict[str, int]


def build_usage_report(
    auth: AuthContext,
    usage_store: UsageStore,
    history_store: FrameHistoryStore,
    active_store: ActiveFrameStore,
    since: datetime | None,
    until: datetime | None,
    users: list[str] | None = None,
) -> UsageReport:
    """Aggregate the tenant's usage, optionally narrowed to specific users.

    With a ``users`` filter, the active-Frames figure is the union of those
    users' own active sets (via ``get_active_frame_ids``) rather than the
    workspace-wide aggregate.
    """

    user_filter = set(users) if users else None

    roster = usage_store.list_users(auth.org_id, auth.workspace_id)
    event_counts = usage_store.count_events(auth.org_id, auth.workspace_id, since, until)
    frame_counts = history_store.count_events(auth.org_id, auth.workspace_id, "frame", since, until)

    if user_filter is not None:
        roster = [entry for entry in roster if entry.user in user_filter]
        event_counts = [item for item in event_counts if item.user in user_filter]
        frame_counts = [item for item in frame_counts if item.actor in user_filter]

    chat_counts = [UserCount(user=item.user, count=item.count) for item in event_counts if item.event == "chat_created"]
    created_counts = _user_counts(frame_counts, "created")
    updated_counts = _user_counts(frame_counts, "updated")

    active_frames: ActiveFramesSummary | None
    try:
        if user_filter is None:
            active_usage = active_store.count_active(auth.org_id, auth.workspace_id)
            active_frames = ActiveFramesSummary(frames=active_usage.frames, users=active_usage.users)
        else:
            frame_ids: set[str] = set()
            holders = 0
            for user in sorted(user_filter):
                ids = active_store.get_active_frame_ids(auth.org_id, auth.workspace_id, user)
                if ids:
                    holders += 1
                    frame_ids.update(ids)
            active_frames = ActiveFramesSummary(frames=len(frame_ids), users=holders)
    except ActiveStateUnavailableError:
        active_frames = None

    summary = UsageSummaryResponse(
        org_id=auth.org_id,
        workspace_id=auth.workspace_id,
        since=since,
        until=until,
        users=UsersSummary(
            total=len(roster),
            active=sum(1 for entry in roster if _in_window(entry.last_seen, since, until)),
        ),
        chats=ChatsSummary(
            created=sum(item.count for item in chat_counts),
            created_by_user=chat_counts,
        ),
        frames=FramesSummary(
            created=sum(item.count for item in created_counts),
            updated=sum(item.count for item in updated_counts),
            created_by_user=created_counts,
            updated_by_user=updated_counts,
        ),
        active_frames=active_frames,
    )
    return UsageReport(
        summary=summary,
        roster=roster,
        chats_by_user={item.user: item.count for item in chat_counts},
        frames_created_by_user={item.user: item.count for item in created_counts},
        frames_updated_by_user={item.user: item.count for item in updated_counts},
    )


@router.post("/usage/events", status_code=status.HTTP_204_NO_CONTENT)
def record_usage_event(
    payload: UsageEventCreate,
    auth: AuthDep,
    usage_store: UsageStoreDep,
) -> Response:
    """Record one client-reported usage event for the calling user."""

    usage_store.record_event(
        auth.org_id,
        auth.workspace_id,
        auth.user,
        payload.event,
        payload.detail,
    )
    USAGE_EVENTS.labels(event=payload.event).inc()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/usage/summary")
def get_usage_summary(
    auth: AuthDep,
    usage_store: UsageStoreDep,
    history_store: HistoryStoreDep,
    active_store: ActiveStoreDep,
    user: UserFilterDep = None,
    since: Annotated[AwareDatetime | None, Query()] = None,
    until: Annotated[AwareDatetime | None, Query()] = None,
) -> UsageSummaryResponse:
    """Return the caller's workspace usage roll-up for an optional time window.

    ``since``/``until`` bound the event-derived counts (chats, frame
    mutations) and the "active users" figure; the user total and the
    active-Frames snapshot are current-state and ignore the window. A
    repeatable ``user`` parameter narrows every per-user figure to the given
    user ids.
    """

    report = build_usage_report(
        auth,
        usage_store,
        history_store,
        active_store,
        _ensure_aware(since),
        _ensure_aware(until),
        users=user,
    )
    return report.summary


@router.get("/usage/me")
def get_my_usage(
    auth: AuthDep,
    usage_store: UsageStoreDep,
    history_store: HistoryStoreDep,
    active_store: ActiveStoreDep,
    since: Annotated[AwareDatetime | None, Query()] = None,
    until: Annotated[AwareDatetime | None, Query()] = None,
) -> UsageSummaryResponse:
    """Return the usage roll-up narrowed to the calling user."""

    report = build_usage_report(
        auth,
        usage_store,
        history_store,
        active_store,
        _ensure_aware(since),
        _ensure_aware(until),
        users=[auth.user],
    )
    return report.summary


@router.get("/usage/users")
def list_usage_users(
    auth: AuthDep,
    usage_store: UsageStoreDep,
    user: UserFilterDep = None,
) -> UsageUsersResponse:
    """Return authenticated users seen in the caller's workspace.

    A repeatable ``user`` parameter narrows the roster to the given user ids.
    """

    user_filter = set(user) if user else None
    roster = usage_store.list_users(auth.org_id, auth.workspace_id)
    if user_filter is not None:
        roster = [entry for entry in roster if entry.user in user_filter]
    return UsageUsersResponse(
        users=[
            UsageUserEntry(
                user=entry.user,
                email=entry.email,
                first_seen=entry.first_seen,
                last_seen=entry.last_seen,
            )
            for entry in roster
        ]
    )


# ── Server-rendered dashboard ────────────────────────────────────────────────

WINDOWS: dict[str, tuple[str, int | None]] = {
    "7d": ("Last 7 days", 7),
    "30d": ("Last 30 days", 30),
    "all": ("All time", None),
}

PAGE_STYLE = """
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto,
         Helvetica, Arial, sans-serif; margin: 2rem auto; max-width: 64rem;
         padding: 0 1rem; color: #1a1a2e; }
  h1 { font-size: 1.5rem; margin-bottom: 0.25rem; }
  h2 { font-size: 1.1rem; margin-top: 2rem; }
  .sub { color: #666; margin-top: 0; }
  .windows a { margin-right: 0.75rem; text-decoration: none; color: #3452d9; }
  .windows a.current { font-weight: 700; color: #1a1a2e; }
  .cards { display: flex; flex-wrap: wrap; gap: 0.75rem; margin: 1.25rem 0; }
  .card { border: 1px solid #ddd; border-radius: 8px; padding: 0.75rem 1rem;
          min-width: 10rem; }
  .card .label { color: #666; font-size: 0.85rem; }
  .card .value { font-size: 1.75rem; font-weight: 600; }
  .card .detail { color: #666; font-size: 0.8rem; }
  table { border-collapse: collapse; width: 100%; margin-top: 0.5rem; }
  th, td { text-align: left; padding: 0.4rem 0.6rem;
           border-bottom: 1px solid #eee; font-size: 0.9rem; }
  th { color: #666; font-weight: 600; }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
  .empty { color: #888; padding: 1rem 0; }
  footer { margin-top: 2rem; font-size: 0.85rem; }
  footer a { color: #3452d9; }
"""


def _window_since(window: str) -> datetime | None:
    days = WINDOWS[window][1]
    if days is None:
        return None
    return datetime.now(timezone.utc) - timedelta(days=days)


def _render_usage_page(report: UsageReport, window: str) -> str:
    """Render the workspace usage dashboard as a static HTML document."""

    summary = report.summary
    window_links = " ".join(
        f'<a href="?window={key}" class="{"current" if key == window else ""}">{label}</a>'
        for key, (label, _days) in WINDOWS.items()
    )

    if summary.active_frames is None:
        active_value, active_detail = "—", "Not tracked on this hub"
    else:
        active_value = str(summary.active_frames.frames)
        active_detail = f"across {summary.active_frames.users} users"

    if report.roster:
        rows = "".join(
            "<tr>"
            f"<td>{html.escape(entry.user)}</td>"
            f"<td>{html.escape(entry.email or '—')}</td>"
            f'<td class="num">{report.chats_by_user.get(entry.user, 0)}</td>'
            f'<td class="num">{report.frames_created_by_user.get(entry.user, 0)}</td>'
            f'<td class="num">{report.frames_updated_by_user.get(entry.user, 0)}</td>'
            f"<td>{entry.last_seen.strftime('%Y-%m-%d %H:%M UTC')}</td>"
            "</tr>"
            for entry in report.roster
        )
        table = (
            "<table><thead><tr>"
            "<th>User</th><th>Email</th>"
            '<th class="num">New chats</th>'
            '<th class="num">Frames created</th>'
            '<th class="num">Frames updated</th>'
            "<th>Last seen</th>"
            "</tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )
    else:
        table = '<p class="empty">No users recorded yet.</p>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hub Usage — {html.escape(summary.workspace_id)}</title>
<style>{PAGE_STYLE}</style>
</head>
<body>
<h1>Hub Usage</h1>
<p class="sub">Workspace <strong>{html.escape(summary.workspace_id)}</strong>
 (org {html.escape(summary.org_id)})</p>
<p class="windows">{window_links}</p>
<div class="cards">
  <div class="card"><div class="label">Users</div>
    <div class="value">{summary.users.total}</div>
    <div class="detail">{summary.users.active} active in window</div></div>
  <div class="card"><div class="label">New chats</div>
    <div class="value">{summary.chats.created}</div></div>
  <div class="card"><div class="label">Frames created</div>
    <div class="value">{summary.frames.created}</div>
    <div class="detail">{summary.frames.updated} updated</div></div>
  <div class="card"><div class="label">Active frames</div>
    <div class="value">{active_value}</div>
    <div class="detail">{active_detail}</div></div>
</div>
<h2>Per-user activity</h2>
<p class="sub">Chat and Frame counts reflect the selected window; the roster is all-time.</p>
{table}
<footer><a href="./docs">API documentation</a></footer>
</body>
</html>"""


@page_router.get("/usage", response_class=HTMLResponse)
def usage_dashboard(
    auth: AuthDep,
    usage_store: UsageStoreDep,
    history_store: HistoryStoreDep,
    active_store: ActiveStoreDep,
    window: Annotated[str, Query()] = "30d",
) -> HTMLResponse:
    """Serve the workspace usage dashboard for browsers.

    Any authenticated workspace member sees the full workspace usage — the hub
    has no role model yet to gate this more tightly (tracked follow-up).
    """

    if window not in WINDOWS:
        window = "30d"
    report = build_usage_report(
        auth,
        usage_store,
        history_store,
        active_store,
        _window_since(window),
        None,
    )
    return HTMLResponse(_render_usage_page(report, window))


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(UsageUnavailableError)
    async def usage_unavailable_handler(_request: Request, exc: UsageUnavailableError):
        return error_response(status.HTTP_503_SERVICE_UNAVAILABLE, "usage_unavailable", str(exc))
