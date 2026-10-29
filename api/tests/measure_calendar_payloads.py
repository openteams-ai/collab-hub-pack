"""Measure Google Calendar connector payload sizes on representative fake data (issue #138).

Run from the api/ folder:
    uv run python tests/measure_calendar_payloads.py

Not a pytest test (no ``test_`` prefix), so CI does not collect it. The fake
Calendar data is seeded, so the numbers are identical on every run and the
before/after comparison is fair.
"""

from __future__ import annotations

import asyncio
import json
import random
from datetime import datetime, timedelta, timezone

import httpx

from collab_hub_api.connectors.calendar_client import GoogleCalendarClient
from collab_hub_api.connectors.models import CalendarReadResponse, CalendarSearchResponse

MONTH_START = datetime(2026, 10, 1, tzinfo=timezone.utc)
MONTH_END = datetime(2026, 11, 1, tzinfo=timezone.utc)
MONTH_EVENTS = 156
SELF_EMAIL = "casey@example.org"
ALL_HANDS_ID = "all-hands-oct"
TEAM_SYNC_ID = "team-sync-oct"
DECLINED_ATTENDEE = {"email": "dana.declines@example.org", "displayName": "Dana Declines"}
WORDS = (
    "agenda release review planning roadmap sync standup retro demo customer "
    "helm cluster connector calendar payload budget design notes follow up "
    "the a to and of we should can please check this that it is on for with"
).split()
FIRST = "Alex Sam Jordan Taylor Morgan Casey Riley Jamie Avery Quinn Rowan Drew Parker Reese".split()
LAST = "Garcia Chen Okafor Novak Silva Kim Patel Rossi Muller Haddad Ito Larsen Moreau Adeyemi".split()
VIDEO_BOILERPLATE = (
    "Join with video: https://meet.example.test/abc-defg-hij\n"
    "Or dial: +1 555-0100 PIN: 123456789#\n"
    "More phone numbers: https://tel.meet.example.test/abc-defg-hij?pin=123456789\n"
)


def _text(rng: random.Random, n_chars: int) -> str:
    out: list[str] = []
    size = 0
    while size < n_chars:
        word = rng.choice(WORDS)
        out.append(word)
        size += len(word) + 1
    return " ".join(out)[:n_chars]


def _person(rng: random.Random, i: int) -> dict:
    first, last = rng.choice(FIRST), rng.choice(LAST)
    return {"displayName": f"{first} {last}", "email": f"{first}.{last}{i}@example.org".lower()}


def _attendee_count(rng: random.Random) -> int:
    """Mostly small meetings, some team-sized, a few org-wide invites."""
    roll = rng.random()
    if roll < 0.04:
        return rng.randint(80, 250)
    if roll < 0.20:
        return rng.randint(12, 40)
    if roll < 0.90:
        return rng.randint(2, 10)
    return 0


def _attendees(rng: random.Random, count: int) -> list[dict]:
    if count == 0:
        return []
    organizer = {"email": SELF_EMAIL, "displayName": "Casey Viewer"} if rng.random() < 0.3 else _person(rng, 0)
    attendees = [{**organizer, "organizer": True, "responseStatus": "accepted"}]
    for i in range(1, count):
        attendee = {
            **_person(rng, i),
            "responseStatus": rng.choices(["accepted", "needsAction", "tentative", "declined"], weights=[55, 30, 8, 7])[
                0
            ],
        }
        if rng.random() < 0.1:
            attendee["optional"] = True
        attendees.append(attendee)
    if organizer["email"] != SELF_EMAIL:
        attendees.insert(
            1, {"email": SELF_EMAIL, "displayName": "Casey Viewer", "self": True, "responseStatus": "accepted"}
        )
    else:
        attendees[0]["self"] = True
    return attendees


def _description(rng: random.Random) -> str:
    roll = rng.random()
    if roll < 0.35:
        return ""
    if roll < 0.75:
        return VIDEO_BOILERPLATE
    return _text(rng, rng.randint(200, 2_500)) + "\n\n" + VIDEO_BOILERPLATE


def _event(rng: random.Random, event_id: str, start: datetime, attendee_count: int, summary: str = "") -> dict:
    attendees = _attendees(rng, attendee_count)
    organizer = next(
        (a for a in attendees if a.get("organizer")), {"email": SELF_EMAIL, "displayName": "Casey Viewer"}
    )
    minutes = rng.choice([15, 30, 30, 30, 45, 60, 60, 90])
    item = {
        "kind": "calendar#event",
        "id": event_id,
        "status": "confirmed",
        "htmlLink": f"https://calendar.google.test/event?eid={event_id}",
        "summary": summary or _text(rng, rng.randint(12, 48)).title(),
        "description": _description(rng),
        "start": {"dateTime": start.isoformat(), "timeZone": "America/New_York"},
        "end": {"dateTime": (start + timedelta(minutes=minutes)).isoformat(), "timeZone": "America/New_York"},
        "organizer": {"email": organizer["email"], "displayName": organizer["displayName"]},
        "eventType": "default",
    }
    if rng.random() < 0.3:
        item["location"] = rng.choice(["Room 4B", "HQ - Atlas (12)", "https://meet.example.test/abc-defg-hij"])
    if attendees:
        item["attendees"] = attendees
    if rng.random() < 0.4:
        item["recurringEventId"] = f"series-{rng.randint(1, 20)}"
        item["originalStartTime"] = {"dateTime": start.isoformat(), "timeZone": "America/New_York"}
    if rng.random() < 0.08:
        item["attachments"] = [
            {"title": "Agenda", "mimeType": "application/vnd.google-apps.document", "fileId": f"doc-{event_id}"}
        ]
    return item


def _month(seed: int, calendar: str, count: int) -> list[dict]:
    """``count`` events spread over working hours of the month, oldest first."""
    rng = random.Random(f"{seed}-{calendar}")
    starts = sorted(
        MONTH_START + timedelta(days=rng.randrange(31), hours=rng.randint(13, 21), minutes=rng.choice([0, 15, 30, 45]))
        for _ in range(count)
    )
    return [_event(rng, f"{calendar}-{i:03d}", start, _attendee_count(rng)) for i, start in enumerate(starts)]


def _fixed_events(seed: int) -> list[dict]:
    rng = random.Random(f"{seed}-fixed")
    all_hands = _event(rng, ALL_HANDS_ID, datetime(2026, 10, 15, 17, tzinfo=timezone.utc), 200, "October All Hands")
    # Both answers to "what did I RSVP?" and "who declined?" sit far past any
    # search preview cap, so a capped search cannot answer them by accident.
    attendees = [a for a in all_hands["attendees"] if not a.get("self")]
    for attendee in attendees[1:]:
        if attendee["responseStatus"] == "declined":
            attendee["responseStatus"] = "accepted"
    attendees.insert(120, {**DECLINED_ATTENDEE, "responseStatus": "declined"})
    attendees.insert(
        150, {"email": SELF_EMAIL, "displayName": "Casey Viewer", "self": True, "responseStatus": "tentative"}
    )
    all_hands["attendees"] = attendees
    return [
        all_hands,
        _event(rng, TEAM_SYNC_ID, datetime(2026, 10, 15, 19, tzinfo=timezone.utc), 8, "Collab Hub Team Sync"),
    ]


def _fake_calendar(seed: int):
    calendars = {
        "primary": _month(seed, "primary", MONTH_EVENTS - 2) + _fixed_events(seed),
        "team@group.calendar.google.test": _month(seed, "team", 60),
        "holidays@group.calendar.google.test": _month(seed, "holidays", 4),
    }
    for events in calendars.values():
        events.sort(key=lambda item: (item["start"]["dateTime"], item["id"]))
    names = {
        "primary": "casey@example.org",
        "team@group.calendar.google.test": "Collab Hub Team",
        "holidays@group.calendar.google.test": "Holidays in United States",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = httpx.URL(str(request.url)).path
        params = request.url.params
        if path.endswith("/users/me/calendarList"):
            items = [
                {"id": cid, "summary": names[cid], "timeZone": "America/New_York", "primary": cid == "primary"}
                for cid in calendars
            ]
            return httpx.Response(200, json={"items": items})
        for calendar_id, events in calendars.items():
            prefix = f"/calendars/{calendar_id}/events"
            if path.endswith(prefix):
                lower = datetime.fromisoformat(params["timeMin"])
                upper = datetime.fromisoformat(params["timeMax"])
                pool = [
                    e
                    for e in events
                    if datetime.fromisoformat(e["end"]["dateTime"]) > lower
                    and datetime.fromisoformat(e["start"]["dateTime"]) < upper
                ]
                offset = int(params.get("pageToken") or 0)
                limit = int(params["maxResults"])
                page = pool[offset : offset + limit]
                body: dict = {"items": page}
                if offset + limit < len(pool):
                    body["nextPageToken"] = str(offset + limit)
                return httpx.Response(200, json=body)
            if f"{prefix}/" in path:
                event_id = path.rsplit("/", 1)[-1]
                for event in events:
                    if event["id"] == event_id:
                        return httpx.Response(200, json=event)
        return httpx.Response(404, json={"error": {"message": "not found"}})

    return handler


def _size(model) -> int:
    return len(model.model_dump_json())


def _report(label: str, size: int) -> None:
    print(f"{label:<44} {size:>10,} chars   ~{size // 4:>8,} tokens")


def _attendee_share(response: CalendarSearchResponse) -> str:
    events = json.loads(response.model_dump_json())["events"]
    total = sum(len(json.dumps(e, separators=(",", ":"))) for e in events)
    attendees = sum(
        len(json.dumps(e["attendee_details"], separators=(",", ":")))
        + len(json.dumps(e["attendees"], separators=(",", ":")))
        for e in events
    )
    return f"attendee data {attendees / total:.0%} of event bytes" if total else "no events"


def _self_status(event) -> str:
    """The viewer's RSVP as a consumer can read it off one event."""
    explicit = getattr(event, "self_response_status", "")
    return explicit or next((a.response_status for a in event.attendee_details if a.self), "")


async def _search_all(
    client: GoogleCalendarClient, *, limit: int, calendar_ids: list[str]
) -> list[CalendarSearchResponse]:
    pages: list[CalendarSearchResponse] = []
    cursor = ""
    while True:
        events, cursor = await client.search(
            query="",
            limit=limit,
            calendar_ids=calendar_ids,
            time_min=MONTH_START,
            time_max=MONTH_END,
            cursor=cursor,
        )
        pages.append(CalendarSearchResponse(events=events, next_cursor=cursor))
        if not cursor:
            return pages


async def main() -> None:
    original = httpx.AsyncClient

    def mock_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(_fake_calendar(seed=138))
        return original(*args, **kwargs)

    httpx.AsyncClient = mock_client
    try:
        client = GoogleCalendarClient(access_token="token", api_base_url="https://google.test/calendar/v3")

        for limit in (25, 100):
            page = (await _search_all(client, limit=limit, calendar_ids=["primary"]))[0]
            _report(f"search, first {limit} hits (primary)", _size(page))
            print(f"  -> {_attendee_share(page)}")

        pages = await _search_all(client, limit=100, calendar_ids=["primary"])
        ids = [event.id for page in pages for event in page.events]
        complete = len(ids) == MONTH_EVENTS and len(set(ids)) == MONTH_EVENTS
        _report(f"search, whole month ({MONTH_EVENTS} events)", sum(_size(p) for p in pages))
        print(
            f"  -> {len(pages)} page(s), largest {max(_size(p) for p in pages):,} chars, "
            f"{'no gaps or repeats' if complete else f'PROBLEM: got {len(ids)} ({len(set(ids))} unique)'}"
        )

        pages = await _search_all(
            client,
            limit=100,
            calendar_ids=["primary", "team@group.calendar.google.test", "holidays@group.calendar.google.test"],
        )
        total = sum(len(p.events) for p in pages)
        _report(f"search, month on 3 calendars ({total} events)", sum(_size(p) for p in pages))

        month = await _search_all(client, limit=100, calendar_ids=["primary"])
        for event_id, label in ((ALL_HANDS_ID, "200 attendees"), (TEAM_SYNC_ID, "8 attendees")):
            hit = next(e for p in month for e in p.events if e.id == event_id)
            _report(f"search, one hit with {label}", _size(hit))
            if event_id == ALL_HANDS_ID:
                print(f"  -> my RSVP from the search hit: {_self_status(hit) or 'MISSING'}")

        for event_id, label in ((ALL_HANDS_ID, "200 attendees"), (TEAM_SYNC_ID, "8 attendees")):
            event, truncated = await client.read("primary", event_id, 12_000)
            _report(f"read, event with {label}", _size(CalendarReadResponse(event=event, truncated=truncated)))
            if event_id == ALL_HANDS_ID:
                declined = [a.display_name for a in event.attendee_details if a.response_status == "declined"]
                found = DECLINED_ATTENDEE["displayName"] in declined
                print(f"  -> declined per read: {len(declined)}, {'includes' if found else 'MISSING'} Dana Declines")
    finally:
        httpx.AsyncClient = original


if __name__ == "__main__":
    asyncio.run(main())
