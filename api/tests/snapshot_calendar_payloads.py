"""Write public-safe Calendar connector response snapshots (issue #138).

Run from the api/ folder:
    uv run python tests/snapshot_calendar_payloads.py tests/fixtures/calendar_payloads/after

Serves synthetic Google Calendar data through the real REST routes and saves
each HTTP response body as JSON: one search covering both attendee cases, and
the full event read of each. The Apollo model-contract eval builds its cases
from these, so running this on the code before and after a change gives
matching before/after inputs. Every name and address is synthetic.

Not a pytest test (no ``test_`` prefix), so CI does not collect it.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import httpx
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, str(Path(__file__).parent))
from test_google_workspace_connectors import _auth_header, _config  # noqa: E402

from collab_hub_api.core import make_app  # noqa: E402

SELF = {"displayName": "Casey Viewer", "email": "casey@example.com"}


def _all_hands() -> dict:
    """200 attendees; the viewer's RSVP and the one decline sit past any preview."""
    attendees = [
        {"displayName": f"Person {index:03d}", "email": f"person-{index:03d}@example.com", "responseStatus": "accepted"}
        for index in range(200)
    ]
    attendees[0] |= {"displayName": "Olivia Organizer", "email": "olivia@example.com", "organizer": True}
    for index in (5, 42, 177):
        attendees[index]["optional"] = True
    for index in range(60, 200, 9):
        attendees[index]["responseStatus"] = "needsAction"
    attendees[120] = {"displayName": "Dana Declines", "email": "dana@example.com", "responseStatus": "declined"}
    attendees[150] = {**SELF, "responseStatus": "tentative", "self": True}
    return {
        "id": "all-hands",
        "summary": "October All Hands",
        "description": "Quarterly results, roadmap, and open Q&A.",
        "start": {"dateTime": "2026-10-15T13:00:00-04:00", "timeZone": "America/New_York"},
        "end": {"dateTime": "2026-10-15T14:00:00-04:00", "timeZone": "America/New_York"},
        "location": "Main auditorium",
        "status": "confirmed",
        "organizer": {"email": "olivia@example.com", "displayName": "Olivia Organizer"},
        "attendees": attendees,
        "eventType": "default",
    }


def _team_sync() -> dict:
    """A small meeting, with one optional invitee only the read identifies."""
    names = ["Sam Rivera", "Jordan Lee", "Avery Chen", "Riley Novak", "Quinn Patel", "Rowan Silva", "Drew Kim"]
    attendees = [{**SELF, "responseStatus": "accepted", "organizer": True, "self": True}]
    for name, status in zip(
        names, ["accepted", "accepted", "tentative", "declined", "accepted", "needsAction", "accepted"]
    ):
        attendees.append(
            {"displayName": name, "email": f"{name.split()[0].lower()}@example.com", "responseStatus": status}
        )
    attendees[3]["optional"] = True
    return {
        "id": "team-sync",
        "summary": "Collab Hub Team Sync",
        "description": "Weekly status and blockers.",
        "start": {"dateTime": "2026-10-15T15:00:00-04:00", "timeZone": "America/New_York"},
        "end": {"dateTime": "2026-10-15T15:30:00-04:00", "timeZone": "America/New_York"},
        "status": "confirmed",
        "organizer": {"email": SELF["email"], "displayName": SELF["displayName"]},
        "attendees": attendees,
        "recurringEventId": "team-sync-series",
        "originalStartTime": {"dateTime": "2026-10-15T15:00:00-04:00", "timeZone": "America/New_York"},
        "eventType": "default",
    }


EVENTS = {event["id"]: event for event in (_all_hands(), _team_sync())}


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/users/me/calendarList"):
        return httpx.Response(
            200,
            json={
                "items": [{"id": "primary", "summary": SELF["email"], "timeZone": "America/New_York", "primary": True}]
            },
        )
    if path.endswith("/calendars/primary/events"):
        return httpx.Response(200, json={"items": list(EVENTS.values())})
    for event_id, event in EVENTS.items():
        if path.endswith(f"/calendars/primary/events/{event_id}"):
            return httpx.Response(200, json=event)
    return httpx.Response(404, json={"error": {"message": "not found"}})


async def main(out_dir: Path) -> None:
    os.environ["FRAMES_UNSAFE_AUTH_ENABLED"] = "true"
    os.environ["FRAMES_BEARER_ALLOW_UNSIGNED"] = "true"
    original = httpx.AsyncClient

    def mock_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(_handler)
        return original(*args, **kwargs)

    httpx.AsyncClient = mock_client
    try:
        with tempfile.TemporaryDirectory() as tmp:
            app = make_app(_config(Path(tmp)))
            async with app.router.lifespan_context(app):
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                    responses = {
                        "search.json": await client.post(
                            "/v1/connectors/google-calendar/search",
                            headers=_auth_header(),
                            json={
                                "since_date": "2026-10-15",
                                "until_date": "2026-10-16",
                                "time_zone": "America/New_York",
                            },
                        ),
                    }
                    for event_id in EVENTS:
                        responses[f"read-{event_id}.json"] = await client.post(
                            f"/v1/connectors/google-calendar/calendars/primary/events/{event_id}/read",
                            headers=_auth_header(),
                            json={},
                        )
    finally:
        httpx.AsyncClient = original

    out_dir.mkdir(parents=True, exist_ok=True)
    for name, response in responses.items():
        response.raise_for_status()
        body = json.dumps(response.json(), indent=2) + "\n"
        (out_dir / name).write_text(body, encoding="utf-8", newline="\n")
        print(f"{out_dir / name}: {len(response.content):,} bytes")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: snapshot_calendar_payloads.py OUT_DIR")
    asyncio.run(main(Path(sys.argv[1])))
