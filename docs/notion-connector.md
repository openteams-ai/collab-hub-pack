# Notion Connector

The Notion connector lets Collab Hub search and read a user's Notion pages and
databases with that user's linked Notion workspace. It is **read-only** — it only
ever calls Notion read endpoints and has no code path that writes to Notion.

## Requirements

> **Capability — the security fact that must be signed off, not discovered.**
> Notion sets read/write capability **on the integration, in Notion's developer
> portal**, not via per-request OAuth `scope` strings. "Read-only" means the
> public integration is configured with the **Read content** capability only and
> **Insert content / Update content are left disabled**. Read-only is therefore a
> *configuration* guarantee at the integration level, reinforced in Collab Hub
> client code (only read methods exist, no write verb is ever constructed, and
> tests assert this at every layer). Enabling any write capability on the
> integration "just in case" violates the standing read-only contract.

> **Notion is not an OIDC provider.** Unlike GitHub/Google/Slack it has no OIDC
> discovery document, no `id_token`, and no standard `userinfo`. It issues a
> **workspace-scoped bot token** (returned alongside `bot_id`, `workspace_id`,
> `workspace_name`, `owner`), **non-expiring with no refresh token**. This shapes
> how the token is brokered (below).

### Token path — Keycloak generic-OAuth broker

The per-user Notion token is brokered by Keycloak. Configure a Keycloak identity
provider using the **generic OAuth2** type (not the built-in OIDC social type),
`Store Tokens` on, pointed at `https://api.notion.com/v1/oauth/authorize` +
`/oauth/token`. `notion_tokens.py` fetches the stored bot token from the broker's
token endpoint exactly like `github_tokens.py`; no hub-side OAuth flow or token
store is involved.

This was proven end-to-end against a live Notion integration (2026-09-14):
account-console link → federated identity stored (bot id + token) → the Hub
fetched the `ntn_` token from the broker with the user's own bearer → that token
read workspace content from `api.notion.com`. No custom Keycloak SPI and no
hub-side shim were needed.

**IdP contract the connector relies on:**

- **Alias `notion`.** `linkOnly` and `hideOnLogin` are on — it is a link-only
  flow, never a login option.
- `storeToken` on, so the Hub fetches the bot token from the broker's internal
  token URL, `https://keycloak.<hub-host>/realms/<realm>/broker/notion/token`,
  called with the user's own access token. Users hold the broker `read-token`
  role. This URL is the `connectors.notion.brokerTokenUrl` value.
- The federated user id Keycloak stores is the **Notion bot id** (the top-level
  `id` from `GET /v1/users/me`) — per-integration-per-workspace, **not** a
  person's Notion user id. Treat it as a stable per-link workspace identifier; it
  changes if the workspace connection is removed and re-made.

**Token lifecycle.** The bot token is non-expiring and carries no refresh token,
so the connector builds **no** refresh handling. It handles **revocation**
instead: once the workspace connection is removed, Notion returns `401
unauthorized` and the connector surfaces `reconnect_required` (the Slack
precedent).

**userinfo is solved at the gateway, not in hub code.** Notion exposes no
standard `userinfo` endpoint, which the generic broker needs to complete a link.
That gap is closed at the deployment's Envoy Gateway with a proxy route that
injects the required `Notion-Version` header, rewrites the broker's `POST` to the
`GET`-only `/v1/users/me`, and performs the host rewrite in Lua (a route-level
host rewrite appends `X-Forwarded-Host`, which Notion's URL validation rejects as
`invalid_request_url`). No hub-side userinfo shim exists or is needed.

### Creating the public Notion integration

1. In Notion's developer portal create a **public** integration.
2. Under **Capabilities**, enable **Read content** only. Leave **Insert content**
   and **Update content** disabled.
3. Set the **redirect URI** to the Keycloak broker endpoint,
   `https://keycloak.<hub-host>/realms/<realm>/broker/notion/endpoint`.
4. Note that the integration only ever sees pages/databases a workspace member
   explicitly shares with it. The bot token is **workspace-scoped**, not
   user-scoped: it grants access to exactly the content that workspace shared.

### Pinned API version

Every Notion request carries `Notion-Version: 2022-06-28`. This is pinned in
config (`notionVersion`) and sent by `notion_client._request`; **do not float
it**. Newer versions (2025-09-03+) split databases into *data sources* and move
the query endpoint — bumping the pin is a code migration, not a config change.

## Hub Helm values

```yaml
connectors:
  notion:
    brokerTokenUrl: ""            # Keycloak broker token endpoint (see IdP contract); empty = fail-closed
    apiBaseUrl: https://api.notion.com
    notionVersion: "2022-06-28"
    requestTimeoutSeconds: 10
    userinfoProxy:                # the gateway route the `notion` IdP needs (Envoy Gateway only)
      enabled: false
      host: ""                    # this hub's Keycloak host, e.g. keycloak.example.com
      namespace: ""               # empty = release namespace
      parentRefs:
        - name: nebari-gateway
          namespace: envoy-gateway-system
          sectionName: https
```

The connector values map to `COLLAB_HUB_API__CONNECTORS__NOTION__*` env vars in
the API deployment. `brokerTokenUrl` is the only value that must be set for the
connector to work; left empty, the connector reports `not_connected` and stays
fail-closed.

`userinfoProxy` renders the gateway route described under the token path
(`templates/notion-userinfo-proxy.yaml`), so a hub no longer copies it by hand.
It sends the same `notionVersion` the connector pins. The render fails when
`host` is empty, or when the cluster has no Envoy Gateway `Backend` CRD; the
`Backend` kind also needs `extensionApis.enableBackend: true` in the Envoy
Gateway config, which a render cannot check. The render also fails without the
`EnvoyExtensionPolicy` CRD or any `BackendTLSPolicy` (v1 or v1alpha3); an offline
`helm template` needs `--api-versions` for each.

The route needs a gateway listener that admits both of its hostnames: the
Keycloak host, and `api.notion.com` (Envoy re-matches the route after the Lua
host rewrite). A listener with no `hostname`, like the Nebari gateway's `https`,
admits both. A listener restricted to a hostname or wildcard silently drops
`api.notion.com`, and a dedicated listener for the Keycloak host takes that
host's traffic from the generic one; in either case point `parentRefs` at a
listener that admits both, or links fail at the userinfo step. The render cannot
see listener hostnames, so this one is on the operator.

A hub that already carries a hand-written copy of this route (for example from
its cluster repository) must remove it when it turns `userinfoProxy` on: two
routes claiming the same host and `/notion-userinfo` path conflict.

## Turning Notion on for a hub

The chart does not hold Keycloak admin access, so the identity provider is the
one step an operator runs by hand. In order:

1. **Create the Notion integration** (see above): public, installation scope
   **Any workspace** (it cannot be changed later), **Read content** only,
   redirect URI `https://<keycloak-host>/realms/<realm>/broker/notion/endpoint`.
   Each hub needs its own integration, or its redirect URI added to one the same
   operator owns: the client secret must never leave the operator that holds it.
2. **Turn on the proxy**: `connectors.notion.userinfoProxy.enabled: true` and
   `host: <keycloak-host>`. Deploy.
3. **Create the `notion` identity provider** in the hub's realm, with Keycloak
   26.3.0 or later (the generic `oauth2` broker is stock from then):

   ```sh
   # NOTION_CLIENT_ID / NOTION_CLIENT_SECRET come from the integration's
   # Configuration tab; never paste them into a ticket or chat. The JSON is
   # piped straight in, so the secret is never written to disk. Values are
   # interpolated unescaped: a secret containing " or \ needs escaping first.
   kcadm.sh create identity-provider/instances -r <realm> -f - <<EOF
   {
     "alias": "notion",
     "providerId": "oauth2",
     "enabled": true,
     "trustEmail": false,
     "storeToken": true,
     "linkOnly": true,
     "hideOnLogin": true,
     "config": {
       "clientId": "$NOTION_CLIENT_ID",
       "clientSecret": "$NOTION_CLIENT_SECRET",
       "authorizationUrl": "https://api.notion.com/v1/oauth/authorize?owner=user",
       "tokenUrl": "https://api.notion.com/v1/oauth/token",
       "userInfoUrl": "https://<keycloak-host>/notion-userinfo",
       "clientAuthMethod": "client_secret_basic",
       "userIDClaim": "id",
       "userNameClaim": "id",
       "fullNameClaim": "name",
       "defaultScope": "",
       "syncMode": "IMPORT",
       "showInAccountConsole": "ALWAYS"
     }
   }
   EOF
   ```

   `linkOnly` is what keeps Notion from becoming a way to sign in or create an
   account; keep it, and `storeToken`, true. `clientAuthMethod` must stay
   `client_secret_basic` (Notion's token endpoint accepts only HTTP Basic).
   Hubs that manage the realm from a setup job put the same JSON there instead,
   gated on a Secret: see openteams-ai/collab.openteams.app
   `clusters/collab-hub/manifests/keycloak/realm-setup-job.yaml`.
4. **Check the provider exists**:
   `curl -s https://<keycloak-host>/realms/<realm>/broker/notion/endpoint` must no
   longer answer `Identity Provider [notion] not found.`
5. **Offer the connector**: set `connectors.notion.brokerTokenUrl` to the broker
   token endpoint and deploy. This is the switch that lists Notion in
   `GET /v1/connectors`, so it goes last; set earlier, users see Notion as
   "reconnect required". Normal users need the broker `read-token` role.

## Endpoints

All are mounted under `/v1/connectors/notion` and require a Hub bearer; the
desktop reaches them through the authenticated loopback proxy with no credentials.

| Method | Path | Purpose |
| --- | --- | --- |
| GET  | `/status` | Capability probe: `GET /v1/users/me` **then** a bounded `POST /v1/search` (page_size 1). A token that brokers but cannot read reports `reconnect_required`, not `connected`. |
| POST | `/search` | Search pages + databases. Friendly date fields are resolved **server-side** (Notion search only *sorts* by `last_edited_time`). |
| POST | `/pages/{page_id}/read` | Read a page's title + assembled block text, bounded by `max_chars`. |
| POST | `/databases/{database_id}/query` | Query a database; friendly date fields become a **native** Notion timestamp filter on `last_edited_time`. |

Error mapping: `409` not connected / reconnect, `422` bad id / stale cursor /
bad params, `502` provider failure (timeout, non-2xx, bad JSON, cursor cycle),
`503` connector unconfigured. Provider error bodies are never forwarded verbatim.

## Runtime boundary

The provider token and raw Notion payloads never enter the desktop runtime. The
Hub exchanges the Hub bearer for the workspace bot token, calls Notion, and
returns a bounded, sanitized, untrusted-marked response. Every data-bearing
response carries `content_trust: "external_untrusted"`. Page/database `url` and
rich-text `href` fields are **deliberately dropped** — link-shaped text crashes
the desktop chat renderer.

## Deployment checklist

**Workspace admin (Notion side)**
- [ ] Create the public integration with **Read content** capability only.
- [ ] Set the redirect URI to the Keycloak broker endpoint.
- [ ] Share the pages/databases the integration should see with it.

**Hub admin (Collab side)**
- [ ] Configure the Keycloak `notion` generic-OAuth IdP with `Store Tokens` on,
      `Link-only` on, and `Hide on Login Page` on; grant normal users the broker
      `read-token` role.
- [ ] Turn on `connectors.notion.userinfoProxy` (or provide an equivalent
      gateway route) so links can complete.
- [ ] Set `connectors.notion.brokerTokenUrl` to the broker token endpoint.
- [ ] Set `notionVersion` and `apiBaseUrl` (defaults are correct for prod).

## Verification recipe (run once with a real linked workspace)

1. Link a Notion workspace through the desktop **Connect** action.
2. `GET /v1/connectors/notion/status` → `connected: true`, `account` = workspace name.
3. `POST /v1/connectors/notion/search` `{"query": "<a shared page title>"}` → the page appears; response contains **no** `url`/`notion.so` text.
4. `POST /v1/connectors/notion/pages/{id}/read` → assembled text, no `href`.
5. Confirm the bot token string never appears in any response body.

## Rollback

Set `connectors.notion.brokerTokenUrl` to `""` — the connector reports
`not_connected` and the desktop hides the Notion tools (fail-closed). No data
migration is required; the connector is read-only and stateless in the Hub.
