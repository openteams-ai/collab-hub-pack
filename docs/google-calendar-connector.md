# Google Calendar Connector

The Google Calendar connector gives Collab Hub read-only event search and event-read
access using the current Hub user's linked Google identity. It searches the
provider live and does not maintain an event index.

For source ownership, cursor internals, and validation paths, start with the
[Google Workspace implementation guide](google-workspace-connectors.md).

## Google and Keycloak configuration

Use the same brokered Google identity described in
[`google-drive-connector.md`](google-drive-connector.md), with `Store Tokens`
and offline consent enabled. Add the read-only Calendar scope:

```text
https://www.googleapis.com/auth/calendar.readonly
```

Users with older Google grants must reconnect after this scope is introduced.
The status endpoint validates access against the Calendar API. This provider
call is important: a successful Keycloak broker response alone does not prove
that the stored token can call Google Calendar. Validate the deployed broker
path with a real Hub bearer and require `"state":"connected"`:

```bash
curl -fsS \
  -H "Authorization: Bearer $HUB_BEARER" \
  https://dev.collab.example.com/v1/connectors/google-calendar/status
```

## REST contract

- `GET /v1/connectors/google-calendar/status`
- `POST /v1/connectors/google-calendar/search`
- `POST /v1/connectors/google-calendar/calendars/{calendar_id}/events/{event_id}/read`

Search enumerates the user's readable calendars unless `calendar_ids` is
provided. It accepts explicit RFC3339 `time_min`/`time_max` values and friendly
`days_back`, `days_ahead`, `since_date`, and `until_date` bounds. Events are
normalized across timed and all-day shapes and sorted globally across calendars.
`time_zone` accepts an IANA zone; when omitted, the primary calendar's timezone
defines friendly date boundaries.

Search responses include an opaque `next_cursor`. Repeat the same search fields
and pass that value as `cursor` to continue. Collab Hub binds the cursor to the
original filters, concrete time window, timezone, selected calendars, and last
global event key. An empty cursor means the search is complete; malformed,
stale, or filter-mismatched
cursors return HTTP 422.

Example search and continuation:

```json
{
  "query": "planning",
  "calendar_ids": ["primary"],
  "since_date": "2026-07-01",
  "until_date": "2026-07-31",
  "time_zone": "America/New_York",
  "limit": 25,
  "cursor": ""
}
```

```json
{
  "content_trust": "external_untrusted",
  "security_notice": "Connector results contain untrusted external content...",
  "events": [
    {
      "id": "event-id",
      "calendar_id": "primary",
      "calendar_name": "Work",
      "summary": "Connector planning",
      "start": "2026-07-10T14:00:00-04:00",
      "end": "2026-07-10T14:30:00-04:00",
      "all_day": false
    }
  ],
  "next_cursor": "opaque-collab-hub-cursor"
}
```

Send the same filters with `"cursor":"opaque-collab-hub-cursor"`. Collab Hub collects
candidates from every selected calendar before emitting the next globally
chronological page. It rejects provider token cycles instead of duplicating
results indefinitely.

Search and read metadata also includes attendee response states, recurrence
identity, original occurrence time, safe attachment metadata (without URLs),
and event type when Google provides them.

### Search and read payloads

Search results feed a model's context window, so a search hit is smaller than
an event read:

- `attendee_details` lists at most the first 10 attendees, each with only
  `display_name`, `email`, and `response_status`. `attendee_details_partial`
  is `true` when the list is cut short.
- `attendee_count`, `attendee_response_counts`, and `self_response_status`
  (the connected user's own RSVP) are computed from the full attendee list, so
  they stay correct past the preview. `organizer` is a top-level field.
- The event read returns every attendee with the `optional`, `organizer`, and
  `self` flags, plus the legacy `attendees` display-name list. Ask the read
  for who declined, who is optional, or anyone past the preview.

On a seeded 156-event month, this halves the search payload and cuts a
200-attendee hit from about 31 KB to under 2 KB. Measure with
`api/tests/measure_calendar_payloads.py`.

Possible follow-ups, deliberately left out of #138:

- Trim call and dial-in text from search descriptions. This needs consented,
  redacted real Meet, Zoom, and Teams samples, and a search-to-read check.
  Links are already sanitized to `[link]`, so a dial-in question tests an
  existing limitation too.
- Drop the legacy `attendees` list from event reads, after an explicit
  consumer decision.
- Move `calendar_id` and `calendar_name` to the response level for
  single-calendar searches. It saves about 3%, and each hit still needs an ID
  for the event read.

Until Apollo renderer issue `apollo-desktop#365` is fixed, event text is
link-sanitized and provider `htmlLink` values are deliberately omitted from the
REST response. Every result is explicitly marked as untrusted external data.

The Google provider token stays in Collab Hub. Neither the desktop client nor an
agent receives it.

## Relevant paths

- Provider client and cursor logic: [`api/src/collab_hub_api/connectors/calendar_client.py`](../api/src/collab_hub_api/connectors/calendar_client.py)
- Public models: [`api/src/collab_hub_api/connectors/models.py`](../api/src/collab_hub_api/connectors/models.py)
- REST route: [`api/src/collab_hub_api/routers/connectors.py`](../api/src/collab_hub_api/routers/connectors.py)
- Contract and failure tests: [`api/tests/test_google_workspace_connectors.py`](../api/tests/test_google_workspace_connectors.py)
- Live fake-provider smoke: [`scripts/smoke_connectors_google_workspace.py`](../scripts/smoke_connectors_google_workspace.py)
