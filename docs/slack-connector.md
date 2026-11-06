# Slack Connector

The Slack connector lets Collab Hub list, search, and read a user's Slack
conversations with that user's linked Slack identity. The connector is
read-only — it only ever requests read scopes and has no code path that posts
to Slack — and it covers public channels and private channels that the linked
user can access. Direct messages (`im`) and group direct messages (`mpim`) are
intentionally not requested for the approved Collab app; teams should move
shareable work into private channels instead.

## Requirements

> **Token type — the one setting that silently breaks reads.** The connector
> calls the Slack **Web API** (`conversations.history`, `search.messages`, …),
> which only accepts a Slack **user token** (`xoxp-…`) carrying the read scopes
> below. A plain "Sign in with Slack" / OpenID Connect login brokers an
> **identity token** (`openid profile email`) instead: linking succeeds, the
> connector shows `connected`, and then *every* read fails with `invalid_auth` /
> `not_authed`. Keycloak must therefore broker the Slack **user access token**,
> not the sign-in/identity token. The connector now guards against this — see
> [Verifying the brokered token](#verifying-the-brokered-token) — but the IdP
> must be configured to broker the right token in the first place.

Configure a Slack identity provider in the hub's Keycloak realm with alias
`slack` and:

- `Store Tokens` enabled (so `/broker/slack/token` can return the stored token).
- Read-only **User Token Scopes** (Slack calls these user scopes; they must be
  requested as `user_scope`, not bot scopes):
  - `channels:read`, `channels:history`
  - `groups:read`, `groups:history`
  - `search:read`
- The IdP must broker Slack's **user** token. Slack's `oauth.v2.access` returns
  the user token nested under `authed_user.access_token` (an `xoxp-…` value),
  *not* at the top level (the top-level `access_token` is a bot token or empty),
  so the IdP/mapper must store that nested user token as the brokered access
  token. If Keycloak stores the top-level/OIDC token instead, reads fail.
- A redirect URI matching the Keycloak broker endpoint:
  `https://keycloak.<hub-host>/realms/<realm>/broker/slack/endpoint`.

Configure the Collab Hub pack with the Keycloak broker token endpoint:

```yaml
connectors:
  slack:
    brokerTokenUrl: http://<keycloak-service>.<namespace>.svc.cluster.local:8080/realms/<realm>/broker/slack/token
```

Keycloak must allow normal hub users to read their own linked broker tokens.
Grant the `broker` client role `read-token` to the role or group that every
normal hub user receives, exactly as for the Google Drive connector — see
[docs/google-drive-connector.md](google-drive-connector.md).

## Endpoints

- `GET /v1/connectors/slack/status`
- `GET /v1/connectors/slack/channels`
- `POST /v1/connectors/slack/search`
- `POST /v1/connectors/slack/channels/{channel_id}/read`
- `POST /v1/connectors/slack/channels/{channel_id}/threads/{message_ts}/read`

The channel listing and both read endpoints are cursor-paginated. The two read
endpoints return `has_more` and `next_cursor`; to continue a long history or
thread, send `next_cursor` back as the `cursor` field on the next `read`
request until `has_more` is no longer `true`. The channel listing returns
`next_cursor` likewise and accepts it via the `cursor` query parameter.

Payloads are kept small for the model (#139):

- Search hits carry at most the first 500 characters of each message and set
  `truncated: true` when the text was cut. Read the message by its `ts` for the
  full text: for a thread reply (the hit has `thread_ts`), use the thread read
  with the hit's `thread_ts` as `message_ts` (a long thread may take a few
  pages), since the channel read only returns replies that were also posted to
  the channel; otherwise use the channel read with `oldest` = `latest` = `ts`.
- The read endpoints accept `max_chars` (default 12,000, max 50,000). A page
  stops before the message that would go over it, never cutting a message, and
  returns `has_more: true` with a `next_cursor` that resumes exactly there
  (`ts:<ts>`, or `ts:<ts>:<oldest>` for a time-windowed read). A read's time
  window (`oldest`, `days_back` or `since_date`) is carried in `next_cursor`,
  so later pages stay inside it; Slack's own cursor is wrapped as
  `slack:<oldest>:<cursor>` for that. These windowed cursors come only from the
  channel read; a thread read only hands out `ts:<ts>`. If a request also sends its own `oldest`,
  that one is used. Treat `next_cursor` as opaque and send it back unchanged; a
  malformed `ts:` or `slack:` cursor is rejected with a 422.
- Messages in a read don't repeat `channel_id`; it is set once on the response.
- Fields that hold their default value (`false`, `""`, `0` or `null`), such as
  `is_im`, `thread_ts`, `truncated`, `has_more`, `next_cursor` and `next_page`,
  are left out. `content_trust` and `security_notice` are always sent.

Slack search is invoked with the linked user's token, but Collab Hub drops any
result Slack marks as `im` or `mpim` before returning the response. The
approved app configuration also omits `im:*` and `mpim:*` scopes, so direct
message history cannot be listed or read by the connector.

The channel `read` endpoint also accepts friendly time-range fields so a
caller (for example an LLM) need not hand-format Slack's `epoch.micros`
timestamps: `days_back` (integer days back from now), `since_date`, and
`until_date` (ISO `YYYY-MM-DD`, interpreted in UTC). They convert server-side
into `oldest`/`latest`; an explicitly supplied `oldest`/`latest` still wins.
`days_back` and `since_date` both set the window start and are mutually
exclusive, while `until_date` sets the end.

## Runtime Boundary

The user's Slack access token stays hub-side. Apollo Desktop sends the user's
Hub bearer token to Collab Hub through its local proxy; Collab Hub uses that token to ask
Keycloak for the current user's Slack broker token and then calls the Slack Web
API. The model and desktop client do not receive the Slack access token.

## Deployment Checklist

The connector is code-complete and tested (unit tests, fake-broker and
fake-Slack HTTP path tests, brokered-token validation via `auth.test`, config
permutations, malformed-upstream and timeout resilience, and Helm schema
validation — see `api/tests/test_connectors.py` and the CI kind smoke job).
Cutting it over to a real workspace is a configuration exercise, not a code
change — the one thing to get right is the brokered **token type** (see the
Requirements callout above and [Verifying the brokered token](#verifying-the-brokered-token)).
Two people are involved:

### From whoever administers the target Slack workspace

1. Create a Slack app at <https://api.slack.com/apps> (or reuse an existing
   internal one) scoped to the workspace(s) that should be searchable.
2. Under **OAuth & Permissions → User Token Scopes**, add exactly:
   `channels:read`, `channels:history`, `groups:read`, `groups:history`,
   `search:read`. Do not add `im:*`, `mpim:*`, bot scopes, or write/post
   scopes — the approved Collab app is not authorized to read direct messages.
3. Add the redirect URL Keycloak will present:
   `https://keycloak.<hub-host>/realms/<realm>/broker/slack/endpoint`.
4. Hand the app's **Client ID** and **Client Secret** to whoever configures
   Keycloak (step 1 below). Collab Hub itself never sees these — they go into the
   Keycloak identity provider, not into Collab Hub's Helm values.

### From whoever administers the Hub (Keycloak + Collab Hub deploy)

1. In the hub's Keycloak realm, add a Slack identity provider with
   **alias `slack`** (must match exactly — Apollo's `SlackConnectorLogin`
   hardcodes this alias when it starts the linking flow) using the Slack
   app's Client ID/Secret from above. Enable `Store Tokens`. Configure it to
   request the read **user scopes** (`user_scope`) listed in Requirements and
   to broker Slack's **user** access token (`authed_user.access_token`,
   `xoxp-…`), not a bare OpenID sign-in token — this is the single most common
   misconfiguration and the connector will report `reconnect_required` until
   it is fixed (see [Verifying the brokered token](#verifying-the-brokered-token)).
2. Grant the `broker` client's `read-token` role to the role/group every
   normal hub user receives (same step already done for Google Drive; see
   [docs/google-drive-connector.md](google-drive-connector.md)). Without
   this, the connector reports `unavailable` with a message naming the
   missing role — verified in this repo's live broker tests.
3. Set the Helm value:
   ```yaml
   connectors:
     slack:
       brokerTokenUrl: http://<keycloak-service>.<namespace>.svc.cluster.local:8080/realms/<realm>/broker/slack/token
   ```
   `apiBaseUrl` and `requestTimeoutSeconds` have working defaults
   (`https://slack.com/api`, `10`) — only override `apiBaseUrl` for
   non-production Slack API mirrors/tests. Do **not** also set
   `staticAccessToken` in production Helm values — it exists for local
   development and CI, and it takes precedence over the broker token when
   both are set (verified in the config matrix), which would silently
   bypass per-user Keycloak brokering for every user in the deployment.
4. Deploy the built image containing this connector (the currently deployed
   dev-hub image predates `/v1/connectors` entirely, including the merged
   Google Drive connector — that gap is tracked separately from this PR).
5. No changes are needed on the Apollo Desktop side. It already derives the
   connectors API URL from the signed-in Hub session; `SlackConnectorLogin`
   and the chat tools work unmodified once Collab Hub reports `connected`. The
   `APOLLO_CONNECTORS_API_BASE` environment variable in `app_hubauth.go` is
   a local-development-only override for pointing at a non-deployed Collab Hub
   instance — it must not be set in any built/shipped configuration.

### Verifying the brokered token

Do this once, with a single linked test user, before announcing the connector.
It is the concrete broker-path validation that the fake-Slack tests cannot cover.

1. **Automatic check.** `GET /v1/connectors/slack/status` now validates the
   brokered token against Slack's `auth.test` before reporting `connected`:
   - `connected: true` with populated `scopes` — the broker returned a usable
     Slack Web API user token. (The reported `scopes` come from Slack's
     `x-oauth-scopes` response header, i.e. what the token was *actually*
     granted — confirm `search:read` and the `*:history` scopes are present.)
   - `state: "reconnect_required"` with a detail naming the `auth.test` error
     (e.g. `invalid_auth`) — Keycloak brokered an identity/sign-in token, not a
     user token. Fix the Slack app User Token Scopes and the Keycloak IdP so it
     brokers `authed_user.access_token`, then reconnect.

2. **Manual cross-check** (optional, proves it independent of Collab Hub). Read one
   user's brokered token straight from Keycloak and call `auth.test`:

   ```bash
   # hub_token = a normal user's Hub bearer token
   TOKEN=$(curl -s -H "Authorization: Bearer ${hub_token}" \
     "https://keycloak.<hub-host>/realms/<realm>/broker/slack/token" \
     | python -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')

   # Expect: {"ok":true, ...}. A leading "xoxp-" and ok:true means a real user
   # token; {"ok":false,"error":"invalid_auth"} means an identity token.
   curl -s -H "Authorization: Bearer ${TOKEN}" https://slack.com/api/auth.test
   ```

   The token should begin with `xoxp-` and `auth.test` should return
   `ok: true`. If it returns `invalid_auth` / `not_authed`, the IdP is brokering
   the wrong token type — revisit the Requirements callout.

### Rollback

Unsetting `connectors.slack.brokerTokenUrl` (or removing the Keycloak IdP)
returns the connector to `not_connected` cleanly — no restart loop, no
partial state — since Collab Hub resolves the token fresh on every request rather
than caching a session.
