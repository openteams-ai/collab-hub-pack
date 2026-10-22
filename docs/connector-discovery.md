# Connector discovery

`GET /v1/connectors` tells a client which connectors this hub offers, what to
call them, and how a user connects each one. A client that renders from this
response needs no connector list of its own, so the deployment decides what its
users see: configure a provider and its connectors appear, switch it off and
they go, with no client release.

The route takes the same bearer token as every other `/v1/connectors` route.

## What is listed

A connector is listed when both hold:

- its provider has credentials in the deployment configuration
  (`connectors.<provider>.brokerTokenUrl`, or a static token in development);
- an operator has not switched the provider off in the admin panel (see
  [Frames operations](frames-operations.md)). The panel switches `google`,
  `slack` and `github`; `notion` has no switch and is listed whenever it is
  configured.

`[]` means this hub offers no connectors. It is an answer, not an error.

Connectors come back in a fixed display order: Google Drive, Gmail, Google
Calendar, Slack, GitHub, Notion.

## Entry fields

```json
{
  "id": "google-drive",
  "name": "Google Drive",
  "short_name": "Drive",
  "description": "Search and read files from the connected Google Drive.",
  "provider": "google",
  "link": {"type": "identity_provider", "alias": "google", "prompt": "consent"},
  "connect_hint": null,
  "connected": false,
  "state": "not_connected",
  "scopes": [],
  "detail": "Google account is not linked",
  "account": ""
}
```

| Field | Meaning |
| --- | --- |
| `id` | Stable identifier, and the path segment of the connector's own routes (`/v1/connectors/<id>/...`). |
| `name` | Display name. |
| `short_name` | Compact label for menus. |
| `description` | One sentence on what the connector lets an assistant do. Safe to show a user. |
| `provider` | The configuration section the connector belongs to, which is also its operator switch where it has one: `google`, `slack`, `github` or `notion`. Connectors that share a provider share one linked account. |
| `link` | How a user connects it, or `null` when there is nothing for the user to do (a static token serves every caller). |
| `connect_hint` | A sentence the user should read before they start connecting, or `null`. Slack uses it to say the workspace is chosen on Slack's own screen. Show it next to the connect action, not after a failure. |
| `connected`, `state`, `scopes`, `detail` | The caller's connection state, as the connector's own status route reports it. `state` is one of `connected`, `not_connected`, `reconnect_required`, `unavailable`. |
| `account` | The linked account or workspace where the connector knows it (GitHub login, Notion workspace name), otherwise `""`. |

### `link`

`type` is `identity_provider` today: the client asks the hub's identity
service to link the provider named by `alias` to the signed-in account
(Keycloak client-initiated account linking). `alias` is read from the
configured broker URL (`.../broker/<alias>/token`), so a realm that named its
identity provider differently is described as it is. A broker URL that is not
shaped that way does not name the alias, and the provider key is used. An alias
outside letters, digits, `.`, `_` and `-` (starting with a letter or digit, at
most 64 characters) is not one a client will link, so the entry carries
`link: null` rather than a guess. `prompt`, when set, is the
OIDC `prompt` value the link request must carry; the Google connectors set
`consent` because they widen one linked identity's scopes and Google only
re-asks when told to.

## Compatibility rules for clients

- Fields are added over time. Ignore the ones you do not know.
- Do not offer a connect action for a `link.type` you do not know.
- Show only what is listed. Do not probe a status route to decide whether a
  connector exists.
- One connector's trouble does not fail the list: a connector whose status
  read fails outright is listed with `state: "unavailable"` and a `detail`.
  Each connector otherwise reports provider trouble as its own status route
  does.

## What this route does not change

The per-connector status, search and read routes answer as before.

A connector missing from this list therefore gets one of two answers on its
own status route, depending on why it is missing: `404` if an operator switched
its provider off, and `200` with `state: "not_connected"` if the deployment
never configured it. Both are deliberate. The 404 is what clients that predate
this list read as "not offered"; the 200 is unchanged so that a deployment that
never touched a switch sees no difference. A client that renders from this list
needs neither: it does not ask about a connector that is not listed.

Because those older clients read the status routes and not this list, a
connector that was never configured, and Notion in particular (it has no
switch), still shows them a connect action that cannot succeed. Only a client
that renders from this list stops showing it.
