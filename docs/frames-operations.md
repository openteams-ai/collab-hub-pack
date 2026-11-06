# Frames Operations

## Observability

The Collab Hub API emits structured access logs for every request and audit logs for
Frame mutations. The request ID comes from `x-request-id` when supplied;
otherwise the API generates one and returns it on the response.

Prometheus metrics are exposed at `/metrics`:

- `frames_server_http_requests_total{method,path,status}`
- `frames_server_http_request_duration_seconds{method,path}`
- `frames_server_audit_events_total{action}`

Audit actions currently include:

- `frame_create`
- `frame_update`
- `frame_delete`
- `suggestion_create`
- `suggestion_close`
- `active_frames_update`
- `group_create`
- `group_update`
- `group_delete`
- `group_owners_update`
- `group_frame_add`
- `group_frame_remove`

## CORS

CORS is configured under `COLLAB_HUB_API__SECURITY__CORS`. The defaults allow
`Authorization` and `Content-Type` headers so Apollo Desktop and browser clients
can use the Hub API auth boundary. Set `allow_credentials=true` only when
`allowed_origins` is a concrete origin list, not a wildcard.

Example:

```yaml
security:
  cors:
    allowed_origins:
      - https://desktop.example.com
    allowed_headers:
      - Authorization
      - Content-Type
    allow_credentials: true
```

## Token Verification And The IdP Contract

Bearer tokens and IdToken cookies are verified against a JWKS endpoint
(`frames.auth.idToken.jwksUrl` / `frames.auth.bearer.jwksUrl`, falling back to
the bearer URL for cookies). One client per URL is shared across every request
in the pod, so steady-state verification does no network I/O. Three timings
govern how a key change reaches a running pod, all in
`api/src/collab_hub_api/frames/auth.py`:

| Constant | Value | What it bounds |
| --- | --- | --- |
| `JWKS_CACHE_LIFESPAN_SECONDS` | 300s | How long a validated key set is served before it is fetched again. |
| `JWKS_FORCED_REFRESH_MIN_INTERVAL_SECONDS` | 30s | Floor between refetches triggered by a token naming an unknown `kid`. |
| `JWKS_FETCH_TIMEOUT_SECONDS` | 5s | One outbound fetch, so a hung IdP cannot pin a request thread. |
| `JWKS_MAX_STALE_SECONDS` | 600s | How long a validated key set keeps verifying after the endpoint last confirmed it. |

Fetches are single-flighted per URL: a cold start or cache expiry under load
costs one outbound request per pod, not one per in-flight caller, and the
callers waiting on a fetch take its outcome — including its failure — rather
than each running their own behind it. A fetch that fails, times out, or
returns a successful-but-unusable body leaves the last validated key set in
place, so a broken IdP response cannot revoke keys that still work, and is
retried a lifespan later. That fallback expires: once the endpoint has gone
`JWKS_MAX_STALE_SECONDS` without confirming the set, it is dropped and
verification fails closed. An IdP outage shorter than that window is invisible
to callers; a longer one takes authentication down with it.

What the IdP must hold up for rotation to be seamless:

- **Unique `kid` per key.** Rotation is detected by an unrecognized `kid`; new
  key material published under a reused `kid` is invisible until the 300s cache
  expires, and tokens signed with it are rejected until then.
- **Publish before use.** A key must appear in the JWKS response before the
  first token signed with it arrives. A token whose key is not yet published can
  be rejected for up to `JWKS_MAX_REJECTION_WINDOW_SECONDS` (35s): a forged
  unknown `kid` arriving first consumes the forced-refresh allowance, so the
  legitimate token waits out the remaining interval plus the fetch it triggers.
  Size the IdP's publish-before-use delay against that number.
- **Overlap old and new keys.** Keep the retiring key in the JWKS response until
  every token it signed has expired. Removing it takes effect on the next fetch,
  and tokens it signed are rejected from that point.
- **Emergency revocation is not immediate, and how long it takes depends on
  what is left behind.** Dropping a compromised key from a JWKS response that
  still carries at least one usable key takes effect within
  `JWKS_CACHE_LIFESPAN_SECONDS` (300s), or sooner if an unknown `kid` forces a
  refresh: the next fetch returns a usable set, which is adopted whole, and the
  removed key goes with it. Revoking *every* key — emptying the response, or
  leaving only unusable entries — is indistinguishable from a broken IdP, so it
  goes through the last-known-good path instead and takes effect within
  `JWKS_MAX_STALE_SECONDS` (600s). Prefer replacing the compromised key over
  emptying the response. When either window is too long, restart the API pods:
  the client cache is per process and starts cold.

## ACL Identity (`frames.auth.identityClaim`)

Every access decision compares the caller's identity string against stored
owners, readers, and actors, so *which claim becomes that string* is a
persistence decision, not an authentication detail. The string is written to
eight places, none of which has a rename path: frame `created_by`, `owners`,
`readers`, and `suggestions[].submitted_by`; group `created_by` and `owners`;
`frames_server_history.actor`; and the `user_id` primary-key component of both
`frames_server_active_frames` and `frames_server_usage_users`.

| `frames.auth.identityClaim` | `FRAMES_AUTH_IDENTITY_CLAIM` | Principal |
| --- | --- | --- |
| `""` (default) or `legacy` | unset / `legacy` | first present of `preferred_username`, `email`, `sub` |
| `sub` | `sub` | the verified `sub` claim only; a token without one is 401 |

**The default is `legacy` so an upgrade never changes an existing deployment's
principals.** Flipping a deployment that already holds Frames data orphans every
record whose owner was recorded under the old string — the owner cannot read,
manage, or repair it, and only a data migration can recover it. Choose `sub`
when the deployment starts empty. Changing the setting later is a data
migration, not a config change.

What `sub` buys: `preferred_username` is user-chosen and can be changed in
Keycloak, and `email` is self-asserted, so under `legacy` a rename silently
orphans that user's frames. `sub` is immutable for the life of the account.

Two consequences to plan for:

- **Grants must name subject ids.** With the pin on, `POST`/`PUT` of
  owners/readers (frames and groups) and creation-time owner seeds reject an
  **email address** with a **422**. That is the mistake worth catching: a stored
  email principal matches no caller, so silently keeping it would show a
  successful share that grants nothing. Everything else is accepted as an opaque
  subject — realm-local UUIDs, federated `f:<provider>:<id>` values and
  service-account subjects are all valid, and the server does not infer identity
  from syntax. (Whether a subject is a real member is a membership question, not
  a syntax one; the scoped member check arrives with
  [#99](https://github.com/openteams-ai/collab-hub-pack/issues/99).) Clients
  need a member picker that submits subject ids before this is turned on.
  Removal routes (`DELETE .../owners/{id}`) and reads of stored records are
  *not* validated, so principals written before the pin still load and can be
  cleaned out.
- **Exactly one trusted issuer.** A `sub` is unique only within its issuer, and
  the IdToken and bearer verifiers are configured independently, so a second
  trusted issuer could mint a colliding `sub` that *is* an existing user. With
  the pin on, the API refuses to start unless every configured verifier names an
  expected issuer and they all name the same one. The IdToken verifier is
  derived exactly as the decode path derives it — its JWKS URL and its issuer
  fall back to the bearer values *separately*, so naming
  `frames.auth.idToken.issuer` while reusing the bearer JWKS is a second issuer
  and is rejected. The chart applies the same check at render time. Trusting
  a second issuer requires issuer-qualified `(iss, sub)` principals first, which
  is another data migration.

An unrecognized `FRAMES_AUTH_IDENTITY_CLAIM` value fails startup rather than
falling back; matching is exact, so `Legacy` or `" sub "` is an error, not a
guess. The chart owns the variable — setting it through
`api.deployment.extraEnv` fails rendering, because that path skips the issuer
checks above.

## Organization Source (`frames.auth.orgSource`)

Every access decision is scoped by the caller's `(org_id, workspace_id)`. This
setting decides where that organization comes from.

| `frames.auth.orgSource` | `FRAMES_AUTH_ORG_SOURCE` | Organization |
| --- | --- | --- |
| `""` (default) or `claims` | unset / `claims` | `org_id`/`workspace_id` token claims, falling back to `frames.auth.defaults` |
| `membership` | `membership` | the caller's one active `collab_org_members` row; workspace is always `default` |
| `single` | `single` | membership resolution, plus auto-admission to one declared organization for sign-ins from declared identity providers (below) |

**Why `claims` is not a neutral default.** No identity provider in use mints an
`org_id` claim, so on a real deployment the *fallback* is what every caller
gets. With `frames.auth.defaults.orgId` set, that means one organization for the
whole server, in which an `internal` Frame is readable by everyone who can sign
in. With it unset, callers resolve to no organization at all and get a `401`.
Neither is a state to arrive at by accident, which is why the chart refuses to
render a default org on an `api.ingress.enabled` deployment unless `orgSource`
is stated explicitly (either value is accepted — the requirement is the
statement, not the choice).

**`membership` is a separate switch from `identityClaim` on purpose.** It would
be shorter to derive it from the identity pin, since membership rows are keyed
by the subject. But a deployment that already has users has to pin identity
first, backfill membership rows against the resulting subjects, verify coverage,
and only then retire the fallback. One fused switch has no middle state to stop
in. Two switches, one a precondition of the other, keeps the steps
independently reversible.

Membership resolution (both `membership` and `single`) refuses to start unless
all of the following hold, each checked at render time by the chart and again
at startup by the API:

- `frames.auth.identityClaim=sub` — membership rows are keyed by the OIDC
  subject, so a legacy principal would be looked up under the wrong key.
- `frames.auth.defaults.orgId` and `.workspaceId` are **empty**. Nothing reads
  them in this mode, which is exactly why leaving them set is dangerous: they
  are invisible until someone flips `orgSource` back to `claims`.
- A shared `frames.postgres` URL (or `existingSecret`) is configured.
- The `collab_` schema is at the version this build requires (see below).

### The single-organization mode (`orgSource=single`)

A common deployment shape: one hub, one organization, everyone in it. Under
plain `membership` that shape is unreachable without invitations — the only
writer of `collab_org_members` is invitation acceptance, so on a hub with
`frames.email.provider: disabled` a brand-new realm account dead-ends at
`no_organization` and an operator has to write SQL. `single` closes that gap
by declaration rather than by fallback:

- The deployment declares **one organization** (`frames.auth.singleOrg.id` and
  `.name`) and **which identity providers produce members**
  (`frames.auth.singleOrg.memberSources`, a list of Keycloak identity-provider
  aliases).
- A caller with **no membership row at all** whose token shows a sign-in
  brokered through a declared provider gets a real `member` row written on
  their first authenticated request. The row is an ordinary membership row:
  the member list shows them, the member picker finds them, and moving to
  multi-org later is a configuration change, not a data migration.
- Everything else is byte-for-byte membership mode. Existing rows — active in
  any organization, or `removed` — are authoritative and never rewritten, so
  **removal keeps taking effect on the very next request** and auto-admission
  can never resurrect a removed member or move anyone. Turning the mode off
  (back to `membership`) simply stops the writes and leaves every member
  intact.

**The gate is the token's `identity_provider` claim.** Keycloak records the
broker alias in the `identity_provider` user session note; surface it with a
"User Session Note" protocol mapper (session note `identity_provider`, claim
name `identity_provider`, added to **ID and access tokens**) on the client or
client scope the hub's tokens come from. A token without the claim — a local
realm account, a realm missing the mapper — is never auto-admitted and
resolves to `no_organization`, exactly like a sign-in from an undeclared
provider. Absence fails closed on purpose: a missing mapper must degrade to
"nobody is auto-admitted", never to "everybody is".

**The access boundary, stated plainly.** On a single-organization hub, *the
declared providers' membership policy is the hub's access policy*: everyone a
listed provider admits becomes a member and can read every `internal` Frame.
That is the intended behaviour of the mode, and it is why admission is gated
on a declared provider rather than on authentication — "every authenticated
user" would also admit self-registered and hand-created realm accounts, the
latter being the one door with no policy on it at all. Before listing a
provider, restrict it to your organization (for a Google provider,
`hostedDomain`); an unrestricted provider on the list admits anyone with an
account there. The API logs `single_org_mode_active` at startup naming the
organization and the declared sources, so the declaration is visible in pod
output rather than inferred from behaviour.

Deliberately unchanged by this mode:

- `identityClaim: sub` is still required; membership rows are keyed by the
  subject, and the retired `frames.auth.defaults` are still refused. `single`
  replaces the old default-org fallback; it does not revive it.
- Auto-admitted members get the `member` role. Owner and operator remain
  deliberate acts — the first operator is still the documented
  `collab_platform_roles` bootstrap insert (below), and an operator signing in
  through a declared provider is admitted as a member like anyone else, on top
  of their platform role.
- Invitations keep mounting: they carry more than membership (the granted
  role, service-access grants on acceptance). On a single-org hub they are
  optional for *access* and still useful for *role* — an owner invitation into
  the declared organization is how a single-org hub grants `owner`, since
  auto-admission never does. Note the ordering: acceptance refuses an existing
  member, and a declared sign-in becomes a member on their first authenticated
  API request, so an owner-to-be should accept their invitation before opening
  the app (an existing member is promoted through the owner surface instead).
  One kind of invitation is refused, at issuance and at acceptance: the
  **org-creating** invitation (no `org_id`), which would mint the second
  organization the mode declares away (`409 organization_creation_refused`;
  a pre-flip invitation refused at acceptance is not consumed). The operator
  invitation *page* only issues org-creating invitations, so under `single` it
  is not mounted — the `/v1` operator routes still list, revoke, and issue
  into existing organizations.

The organization row itself is created alongside the first admission (same
transaction), with `created_by = 'single-org-configuration'`, if no
`collab_orgs` row with the declared id exists; the configured name applies
only at that creation and never renames an existing organization. The declared
id is load-bearing: changing it later points new sign-ins at a *different*
organization while every existing membership row stays where it was.

### What a caller without an organization gets

An authenticated caller with **no** membership row, or one whose row has
`status='removed'`, is answered:

```
HTTP/1.1 403 Forbidden
{"error": {"code": "no_organization", "message": "..."}}
```

Deliberately not a `401`: the desktop maps `401` to "sign in", and a pending
invitee or a removed member is already signed in, so that answer is a re-login
loop that cannot succeed. Clients branch on the `code` — the `message` is for
logs and is not shown. The same envelope is emitted on the API routes, the MCP
endpoint, and the protected pages.

### Removal semantics, stated precisely

Membership is resolved on **every request**, with no cache, so flipping a row
from `active` to `removed` takes effect on the caller's next request — there is
no session or cache window to wait out. Removal never deletes the row: the
binding is retained so a removed login cannot re-register into a different
organization.

What removal does **not** do: it is not total revocation. Losing the
organization mechanically removes `internal` access, because `internal` is
scoped by organization and a removed caller has none. An **explicit** `readers`
grant naming that subject is untouched — reader grants are user-scoped and are
not evaluated against membership — so restoring the membership restores those
reads with nobody re-sharing anything. Describe removal as "loses their
organization", not as "access revoked".

### Database outage behavior

Membership resolution is a database read on the authentication path, so it
fails **closed**: while the database is unreachable, every authenticated
request answers `503 database_unavailable` (`503 organizations_unavailable` if
no backend is configured at all). Never a `401`, which would sign every user out
of a healthy deployment over a transient blip, and never `no_organization`,
which would claim as fact something the server could not determine. There is no
membership cache: a cache would trade the per-request revocation this design
provides for a stale window, and would make an outage deny some callers while
serving others. Add one only on operational evidence.

## Storage And Active State

Frame content uses the configured Frames store:

- `local` for local development and single-pod installs,
- `s3` for shared production-style storage.

### One shared Postgres for the relational features

History and Frame Groups are **required relational features** with **no
per-feature `disabled` toggle**. They — and, by fallback, active-state — ride a
**single shared Postgres URL** configured at
`COLLAB_HUB_API__FRAMES__POSTGRES__URL` / `__AUTO_MIGRATE` (Helm:
`frames.postgres`). Setting that one URL lights up active-state, history, and
groups together; leaving it unset is the *only* off state. This replaces the
earlier per-feature `disabled`-by-default backends, which let a deploy enable
one feature and silently leave the others off.

- **Active-frame selection** (`frames.activeState.backend`: `disabled` (default),
  `memory`, `postgres`) is the one feature that may still be fully disabled.
  With `postgres` and no `activeState.postgres.url` of its own, it **falls back
  to the shared `frames.postgres.url`**.
- **Change history** (`GET /v1/frames/{id}/history`) persists to
  `frames_server_history` (created when `frames.postgres.auto_migrate` is set).
  In-memory is available only as a test/dev override
  (`COLLAB_HUB_API__FRAMES__HISTORY__BACKEND=memory`). With no shared DB the
  history endpoints return `503` and recording is a no-op. History writes are
  best-effort relative to the mutation: a failed write is logged and counted
  (`frames_server_history_write_failures_total`) but never fails or rolls back
  the mutation that already succeeded.
- **Frame Groups** (`/v1/frame-groups`) persist to `frames_server_groups`
  (relational; groups have no document body). Same model: `…__FRAMES__GROUPS__BACKEND=memory`
  test override; with no shared DB **every** group endpoint returns
  `503 groups_unavailable` (no silent empty/404 fallback). Group change events
  reuse the history store with `entity_type='group'`. Frame-side touches of the
  group store (delete-time reconciliation, the `?group_id=` list filter) tolerate
  the unavailable store so a missing groups DB never breaks frame operations.

### Collab tenancy tables and versioned migrations

The organization/membership tables use the `collab_` prefix and ride the **same**
shared `frames.postgres.url`, migrated by the same
`COLLAB_HUB_API__FRAMES__POSTGRES__AUTO_MIGRATE` switch — no new setting.

- `collab_orgs` — one row per organization. `id` is an **opaque** public
  identifier (no slug, never derived from a person or company name); `name` is
  display-only (nullable, non-unique, defaulted to a neutral placeholder).
- `collab_org_members` — **one home organization per login**: `user_id` (the
  OIDC `sub`) is the primary key, so the binding is enforced by the schema and
  the hot lookup is an exact primary-key read. Removal never deletes a row; it
  sets `status='removed'` and the row is retained so the binding stays
  enforceable. `email`/`display_name` are display/contact fields only and are
  never authorization principals. Indexed on `org_id` for member listing.
- There is **no workspaces table**: `workspace_id` is the literal constant
  `"default"`.
- `collab_track_events` and `collab_track_payloads` (version 12) — the Track
  of every Cog run ([track](cog-execution/track.md)): the append-only events,
  with a global `sequence` for replay order and a `schema` version per event,
  and the step payloads too large to keep inline. The execution package's
  `PostgresTrackStore` reads and writes them; on the hub they are created here
  and never by that store's own `ensure_schema`, which serves the standalone
  package and local use. A partial unique index admits one `op_submitted` per
  run, so two replicas cannot both start it.

Unlike the `frames_server_` tables, these are created by a single **versioned,
lock-guarded runner**. One transaction takes
`pg_advisory_xact_lock(<"collab_1">)` before touching the catalog, then applies
every migration above the highest version recorded in `collab_schema_migrations`
and records what it applied — lock, DDL, and bookkeeping commit together.

- Concurrent replica startup is safe. A bare `CREATE TABLE IF NOT EXISTS` is
  **not** concurrency-safe in Postgres: two replicas can both pass the existence
  check and race the catalog insert, killing one pod with a duplicate-key error
  on `pg_type`/`pg_class`. The advisory lock removes that race for `collab_`
  tables; the existing `frames_server_` stores still have it (issue #84), masked
  today only by `replicaCount: 1`.
- Migration versions are **append-only** and the SQL of a released version is
  frozen: a database that recorded version N will never re-run it, so a
  correction is a new version, never an edit.
- The frozen-text rule is enforced mechanically (issue #73): each registry row
  records a SHA-256 checksum of the version's statement text, and the runner
  verifies every already-applied version against the code's current text before
  applying anything. An in-place edit of a shipped version fails the startup
  with an error naming the version. Rows written by a pre-checksum build (or
  inserted by hand without one) have a `NULL` checksum, which is accepted once
  and backfilled from the current text on the next run.
- Running the migration again is a no-op (it re-reads the version table inside
  the lock), so restarts and rolling updates cost one locked read.
- With `auto_migrate` off, apply the statements out of band and insert the
  matching `collab_schema_migrations` rows (the `checksum` column may be left
  `NULL`; the next auto-migrating startup backfills it). To record a real
  checksum instead of relying on the backfill, compute it from the build you
  applied:

  ```sh
  python -c 'from collab_hub_api.frames.collab_schema import \
  COLLAB_SCHEMA_MIGRATIONS, collab_migration_checksum; \
  print({v: collab_migration_checksum(s) for v, s in COLLAB_SCHEMA_MIGRATIONS})'
  ```

#### Startup version preflight

A deployment that reads these tables (`frames.auth.orgSource=membership` or
`single`)
checks the recorded schema version at startup, before serving. Without it,
`autoMigrate: false` against a database nobody migrated installs cleanly, starts
cleanly, and then fails with `relation "collab_org_members" does not exist` —
at request time, in whichever endpoint touched it first.

- **Behind this build** — the pod refuses to start, naming the applied version,
  the required version, and whether auto-migration was supposed to have run.
- **Ahead of this build** — logged, not fatal. A newer replica migrating the
  shared database while older replicas still serve is what an ordinary rolling
  update looks like; making it fatal would turn every deploy into an outage of
  the old replicas and would block rollbacks. Migrations are append-only, so an
  older build's statements stay valid against a newer schema.
- **Unreachable database** — logged, not fatal *at this step*. Nothing is
  asserted about the schema; the check simply could not run.

  Read that narrowly. With `autoMigrate: true` the migration runner has already
  run — and already failed — before the preflight, so such a pod does not start
  and is restarted until Postgres is reachable. That is unchanged from the
  migration runner's own contract and is the correct outcome: migrations run
  only at startup, so a pod that skipped its migration and served anyway would
  keep failing after the database returned. The preflight's tolerance therefore
  matters in the `autoMigrate: false` case, where startup does not otherwise
  depend on the database being reachable.

  That case leaves one gap the preflight cannot close: the pod started without
  being able to read the version, the database later returns, and the migration
  was never applied out of band. The first membership query then finds no table.
  That is answered with `503 organizations_unavailable` and one log line naming
  the migration — not the `500` an unhandled `UndefinedTable` would produce —
  so the deployment fails closed and says what to do.

CI runs these migrations against a disposable Postgres service container, so
the DDL, the constraints, the transactional rollback, and eight concurrent
migrators are all exercised on a real server. To reproduce locally, point
`COLLAB_HUB_TEST_POSTGRES_URL` at a throwaway database (the tests drop and recreate
every `collab_` table); without it, those tests skip.

### Operators and the audit event log

Two more `collab_` tables (migration version 2, issue #87) carry deployment
operator authority and the record of its use:

- `collab_platform_roles` — deployment-wide roles, keyed by the OIDC `sub`.
  `operator` is the only role. This is a **second authority axis** from the
  org-scoped `role` in `collab_org_members`: an org owner has authority inside
  one organization, an operator across all of them. Resolved at the auth choke
  point in the same read as membership (`AuthContext.platform_role`); a
  `revoked` row grants nothing, effective on the caller's next request.
  Deliberately not a Keycloak realm role — Keycloak authenticates, this server
  authorizes. An operator **need not belong to an organization**: an active
  grant with no membership row yields a hub-scoped context (this is how the
  bootstrap operator issues the first invitation on a fresh deployment, before
  any organization exists). Such a caller can use operator surfaces only —
  every org-scoped page and endpoint answers `no_organization` for them.
  Migration version 7 adds a `source` column (`manual` or `idp`, default
  `manual`) that records who administers the row; see
  [Where operator rows come from](#where-operator-rows-come-from-source-and-sign-in-sync).
- `collab_audit_events` — every privileged action's durable record, written
  **in the same database transaction** as the mutation it describes (the
  `audited()` primitive in `frames/audit.py` is the only writer). `actor` is
  the immutable `sub`; `actor_label`/`target_label` snapshot the email/display
  name *at the time of the action*, because a row read months later showing
  only a UUID is useless. `org_id` is `NULL` for hub-scoped events, never
  "unknown". `detail` is redacted and never holds an invitation secret.

**Append-only is a code convention, not an enforced boundary.** The
application role owns the table (auto-migration creates it over the runtime
pool), so revoking its own `UPDATE`/`DELETE` would be theater — an owner can
re-grant to itself. What is true, and tested against the source tree, is that
no application code path updates or deletes audit rows. Enforcing it for real
needs a separate migration/owner role and a non-owning runtime role; out of
scope for the beta, recorded here so it is not re-derived.

#### Where operator rows come from (`source` and sign-in sync)

Every row in `collab_platform_roles` has a `source` (migration version 7):

- `manual`: administered by a person, either a hand-run `psql` insert or a
  grant from the admin panel. Every row that existed before version 7 ran was
  inserted by hand, so the column defaults to `manual` and those rows keep
  their authority.
- `idp`: created by sign-in sync from the identity provider's groups.

Sign-in sync (`frames/platform_role_sync.py`) is off unless `web.admin_group`
(`COLLAB_HUB_API__WEB__ADMIN_GROUP`) names a group. When it is set, each
browser sign-in reads the verified ID token's `groups` claim and reconciles
that person's operator row:

- In the admin group, with no active row (or a revoked `idp` row): an active
  `idp` row is written, and a `platform_role.grant` audit row with
  `detail = {"origin": "oidc", "group": "<admin_group>"}`.
- Not in the admin group, with an active `idp` row: that row is revoked, and a
  `platform_role.revoke` audit row is written with the same `detail`.
- A `manual` row is never granted or revoked by sync, in either direction. So
  the bootstrap operator stays an operator whatever groups they are in, and a
  deliberate revocation by hand is not undone by group membership.
- Sync never revokes the last active operator. It keeps the row, logs
  `platform_role_sync_kept_last_operator`, and the person stays an operator.
  A renamed admin group or a changed groups mapper would otherwise remove
  every synced operator at their next sign-in.
- The group name matches with or without a leading slash, so `/collab-admins`
  in the setting matches `collab-admins` in the claim and the reverse.
- A missing `groups` claim, or one that is not a list, changes nothing. The
  sign-in continues and `platform_role_sync_no_groups_claim` is logged. The
  realm client needs a groups mapper (or the `groups` client scope) so the ID
  token carries the claim at all.
- A reconcile that fails is logged as `platform_role_sync_failed` and the
  sign-in continues on the row as it already stood.

The audit row's `actor` is the person who signed in, since their sign-in
caused the change. On the browser session a sync revocation takes effect at
the holder's next sign-in.

#### Granting and revoking from the admin panel

`POST /admin/api/users/{user_id}/role` with a JSON body
`{"action": "grant" | "revoke", "user_label": "<optional>"}` changes the
operator role (`frames/platform_role_admin.py`). It needs an operator session
and the `X-CSRF-Token` header, like every write under `/admin/api`, and the
shared `frames.postgres` URL; without a database it answers 503
`role_management_unavailable`. The Users section of the panel calls it.

- **A grant writes a `manual` row.** It is an upsert, so re-granting a revoked
  person reuses their row, and it sets `source = 'manual'` even on a row that
  was `idp`. Sync then leaves it alone, so a panel grant stays in place after
  the person leaves the admin group.
- **A revoke reaches any row**, `manual` or `idp`. A revoked `idp` row stays
  `idp`, so if that person is still in the admin group their next sign-in
  grants the role again.
- **Some revokes are refused with HTTP 409** and a body of
  `{"error": "<reason>"}`:
  - `self_revoke`: the operator is revoking themselves.
  - `last_operator`: the person is the only active operator.
  - `not_operator`: the person holds no active operator role, so there is
    nothing to revoke.

Each change writes a `platform_role.grant` or `platform_role.revoke` audit row
in the same transaction, with `actor` set to the operator who clicked and
`detail = {"origin": "admin_panel"}`. A refused revoke writes no audit row.
Both revoke paths (panel and sync) lock the active operator rows before they
write, so two revokes at the same moment cannot together remove the last
operator.

#### Bootstrapping the first operator

The panel can grant the role, but only an operator can open the panel. So the
first operator is either a member of `web.admin_group` (see above) or one
`psql` insert, recorded as an `operator.manual` event in the same transaction:

```sql
BEGIN;
INSERT INTO collab_platform_roles (user_id, role, granted_by)
VALUES ('<operator-sub>', 'operator', NULL);
INSERT INTO collab_audit_events (actor, actor_label, action, target_type, target_id, detail)
VALUES ('<operator-sub>', '<operator-email>', 'operator.manual', 'user', '<operator-sub>',
        '{"summary": "bootstrap operator grant"}');
COMMIT;
```

Revocation is `UPDATE collab_platform_roles SET status = 'revoked' WHERE
user_id = '<sub>'`, plus the same kind of `operator.manual` row.

#### Recording manual `psql` work (`operator.manual`)

Most privileged work in this beta still happens in `psql`, and no
application-written row can see it. Whenever you run privileged SQL by hand,
add an `operator.manual` event **in the same transaction** — this is the
expected step, not an optional courtesy; without it the log looks complete
while omitting the highest-privilege actions:

```sql
INSERT INTO collab_audit_events (actor, actor_label, action, detail)
VALUES ('<your-sub>', '<your-email>', 'operator.manual',
        '{"summary": "<one line describing what was run and why>"}');
```

Never paste secrets (invitation tokens, credentials) into `detail`.

The `action` and `target_type` columns are CHECK-constrained to the ratified
vocabularies (`operator.manual` is in the set, so runbook inserts always
pass); a typo'd action in a hand-run insert is refused by the schema instead
of creating a row no query ever finds.

#### Reading the log

The admin panel's Audit log section shows it, newest first, through
`GET /admin/api/audit` (operator session required). The endpoint takes
optional `actor` and `action` filters and a page size `limit` (1 to 200,
default 50). Paging is by cursor: each page returns `next_before_id`, and
passing that back as `before_id` returns the entries older than it, so rows
written while someone is reading do not shift the pages. `next_before_id` is
`null` on the last page. Without a database the endpoint answers 503
`audit_log_unavailable`.

The same log read with `psql`:

```sql
SELECT at, actor, actor_label, action, target_type, target_id, target_label, org_id, detail
FROM collab_audit_events
ORDER BY at, id;
```

Filter with `WHERE org_id = '<org>'`, `WHERE actor = '<sub>'`, or
`WHERE action = 'invitation.send'` as needed; the action vocabulary is the
closed set in `frames/audit.py` (`AUDIT_ACTIONS`). Rows are permanent — there
is no retention or rotation policy in the beta, and the table holds email
snapshots, so it is in scope for any future deletion request handling.
Migration version 8 adds `collab_audit_events_actor_idx` on `(actor, id DESC)`
and `collab_audit_events_action_idx` on `(action, id DESC)`, which serve the
actor and action filters; the unfiltered listing uses the primary key.
Retention and erasure-request handling are tracked for resolution before GA
in [#113](https://github.com/openteams-ai/collab-hub-pack/issues/113).

#### Actions the admin panel and sign-in sync record

Migrations 7, 9 and 10 widen the `action` CHECK constraint, and migration 10
adds `connector` to the `target_type` constraint:

| Action | Migration | Written by | `target_type` / `target_id` | `detail` |
| --- | --- | --- | --- | --- |
| `platform_role.grant` | 7 | panel role change, or sign-in sync | `user` / the person's `sub` | `{"origin": "admin_panel"}` or `{"origin": "oidc", "group": ...}` |
| `platform_role.revoke` | 7 | panel role change, or sign-in sync | `user` / the person's `sub` | same as grant |
| `service_access.revoke` | 9 | panel model access change | `user` / the person's `sub` | `{"group_path": ...}` |
| `connector.enable` | 10 | panel connector switch | `connector` / `google`, `slack` or `github` | `{"enabled": true}` |
| `connector.disable` | 10 | panel connector switch | `connector` / `google`, `slack` or `github` | `{"enabled": false}` |

All of them are hub-scoped (`org_id` is `NULL`). The panel also writes
`service_access.grant` rows, whose shape differs from the acceptance path's;
see [below](#the-audit-trail-for-a-grant-service_accessgrant).

#### Finding grants that are still owed

When `frames.serviceAccess.grantOnAcceptance` is set, accepting an invitation
adds the accepter to those identity-provider groups. The grant happens **after**
the acceptance commits — a group write cannot be rolled back, and an
identity-provider outage must not cost somebody a membership they hold a valid
invitation and a verified address for. A failure therefore leaves a correct
acceptance whose holder has no service access, and the page has already told
them they are in, truthfully.

So the acceptance transaction records **what it owes** before any of that
happens: one `collab_service_access_grants` row per group, `state = 'pending'`,
committed with the acceptance itself. The attempt then settles that row to
`granted` or `failed`. What is owed and undelivered is one query:

```sql
SELECT user_id, group_path, state, invitation, created_at, updated_at
FROM collab_service_access_grants
WHERE state <> 'granted'
ORDER BY created_at;
```

The same query is
`PostgresInvitationService.outstanding_service_access_grants()`, so the runbook
and the code do not hold two opinions about what "outstanding" means.

`pending` and `failed` are both outstanding and differ only in what is known:

- **`failed`** — the identity provider refused. Look at
  `service_access_grant_failed` in the API logs for the reason.
- **`pending`** — nobody saw an answer. Either the attempt is in flight right
  now, or a pod stopped between the acceptance and the settle. A `pending` row
  more than a few seconds old is the second case.

**Every way the process can stop leaves something to retry.** That is the
reason the intent is written first rather than the outcome written after:

| Where it stops | Row says | What happens |
| --- | --- | --- |
| before the group call | `pending` | retried; the add is idempotent |
| after the call, before the settle | `pending` | retried, idempotent again |
| the settle itself fails | `pending` | same |
| the provider refused | `failed` | retried |
| nothing failed | `granted` | nothing to do |

Retrying a row that turns out to be fine costs one redundant call, because
adding a user to a group is idempotent. That is the safe direction, and it is
the one this errs in.

The row carries no address: `user_id` is the opaque `sub`. Join
`collab_invitations` on `invitation` for the address as invited, which is the
same string Gate B matched — or read `actor_label` off the matching
`service_access.grant` audit row, where `audited()` snapshots it.

##### Clearing one by hand

Add the group in Keycloak, then **settle the row**. An `operator.manual` audit
row does not clear anything — reconciliation reads the state table, so a repair
recorded only in the audit log leaves the item outstanding forever:

```sql
UPDATE collab_service_access_grants
SET state = 'granted', updated_at = now()
WHERE user_id = '<accepter-sub>' AND group_path = '/llm';
```

Then record the hand-run work with an `operator.manual` audit row as usual —
that one is the note that you did it, and the `UPDATE` is the state change.

`granted` is terminal: a later failed attempt will not move a row back out of
it, because a failed retry removes no membership somebody already holds.

There is no automated sweep yet
([#180](https://github.com/openteams-ai/collab-hub-pack/issues/180) records
what is owed; the operator surface that finishes one is
[#176](https://github.com/openteams-ai/collab-hub-pack/issues/176)).

#### The audit trail for a grant (`service_access.grant`)

Separately from the state table, each attempt writes an audit row with
`detail.outcome` of `granted` or `failed` — the history of what was tried and
when, as opposed to what is still due. That write is best-effort: losing it
loses a line of history, not the fact that a grant is owed, which is the whole
reason the state row exists.

```sql
SELECT at, actor, actor_label, target_id, org_id, detail
FROM collab_audit_events
WHERE action = 'service_access.grant'
ORDER BY at, id;
```

`actor` and `target_id` are both the accepter — they are the actor of their own
grant — and `actor_label` is their address as `audited()` snapshotted it.

**Rows written by the admin panel look different.** When an operator adds
someone to a model group from the panel (`POST /admin/api/model-access`,
`frames/model_access.py`), the `service_access.grant` row has:

- `actor` and `actor_label`: the operator who made the change.
- `target_id`: the person added, and `target_label`: the address the panel
  sent with the request.
- `org_id`: `NULL`, because model access belongs to the hub.
- `detail`: `{"group_path": "<group>"}` only, with no `outcome` and no
  `invitation_id`.

The Keycloak call runs inside the audited transaction, so a refusal from
Keycloak rolls the audit row back: a panel row exists only for a change that
Keycloak accepted. Removing someone writes the same shape under
`service_access.revoke`. The panel path writes no
`collab_service_access_grants` row, so the reconciliation query above does not
show it. If Keycloak accepts the change and the database commit then fails,
the membership change stands with no audit row.

To tell the two apart, filter on `detail`:

```sql
SELECT at, actor_label, action, target_id, target_label, detail
FROM collab_audit_events
WHERE action IN ('service_access.grant', 'service_access.revoke')
  AND NOT detail ? 'invitation_id'
ORDER BY at, id;
```

**Membership rides in the token.** A grant does not reach a session that is
already signed in; the group appears on the accepter's next token. If
somebody reports no model access immediately after accepting, a fresh sign-in
is the first thing to try, before this query.

### Connector switches (`collab_connector_state`)

Migration version 10 adds `collab_connector_state`, one row per connector an
operator has switched from the admin panel's Connectors section:

| Column | Meaning |
| --- | --- |
| `connector` | Primary key: `google`, `slack` or `github` (`CONNECTOR_KEYS` in `frames/connector_state.py`). |
| `enabled` | `false` switches the connector off. |
| `updated_at` | When it was last switched. |
| `updated_by` | The operator's `sub`. |

A connector with no row is enabled, so a deployment that never opens the
panel behaves as it did before the table existed. The table holds no
credentials: deployment configuration decides whether a connector can work at
all, and this row decides whether it is currently offered. Switching on a
connector that has no credentials leaves it unusable.

`POST /admin/api/connectors/{connector}` with `{"enabled": true | false}`
writes the row and a `connector.enable` or `connector.disable` audit row in
the same transaction. The connector routes read the table on every request,
with no cache, and blank the credential fields of a switched-off connector, so
its search and read routes answer exactly as an unconfigured one's do from the
next request. Without a database the endpoint answers 503
`connector_switch_unavailable`, and a database that has not yet applied
version 10 reads as nothing switched off. If the table cannot be read on a
request, that request treats every connector as switched on and logs
`connector_state_unavailable`.

The status routes are the exception, because `not_connected` tells a client
to offer the user a connection that cannot succeed. While a connector is
switched off, its status routes answer 404 with
`{"detail": "The <connector> connector is switched off on this hub"}` and
`GET /v1/connectors` leaves its entries out (see
[connector discovery](connector-discovery.md)), so a client shows it as not
offered here rather than as broken. `google` covers
`/v1/connectors/google-drive/status`, `/v1/connectors/gmail/status` and
`/v1/connectors/google-calendar/status`. A connector that was never switched
and merely has no credentials still answers 200 `not_connected`. Each such 404
logs `connector_status_switched_off` with the connector's key at debug level
(not info, because clients poll these routes). The 404's `detail` ("The
`<key>` connector is switched off on this hub") tells it apart from a 404 for
a path nothing serves; with debug logging on, so does that log line. This is the
switch to use while a provider's OAuth app is not yet usable by this
deployment's users — still in the provider's verification process, say —
and to flip back when it is.

To see what is switched off:

```sql
SELECT connector, enabled, updated_at, updated_by
FROM collab_connector_state
ORDER BY connector;
```

### Invitations

The invitation surface exists only where organizations are resolved from
membership (`frames.auth.orgSource=membership` or `single`) and the shared
`frames.postgres` URL is set. Elsewhere the routes are either
not mounted at all (claims-sourced deployments) or answer 503
`invitations_unavailable` — invitations write `collab_org_members`, and a
"successful" invitation that grants nothing is worse than an absent feature.

Seven routes, in three groups whose split *is* the authorization model:

| Route | Who |
| --- | --- |
| `POST /v1/operator/invitations` | platform operator; body `{email, org_id?}` |
| `GET /v1/operator/invitations` | platform operator; every invitation |
| `POST /v1/operator/invitations/{id}/revoke` | platform operator |
| `POST /v1/orgs/{org}/invitations` | owner of `{org}`; body `{email}` |
| `GET /v1/orgs/{org}/invitations` | owner of `{org}` |
| `POST /v1/orgs/{org}/invitations/{id}/revoke` | owner of `{org}` |
| `POST /v1/invitations/accept` | any authenticated login; body `{token}` |

An operator may invite into any organization or into none; omitting `org_id`
issues the **org-creating** invitation whose accepter becomes the owner of a
brand-new organization. That is the bootstrap invitation, and it is
operator-only by construction — the owner surface has no field for it. An
owner naming an organization they do not own is a plain 403.

Acceptance is the *only* thing that creates a membership row. Registering an
account grants nothing, and neither does being invited.

#### Whether a verified address is also required

Gate B is **two** checks: the accepter's address is verified by the identity
provider, *and* it equals the invited address. `frames.invitations.requireVerifiedEmail`
(default `true`) controls the first. The second is not configurable and never
becomes conditional, and a test asserts that relaxing one does not relax the
other. In strict mode that keeps the invitation from being a bearer token
redeemable under any identity; in relaxed mode it keeps the recorded address
honest, which is a smaller claim — see what the match does and does not buy,
below.

**The argument for `false`.** The invitation token is already proof of mailbox
control: a 256-bit secret delivered only to the invited address, which is
exactly what a verification link proves. Requiring both is defence in depth
rather than one necessary check, and the second round trip is real friction —
Keycloak's verification link does not return the reader to the invitation.

**What it costs, stated so the choice is informed.** Turning this off means also
turning off verification at the identity provider (below), and at that point the
`email` claim is **self-asserted** — whoever holds the link can register an
account typing the invited address, or point an existing account at it, and
redeem. So the invitation becomes usable by **anyone who obtains the link**;
forwarding and shared mailboxes are the mundane cases, not the boundary.

What a link-holder receives is the organization membership and role the
invitation grants, **plus whatever `frames.serviceAccess.grantOnAcceptance`
grants at acceptance** — identity-provider group membership included, which on
most deployments is the access that actually matters. And the account carries
the invited person's address permanently, so the member list is misleading
afterwards too.

**What the retained address match does and does not buy.** With verification
required it is an access control: the accepter proved control of the invited
address, and the match ties that proof to this invitation. Without it the match
is a *labelling* property — the address recorded on the membership row is the
invited one — and not a barrier, because the claim it compares is unverified.
Keeping it unconditional is still right, and it is not what stands between a
link-holder and the grant.

What the token still handles regardless: a revoked, expired or already-used
invitation is refused, and it is single-use.

**Turning it off takes two changes, not one.** The realm must also stop forcing
verification:

```sh
kubectl exec -i -n keycloak keycloak-keycloakx-0 -- bash -s <<'REMOTE'
/opt/keycloak/bin/kcadm.sh config credentials --server http://localhost:8080 \
  --realm master --user admin --password "$KEYCLOAK_ADMIN_PASSWORD" >/dev/null 2>&1
/opt/keycloak/bin/kcadm.sh update realms/nebari -s verifyEmail=false
/opt/keycloak/bin/kcadm.sh get realms/nebari --fields verifyEmail
REMOTE
```

The `config credentials` line is not optional: each `exec` is a fresh session
with no `~/.keycloak/kcadm.config`, so a bare `kcadm.sh update` exits with "No
server or realm configured" and **changes nothing**. Since the server setting
ships first, an operator who missed that would believe both halves were done
while Keycloak still refuses tokens to unverified accounts — which presents
exactly as "nothing appears to happen". Read the last line back rather than
trusting the exit status.

Without that, Keycloak will not issue a token to an unverified account at all,
so the acceptance check is never reached.

**There is no ordering with a harmless middle, so keep the window short.** The
setting drives the invitation copy as well as the check, which means both
intermediate states are wrong — in opposite directions and to very different
degrees:

| Middle state | What an invitee hits |
| --- | --- |
| **server relaxed, realm still verifying** (ship server first) | The email omits the verification step, but Keycloak still sends the mail and still shows its own "verify your email" screen. They can finish unaided; the email simply did not warn them. |
| **realm flipped first, server still strict** | The email tells them to look for verification mail the realm no longer sends, *and* acceptance then refuses them. Nothing they can do. |

So **ship the server setting first** — not because that window is harmless, but
because the invitee can get themselves out of it and the other way they cannot.
Then flip the realm promptly, and prefer not issuing invitations in between.

**Invitations already in flight carry the copy for the previous setting.**
Anything sent in the preceding 48 hours (the TTL) tells its reader to expect a
verification email, and after the realm flip that mail is no longer sent — so
those invitees register, wait, and stop. That is the stranding this change
exists to prevent, arriving through the transition itself. Either wait out the
TTL before flipping the realm, or revoke and reissue the live invitations
afterwards so they carry the new copy.

**If somebody is stuck with this relaxed, do not tell them to verify.** On a
deployment with the setting off, the `email_not_verified` outcome no longer
means "unverified" — it is reachable only when the **signed-in account**
carried no usable email address, and no verification mail exists to complete. Setting
`emailVerified` on the account fixes nothing either. Check the account's email
address at the identity provider and the client's `email` scope instead. The
API's message distinguishes the two cases; the wire code does not.

**When to leave it `true`.** Any deployment whose invitees arrive through an
identity provider with `trustEmail` — those accounts are verified on creation,
the check costs nothing, and the invitee never sees a verification step.
Relaxing it there buys nothing and gives up the forwarded-mail protection for
free.

**The invitation email follows this setting.** The onboarding copy's
verification instruction is conditional on the same value, so a deployment that
relaxes the check stops telling invitees to watch for a verification email. That
is one setting rather than two on purpose: two switches would eventually be set
differently, and the failure is silent — people waiting for mail that is no
longer sent, which is the defect
[nebari-nexus-pack#171](https://github.com/nebari-dev/nebari-nexus-pack/pull/171)
fixed, with the sign flipped. A test asserts the copy and the rule read one
value.

#### The invited address must match exactly

The invitee has to sign in with the address the invitation was sent to. The
comparison folds **ASCII case and nothing else** (the amendment on
[#157](https://github.com/nebari-dev/nebari-nexus-pack/issues/157)), so
`Alice@example.com` and `alice@example.com` are the same invitation, while
plus-tags and dots are **not** folded: `a+tag@example.com` is a different
address from `a@example.com`. There is no provider ruleset beyond that. A
mismatch does **not** consume the invitation, so the fix is for the right
mailbox to accept, or for you to revoke and reissue.

Whether the IdP must also assert `email_verified` as a boolean `true` depends
on `frames.invitations.requireVerifiedEmail` — see
[Whether a verified address is also required](#whether-a-verified-address-is-also-required)
above. The address match described here applies either way and is not
configurable.

The match is against the claim presented *at acceptance time*. If someone
changes their verified address after being invited, the old invitation stops
working for them and starts working for whoever now holds the invited
address, with no server-side state to fix up.

#### The token

Generated server-side (256 random bits), stored only as a SHA-256 hash, valid
for **48 hours** (shortened from 7 days by collab-hub-pack#131 as the
compensating control while the web pages rendered the redemption link; the
display is gone — see
[what this replaced](web-surface.md#what-this-replaced-and-how-it-lapsed) — and
restoring 7 days is a decision with its own receipt, not yet taken),
and single-use. Invitations issued before the change keep their own stored
`expires_at`. It travels in exactly two places: the email body, and the
`POST /v1/invitations/accept` request body. It is in no response
body, no URL, no log line, and no audit row — do not put one in a ticket, a
chat message, or an `operator.manual` `detail`. To find out what happened to a
particular invitation, use its **id**, which is opaque and safe to quote.

If `delivery_status` in the create response is anything other than
`provider_accepted`, the invitation exists but its email may not have been
sent. Delivery deliberately runs *after* the database transaction commits (a
rollback cannot un-send a message), so this is the failure that is left. There
is no resend endpoint: revoke the invitation and issue a new one.

#### A prefixed deployment must set `server.rootPath`

The accept route's membership exemption matches the request path with any
configured `root_path` stripped. Behind a proxy that forwards a URL prefix
while `server.rootPath` is unset, the accept path never matches, so a
hardened deployment refuses the invitee with `no_organization` before routing
ever happens. This fails **closed** — an invitee is turned away, nothing is
bypassed — and the symptom is distinctive: every acceptance answers
`no_organization` while signed-in members use the API normally. The fix is
configuration, not code: set `server.rootPath` (env
`COLLAB_HUB_API__SERVER__ROOT_PATH`) to the prefix, exactly as the
protection map already requires for the same reason (see
`standalone-deployment.md`). The 422-redaction rule on this path
deliberately suffix-matches so it holds in this misconfiguration too; the
membership exemption cannot follow suit, because widening an
*authentication-level* carve-out by suffix would let any path ending in the
accept path's name skip the membership requirement.

#### Reading invitation activity in the audit log

```sql
SELECT at, actor, actor_label, action, target_id, target_label, org_id, detail
FROM collab_audit_events
WHERE action IN ('invitation.send', 'invitation.revoke', 'invitation.redeem', 'org.create')
ORDER BY at, id;
```

One row per action, and the action names the *consequential* thing:

- `invitation.send` — issued. `target_id` is the invitation id, `target_label`
  the invited address, `org_id` the target organization or NULL for an
  org-creating invitation. Operator-issued and owner-issued rows are
  identical apart from `actor`.
- `invitation.revoke` — revoked. Revoking an already-revoked invitation is an
  idempotent no-op and writes **no** row: a revoke that changed nothing is not
  an action.
- `invitation.redeem` — accepted into an **existing** organization.
- `org.create` — accepted an org-creating invitation, with the **accepter** as
  actor. This is the one asymmetry worth knowing: such an acceptance produces
  an `org.create` row and *not* an `invitation.redeem` row, because creating
  the organization is the consequential act. The invitation is still named, in
  `detail.invitation_id`. To follow one invitation end to end, query on that:

```sql
SELECT at, actor, action, org_id, detail
FROM collab_audit_events
WHERE target_id = '<invitation-id>' OR detail->>'invitation_id' = '<invitation-id>'
ORDER BY at, id;
```

A *failed* acceptance writes nothing at all — expired, revoked, replayed,
unverified, mismatched, and already-in-an-organization each roll their whole
transaction back. Absence of a row means the invitation was not redeemed, and
the invitation table's own `status` is the record of what it is now.

#### Listing invitations is paged

Both list endpoints are bounded: `limit` defaults to 50 and is capped at 200,
`offset` walks the pages, and the response carries `has_more`. A `limit` above
the cap or below 1 is refused with a 422 rather than silently clamped, so a
script asking for everything learns that it cannot. Ordering is
`created_at DESC, id` — a total order, so consecutive pages neither repeat a
row nor skip one.

#### Issuing twice for one address

**Scoped to the operator page, not deployment-wide.** The browser operator page
(`/admin/invitations`) calls `create_unless_live`, which **refuses to mint
while a `pending`, unexpired invitation for the same address exists** and
returns that invitation instead; the page then tells the operator to revoke it
and issue again. A refusal writes no audit row: it is not an action.

The `/v1` operator and owner routes still call `create`, which mints
unconditionally — their contract is unchanged. So an address **can** hold two
live invitations if one of them was issued through the API. The rule exists for
the case it is scoped to: a human retrying on the page after an ambiguous send.
Unifying the two paths would change the semantics of a shipped endpoint the
desktop is built against; re-send policy (including rotation) is issue #93.

To ask whether an address holds more than one live invitation, query it:

```sql
SELECT email, count(*) FROM collab_invitations
WHERE status = 'pending' AND expires_at > now()
GROUP BY email HAVING count(*) > 1;
```

The rule runs inside the audited transaction behind
`pg_advisory_xact_lock(0x494E, <sha256 of the address>)`. The lock is what
makes it hold under concurrency — a row that does not exist yet cannot be
locked, so a check-then-insert would let two simultaneous issuances (a
double-submitted form) both read "none" and both insert. It is transaction
scoped, so it is released by the transaction ending, on commit or rollback.

Matching is exact, like every other address comparison here (Gate B):
`Alice@example.com` and `alice@example.com` can each hold a live invitation.

If you see contention on advisory locks in `pg_locks` with `classid = 18766`,
that is this rule; it is held for the duration of one issuance only.

#### What bounds abuse on this path (nothing, yet)

No quota, cap, or rate limit applies to invitation issuance on this build. An
operator or owner can issue invitations as fast as they can call the endpoint,
and nothing bounds how many pending invitations an organization accumulates.
This is deferred to issue #93, which owns per-organization and per-operator
invitation-email rate limits; rate limiting the redemption endpoint is Phase 5
abuse-control work. Until then the control is observational — `invitation.send`
rows in the audit log, and the `collab_invitations` query above.

#### Isolation level

The service is written against **READ COMMITTED**, PostgreSQL's default: the
`FOR UPDATE` row lock and the `collab_org_members` primary key resolve every
concurrent acceptance by blocking and re-reading. If a deployment sets
`default_transaction_isolation` to `repeatable read` or `serializable` — on
the database or on the role — those races abort with a serialization failure
instead. Correctness is unaffected either way (an aborted transaction leaves
no membership, no organization, and no audit row), so the app does **not**
refuse to start; it retries the whole mutation up to three times, which turns
the abort back into the terminal state the wire contract promises rather than
a 503. Retries are logged as `invitation_transaction_retry`; a steady stream
of them means the isolation level is costing latency for no benefit here.

#### `org_id` is immutable, and the database enforces it

A trigger refuses any `UPDATE` that changes `collab_invitations.org_id`,
including one you run by hand:

```
ERROR:  collab_invitations.org_id is immutable (invitation <id>)
```

This is not tidiness. Acceptance decides whether it is creating an
organization from a read taken *before* its transaction, and re-checks only
`status` inside it — so a moved `org_id` could commit a join while the audit
log recorded the creation of an organization that never existed. If you need
an invitation to point somewhere else, revoke it and issue a new one.

#### Invitation state, read directly

```sql
SELECT id, org_id, email, status, created_at, created_by, expires_at,
       accepted_at, accepted_by, accepted_org_id, revoked_at, revoked_by
FROM collab_invitations ORDER BY created_at DESC;
```

`status` is only ever `pending`, `accepted`, or `revoked`. **`expired` is
derived**, not stored: a `pending` row whose `expires_at` has passed is
expired everywhere the API presents it, and no sweeper process exists or is
needed. `token_hash` is the only trace of the secret; there is no column, and
no query, that can recover a link.

### The Cog catalog (`collab_cog_artifacts`)

Migration version 11 adds the Cog catalog: one row per artifact the indexer has
seen in a configured registry source, keyed by `(source_id, repository,
digest)`. It rides the same shared `frames.postgres.url` and the same
`autoMigrate` switch as every other `collab_` table.

- **Identity is the digest.** `cog_id`/`name`/`version`/`kind`/`publisher` are
  search keys read from the Cog's own declarations; the repository path is not
  identity (published names carry an id suffix, and one Cog may be published to
  several repositories, each of which is its own row). The pinned install
  reference `<host>/<repository>@<digest>` is rebuilt from the row.
- **`card` is the bundle reader's output, verbatim, as `jsonb`** — the whole
  profile as structured data. A GIN index (`jsonb_path_ops`) on it serves the
  containment filters (`card @> …`) the catalog read API
  ([cog-registry.md](cog-registry.md#read-api)) uses for
  requires/provides/io; plain indexes cover `cog_id`, `kind` and `removed_at`.
  With no `frames.postgres.url` the read API answers `503
  cog_catalog_unavailable` rather than an empty catalog, unless the
  development override `cogs.catalog.backend=memory` is set (level 1 of
  `dev/`; no chart value).
- **`status`** is `indexed` (the reader produced a card — a draft or a Cog with
  a broken profile is still a Cog and lists with what it declared, its errors
  mirrored into `read_errors`), `non_cog` (the manifest carries no `COG.md` and
  no Prog `pixi.toml`; recorded with a reason so a repository full of images is
  not re-read every sweep) or `failed` (the fetch or read failed; retried on
  the next sweep).
- **Rows are never deleted.** An artifact that disappears from its registry is
  marked `removed_at = now()` and stays readable by digest — installs and runs
  may reference it long after its publisher removed it. A digest that comes
  back is restored (and retagged) without a refetch.
- **A removed source's blob descriptors are collected; its rows are not.**
  When a source leaves `cogs.registry_sources`, its catalog rows stay as they
  are, and the descriptors the pull path recorded for its manifests
  (`collab_cog_manifest_blobs`) are deleted by the indexer once the source has
  gone unseen in the configuration for `cogs.index.removedSourceRetentionHours`
  (default a week), at most 500 manifests per sweep. Migration version 16 adds
  `collab_cog_source_activity`, one `last_configured_at` stamp per source id,
  which every sweep and every first recording of a manifest's descriptors
  moves forward. The rules, including rollouts and re-adding a source, are in
  [cog-registry.md](cog-registry.md#removing-a-source). To get the space back
  without waiting, or on a deployment that does not run the indexer, delete
  them by hand once **no release using this database still lists the source**:

  ```sql
  -- What is still stored, per source, and when each was last seen configured.
  SELECT b.source_id, count(DISTINCT b.manifest_digest) AS manifests, count(*) AS descriptors,
         s.last_configured_at
  FROM collab_cog_manifest_blobs b
  LEFT JOIN collab_cog_source_activity s USING (source_id)
  GROUP BY b.source_id, s.last_configured_at;

  -- The descriptors of one removed source. Nothing else: never delete from
  -- collab_cog_artifacts, which is the catalog's history.
  DELETE FROM collab_cog_manifest_blobs WHERE source_id = '<removed source id>';
  ```

  Descriptors grant nothing on their own, so this cannot open a pull; if the
  source is configured again, its manifest reads record them again.

The indexer is a reconciliation loop: enumerate each source, skip digests
already indexed with the same tag set, fetch and read only new digests
(`COG.md` first, then the profile file it names — never the lockfile, by
title **or** media type), then mark this source's rows whose digest is no
longer present as removed. A card the database cannot store (a NUL character
anywhere in its JSON, or an oversized card) becomes a `failed` row with a
reason instead of a sweep-aborting error. Fetching is budgeted per sweep
(already-known digests cost nothing), so a large registry's tail is reached
across successive sweeps rather than starved; deferred artifacts still count
as present, so removal stays correct. Safety rules operators should know:

- **Single flight is the deployment's shape; the lock is a belt.** With
  `cogs.index.enabled` the chart renders a dedicated **indexer** Deployment
  (`<release>-indexer`, issue #148): the API image and settings, `replicas:
  1` (fixed in the template, not a value), `strategy: Recreate` and no
  Service — so a rolling update of the API never starts a sweep, and a
  rollout of the indexer never overlaps two. The API replicas render
  `cogs.index.enabled=false` whatever the values say, and keep serving the
  read API. That switch gates the sweep loop alone: every process with
  sources and the catalog database builds the indexer, so the API replicas
  hold its lock-less targeted entry points for the webhook receiver (issue
  #86) to call. Under that shape a sweep
  still takes the session-level advisory lock
  `pg_try_advisory_lock(<"cogidx_1">)` on one pooled connection for its
  whole duration (outside any transaction, so a minutes-long sweep pins no
  snapshot); what the lock catches now is two indexer releases against one
  database — the loser logs `cog_index_sweep_skipped` and waits for its next
  interval. That connection is one slot of the indexer's pool while a sweep
  runs, and it carries the sweep's reads and writes (see the fencing bullet
  below).
- **Removal needs a complete picture — per repository.** Rows are marked
  removed only in repositories whose listing fully succeeded this sweep. If the
  source's *repository list* cannot be obtained, nothing is reconciled and
  nothing is declared gone. If one repository fails to list, or is refused for
  exceeding the per-repository artifact bound, only **that repository's** rows
  are shielded from removal (the summary says `removal skipped for it` and
  counts the source in `sources_failed`); every other repository — and every
  repository that has vanished from the source's list — still reconciles,
  removal included. A transient registry outage therefore never marks a
  catalog gone, and one permanently oversized repository does not freeze
  removal for its neighbours. The adapters uphold their half by **raising past
  their own limits instead of truncating** (a repository over the static
  adapter's tag bound, a Harbor listing over its page bound), and the indexer's
  own artifact bound is a refusal, never a prefix: a truncated list presented
  as complete would turn a bound into false removals, and a prefix cut at the
  same sorted position every sweep would permanently starve what lies behind
  it.
- **The fetch budget is dealt fairly.** Fetch slots go to never-seen artifacts
  and to retries of `failed` rows in a fixed interleave (every fourth slot is a
  retry slot; a class that has run out yields its slot). The interleave's phase
  advances each sweep, and retries are served from a rotating order of
  `(repository, digest)` identities — identities rather than positions, because
  the failed set changes between sweeps and a positional cursor would step over
  a candidate each time it did. A candidate moves to the back of the rotation as
  its attempt *begins*, so a sweep that dies partway does not rotate past work it
  never tried. The effect: even a budget of one reaches a retry within four
  sweeps under a constant stream of new artifacts, and a stable prefix of
  permanent failures cannot occupy the retry slots while a recoverable failure
  behind it waits. Never-seen artifacts keep enumeration order; once indexed
  they cost nothing, so the tail of a large registry is reached across sweeps
  without rotation.
- **Sweep writes ride the lock's session — through the view the lock
  returns.** The lock hands the sweep a connection-bound view of the store,
  and every read and write of the sweep goes through it, so a sweeper that
  dies mid-write loses the write and the lock together: the server drops
  both with the one session, and no write of a dead sweep can land after
  another sweeper has acquired the lock (issue #128). The store itself has
  no notion of a running sweep: its own methods — the targeted webhook
  writes (`reindex` and `mark_removed_one`), a direct call — always check out
  their own pooled, statement-bounded connection, so a push or delete event
  lands concurrently with a sweep and nothing but the view can ride, or die
  with, the lock session (issue #148).
- **A cancelled sweep releases the lock or dies with its session.** Nothing
  drains: a store call in flight is not waited for. Every store call of a
  sweep — the acquisition, the view's reads and writes, the release — runs
  on one thread of the sweep's own, in submission order, so a cancelled
  sweep queues its release behind the call it abandoned and moves on; the
  unlock runs the moment that call returns and can neither overtake it nor
  be dropped by a later cancellation or by shutdown. If the pod exits first,
  the session ends and the server drops the lock and the write together —
  which, for a one-replica workload that is recreated rather than rolled, is
  the designed outcome. The app lifespan waits at most 5 s for the cancelled
  indexer task (in practice it returns at once, since it waits for nothing)
  and otherwise leaves it pending with `cog_indexer_shutdown_abandoned`.
  Sweep-path statements carry a 20 s server-side `statement_timeout` and
  every pooled connection sets TCP keepalives, which covers the ordinary
  partition; a peer whose kernel still answers probes while the database
  process is stopped is ended by the pod's `SIGKILL` at
  `terminationGracePeriodSeconds`.
- **A doubtful lock connection is discarded, not returned.** If any step of
  taking or giving back the lock fails (setting autocommit, the acquire, the
  unlock, `RESET statement_timeout`, restoring autocommit), the connection is
  **closed** (`cog_index_lock_connection_discarded`) so the pool opens a fresh
  one instead of handing a session with an altered timeout — or one still
  holding the lock — to the next borrower.
- **Indexing needs two pooled connections — on the indexer.** The sweep
  occupies one connection of the indexer's `frames.postgres` pool for its
  whole duration (the lock is held on it and the sweep's reads and writes
  ride it), so the sweeping process refuses to start with `cogs.index.enabled`
  and `frames.postgres.pool.max_size < 2`: everything else it does — the
  catalog reads and webhook writes it can also serve — needs a connection
  while a sweep runs. The check never constrains an API replica, which the
  chart starts with `cogs.index.enabled=false`.

Every sweep logs one `cog_index_sweep` line (indexed / skipped / retagged /
non_cog / failed / removed / sources_failed / collected / duration) and exports
metrics: `frames_server_cog_index_artifacts_total{outcome}`,
`frames_server_cog_index_sweeps_total{result}` (`completed`, `locked_out`,
`failed`, or `cancelled` for a sweep a shutdown interrupted) and
`frames_server_cog_index_sweep_duration_seconds`. Per-artifact failures are
stored in the row's `read_errors` and never abort the sweep.

On content and secrecy, precisely: the `card` is the published bundle's own
declarations, stored verbatim — whatever a publisher writes in `COG.md` or the
profile is what the catalog holds. The guarantee is narrower and absolute:
*configured registry credentials* never reach cards, `read_errors`, or logs;
`read_errors` and log lines carry exception class names (plus the message only
for the OCI client's own errors, whose contract is that messages name neither
URLs nor headers), never raw URLs or exception chains.

### Connection pooling

All Postgres-backed stores draw connections from a shared `psycopg_pool`
connection pool — one pool per distinct database URL, so in the common
single-URL deployment every relational feature shares one pool. Stores never
open per-request connections. Pool behavior is tunable under
`COLLAB_HUB_API__FRAMES__POSTGRES__POOL__*`:

- `MIN_SIZE` (default `1`) / `MAX_SIZE` (default `10`) — pool bounds. They are
  validated at startup (`0 ≤ minSize ≤ 500`, `1 ≤ maxSize ≤ 500`, and
  `maxSize >= minSize`); an invalid combination fails configuration parsing
  rather than producing a pod with an unusable pool.
- `TIMEOUT_SECONDS` (default `5.0`, max `60`) — how long a request waits for a
  connection when the pool is exhausted **or Postgres is down** before the
  API answers `503 database_unavailable`.
- `MAX_WAITING` (default `50`) — how many callers may queue for a connection at
  once. Once the queue is full, further callers fail immediately with
  `TooManyRequests` (also `503 database_unavailable`) instead of piling up
  behind a saturated pool. `0` disables the bound (psycopg's own default).

Lifecycle and failure semantics:

- Pools open at app startup **without waiting** for Postgres, so a database
  outage at boot does not crash the pod; requests needing the DB fail with
  `503 database_unavailable` until it recovers. (With `auto_migrate` enabled
  the startup DDL still needs a live database, as before.)
- Pools close at app shutdown.
- `GET /health/db` reports per-pool status (`SELECT 1` round-trip): `200`
  with `{"status": "ok"}` when healthy, `503` with `{"status": "unavailable"}`
  when any pool cannot reach its database, and `{"status": "not_configured"}`
  when no Postgres URL is set. `/health` stays a pure liveness probe — a
  Postgres outage must not make Kubernetes restart otherwise-healthy pods.
- Best-effort accounting — seen-user capture, which runs inside auth for HTTP
  **and** MCP traffic — never touches the database on a request thread. The
  request does its in-memory throttle bookkeeping and hands the write to a
  small pool of background daemon threads, which is what keeps a database that
  is slow *after* handing over a connection from holding an authenticated
  request or blocking the MCP event loop. Everything about that dispatch is
  bounded:

  - The handoff is a non-blocking enqueue onto a bounded queue (100). A full
    queue drops the write instead of waiting for room.
  - Two worker threads run the writes. A database that never answers can park
    them and hold their connections — an in-process client cannot escape that —
    but it can never park more than two, and never a request thread.
  - Each write checks out with a near-zero acquisition budget (50 ms) instead
    of the full pool timeout, and runs under a transaction-local
    `statement_timeout` (2 s) with a client-side cancel behind it (3 s), so a
    slow query is bounded by the database rather than by the socket closing.
    That cancel is aimed at one statement only: the connection is not released
    back to the pool until a cancel already in flight has finished, so it can
    never reach the next borrower's query.
  - Dropped and failed writes back the caller off exponentially (1 s, doubling,
    capped at the 5 min throttle window), so repeated requests do not queue the
    same doomed write. Both are counted in
    `frames_server_usage_write_failures_total`, labelled
    `user_seen_dropped` and `user_seen_failed`.
  - At shutdown the writer stops accepting work, abandons what is still queued,
    and waits at most 2 s for writes in progress. Its threads are daemons, so a
    parked one cannot hold up pod termination.

## User Directory

Collab Hub can be configured with Keycloak user-directory access so Apollo clients can
look up users and groups for Frame sharing. This is disabled by default. A Hub
admin must create or reuse a confidential Keycloak client with service accounts
enabled and grant its service account these `realm-management` roles:

- `query-users`
- `view-users`
- `query-groups`

The directory API is a realm-global picker by design. It requires an
authenticated Hub request, but it does not filter results by the caller's
`org_id` or `workspace_id`. Do not enable it for a shared Keycloak realm unless
all authenticated users are allowed to discover the realm's user and group names
for sharing workflows.

Store the client credentials in a Kubernetes Secret in the Collab Hub namespace:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: nexus-user-directory
type: Opaque
stringData:
  client-id: nexus-user-directory
  client-secret: "<client secret>"
```

Then point the chart at that Secret:

```yaml
userDirectory:
  enabled: true
  provider: keycloak
  keycloak:
    issuerUrl: https://keycloak.example.com/realms/nebari
    # Optional; defaults to <issuerUrl>/protocol/openid-connect/token.
    tokenUrl: https://keycloak.example.com/realms/nebari/protocol/openid-connect/token
    adminApiBaseUrl: https://keycloak.example.com/admin/realms/nebari
    existingSecret: nexus-user-directory
```

## User Directory

Collab Hub can be configured with Keycloak user-directory access so Apollo clients can
look up users and groups for Frame sharing. This is disabled by default. A Hub
admin must create or reuse a confidential Keycloak client with service accounts
enabled and grant its service account these `realm-management` roles:

- `query-users`
- `view-users`
- `query-groups`

The directory API is a realm-global picker by design. It requires an
authenticated Hub request, but it does not filter results by the caller's
`org_id` or `workspace_id`. Do not enable it for a shared Keycloak realm unless
all authenticated users are allowed to discover the realm's user and group names
for sharing workflows.

Store the client credentials in a Kubernetes Secret in the Collab Hub namespace:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: nexus-user-directory
type: Opaque
stringData:
  client-id: nexus-user-directory
  client-secret: "<client secret>"
```

Then point the chart at that Secret:

```yaml
userDirectory:
  enabled: true
  provider: keycloak
  keycloak:
    issuerUrl: https://keycloak.example.com/realms/nebari
    # Optional; defaults to <issuerUrl>/protocol/openid-connect/token.
    tokenUrl: https://keycloak.example.com/realms/nebari/protocol/openid-connect/token
    adminApiBaseUrl: https://keycloak.example.com/admin/realms/nebari
    existingSecret: nexus-user-directory
```

## Service Access at Acceptance

Accepting an invitation can add the accepter to identity-provider groups — the
mechanism that gives a new invitee access to served models without an operator
visiting the Keycloak console per person. Disabled by default:
`grantOnAcceptance: []` grants nothing, which is the correct default for any
deployment that has not decided otherwise.

```yaml
frames:
  serviceAccess:
    # Group paths as the provider spells them. Empty means grant nothing.
    grantOnAcceptance:
      - /llm
    keycloak:
      issuerUrl: https://keycloak.example.com/realms/nebari
      # Optional; defaults to <issuerUrl>/protocol/openid-connect/token.
      tokenUrl: https://keycloak.example.com/realms/nebari/protocol/openid-connect/token
      adminApiBaseUrl: https://keycloak.example.com/admin/realms/nebari
      existingSecret: nexus-invitation-provisioning
      # Required unless the credential can read groups -- see below.
      groupIds:
        /llm: fcd9e0f8-d05e-4e13-ae25-97846cfade17
```

### Why the group id is configuration

Keycloak's membership endpoint needs a group **id**, and configuration names
groups the way a person reads them (`/llm`). Something has to bridge the two,
and which way you bridge it decides how much authority this credential needs.

**Supplying the id in `groupIds` is the recommended way, and on a
correctly-scoped deployment it is the only way that works.** Resolving a path
to an id means *reading* groups — `GET /groups` — and a credential scoped to
write group membership and nothing else is refused every read. That is not a
gap in the scoping; it is the scoping working. Measured on collab-hub, the
credential answers 403 to `GET /groups`, `GET /users`, `POST /users`, password
reset, email change, deletion, impersonation, and anything cross-realm, while
`PUT`/`DELETE` on the granted group's membership answer 204.

Two consequences worth knowing:

- **Startup makes no call to the identity provider at all.** A provider blip
  during a rollout cannot stop the API from starting. If the id were looked up,
  it could.
- **A stale id fails at grant time, not at startup.** Going stale takes
  deleting and recreating the group — a deliberate act, not drift — and the
  failure is not silent: it lands as a durable `failed` row that
  [the reconciliation query](#finding-grants-that-are-still-owed) returns.

A path left out of `groupIds` is looked up instead, for a deployment whose
credential holds group-read authority. Both are supported; only one of them
needs a wider credential. Find an id in the Keycloak console (Groups → the
group → the `id` in the URL) or with `kcadm get groups -r <realm>`.

Two startup checks exist because the failure they prevent is quiet: an id
mapped to a path nothing grants refuses to start (usually the other list spells
the path differently, which would leave the real path unmapped), and a value
that is empty or looks like a path refuses too. Nothing stricter — the id's
format is Keycloak's to choose.

**Use a different credential from `userDirectory.keycloak`.** That one is
read-only and this one writes; sharing the configuration would invite sharing
the secret.

### The authority to grant, and the pair never to grant together

With Keycloak's fine-grained admin permissions V2 (`KC_FEATURES` including
`admin-fine-grained-authz:v2`, plus `adminPermissionsEnabled=true` on the
realm), grant the client exactly two scopes:

- `Groups/manage-membership` **on each granted group**, and
- `Users/manage-group-membership`.

Both are required: the group-side scope alone refuses everything, including
adding a member. Measured on Keycloak 26.5, not inferred from the names.

**No read scope, which is why `groupIds` is configuration.** These two permit
writing membership and nothing else, so the credential cannot look a group path
up. Adding a read scope to avoid configuring the id would widen a credential
whose narrowness is the point, and would make startup depend on the provider
being reachable.

**Never grant `Groups/manage-members` on a group this credential can also
change membership of.** `manage-members` confers password reset, email change,
and deletion over members of the group — so on a group holding every invitee,
the two together compose into account takeover: add anyone, then reset their
password. Disjoint groups let one client hold both safely; two clients in one
namespace is separation on paper only.

The credential's boundary is worth re-checking after any realm change. A
correctly scoped one can `PUT`/`DELETE` the configured groups and is refused
password reset, email change, user deletion, impersonation, any other group,
user creation, directory reads, and cross-realm access — all `403`, including
while its target *is* a member of the granted group.

### What the invitee sees, and what they do not

The grant happens **after** the acceptance transaction commits, deliberately: a
group write cannot be rolled back, and an identity-provider outage must not
cost somebody a membership they hold a valid invitation and a verified address
for. So a failed grant leaves a correct acceptance with no service access, and
the acceptance page still says "you are in" — which is true. What makes that
recoverable is that the acceptance transaction records the grant it owes before
the call happens, so a failure at any point leaves a row a reconciler can act
on: [Finding grants that are still owed](#finding-grants-that-are-still-owed).

Group membership rides in the token, so the grant reaches the accepter on their
next token rather than the session they accepted in.

## Model Access from the Admin Panel

The admin panel's Models and access section lists the models the serving
layer offers, shows who is in the group that gates each one, and adds or
removes people from those groups. The serving gateway reads Keycloak group
membership, so the change lands in Keycloak and the hub records that it made
it (`frames/model_access.py`, `frames/group_membership.py`). Disabled by
default.

The chart has no `frames.modelAccess` values, so the settings arrive as
environment variables through `api.deployment.extraEnv`; see
[Configuring the admin panel](standalone-deployment.md#configuring-the-admin-panel)
for a worked example. The settings are `frames.model_access.*` in the API's
config, under `COLLAB_HUB_API__FRAMES__MODEL_ACCESS__`:

| Setting | Env | Meaning |
| --- | --- | --- |
| `catalog_base_url` | `…__CATALOG_BASE_URL` | The serving layer's origin. The panel reads `<catalog_base_url>/v1/models`. Empty shows no models, with `catalog_error: not_configured`, and leaves the rest of the panel working. |
| `keycloak.token_url` | `…__KEYCLOAK__TOKEN_URL` | The realm's token endpoint. Required: unlike the other two credentials, there is no issuer fallback. |
| `keycloak.admin_api_base_url` | `…__KEYCLOAK__ADMIN_API_BASE_URL` | `https://keycloak.example.com/admin/realms/<realm>`. |
| `keycloak.client_id` | `…__KEYCLOAK__CLIENT_ID` | The confidential client whose service account holds the scopes below. |
| `keycloak.client_secret` | `…__KEYCLOAK__CLIENT_SECRET` | Its secret. Supply it from a Secret with `valueFrom`. |
| `group_ids` | `…__GROUP_IDS` | A JSON object from group path to Keycloak group id, for example `{"/models/example-model": "<group id>"}`. It is also the boundary: a group path missing from it is refused before any request is made. |
| `model_groups` | `…__MODEL_GROUPS` | A JSON object from model id to the group path that gates it. A model with no entry is shown as ungated. |

Membership changes are available only when `token_url`,
`admin_api_base_url`, `client_id` and `group_ids` are all set and the shared
`frames.postgres` URL is set, because every change writes an audit row.
Otherwise the endpoints answer 503 `model_access_unavailable` and the panel
shows model access as unavailable. A Keycloak refusal answers 502
`group_unavailable`.

### Why this is a third credential

The service-access credential above can add a new account to a group and
nothing else: it cannot read, so it cannot list members, and it cannot remove
anyone. The panel needs both. Widening that credential would grow the
invitation path's authority because an admin screen needed it, so the panel
uses its own client. Use a different client from `userDirectory.keycloak` and
from `frames.serviceAccess.keycloak`.

Group ids are configuration for the same reason as at acceptance: resolving a
path would need group-read authority, and startup should not depend on the
identity provider being reachable.

### The authority to change membership, and the scope never to grant

Grant the client's service account exactly these three scopes:

- `Groups/view-members`, to list who is in a group;
- `Groups/manage-membership`, and
- `Users/manage-group-membership`, together to add and remove members.

**Never grant it `Groups/manage-members`.** Over a member of a group that
scope also permits password reset, email change and deletion. A credential
that can put somebody into a model group and also rewrite their password can
take over any account it chooses: the two halves are safe apart and are
account takeover together.

The code keeps to that boundary by construction. The client issues only a
member listing and a membership `PUT` or `DELETE`, has no method that touches
an account, and acts only on the groups in `group_ids`. The realm scoping is
the backstop.

### What each change records

Each add or remove writes a `service_access.grant` or `service_access.revoke`
audit row whose `actor` is the operator; see
[The audit trail for a grant](#the-audit-trail-for-a-grant-service_accessgrant)
for the row shape and how it differs from acceptance-time grants. Membership
rides in the token, so a person added from the panel sees the model on their
next token.

## Invitation Email Provider

Invitation email is disabled by default. The SES adapter uses the AWS SDK and
the API pod's workload identity; it does not accept an access key or SMTP
password setting. Provider configuration comes from an existing Kubernetes
Secret with these keys:

- `SES_SENDER_ADDRESS`
- `SES_REGION`
- `SES_CONFIGURATION_SET`

Enable the adapter with:

```yaml
frames:
  email:
    provider: ses
    acceptUrl: https://collab.example.com/invite/accept
    ses:
      existingSecret: collab-hub-invitation-email
```

The chart renders only `secretKeyRef` references for the three SES settings.
The acceptance-page URL must use HTTPS and contain no query string or fragment;
the application appends the one-time secret to the fragment when it renders the
message. The acceptance page must decode that fragment, retain the secret only
in browser memory or session storage, and clear it from the visible URL before
continuing the registration or sign-in flow.

A successful SES API response means `provider_accepted`, not delivered. Final
delivery, bounce, complaint, and delay evidence arrives asynchronously through
the deployment's SES event destination. The adapter does not retry an ambiguous
transport failure because SES `SendEmail` has no idempotency token and the
request may already have been accepted.

The invitation lifecycle (issue #89) owns audited mutations and the invitation
state machine. It does **not** own durable delivery state, resend token
rotation, or per-organization/per-operator rate limits — those are issue #93,
and none of them exists yet. Concretely, on this build:

- **Delivery outcome is reported, not stored.** The create response carries
  `delivery_status` and `delivery_error_code`, and the same outcome is logged
  as `invitation_email_delivery_outcome`. No column records it, so there is
  no query that lists "invitations whose email failed" — an issuer who missed
  the response has to ask the invitee.
- **There is no resend.** The remedy for a failed or lost delivery is to
  revoke the invitation and issue a new one, which mints a new secret. There
  is no endpoint that re-sends the existing one, and there could not be: the
  secret is stored only as a hash.
- **There is no rate limit or quota anywhere in this path.** Nothing bounds
  how many invitations an operator or an owner may issue, or how many pending
  invitations an organization may accumulate. Until #93 lands, that bound is
  operational: watch `invitation.send` rows in the audit log.

Until the lifecycle service is installed, enabling this adapter creates no
invitation endpoint.

## Smoke Validation

Run the API tests before merging Frames changes:

```bash
PYTHONPATH=api/src pytest api/tests
```

For a deployed or locally running Collab Hub API, create a Frame through REST, then
verify the MCP contract with:

```bash
python scripts/smoke_frames_mcp.py \
  --base-url http://127.0.0.1:8000 \
  --frame-id 0123456789abcdef0123456789abcdef \
  --expected-body "# Body"
```

The default smoke helper sends an unsigned test `IdToken-*` cookie. Use that
only against a local/test server started with `FRAMES_UNSAFE_AUTH_ENABLED=true`
and `FRAMES_IDTOKEN_ALLOW_UNSIGNED=true`.

For shared authenticated environments, pass a real Hub bearer token:

```bash
python scripts/smoke_frames_mcp.py \
  --base-url https://hub.example.com \
  --frame-id 0123456789abcdef0123456789abcdef \
  --expected-body "# Body" \
  --bearer-token "${HUB_ID_TOKEN}"
```

Add `--require-stored-active` after setting `/v1/active-frames` for the same
authenticated user.

The pack also includes Collab Hub deployment smokes for the operational cases that
matter for Frames:

- `scripts/smoke_frames_http.py` validates REST, MCP, metrics, and optional active state.
- `scripts/smoke_frames_observability.sh` checks metrics plus structured request/audit logs.
- `scripts/smoke_frames_s3.sh` installs an S3 store (SeaweedFS) and validates S3-backed Frame content.
- `scripts/smoke_frames_postgres_active_state.sh` installs Postgres and validates stored active-frame state.

## Identity Inventory Dry Run (Read-Only)

Every ACL principal the Frames server has stored so far is whatever the legacy
claim precedence (`preferred_username`, then `email`, then `sub`) resolved at
write time. Pinning the principal to the immutable `sub` on a deployment that
already holds data is a data migration, and the migration must not be attempted
before someone has read a report of what it would do.

`python -m collab_hub_api.identity_inventory` produces that report. It
**writes nothing** — not to Postgres, not to S3, not to the frames directory,
not to Keycloak. The production rewrite is a separate, later step.

### Running it

The tool reads the same environment variables the API pod already has, so the
simplest place to run it is a one-off pod (or `kubectl exec`) using the API
image and the API's own configuration:

```bash
python -B -m collab_hub_api.identity_inventory \
  --output /tmp/identity-inventory.md \
  --json-output /tmp/identity-inventory.json
```

`-B` disables bytecode caching, which is the one filesystem write Python
performs unbidden. The API image also sets `PYTHONDONTWRITEBYTECODE=1`; either
is sufficient, and without one of them importing the package could create
`__pycache__` directories and make the no-writes claim false.

Variables consulted: `COLLAB_HUB_API__FRAMES__STORAGE_BACKEND`,
`...__STORAGE__FRAMES_PATH`, `...__FRAMES__S3__{BUCKET,PREFIX,ENDPOINT_URL,REGION}`,
`...__FRAMES__POSTGRES__URL`, and
`...__USER_DIRECTORY__KEYCLOAK__{ISSUER_URL,TOKEN_URL,ADMIN_API_BASE_URL,CLIENT_ID,CLIENT_SECRET}`.

Secrets are accepted only from the environment; there is no command-line flag
for the client secret or the database URL, because anything on `argv` is
visible in `ps` on a shared host. The database URL is redacted (host, port, and
database only) everywhere it is printed.

Useful flags:

- `--directory-json users.json` maps against an exported Keycloak user list when
  the admin API is not reachable from where the report is being produced.
- `--skip-postgres`, `--frames-backend none` scope a partial run. Whatever is
  skipped is reported as **not scanned** in the coverage table rather than
  quietly omitted.
- `--redact` replaces every principal with a stable `principal:<hash>`
  pseudonym.

Exit codes:

| Code | Verdict | Meaning |
| --- | --- | --- |
| 0 | `clear` | Every source scanned, and every frame, group, and task-owner document keeps an owner whose mapping is **certain**. Nothing else returns 0. |
| 1 | `blocked` / `needs_human_confirmation` | Findings that need a person: orphans, ambiguity, suspected reassignment, or entities resting only on unverified matches. |
| 2 | `incomplete_scan` | A source was skipped, a table was missing, a record could not be read, or the run errored. |

**Coverage gaps force a non-clear verdict.** This is deliberate and is the most
important property of the tool: a partial scan can only establish that nothing
was found *where it looked*, and the record it could not read may be the orphan.
`--skip-postgres` and a missing table are therefore both `incomplete_scan`, not
"clean".

### Least-privilege credentials

The tool refuses to write, but it should not be given the ability to:

- **Postgres** — create a role with `CONNECT` on the database, `USAGE` on the
  schema, and `SELECT` on the frames/task tables, and nothing else. Independent
  of the role, every connection is opened with
  `options=-c default_transaction_read_only=on` and the session is verified with
  `SHOW transaction_read_only` before the first query, so the *server* rejects a
  write even if the role could perform one. **That server-side transaction is
  the guarantee.** The tool's own `SELECT`-only statement guard (which also
  rejects multi-statement strings and data-modifying CTEs) sits in front of it
  as defence in depth, not as an independent layer.
- **S3** — an IAM policy granting only:

  ```json
  {
    "Version": "2012-10-17",
    "Statement": [
      {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": "arn:aws:s3:::FRAMES_BUCKET"},
      {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": "arn:aws:s3:::FRAMES_BUCKET/*"}
    ]
  }
  ```

  A botocore `before-call.s3` hook additionally raises on any operation outside
  the read allowlist, which covers paginators and any future call path.
- **Keycloak** — the existing user-directory client credentials. The tool only
  issues `GET /users`, so the read-only `query-users`/`view-users` the member
  picker already needs are sufficient; no additional role is required.

### Reading the report

The report is ordered by what the go/no-go decision needs: the verdict, then
**coverage** (a source that was not scanned proves nothing), then the **orphan
check**, then ambiguity, then unmapped principals, then the proposed mapping,
then the carrier inventory.

- **Orphan check** — every frame, group, and `nexus_task_state` document must
  keep at least one owner that maps to a live subject. One that does not is
  unmanageable after the migration: nobody can publish, rename, re-own, or
  delete it through the API. The severities are reported separately because they
  have different causes and different fixes: "no owners recorded", "no owner
  maps", "owner account newer than the record", "only unverified owner
  mappings", and "every mapping owner is disabled".
- **Ambiguous principals** — one stored string matching two accounts (typically
  one person's username equalling another's email). Never mapped automatically;
  choosing wrong hands one person's frames to another.
- **Unmapped principals** — left exactly as stored. `readers` are arbitrary,
  unvalidated strings, and an address that never matched an account still
  records an intent to grant; deleting it during a migration silently revokes
  access nobody agreed to revoke. Principals stored with surrounding whitespace
  are likewise reported and never trimmed: the service compares exactly, so
  trimming would invent access that does not exist today.

### Mapping confidence: why a match is not proof

The report labels every mapping, and the label decides what it is allowed to
conclude:

- **`certain`** — the stored value *is* a subject. Subjects are immutable and
  never reissued, so this is the only mapping that clears an entity by itself.
- **`unverified`** — matched on an email or username. Those claims are mutable
  and reusable. If someone left and their address was later assigned to a new
  hire, a point-in-time read of Keycloak finds exactly one candidate — the new
  hire — and nothing in the data says the string ever meant anyone else.
  Migrating on that evidence hands the departed person's content to somebody who
  was never given it.

So an entity whose owners are all `unverified` is reported as **needs human
confirmation**: a distinct outcome from both "clear" and "orphaned", with a
distinct fix — somebody has to confirm it.

One signal for reassignment is available and is used: an account whose Keycloak
`createdTimestamp` is *later* than the record it would be mapped into cannot be
the account that principal named when it was written. Those mappings are
discarded, not merely downgraded. The check is one-sided — a reassignment within
an account's own lifetime leaves no trace in a point-in-time read — which is
exactly why `unverified` stays `unverified` even when the timestamps look fine.

### The report contains personal data

It lists emails and usernames alongside what they can access, so treat the
rendered file as a production access record: it is written `0600` (and an
existing file's mode is tightened on rewrite, so yesterday's `0644` report does
not stay world-readable), and it should not be attached to tickets or pasted
into chat.

The report writer refuses a symlink output path before opening or truncating
anything. The local-frame scanner likewise treats a symlinked frame directory
or `metadata.json` as an incomplete scan instead of following it outside the
configured frames root.

`--redact` replaces every principal, every target subject, every account label,
and every identity embedded inside an entity id or storage path — several
carriers build their ids out of the principal itself — with a stable
`principal:<hash>`. That is pseudonymisation, not anonymisation: anyone holding
the user list can reverse it, so a redacted report is safer to circulate, not
safe to publish. Delete the un-redacted report once the migration decision is
made.

## Identity Rewrite (Writes)

The counterpart of the dry run above: `python -m collab_hub_api.identity_rewrite`
performs the migration the inventory describes. Run it only inside an agreed
window, with the API scaled to zero — a write that lands mid-rewrite is recorded
under a legacy principal that no longer exists anywhere else.

**Every rewrite rests on an unverified mapping.** The confidence model has two
levels: `certain` means the stored value is already a subject and needs no
change, and `unverified` means it matched a mutable email or username. So every
principal this tool changes is, by construction, an unverified match. That is
why the mapping is an *input* — reviewed by a person in `inventory.md` — and
never re-derived here. The tool never contacts Keycloak.

### Running it

```bash
python -B -m collab_hub_api.identity_rewrite \
  --inventory /var/frames/inventory.json \
  --manifest  /var/frames/rewrite-manifest.json \
  --apply
```

Without `--apply` it plans, reports, and writes nothing. `--apply` requires
`--manifest`: a rewrite with no record of what it changed can be neither
reviewed nor reversed.

**Quiescing the deployment is yours to do, and the tool cannot check it.**
Nothing detects a running API. Scale it to zero first: a write landing
mid-rewrite is recorded under a legacy principal that no longer exists anywhere
else, and no exit code will report it.

Note the path. The API container runs with `readOnlyRootFilesystem: true`, so
`/tmp` is not writable; `frames.storage.mountPath` (default `/var/frames`) is.
That volume is an `emptyDir` on an S3-backed deployment — **copy the manifest
out before scaling the deployment back up, or it is lost with the pod.**

### What it refuses

| Refusal | Why |
| --- | --- |
| A `--redact`ed report | Its principals are `principal:<hash>` pseudonyms. Applying one would rewrite live ACLs to hashes. |
| `ambiguous` | Two or more subjects match. Never migrated automatically. |
| `reassignment_suspected` | The matching account is newer than the data; the address may have been reassigned. |
| `padded` | The value carries surrounding whitespace. Trimming it to reach an account would *invent* access. |
| A blocking orphan | An entity would be left with no live owner. |

The last four stop the run until acknowledged by name
(`--acknowledge ambiguous`). Acknowledging one does **not** rewrite it — nothing
in that list is ever rewritten — it records that a person read the finding and
chose to proceed without it.

`unmapped` principals are left exactly as they are, with no acknowledgement
required. A reader address that never matched an account still expresses an
intent to grant, and deleting it during a migration silently revokes access
nobody agreed to revoke.

### A match is only substituted where an identity belongs

Documents are swept **whole** — that is how the scanner found the carrier its
first draft missed — but a value equal to a mapped principal is *rewritten* only
under an identity-bearing key (the same `IDENTITY_KEYS` set the scanner uses), or
at a path you name with `--allow-path '$.some_field'` after reviewing it.

One column is the exception, and it is declared rather than inferred:
`frames_server_groups.owners` stores a **bare array** (`["alice@x"]`), so its
elements have no key above them. It is marked as an identity root in
`JSON_COLUMNS`, which makes its elements eligible. Payload and detail columns are
deliberately *not* marked — there, only identity-bearing keys are rewritten.

**If a genuine ACL carrier ever appears under "NOT substituted", that is a
defect, not a coincidence.** Watch for a carrier reporting zero changes while its
sibling migrates: a group whose `created_by` moved but whose `owners` did not is
still listed and no longer manageable by its owners.

A `description`, `name`, or extension field whose whole value happens to equal a
mapped address is a coincidence, not a principal. Such matches are listed in the
summary and the manifest, left **unchanged**, and make the run exit `1` so they
cannot pass unnoticed. Substring matches are never candidates at all.

### Local writes stay inside the store

Symlinks and anything that is not a regular file are refused rather than
followed, and a path resolving outside the configured root is refused: a planted
`metadata.json` link would otherwise rewrite a file on the operator's host.
Replacement is atomic — temporary file in the same directory, then `os.replace` —
so an interrupted run cannot leave a truncated sidecar, and the original file
mode is preserved. The manifest is written with the same no-follow, atomic
semantics and an enforced `0600`, because `os.open` applies its mode only when it
*creates* a file: writing over an existing world-readable path would otherwise
leave it world-readable.

### The manifest is a diagnostic, not the rollback mechanism

Stated first because the stronger reading is the dangerous one: an operator who
believes the manifest is a complete reversal record will not take the snapshot
that actually gets them home.

**The authoritative way back is verified database and frame-store
backup/restore.** Take it before the window — the runbook's Phase 0 exists for
this. **The authoritative description of what is currently stored is a fresh
read-only inventory run**, not the manifest.

Two limits, so nobody plans around them:

- **It is not durable across abrupt termination.** The file is written at startup
  and at termination, so a run killed mid-way can leave committed file or object
  writes absent from it. Making it durable would mean checkpointing around every
  independent commit — a journal rather than a report — which this deliberately
  is not.
- **It is not a complete pre-image of every carrier.** Collision merges capture
  both rows; ordinary substitutions capture the value replaced, not the whole
  record.

### What the manifest does record

Each change carries a `state`:

| state | meaning |
| --- | --- |
| `planned` | a dry run; nothing was attempted |
| `pending` | the database statement ran, its transaction has **not** committed |
| `committed` | the write returned, or the transaction committed |
| `failed` | the write raised, or the transaction rolled back |

**A statement running is not a commit.** Database changes stay `pending` until
the surrounding transaction commits and are promoted only then; a rollback
demotes the whole batch to `failed` and sets `transaction_rolled_back`. Object
and file writes *are* their own commit and are recorded when they return.
`committed_changes` is the count that matters after an apply.

Text carriers name the rows they touched rather than counting them — a count is
not a reversal instruction.

**Collision merges carry a `before_image` holding both rows as they were** — the
two frame-id sets, both sides' usage email and timestamps, both task payloads,
both device registrations. Both sides, not just the discarded one: when the
legacy row is newer its payload overwrites the subject's, so the surviving row's
prior payload is destroyed by the merge and is recorded there or nowhere.

**The destination is reserved before the first mutation.** An unsafe or
unwritable manifest path fails the run at startup, with nothing written — a
migration that has changed data and has nowhere to record it is the one state
with no way back.

`usage_users.email` is **not** rewritten. It is a contact column holding an
address, not an ACL principal; authorization keys on `user_id`, which is
rewritten. Replacing the address with a subject would destroy information and
gain nothing.

### Primary-key carriers are merged, not updated

Two legacy principals routinely map to one subject — an email and a username for
the same person — and a person may already hold rows under both a legacy
principal and their subject. Three carriers are primary-key components, so a
plain `UPDATE` there raises a unique violation at best and drops a row at worst:

| Table | Merge rule |
| --- | --- |
| `frames_server_active_frames` | Union the `frame_ids`. Dropping either side would silently deselect frames the user had open. |
| `frames_server_usage_users` | `LEAST(first_seen)`, `GREATEST(last_seen)`; keep whichever email is set. |
| `nexus_task_state` | Keep the more recently updated payload; the other is recorded in the manifest. Opaque client documents cannot be merged without inventing state. |

Every merge is listed in the run summary and the manifest.

### Interruption

There is no progress ledger, and none is needed. The plan is keyed on legacy
principals and a subject is never itself a legacy key, so a completed
substitution matches nothing on a second pass: **re-running after a failure is
the resume path**, and a run over already-migrated data reports zero changes.

Exit codes: `0` planned or applied cleanly · `1` the report was refused, a waiver
is missing, or matches were found where no identity belongs (nothing was written
at those paths) · `2` the run failed partway — the manifest is still written and
distinguishes committed from failed.

### Verifying

Re-run the inventory and diff its JSON against the pre-rewrite copy; the
machine-readable report exists for exactly this. The change set is the evidence
that the rewrite did what the mapping said and nothing else. Note that a
deployment with a coverage gap (for example `nexus_task_devices` on an install
whose task store is `backend: memory`) can never reach the `clear` verdict, so
the gate is "no gaps beyond the documented one", not exit code 0.
