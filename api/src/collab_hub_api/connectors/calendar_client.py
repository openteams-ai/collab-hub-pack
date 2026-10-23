from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .connector_text import sanitize_connector_text
from .models import (
    CalendarAttachment,
    CalendarAttendee,
    CalendarAttendeePreview,
    CalendarEventMetadata,
    CalendarSearchEvent,
)

_MAX_CALENDARS = 1000
_MAX_CALENDAR_LIST_PAGES = 20
_MAX_PROVIDER_PAGES_PER_CALENDAR = 25
_MAX_SEARCH_CALENDARS = 100
_MAX_CURSOR_TIME_SPAN = timedelta(days=7301)
_SEARCH_ATTENDEE_PREVIEW = 10


class CalendarUpstreamError(RuntimeError):
    def __init__(self, *, operation: str, status_code: int | None = None, message: str = "") -> None:
        self.operation = operation
        self.status_code = status_code
        self.message = message
        detail = f"Google Calendar {operation} failed"
        if status_code is not None:
            detail = f"{detail} with HTTP {status_code}"
        if message:
            detail = f"{detail}: {message}"
        super().__init__(detail)


class CalendarSearchError(ValueError):
    """Raised for a caller-correctable Calendar search request."""


class CalendarCursorError(CalendarSearchError):
    """Raised when a Calendar continuation cursor is malformed or stale."""


@dataclass(frozen=True)
class CalendarInfo:
    id: str
    name: str
    time_zone: str
    primary: bool


@dataclass(frozen=True)
class CalendarCursorState:
    """Validated continuation state encoded in the public opaque cursor."""

    after_start: datetime
    after_all_day: bool
    after_calendar_id: str
    after_event_id: str
    time_min: datetime
    time_max: datetime
    time_zone: str
    calendar_signature: str
    fingerprint: str


class GoogleCalendarClient:
    def __init__(self, *, access_token: str, api_base_url: str, timeout_seconds: float = 10.0):
        self.access_token = access_token
        self.api_base_url = api_base_url.rstrip("/")
        self.timeout = httpx.Timeout(timeout_seconds)

    async def verify_access(self) -> None:
        # Exercise both halves of the actual default search contract. A
        # calendar-list-only scope cannot read events, while an event-only grant
        # cannot discover the user's readable calendars.
        await self._get_json(
            "/users/me/calendarList",
            params={"maxResults": "1", "showHidden": "false"},
            operation="access check",
        )
        await self._get_json(
            "/calendars/primary/events",
            params={
                "maxResults": "1",
                "singleEvents": "true",
                "timeMin": _rfc3339(datetime.now(timezone.utc)),
            },
            operation="access check",
        )

    async def list_calendars(self) -> list[CalendarInfo]:
        """List a bounded, complete set of readable calendars.

        Google normally returns dense 250-item pages. The page-count and token
        cycle guards prevent a malformed or emulated provider from hanging a
        connector request on empty or repeating pages.
        """
        calendars: list[CalendarInfo] = []
        page_token = ""
        seen_page_tokens = {page_token}
        for _page_number in range(_MAX_CALENDAR_LIST_PAGES):
            params = {"maxResults": "250", "showHidden": "false"}
            if page_token:
                params["pageToken"] = page_token
            payload = await self._get_json("/users/me/calendarList", params=params, operation="calendar list")
            items = payload.get("items", [])
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    calendar_id = _string(item.get("id"))
                    if not calendar_id:
                        continue
                    name = _string(item.get("summaryOverride")) or _string(item.get("summary"))
                    calendars.append(
                        CalendarInfo(
                            id=calendar_id,
                            name=name,
                            time_zone=_string(item.get("timeZone")),
                            primary=item.get("primary") is True,
                        )
                    )
                    if len(calendars) >= _MAX_CALENDARS:
                        return calendars
            next_page_token = _string(payload.get("nextPageToken"))
            if not next_page_token:
                return calendars
            if next_page_token in seen_page_tokens:
                raise CalendarUpstreamError(
                    operation="calendar list",
                    message="provider pagination did not advance",
                )
            seen_page_tokens.add(next_page_token)
            page_token = next_page_token
        raise CalendarUpstreamError(
            operation="calendar list",
            message=f"provider exceeded {_MAX_CALENDAR_LIST_PAGES} pages",
        )

    async def search(
        self,
        *,
        query: str,
        limit: int,
        calendar_ids: list[str] | None = None,
        time_min: datetime | None = None,
        time_max: datetime | None = None,
        days_back: int = 0,
        days_ahead: int = 0,
        since_date: date | None = None,
        until_date: date | None = None,
        time_zone: str = "",
        cursor: str = "",
    ) -> tuple[list[CalendarSearchEvent], str]:
        """Return a globally chronological page across readable calendars.

        Every selected calendar contributes candidates before Collab Hub chooses the
        page. Continuations store the last emitted global sort key and concrete
        time window, so secondary calendars cannot surface older events on a
        later page and relative date windows do not drift.
        """
        # Defense in depth for direct callers as well as the API model: an
        # omitted or empty selection means primary, never all readable calendars.
        selected_ids = _dedupe(
            value.strip() for value in (calendar_ids or ["primary"]) if value.strip()
        ) or ["primary"]
        fingerprint = _search_fingerprint(
            query=query,
            calendar_ids=selected_ids,
            time_min=time_min,
            time_max=time_max,
            days_back=days_back,
            days_ahead=days_ahead,
            since_date=since_date,
            until_date=until_date,
            time_zone=time_zone,
        )
        state: CalendarCursorState | None = None
        if cursor:
            state = _decode_cursor(cursor)
            if state.fingerprint != fingerprint:
                raise CalendarCursorError(
                    "Calendar cursor does not match the current search filters; restart the search."
                )
            lower = state.time_min
            upper = state.time_max

        # Invalid cursors are decoded before provider I/O. A valid continuation
        # still rechecks the selected calendar set so access changes cannot make
        # the cursor silently skip or duplicate a calendar.
        available = await self.list_calendars()
        by_id = {calendar.id: calendar for calendar in available}
        calendars = [
            by_id.get(value, CalendarInfo(value, "", "", False))
            for value in selected_ids
        ]
        if len(calendars) > _MAX_SEARCH_CALENDARS:
            raise CalendarSearchError(
                f"Search spans {len(calendars)} calendars; choose at most {_MAX_SEARCH_CALENDARS} calendar_ids."
            )

        calendar_signature = _calendar_signature(calendars)
        if state is not None:
            if state.calendar_signature != calendar_signature:
                raise CalendarCursorError("Calendar list changed; restart the search.")
            zone = _time_zone(state.time_zone)
        else:
            zone = _time_zone(time_zone or _default_calendar_time_zone(available))
            lower, upper = _time_bounds(
                time_min=time_min,
                time_max=time_max,
                days_back=days_back,
                days_ahead=days_ahead,
                since_date=since_date,
                until_date=until_date,
                zone=zone,
            )
            if upper - lower > _MAX_CURSOR_TIME_SPAN:
                raise CalendarSearchError("Google Calendar search window cannot exceed 7301 days.")

        after_key = _cursor_sort_key(state) if state is not None else None
        provider_lower = lower
        if state is not None:
            # Keep same-start events eligible while avoiding a full rescan of
            # earlier pages. Google treats timeMin as an exclusive lower bound
            # on event end, so one microsecond of overlap preserves zero-length
            # and same-instant events for the deterministic key filter below.
            provider_lower = max(lower, state.after_start - timedelta(microseconds=1))

        candidates: list[CalendarSearchEvent] = []
        provider_has_more = False
        for calendar in calendars:
            calendar_candidates, calendar_has_more = await self._calendar_candidates(
                calendar=calendar,
                query=query,
                limit=limit,
                time_min=provider_lower,
                time_max=upper,
                zone=zone,
                after_key=after_key,
            )
            candidates.extend(calendar_candidates)
            provider_has_more = provider_has_more or calendar_has_more

        candidates.sort(key=lambda event: _event_sort_key(event, zone))
        results = candidates[:limit]
        next_cursor = ""
        if results and (len(candidates) > limit or provider_has_more):
            last = results[-1]
            last_key = _event_sort_key(last, zone)
            next_cursor = _encode_cursor(
                after_start=last_key[0],
                after_all_day=last.all_day,
                after_calendar_id=last.calendar_id,
                after_event_id=last.id,
                time_min=lower,
                time_max=upper,
                time_zone=zone.key,
                calendar_signature=calendar_signature,
                fingerprint=fingerprint,
            )
        return results, next_cursor

    async def _calendar_candidates(
        self,
        *,
        calendar: CalendarInfo,
        query: str,
        limit: int,
        time_min: datetime,
        time_max: datetime,
        zone: ZoneInfo,
        after_key: tuple[datetime, int, str, str] | None,
    ) -> tuple[list[CalendarSearchEvent], bool]:
        """Collect enough ordered candidates from one calendar for a global page.

        Google orders events by start time ascending, so all events sharing a
        start instant are returned contiguously, but their order *within* that
        instant is unspecified and may split across provider pages. Because the
        global page is cut by the deterministic ``_event_sort_key`` (which breaks
        same-instant ties by ``event_id``), we must never truncate in the
        provider's arbitrary in-page order: doing so can strand a same-instant
        event with a smaller key below the emitted cursor, losing it silently.
        Instead we keep paging until the instant at the ``limit``-th ordered
        candidate is provably drained (a strictly later start has been seen) or
        the provider is exhausted, then sort and cut deterministically.
        """
        results: list[CalendarSearchEvent] = []
        seen_events: set[tuple[str, str]] = set()
        max_start_seen: datetime | None = None
        page_token = ""
        seen_tokens: set[str] = set()
        for _page_number in range(_MAX_PROVIDER_PAGES_PER_CALENDAR):
            params = {
                "maxResults": str(limit),
                "singleEvents": "true",
                "orderBy": "startTime",
                "timeMin": _rfc3339(time_min),
                "timeMax": _rfc3339(time_max),
            }
            if query.strip():
                params["q"] = query.strip()
            if page_token:
                params["pageToken"] = page_token
            payload = await self._get_json(
                f"/calendars/{_path_segment(calendar.id)}/events",
                params=params,
                operation="event search",
            )
            items = payload.get("items", [])
            if not isinstance(items, list):
                items = []
            if len(items) > limit:
                raise CalendarUpstreamError(
                    operation="event search",
                    message="provider returned more events than requested",
                )
            for item in items:
                if not isinstance(item, dict):
                    continue
                event_metadata, _truncated = _event_metadata(
                    item,
                    calendar_id=calendar.id,
                    calendar_name=calendar.name,
                    max_description_chars=2000,
                    # ``attendee_details`` is the canonical search shape. Keep
                    # the legacy alias field in the schema, but do not repeat
                    # every attendee in high-volume search responses.
                    include_attendee_aliases=False,
                    # Hit size would otherwise grow with the invite list. The
                    # event read keeps every attendee.
                    max_attendee_details=_SEARCH_ATTENDEE_PREVIEW,
                )
                event = _search_event(event_metadata)
                event_identity = (event.id, event.start)
                if event_identity in seen_events:
                    continue
                seen_events.add(event_identity)
                key = _event_sort_key(event, zone)
                # Track the latest start instant seen across every fetched event
                # (even ones filtered out below) to know when a boundary instant
                # has been fully returned by the provider.
                if max_start_seen is None or key[0] > max_start_seen:
                    max_start_seen = key[0]
                if after_key is None or key > after_key:
                    results.append(event)

            next_page_token = _string(payload.get("nextPageToken"))
            if not next_page_token:
                # Provider exhausted: results now holds every event above the
                # cursor for this calendar, so the deterministic top `limit` is
                # final. Truncating is safe and bounds memory.
                results.sort(key=lambda event: _event_sort_key(event, zone))
                return results[:limit], len(results) > limit
            if next_page_token == page_token or next_page_token in seen_tokens:
                raise CalendarUpstreamError(
                    operation="event search",
                    message="provider pagination did not advance",
                )
            seen_tokens.add(next_page_token)
            page_token = next_page_token
            if len(results) >= limit:
                results.sort(key=lambda event: _event_sort_key(event, zone))
                boundary_start = _event_sort_key(results[limit - 1], zone)[0]
                # Only stop once the provider has moved strictly past the
                # boundary instant; otherwise a smaller-keyed same-instant event
                # could still arrive on a later page.
                if max_start_seen is not None and boundary_start < max_start_seen:
                    return results[:limit], True

        raise CalendarUpstreamError(
            operation="event search",
            message=(
                f"provider exceeded {_MAX_PROVIDER_PAGES_PER_CALENDAR} pages while "
                f"continuing calendar {calendar.id!r}; narrow the time window"
            ),
        )

    async def read(
        self,
        calendar_id: str,
        event_id: str,
        max_chars: int,
    ) -> tuple[CalendarEventMetadata, bool]:
        names = {calendar.id: calendar.name for calendar in await self.list_calendars()}
        payload = await self._get_json(
            f"/calendars/{_path_segment(calendar_id)}/events/{_path_segment(event_id)}",
            params={},
            operation="event read",
        )
        return _event_metadata(
            payload,
            calendar_id=calendar_id,
            calendar_name=names.get(calendar_id, ""),
            max_description_chars=max_chars,
        )

    async def _get_json(self, path: str, params: dict[str, str], *, operation: str) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(
                self.api_base_url + path,
                headers={"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"},
                params=params,
            )
        _raise_for_calendar_status(response, operation=operation)
        try:
            payload = response.json()
        except ValueError as exc:
            raise CalendarUpstreamError(
                operation=operation,
                status_code=response.status_code,
                message="provider returned invalid JSON",
            ) from exc
        return payload if isinstance(payload, dict) else {}


def _time_bounds(
    *,
    time_min: datetime | None,
    time_max: datetime | None,
    days_back: int,
    days_ahead: int,
    since_date: date | None,
    until_date: date | None,
    zone: ZoneInfo,
) -> tuple[datetime, datetime]:
    now = datetime.now(zone)
    if (
        time_min is None
        and time_max is None
        and since_date is None
        and until_date is None
        and days_back == 0
        and days_ahead == 0
    ):
        lower = datetime.combine(now.date(), time.min, tzinfo=zone).astimezone(timezone.utc)
        upper = datetime.combine(now.date() + timedelta(days=1), time.min, tzinfo=zone).astimezone(timezone.utc)
        return lower, upper

    if time_min is not None:
        lower = _aware(time_min, zone)
    elif since_date is not None:
        lower = datetime.combine(since_date, time.min, tzinfo=zone).astimezone(timezone.utc)
    else:
        lower = (now - timedelta(days=days_back)).astimezone(timezone.utc)

    if time_max is not None:
        upper = _aware(time_max, zone)
    elif until_date is not None:
        upper = datetime.combine(until_date + timedelta(days=1), time.min, tzinfo=zone).astimezone(timezone.utc)
    else:
        upper = (now + timedelta(days=days_ahead)).astimezone(timezone.utc)
    if upper <= lower:
        if (time_max is not None or until_date is not None) and time_min is None and since_date is None:
            lower = upper - timedelta(days=1)
        else:
            upper = lower + timedelta(days=1)
    return lower, upper


def _event_metadata(
    payload: dict,
    *,
    calendar_id: str,
    calendar_name: str,
    max_description_chars: int,
    include_attendee_aliases: bool = True,
    max_attendee_details: int | None = None,
) -> tuple[CalendarEventMetadata, bool]:
    start_data = payload.get("start") if isinstance(payload.get("start"), dict) else {}
    end_data = payload.get("end") if isinstance(payload.get("end"), dict) else {}
    start = _string(start_data.get("dateTime")) or _string(start_data.get("date"))
    end = _string(end_data.get("dateTime")) or _string(end_data.get("date"))
    description = sanitize_connector_text(_normalize_text(_string(payload.get("description"))))
    truncated = len(description) > max_description_chars
    organizer_data = payload.get("organizer") if isinstance(payload.get("organizer"), dict) else {}
    organizer = _principal(organizer_data)
    attendees_data = payload.get("attendees", [])
    if not isinstance(attendees_data, list):
        attendees_data = []
    attendees = (
        [
            sanitize_connector_text(principal)
            for principal in (_principal(item) for item in attendees_data if isinstance(item, dict))
            if principal
        ]
        if include_attendee_aliases
        else []
    )
    attendee_items = [item for item in attendees_data if isinstance(item, dict)]
    # Counts and the viewer's own status come from the full list, so a capped
    # preview never hides them.
    response_counts = Counter(
        status for status in (_string(item.get("responseStatus")) for item in attendee_items) if status
    )
    self_response_status = next(
        (_string(item.get("responseStatus")) for item in attendee_items if item.get("self") is True),
        "",
    )
    attendee_details_partial = (
        max_attendee_details is not None and len(attendee_items) > max_attendee_details
    )
    attendee_details = [
        CalendarAttendee(
            display_name=sanitize_connector_text(_string(item.get("displayName"))),
            email=sanitize_connector_text(_string(item.get("email"))),
            response_status=_string(item.get("responseStatus")),
            optional=item.get("optional") is True,
            organizer=item.get("organizer") is True,
            self=item.get("self") is True,
        )
        for item in (attendee_items[:max_attendee_details] if attendee_details_partial else attendee_items)
    ]
    original_start_data = (
        payload.get("originalStartTime")
        if isinstance(payload.get("originalStartTime"), dict)
        else {}
    )
    original_start = _string(original_start_data.get("dateTime")) or _string(
        original_start_data.get("date")
    )
    recurrence_data = payload.get("recurrence", [])
    if not isinstance(recurrence_data, list):
        recurrence_data = []
    recurrence = [
        sanitize_connector_text(value)
        for value in recurrence_data
        if isinstance(value, str)
    ]
    attachments_data = payload.get("attachments", [])
    if not isinstance(attachments_data, list):
        attachments_data = []
    attachments = [
        CalendarAttachment(
            title=sanitize_connector_text(_string(item.get("title"))),
            mime_type=_string(item.get("mimeType")),
            file_id=_string(item.get("fileId")),
        )
        for item in attachments_data
        if isinstance(item, dict)
    ]
    return (
        CalendarEventMetadata(
            id=_string(payload.get("id")),
            calendar_id=calendar_id,
            calendar_name=sanitize_connector_text(calendar_name),
            summary=sanitize_connector_text(_string(payload.get("summary"))),
            description=description[:max_description_chars],
            start=start,
            end=end,
            all_day=bool(start_data.get("date") and not start_data.get("dateTime")),
            time_zone=_string(start_data.get("timeZone")) or _string(end_data.get("timeZone")),
            location=sanitize_connector_text(_string(payload.get("location"))),
            status=_string(payload.get("status")),
            organizer=sanitize_connector_text(organizer),
            attendees=attendees,
            attendee_details=attendee_details,
            attendee_details_partial=attendee_details_partial,
            attendee_count=len(attendee_items),
            attendee_response_counts=dict(sorted(response_counts.items())),
            self_response_status=self_response_status,
            recurring_event_id=_string(payload.get("recurringEventId")),
            original_start=original_start,
            recurrence=recurrence,
            attachments=attachments,
            event_type=_string(payload.get("eventType")) or "default",
        ),
        truncated,
    )


def _search_event(event: CalendarEventMetadata) -> CalendarSearchEvent:
    """Project an event onto the search shape.

    The role flags stay on the event read: the organizer and the viewer's own
    status are already top-level fields.
    """
    return CalendarSearchEvent(
        **{
            **dict(event),
            "attendee_details": [
                CalendarAttendeePreview(
                    display_name=attendee.display_name,
                    email=attendee.email,
                    response_status=attendee.response_status,
                )
                for attendee in event.attendee_details
            ],
        }
    )


def _dedupe(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _search_fingerprint(
    *,
    query: str,
    calendar_ids: list[str],
    time_min: datetime | None,
    time_max: datetime | None,
    days_back: int,
    days_ahead: int,
    since_date: date | None,
    until_date: date | None,
    time_zone: str,
) -> str:
    payload = {
        "query": query.strip(),
        "calendar_ids": _dedupe(value.strip() for value in calendar_ids if value.strip()),
        "time_min": time_min.isoformat() if time_min is not None else None,
        "time_max": time_max.isoformat() if time_max is not None else None,
        "days_back": days_back,
        "days_ahead": days_ahead,
        "since_date": since_date.isoformat() if since_date is not None else None,
        "until_date": until_date.isoformat() if until_date is not None else None,
        "time_zone": time_zone.strip(),
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(serialized).hexdigest()


def _encode_cursor(
    *,
    after_start: datetime,
    after_all_day: bool,
    after_calendar_id: str,
    after_event_id: str,
    time_min: datetime,
    time_max: datetime,
    time_zone: str,
    calendar_signature: str,
    fingerprint: str,
) -> str:
    payload = {
        "v": 2,
        "a": _rfc3339(after_start),
        "d": after_all_day,
        "c": after_calendar_id,
        "e": after_event_id,
        "l": _rfc3339(time_min),
        "u": _rfc3339(time_max),
        "z": time_zone,
        "s": calendar_signature,
        "f": fingerprint,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(value: str) -> CalendarCursorState:
    try:
        padded = value + ("=" * (-len(value) % 4))
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        if not isinstance(payload, dict) or payload.get("v") != 2:
            raise ValueError
        after_all_day = payload.get("d")
        after_calendar_id = payload.get("c")
        after_event_id = payload.get("e")
        time_zone_name = payload.get("z")
        calendar_signature = payload.get("s")
        fingerprint = payload.get("f")
        if not isinstance(after_all_day, bool):
            raise ValueError
        if not isinstance(after_calendar_id, str) or not after_calendar_id or len(after_calendar_id) > 1024:
            raise ValueError
        if not isinstance(after_event_id, str) or not after_event_id or len(after_event_id) > 1024:
            raise ValueError
        if not isinstance(time_zone_name, str) or not time_zone_name or len(time_zone_name) > 128:
            raise ValueError
        _time_zone(time_zone_name)
        if not isinstance(calendar_signature, str) or len(calendar_signature) != 64:
            raise ValueError
        if not isinstance(fingerprint, str) or len(fingerprint) != 64:
            raise ValueError
        after_start = _cursor_datetime(payload.get("a"))
        lower = _cursor_datetime(payload.get("l"))
        upper = _cursor_datetime(payload.get("u"))
        if upper <= lower or upper - lower > _MAX_CURSOR_TIME_SPAN:
            raise ValueError
    except (binascii.Error, ValueError, TypeError, UnicodeDecodeError) as exc:
        raise CalendarCursorError("Invalid Google Calendar cursor; restart the search.") from exc
    return CalendarCursorState(
        after_start=after_start,
        after_all_day=after_all_day,
        after_calendar_id=after_calendar_id,
        after_event_id=after_event_id,
        time_min=lower,
        time_max=upper,
        time_zone=time_zone_name,
        calendar_signature=calendar_signature,
        fingerprint=fingerprint,
    )


def _cursor_datetime(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError
    return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _time_zone(value: str) -> ZoneInfo:
    name = value.strip() or "UTC"
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise CalendarSearchError(f"Unknown IANA time_zone {name!r}.") from exc


def _default_calendar_time_zone(calendars: list[CalendarInfo]) -> str:
    for calendar in calendars:
        if calendar.primary and calendar.time_zone:
            return calendar.time_zone
    for calendar in calendars:
        if calendar.time_zone:
            return calendar.time_zone
    return "UTC"


def _calendar_signature(calendars: list[CalendarInfo]) -> str:
    raw = json.dumps(sorted(calendar.id for calendar in calendars), separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _event_sort_key(event: CalendarEventMetadata, zone: ZoneInfo) -> tuple[datetime, int, str, str]:
    if not event.id:
        raise CalendarUpstreamError(
            operation="event search",
            message="provider returned an event without an id",
        )
    try:
        if event.all_day:
            start = datetime.combine(date.fromisoformat(event.start), time.min, tzinfo=zone)
        else:
            start = datetime.fromisoformat(event.start.replace("Z", "+00:00"))
            if start.tzinfo is None:
                start = start.replace(tzinfo=zone)
    except ValueError as exc:
        raise CalendarUpstreamError(
            operation="event search",
            message=f"provider returned invalid event start for {event.id!r}",
        ) from exc
    return (start.astimezone(timezone.utc), 0 if event.all_day else 1, event.calendar_id, event.id)


def _cursor_sort_key(state: CalendarCursorState) -> tuple[datetime, int, str, str]:
    return (
        state.after_start,
        0 if state.after_all_day else 1,
        state.after_calendar_id,
        state.after_event_id,
    )


def _principal(payload: dict) -> str:
    display_name = _string(payload.get("displayName"))
    email = _string(payload.get("email"))
    if display_name and email:
        return f"{display_name} <{email}>"
    return email or display_name


def _aware(value: datetime, default_zone: ZoneInfo | timezone = timezone.utc) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=default_zone).astimezone(timezone.utc)
    return value.astimezone(timezone.utc)


def _rfc3339(value: datetime) -> str:
    return _aware(value).isoformat().replace("+00:00", "Z")


def _normalize_text(value: str) -> str:
    lines = [line.rstrip() for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return "\n".join(lines).strip()


def _path_segment(value: str) -> str:
    return quote(value, safe="")


def _string(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _raise_for_calendar_status(response: httpx.Response, *, operation: str) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise CalendarUpstreamError(
            operation=operation,
            status_code=response.status_code,
            message=_google_error_message(response),
        ) from exc


def _google_error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        # A non-JSON body (e.g. an intermediary proxy's HTML error page) may carry
        # infrastructure detail; never echo it to the caller. Only Google's own
        # structured error.message is surfaced below.
        return ""
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str):
            return message[:240].strip()
    return ""
