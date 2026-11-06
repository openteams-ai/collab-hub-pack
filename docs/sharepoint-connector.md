# SharePoint Connector

The SharePoint connector lets Collab Hub search and read files in a user's
SharePoint document libraries with that user's linked Microsoft account. It is
**read-only** — it only ever calls Microsoft Graph read endpoints and has no
code path that writes to SharePoint or OneDrive.

## Requirements

> **Permissions — the security fact that must be signed off, not discovered.**
> The Keycloak `microsoft` identity provider requests exactly these **delegated**
> Microsoft Graph permissions: `User.Read`, `Sites.Read.All`, `Files.Read.All`
> (plus the OIDC sign-in scopes `openid profile email offline_access`). All three
> are look-only: Graph has no write capability behind them. Read-only is
> therefore a *token* guarantee, reinforced in Collab Hub client code (only read
> methods exist; `POST /search/query` is Microsoft Search's read shape, not a
> write) and asserted by tests. Adding `Sites.ReadWrite.All` or `Files.ReadWrite.All`
> "just in case" violates the standing read-only contract. Delegated means the
> Hub acts as the signed-in person and sees only what they could already open.

> **Microsoft Entra ID is a real OIDC provider**, so no custom Keycloak provider
> or gateway shim is needed (unlike Notion). Keycloak's built-in **Microsoft**
> identity provider handles the link; Keycloak ≥ 23 exposes the **Tenant ID**
> setting the single-tenant app registration requires.

### Token path — Keycloak built-in Microsoft broker

The per-user Graph token is brokered by Keycloak. `sharepoint_tokens.py` fetches
the stored token from the broker's token endpoint exactly like
`github_tokens.py`; the Hub never holds the Entra client secret and runs no OAuth
flow of its own.

This was proven end-to-end on the dev Hub (2026-10-07/08): account-console link
→ federated identity stored → the Hub-side probe fetched the Graph token from
`/broker/microsoft/token` with the user's own bearer → that token answered
`GET /me` and a `POST /search/query` against the test tenant → **a day later the
same probe received a freshly renewed token without re-linking**, so Keycloak
refreshes the ~1 hour Graph token itself through the stored `offline_access`
refresh token.

**IdP contract the connector relies on** (reviewable artifact:
`docs/keycloak-microsoft-idp-partial-import.json`):

- **Alias `microsoft`.** The Hub derives the alias the desktop links from
  `brokerTokenUrl` (`.../broker/microsoft/token`), so the IdP alias must match
  the broker URL. Display name is free (the dev realm shows "Microsoft").
- **Provider type `microsoft`** (built-in) with **Tenant ID** set to the Entra
  directory id. It defaults to the multi-tenant `common` endpoint, which a
  single-tenant app registration rejects.
- `Store Tokens` on and `Stored Tokens Readable` on, so the Hub fetches the token
  from `https://keycloak.<hub-host>/realms/<realm>/broker/microsoft/token`,
  called with the user's own access token. Users hold the broker `read-token`
  role (`addReadTokenRoleOnCreate` in the import, or grant it to the realm's
  default roles as for GitHub). This URL is the
  `connectors.sharepoint.brokerTokenUrl` value.
- `Hide on Login Page` on — the Microsoft account is for linking, not for signing
  in to the Hub.
- Default scopes: `openid profile email offline_access User.Read Sites.Read.All
  Files.Read.All`. `offline_access` is what produces the refresh token.

**Token lifecycle.** Graph access tokens last about an hour. Keycloak renews the
stored token at the broker endpoint, so the connector builds **no** refresh
handling. Revocation (consent withdrawn, secret rotated and not updated in
Keycloak) surfaces as a Graph `401`/`403` on the capability probe and the
connector reports `reconnect_required`.

### Registering the Entra app

Done once per Microsoft tenant by a Global Administrator (or an app admin):

1. **Entra admin center → App registrations → New registration.** Single-tenant
   ("accounts in this organizational directory only"). Platform **Web**, redirect
   URI `https://keycloak.<hub-host>/realms/<realm>/broker/microsoft/endpoint`
   (Keycloak displays the canonical value on the identity provider's page; it
   must match exactly). Leave the implicit-grant boxes unchecked.
2. **Certificates & secrets → New client secret.** Copy the secret **value** (the
   long string), not the Secret ID. Record its expiry; a rotation owner must
   replace it in Keycloak before then.
3. **API permissions → Add → Microsoft Graph → Delegated:** `openid`, `profile`,
   `email`, `offline_access`, `User.Read`, `Sites.Read.All`, `Files.Read.All`.
   Click **Grant admin consent** so individual users are not prompted (most
   tenants block user consent by policy). Every row should show "Granted".
4. Hand the Keycloak admin the Application (client) ID and Directory (tenant) ID
   in the partial-import file, and the secret value through the secret store.

### Test tenant

Never develop against a production tenant. The free Microsoft 365 Developer
Program sandbox now requires a Visual Studio subscription or a Microsoft partner
membership; a **Microsoft 365 E5 trial** (30 days, ~25 seats, created from a
private browser window with a non-company email) or one or two paid licenses on
a dedicated tenant also work. Create a licensed user (set *Usage location* before
assigning the license), sign in once at `https://<tenant>.sharepoint.com`, create
a site, and upload Word, PowerPoint, Excel, PDF, text, image and zip files so the
read route's every branch has real content. Search indexing of new files can lag
by minutes.

## Hub Helm values

```yaml
connectors:
  sharepoint:
    brokerTokenUrl: ""            # Keycloak broker token endpoint (see IdP contract); empty = fail-closed
    apiBaseUrl: https://graph.microsoft.com/v1.0
    requestTimeoutSeconds: 15     # broker, search, metadata
    downloadTimeoutSeconds: 30    # file download and Office -> PDF conversion
    maxDownloadBytes: 26214400    # 25 MiB; larger files are reported as unsupported, never fetched
    includePersonalOnedrive: false
    extractionTimeoutSeconds: 30     # a parse past this is killed; the file reads as unsupported
    extractionMemoryLimitBytes: 268435456  # 256 MiB worker ceiling; stay inside the pod's 512Mi
    extractionWaitSeconds: 10        # wait for the one-at-a-time document reader; past it: 503 + Retry-After
```

These map to `COLLAB_HUB_API__CONNECTORS__SHAREPOINT__*` env vars in the API
deployment. `brokerTokenUrl` is the only value that must be set for the connector
to work; left empty, the connector reports `not_connected` and stays fail-closed.
`includePersonalOnedrive` is off by default: the same permissions reach each
user's personal OneDrive, and a daily digest should not surface private drafts
unless a hub decides it should.

## Endpoints

All are mounted under `/v1/connectors/sharepoint` and require a Hub bearer; the
desktop reaches them through the authenticated loopback proxy with no credentials.

| Method | Path | Purpose |
| --- | --- | --- |
| GET  | `/status` | Capability probe: `GET /me` **then** a bounded `POST /search/query` (size 1). A token that brokers but cannot read reports `reconnect_required`, not `connected`. Zero hits is still connected. `account` is the signed-in display name. |
| POST | `/search` | Microsoft Search over `driveItem`s (files and folders in document libraries). `query` words are wrapped in their own KQL group; `days_back`/`since_date`/`until_date` (in `time_zone`) become a `LastModifiedTime` clause **and** are re-checked per hit. `sort` is `relevance` or `modified` (newest first). Offset paging via an opaque `page_token` bound to the search. |
| POST | `/drives/{drive_id}/items/{item_id}/read` | Metadata plus bounded text of one file. Text files are read directly; PDFs are downloaded; Word/PowerPoint/Excel (and the other formats Graph converts) go through `/content?format=pdf`; text is extracted in the Hub. Folders, images, archives, oversized or image-only files return `unsupported: true` with a reason and the metadata. |

Error mapping: `409` not connected / reconnect, `422` bad id / stale or garbage
`page_token` / bad params / Graph `400`, `404` item not visible (with guidance),
`429` Graph throttling with `Retry-After` passed through, `502` provider failure
(timeout, 5xx, bad JSON), `503` connector unconfigured. Only Graph's top-level
`error.message` is ever surfaced; `innerError` (request and tenant ids) and
non-JSON bodies are never forwarded.

## Runtime boundary

The provider token and raw Graph payloads never enter the desktop runtime. The
Hub exchanges the Hub bearer for the user's Graph token, calls Graph, and returns
a bounded, sanitized, untrusted-marked response. Every data-bearing response
carries `content_trust: "external_untrusted"`.

- `webUrl` and every `@odata.*` link are **deliberately dropped**; `site_name` is
  derived from the item's site path before the URL is discarded. Snippets have
  Microsoft Search's `<c0>`/`<ddd/>` markup removed, then the strict link
  sanitizer applied. File, site and people names use the code-aware sanitizer
  (real URLs and emails masked, dotted names like `report.docx` kept).
- **Document text is produced in the Hub**, not the desktop: Graph converts
  Office files to PDF server-side; the Hub extracts text with PDFium
  (`pypdfium2`, the same engine the Collab desktop app's OCR sidecar uses). The
  parse never runs in the API process: each one runs in a separate worker
  process (`sharepoint_pdf_worker`) under `extractionTimeoutSeconds` (the
  worker is killed and reaped when it overruns) and
  `extractionMemoryLimitBytes` (an address-space ceiling set inside the worker
  before PDFium loads, plus a CPU ceiling as a second line). An adverse file
  costs one dead worker and an `unsupported` answer; the Hub keeps serving.
- **The worker cannot read the API's secrets.** It starts with a fixed minimal
  environment (`PATH`, `LANG`, `PYTHONDONTWRITEBYTECODE`), never the API's, so
  none of the deployment's secrets (database URLs, broker client secrets,
  cloud credentials) are inherited. And before the first worker is spawned the
  API process makes itself non-dumpable (`prctl(PR_SET_DUMPABLE, 0)`), so a
  worker that a file has taken over cannot read the API's initial environment,
  memory or open files through `/proc/<pid>/*` or ptrace either, although it
  runs as the same Linux user (the kernel refuses those to everyone but root;
  the API's own `/proc/self` stays usable; the API writes no core dumps). Both
  are pinned by tests, the second by one that tries the read from a worker on
  Linux. **Not isolated by this:** the worker shares the pod's Linux user, so
  it can still read whatever files that user can, including a mounted service
  account token and, on hubs using the local frame store (the default
  `storage_backend`), the frames themselves: `/var/frames` is mounted
  read-write for that user, so a taken-over worker could read or change every
  frame. It can also reach the network. Closing those means parsing outside
  the API pod, which is a separate pattern decision.
- **Document reads are admitted one at a time, before anything is downloaded.**
  One permit spans a PDF or Office read from before its download (or
  conversion) to after its parse, so a read waiting its turn holds no body:
  the pod's cost for document reads is one body plus one worker at a time,
  however many reads arrive. The wait is bounded by `extractionWaitSeconds`;
  past it the route answers `503` with `Retry-After` and the desktop tool
  relays "busy, try again". The permit is released on failure and when the
  caller gives up. Text files skip the permit: they are streamed only up to
  a few bytes per requested character. Other bounds: `maxDownloadBytes`
  before download (streamed through `read_capped`), 200 pages, and
  `max_chars`. Scanned, image-only PDFs extract to nothing and are reported
  as unsupported; OCR is out of scope.
- `/content` answers `302` to a pre-authenticated download host. The Hub follows
  it by hand: each hop must be https (or the configured Graph origin, for local
  fakes), and the Graph token is sent only on the first request (asserted by tests).

## Deployment checklist

**Microsoft tenant admin**
- [ ] Register the single-tenant Entra app with the Keycloak broker redirect URI.
- [ ] Add the delegated read-only Graph permissions above and **grant admin consent**.
- [ ] Issue a client secret; record its expiry and who rotates it.

**Keycloak admin**
- [ ] Import `docs/keycloak-microsoft-idp-partial-import.json` with the tenant id
      and client id filled in and the secret injected from the secret store
      (the secret **value**, not the Secret ID). Alias stays `microsoft`.
- [ ] Confirm the provider page's Redirect URI matches the Entra registration.
- [ ] Grant normal users the broker `read-token` role (as for GitHub).

**Hub admin (Collab side)**
- [ ] Set `connectors.sharepoint.brokerTokenUrl` to the broker token endpoint and deploy.
- [ ] Decide `includePersonalOnedrive` (default off).

## Per-environment rollout (cluster repos)

Each environment (dev, the public hub, prod) needs its **own** Entra app with
that environment's callback URL, its own credential Secret, the identity
provider in that environment's Keycloak, and the Hub pointed at the broker.
The GitHub connector set the pattern in the dev and public-hub cluster
repositories; SharePoint follows it with these three changes in the cluster
repo, applied by whoever operates that cluster:

1. **Credential Secret** (created by hand, never committed), with a third key
   for the tenant because the app is single-tenant:

   ```sh
   kubectl -n keycloak create secret generic keycloak-microsoft-idp-credentials \
     --from-literal=client-id='<entra-application-client-id>' \
     --from-literal=client-secret='<entra-client-secret-value>' \
     --from-literal=tenant-id='<entra-directory-tenant-id>'
   ```

   The Entra app must allow this callback URL:
   `https://keycloak.<hub-host>/realms/nebari/broker/microsoft/endpoint`.

2. **Realm setup job** (`manifests/keycloak/realm-setup-job.yaml`): three env
   vars from that Secret (`MICROSOFT_CLIENT_ID`, `MICROSOFT_CLIENT_SECRET`,
   `MICROSOFT_TENANT_ID`, all `optional: true`) and a block next to GitHub's:

   ```sh
   if [ -n "${MICROSOFT_CLIENT_ID:-}" ] && [ -n "${MICROSOFT_CLIENT_SECRET:-}" ] && [ -n "${MICROSOFT_TENANT_ID:-}" ]; then
     echo "Configuring Microsoft identity provider (SharePoint connector)..."
     # Built-in Microsoft provider; tenantId is required because the Entra app is
     # single-tenant (the provider defaults to the multi-tenant 'common' endpoint).
     # Delegated read-only Graph scopes only; see collab-hub-pack docs/sharepoint-connector.md.
     MICROSOFT_IDP_FILE=$(mktemp)
     cat > "$MICROSOFT_IDP_FILE" <<EOF
   {
     "alias": "microsoft",
     "displayName": "Microsoft",
     "providerId": "microsoft",
     "enabled": true,
     "trustEmail": false,
     "storeToken": true,
     "addReadTokenRoleOnCreate": true,
     "linkOnly": true,
     "hideOnLogin": true,
     "config": {
       "clientId": "$MICROSOFT_CLIENT_ID",
       "clientSecret": "$MICROSOFT_CLIENT_SECRET",
       "tenantId": "$MICROSOFT_TENANT_ID",
       "defaultScope": "openid profile email offline_access User.Read Sites.Read.All Files.Read.All",
       "syncMode": "IMPORT",
       "showInAccountConsole": "ALWAYS"
     }
   }
   EOF
     if $KCADM get identity-provider/instances/microsoft -r nebari >/dev/null 2>&1; then
       $KCADM update identity-provider/instances/microsoft -r nebari -f "$MICROSOFT_IDP_FILE"
     else
       $KCADM create identity-provider/instances -r nebari -f "$MICROSOFT_IDP_FILE"
     fi
     rm -f "$MICROSOFT_IDP_FILE"
   else
     echo "Skipping Microsoft identity provider; keycloak-microsoft-idp-credentials secret is not configured"
   fi
   ```

3. **Hub values patch** (`apps/patches/nebari-nexus-pack.yaml`), next to the
   other connectors:

   ```yaml
   sharepoint:
     brokerTokenUrl: http://keycloak-keycloakx-http.keycloak.svc.cluster.local:8080/realms/nebari/broker/microsoft/token
     apiBaseUrl: https://graph.microsoft.com/v1.0
     requestTimeoutSeconds: 15
     downloadTimeoutSeconds: 30
     maxDownloadBytes: 26214400
     includePersonalOnedrive: false
   ```

On the dev hub the identity provider was first created by hand (October 2026)
so the connector could be proven before this job existed; applying the job with
the same values is idempotent (`update` path) and makes the setup reproducible.

## Local development (`dev/`)

`make api-fakes` runs the fake Graph (`scripts/testdata/fake_graph_app.py`)
alongside the fake Google, Slack and GitHub providers and points the Hub at it
(`FAKE_CONNECTOR_ENV` in `dev/Makefile`); `scripts/smoke_connectors_sharepoint.py
--base-url http://127.0.0.1:8080` then exercises the whole contract. `make
idp-microsoft MICROSOFT_CLIENT_ID=... MICROSOFT_CLIENT_SECRET=...
MICROSOFT_TENANT_ID=...` creates the real identity provider in the local
Keycloak for `make api-full`.

## Verification recipe (run once with a real linked account)

1. Link a Microsoft account through the Keycloak account console (**Account
   security → Linked accounts → Microsoft**) or the desktop **Connect** action.
2. `GET /v1/connectors/sharepoint/status` → `connected: true`, `account` = the
   person's display name, `scopes` = the three Graph permissions.
3. `POST /v1/connectors/sharepoint/search` `{"query": "<a word from a document>"}`
   → the file appears with `site_name`; response contains **no** `sharepoint.com`
   or `@odata` text.
4. `POST /v1/connectors/sharepoint/drives/{drive_id}/items/{item_id}/read` on a
   Word file → `text` holds the document body, `unsupported: false`.
5. Repeat step 2 more than an hour after linking: still `connected` without
   re-linking (Keycloak refreshed the token).
6. Confirm the Graph token string never appears in any response body.

Locally, `scripts/testdata/fake_graph_app.py` plus
`scripts/smoke_connectors_sharepoint.py` exercise the same contract against a
fake Graph (two search pages, a personal-OneDrive hit, a Word → PDF conversion,
a text file, an unsupported image, a not-found item).

## Rollback

Set `connectors.sharepoint.brokerTokenUrl` to `""` — the connector reports
`not_connected` and the desktop hides the SharePoint tools (fail-closed). The
admin panel's connector switch (`sharepoint`) does the same without a redeploy.
No data migration is required; the connector is read-only and stateless in the
Hub. Removing the Keycloak identity provider deletes every user's link.
