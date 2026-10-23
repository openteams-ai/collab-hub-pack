# Calendar connector payload snapshots (issue #138)

Public-safe HTTP response bodies from the Google Calendar connector, for the
Apollo model-contract eval to compare answers before and after the search
payload change. Every name and address is synthetic.

- `before/`: the code on `main` before #138.
- `after/`: the code with #138 applied.

Each folder holds:

- `search.json`: one search (`events`, `next_cursor`) that returns both cases.
- `read-all-hands.json`: the full event read (`event`, `truncated`) of a
  200-attendee event. The viewer's RSVP (`tentative`) is attendee 151 and the
  only decline (Dana Declines) is attendee 121, both past the search preview.
- `read-team-sync.json`: the full read of an 8-attendee event with one optional
  invitee (Avery Chen), which only the read identifies.

Regenerate from `api/` with:

```
uv run python tests/snapshot_calendar_payloads.py tests/fixtures/calendar_payloads/after
```

Run the same command with the pre-change code checked out, writing to `before`,
to refresh the baseline. Payload sizes over a full 156-event month come from
`tests/measure_calendar_payloads.py`, not from these snapshots.
