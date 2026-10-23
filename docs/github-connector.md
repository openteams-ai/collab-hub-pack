# GitHub Connector

The GitHub connector lets Collab Hub search and read a user's GitHub issues, pull
requests, and repository files with that user's linked GitHub identity. It is
**read-only** — it only ever calls GitHub read endpoints and has no code path
that writes to GitHub.

## Requirements

> **Scope — the security fact that must be signed off, not discovered.**
> Keycloak brokers a **classic GitHub OAuth App** token, and GitHub has **no
> read-only scope for private repositories**: the `repo` scope is the floor and
> it is **read *and* write** capable. (Fine-grained read-only permissions like
> `contents:read` are not available to a **classic OAuth App** — they require a
> **GitHub App**; see "Why the classic OAuth App" below.) Read-only is therefore
> enforced **in Collab Hub client code** — only read endpoints are implemented, no
> write verb is ever constructed, and tests assert this at every layer — not by
> the token. If you only need public repositories, request `read:user read:org`
> and omit `repo` entirely; then the token has no write capability by
> construction, at the cost of no private-repo visibility.

> **Why the classic OAuth App, not a GitHub App?** A GitHub App with fine-grained
> read permissions would make read-only a *token-level* guarantee, and its
> user-to-server tokens use the same OAuth endpoints, so Keycloak could broker one
> the same way. We ship the classic OAuth App because it reuses the exact
> brokering path the other connectors already use — no signing key to manage and
> no per-org app installation — and enforce read-only in code instead. Migrating
> to a fine-grained GitHub App is the path if we later want token-level read-only.

Configure a GitHub identity provider in the hub's Keycloak realm with alias
`github` and:

- `Store Tokens` enabled (so `/broker/github/token` can return the stored token).
- **Read scopes.** In Keycloak the identity provider's **Scopes** field
  *replaces* the provider defaults rather than appending to them, so set it to
  the full space-separated list you want — for private-repo access plus project
  boards:

  ```
  user:email repo read:org read:project
  ```

  A bare `repo` would drop the default `user:email` and break the account-email
  mapper. `read:project` is the **read-only** scope for Projects V2 boards
  (`list_github_projects` / `read_github_project`, which use GitHub's GraphQL
  API); omit it if you don't need boards. There is deliberately no write/push
  scope in this list.

  > **Adding `read:project` to an existing IdP does not re-prompt already-linked
  > users** — their stored token keeps its old scopes. Every linked user must
  > **unlink and relink** GitHub to pick up board access. (Same for any scope
  > change; see the callout below.) For local static-token testing, the PAT you
  > use as `static_access_token` must also include `read:project`.
- **Hide on Login Page** enabled on the identity provider, so nobody can sign in
  to Collab *with* GitHub (which would trigger Keycloak first-broker-login and
  create a GitHub-primary account). GitHub is only ever linked as a *secondary*
  identity to an existing Hub account.
- A redirect URI matching the Keycloak broker endpoint:
  `https://keycloak.<hub-host>/realms/<realm>/broker/github/endpoint`.

Configure Collab Hub with the Keycloak broker token endpoint:

```yaml
connectors:
  github:
    brokerTokenUrl: http://<keycloak-service>.<namespace>.svc.cluster.local:8080/realms/<realm>/broker/github/token
```

Keycloak must allow normal hub users to read their own linked broker tokens.
Grant the `broker` client role `read-token` to the role or group that every
normal hub user receives, exactly as for the other connectors:

```sh
kcadm.sh add-roles -r <realm> --rname default-roles-<realm> \
  --cclientid broker --rolename read-token
```

> **Two things that will make the connector go green while seeing nothing.**
> 1. **Org owner approval.** For any org with OAuth App access restrictions, an
>    org owner must *approve* the Collab OAuth App (and, if the org uses SAML
>    SSO, the user must authorize their token for that org). Until then the token
>    brokers fine and `/status` can read the user's own repos, but org
>    repositories return `404`. Make org-owner approval a named prerequisite of
>    rollout, not an afterthought.
> 2. **Scope changes don't re-prompt existing links.** If you add or change
>    scopes on the IdP after users have already linked GitHub, GitHub does **not**
>    re-prompt them — their stored token keeps the old scopes. A scope change
>    requires each linked user to **unlink and relink** GitHub.

## Endpoints

- `GET  /v1/connectors/github/status`
- `POST /v1/connectors/github/search`
- `POST /v1/connectors/github/items/{number}/read`
- `POST /v1/connectors/github/files/read`
- `POST /v1/connectors/github/repos/list`
- `POST /v1/connectors/github/projects/list`
- `POST /v1/connectors/github/projects/{number}/read`
- `POST /v1/connectors/github/api/get`

The two `projects` endpoints read **Projects V2** boards via GitHub's **GraphQL**
API (`read:project` scope). `projects/list` takes an `owner` (org *or* user
login) and returns each board's number/title/description/item count.
`projects/{number}/read` returns the board's items, each with its status column,
`state` (open/closed of the linked issue/PR), and — when the item is a linked
issue or PR — the `repo` (owner/name), `number`, `assignees`, and `labels`, so a
caller can triage by person or label and chain `items/{number}/read` to read
that issue/PR. The item read excludes archived items (`archived_policy:
"excluded"` on `counts`).

By default the read returns one small page (`max_items: 50`) rather than the
whole board. Check `truncated` to tell a partial listing from a complete one,
and page with the response's `next_cursor`: send it back as `after` (with the
same owner, number and `query`) to get the next items without re-downloading
the first page. `next_cursor` is `null` once the listing is exhausted. Raise
`max_items` (up to 500) only when the full listing is needed in one call.
An optional `query` narrows the items with GitHub board filter DSL
(e.g., `status:"In Progress"`, `assignee:octocat`). The response echoes the
applied filter as `query` and reports `matched_count` (how many items it
selected) next to `total_count`, which is always the board's full size (the same
scalar as `project.items_count` and `counts.total`). `truncated` compares the
returned items against `matched_count`. A filter that matches nothing -- GitHub
silently returns zero for an unrecognised filter rather than an error -- shows
as `matched_count: 0` against a non-zero `total_count`, so it can't be mistaken
for an empty board, and the response carries a `note` saying so and listing
the board's real Status column names (GitHub matches `status:` values ignoring
case but not emoji, so `status:"In Progress"` finds nothing on a board whose
column is `In progress 🏗`). `counts` below stays board-wide regardless of
either and is never what the filter matched.

The read also returns a `counts` object with the board's aggregate breakdowns: an
exact non-archived `total` (scope recorded in `archived_policy`), per-status-column
counts (`by_status` plus `no_status` for the blank column, which reconcile to the
total), `by_type` (issue / pull_request / draft / redacted), and `by_state`
(open / closed of linked issues/PRs). Projects V2 has **no** group-by aggregate,
so each bucket is a `totalCount`-only count query under one `archivedStates`
policy — which makes the counts accurate **even when the returned item list is
truncated**. If those count queries are unavailable the breakdowns fall back to
sampling: an unfiltered read from the top of the board is continued with a
counting-only query (up to the 500 ceiling); a filtered or `after` read, whose
items can't stand in for the board, is re-sampled from the top. When the count
queries failed because GitHub is rate limiting or failing (429, 5xx, timeout) no
further requests are made and only the items in hand are bucketed. Sampled
counts set `authoritative: false` with `counted_items` (zero when nothing could
be sampled, with `total` still the board size), so a partial sample is never
mistaken for a full count. `by_status` + `no_status` and `by_type` are each independent partitions of
`total` and should not be cross-added; `by_state` covers only linked issues/PRs
so it need not sum to `total`.

`search` runs against GitHub's issue-and-PR search (`/search/issues`, which
returns both issues and pull requests) and is **page/`per_page`-paginated with a
hard 1000-result cap**. Collab Hub returns an opaque `next_page_token` that encodes
the page number plus a fingerprint of the query/filters; feed it back unchanged
to page. Changing the query or `repo` and reusing an old token is a stale cursor
and returns `422`. `incomplete_results` is surfaced when GitHub's search times
out before scanning every candidate. GitHub's search rate limit (30 req/min; 10
for code search, which this connector does not use) is normalized to `429` with
`Retry-After` echoed.

`items/{number}/read` reads one issue or pull request (body plus recent comments,
capped at `max_chars`). Both issues and PRs carry their `assignees` and `labels`;
pull requests additionally carry `requested_reviewers`, `requested_teams`,
`is_draft`, a `merge_state` (`merged` / `closed_unmerged` / `open`, so a closed PR
is never ambiguous about whether it landed), and submitted `reviews` (user, review
state, and the reviewer's sanitized, length-capped `body`), fetched from the pulls
endpoint. A requested reviewer who has already submitted a review moves from
`requested_reviewers` into `reviews`, so read both to see everyone involved. For
changed files and diffs, use `api/get` (below) against `/pulls/{number}` with
`media_type: "diff"`, or `/pulls/{number}/files`. `files/read` reads a repository
file by `repo`, `path`,
and optional `ref` (branch/tag/SHA; default branch when omitted). The contents
API only returns files up to 1 MB, so larger files come back with `too_large`;
non-UTF-8 files come back with `binary`; git-lfs pointers and directories come
back with an `unsupported_reason`. In every case `content` is empty rather than
garbage. `path` and `ref` are validated against traversal (`..`, absolute paths,
control characters) before they are ever placed in an upstream URL.

`repos/list` returns the repositories visible to the linked account for an
`owner` (org or user login).

`api/get` is the generic long-tail read: a single **GET** against an arbitrary
GitHub REST `path` (query args via `params`) for the endpoints the curated tools
don't cover — e.g. `/repos/{o}/{r}/pulls/{n}/files`, `/commits`, `/releases`, or
`/commits/{ref}/check-runs`. Prefer the curated tools for triage. `path` rejects
`%`, so a contents path with a space or other percent-encoded character is
unreachable via `api/get` — use `files/read` for those. The verb is
always GET and `media_type` (`json`|`diff`|`patch`) selects a fixed `Accept`
header, so GraphQL (POST-only) and writes are excluded **by construction**, not
by a denylist. Pages via `params` `per_page`/`page`; `has_more` echoes the
upstream `Link rel="next"`. `media_type: "diff"`/`"patch"` works only on
pulls/commits/compare — issues silently return JSON, so check the echoed
`content_type`. Bodies are size-capped (`max_chars`, default 20k for json / 50k
for diff, 50k ceiling) with `truncated` set when cut; a `202` with an empty body
means GitHub is still computing the result — retry. A `204` with an empty body
is an affirmative result with nothing to return (e.g. a collaborator/following
membership check). The read is origin-locked
(GET stays on the API host; archive/binary redirects are refused). It ships
**off by default** (fail-closed): enable it per hub with `api_get_enabled: true`
— ideally alongside a set `allowed_orgs` — without affecting the curated reads,
which stay on regardless.

`api/get` honors the `connectors.github.allowed_orgs` allowlist: when it is set,
the read is confined to owner-qualified paths (`/repos`, `/orgs`, `/users`) under
an allowed org, and paths that can read across orgs — `/search/*`, the `/user/*`
self-endpoints, `/issues`, `/gists`, `/notifications` — are refused (a redirect
onto a disallowed owner is re-checked and refused too). An empty allowlist (the
default) means the token's full visibility. Set `allowed_orgs` in real deploys so
the generic read cannot reach outside your orgs.

The curated search reads the same key. With `allowed_orgs` set and no `repo`
given, the search is scoped with a repeated `org:` qualifier per allowed login
(GitHub ORs them); with a `repo` given, its owner must itself be in the
allowlist or the request is refused as a `422`. The `repo` must be exactly
`owner/name`; anything else is a `422`. With the allowlist set the hub owns the
search scope, so a `query` containing an `org:`, `user:`, `owner:` or `repo:`
qualifier is refused as a `422` too (GitHub would OR it with the hub's own
qualifiers). That also refuses a quoted phrase containing one of those tokens.
As a second layer, any returned hit whose repository owner is outside the
allowlist is dropped. An explicit `repo` therefore narrows within the allowlist
rather than escaping it. An empty allowlist leaves the search at the token's
full visibility, unchanged.

The curated endpoints return no GitHub URL of any kind: all provider text is
link-sanitized (bare domains included) and `repo`/`number` (not URLs) are what a
follow-up read needs. This began as a workaround for a chat renderer that crashed
on link-shaped text (apollo-desktop#365, now fixed) and is retained as
defense-in-depth against link/markup injection through tool output. `api/get`
uses a **code-aware** variant of that sanitizer:
it still masks scheme-, `www.`-, markdown-, and `mailto`-shaped links, but
preserves code-shaped text (dotted identifiers like `config.py`, SHAs, refs) so
diffs and file contents come back intact.

## Runtime Boundary

The user's GitHub access token stays hub-side. Apollo Desktop sends the user's
Hub bearer token to Collab Hub through its local proxy; Collab Hub uses that token to ask
Keycloak for the current user's GitHub broker token and then calls the GitHub
API. The model and desktop client never receive the GitHub access token.

## Deployment Checklist

The connector is code-complete and tested (unit tests over the full
router→client→GitHub path via a mock transport: happy paths, every token-error
state, static/broker config precedence, 429/`Retry-After`, pagination plus
stale-cursor `422`, `incomplete_results`, oversized/binary/lfs
file handling, path/ref traversal rejection, link sanitization, and an assertion
that the token never appears in any response body — see
`api/tests/test_github_connector.py` — plus Helm schema validation). Cutting it
over to a real org is a configuration exercise. Two roles are involved.

### From whoever owns the GitHub org(s)

1. Ensure the Collab OAuth App is **approved** for each org whose repositories
   should be searchable (Org Settings → Third-party Access / OAuth App access).
2. If the org enforces SAML SSO, tell users they must authorize their linked
   token for the org after linking.
3. Confirm the read scopes above are acceptable for the org's data policy — note
   the `repo`-is-write-capable caveat and that read-only is enforced in Collab
   Hub code.

### From whoever administers the Hub (Keycloak + Collab Hub deploy)

1. In the hub's Keycloak realm, add a GitHub identity provider with **alias
   `github`** (must match exactly — Apollo's `GitHubConnectorLogin` hardcodes
   this alias). Enable `Store Tokens` and **Hide on Login Page**, set the
   **Scopes** field to `user:email repo read:org read:project`, and register the redirect URI
   above. **Reviewable artifact:** `docs/keycloak-github-idp-partial-import.json`
   is a ready-to-import version of exactly this IdP — fill in the two `REPLACE_`
   values (GitHub OAuth App client id/secret; prefer injecting the secret from
   your secret store) and import it via **Realm settings → Action → Partial
   import**, or:
   ```sh
   kcadm.sh create partialImport -r <realm> -s ifResourceExists=SKIP \
     -f docs/keycloak-github-idp-partial-import.json
   ```
   It sets Store Tokens, Hide on Login Page, the read scopes, and
   `addReadTokenRoleOnCreate`, so step 2 is handled for users created through it.
2. Grant the `broker` `read-token` role to normal users (the `kcadm.sh` line
   above) — belt-and-suspenders alongside the partial import's
   `addReadTokenRoleOnCreate`.
3. Set `connectors.github.brokerTokenUrl` in the Collab Hub Helm values and deploy.
4. Run the verification recipe below once with a real linked user.

### Verifying the connection

With a linked user's Hub bearer token:

```sh
curl -s -H "Authorization: Bearer $HUB_TOKEN" \
  https://<nexus-host>/v1/connectors/github/status | jq
```

A healthy connection reports `"state": "connected"` and an `"account"` matching
the linked GitHub login. `"reconnect_required"` means the token brokered but
cannot read repositories (bad/expired token, or org SSO not authorized).
`"unavailable"` means the connector is unconfigured or the broker `read-token`
role is missing. Then confirm a search returns results the user expects:

```sh
curl -s -H "Authorization: Bearer $HUB_TOKEN" -H 'Content-Type: application/json' \
  -d '{"query":"is:open","repo":"your-org/your-repo"}' \
  https://<nexus-host>/v1/connectors/github/search | jq '.hits[0]'
```

If `status` is `connected` but org searches are empty or a known item `read`
returns `404`, the most likely cause is missing **org owner approval** or **SSO
authorization** — not a Collab Hub problem.

## Rollback

Remove or blank `connectors.github.brokerTokenUrl` and redeploy: `/status`
reports `unavailable` and the tools disappear from Apollo. To fully remove
access, delete the GitHub identity provider from the Keycloak realm; existing
users' stored tokens are dropped with it.
