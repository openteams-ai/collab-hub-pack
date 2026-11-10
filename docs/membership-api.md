# Organization membership API for add-on services

Add-on services, for example usage control and a subscription manager, need to
know who belongs to an organization and who owns it. They keep no organization
directory of their own; they read it from the hub through this API.

The API is read-only and service-to-service. It answers three questions:

| Route | Answers |
| --- | --- |
| `GET /v1/membership/subjects/{subject}` | Which organizations a person belongs to, and their role in each |
| `GET /v1/membership/organizations/{org_id}` | An organization's members and owners, with their names and emails, and the member count |
| `GET /v1/membership/organizations` | Every organization with its members, one page at a time, for a full sync |

It is off by default. A deployment that doesn't name any add-on serves none of
these routes.

## Enabling it

1. Organizations must come from membership rows: `frames.auth.orgSource` is
   `membership` or `single`. Under `claims` the hub doesn't read its membership
   table, so the chart and the API both refuse to start with the API enabled.
2. Bearer tokens must be verifiable: `frames.auth.bearer.jwksUrl` (and, as
   membership resolution already requires, `frames.auth.bearer.issuer`).
3. In the realm, give each add-on **its own** confidential client with
   *Service accounts* (the client-credentials grant) enabled. Don't share one
   client between add-ons, and don't reuse a client that people sign in
   through.
4. List the client ids in the chart values:

   ```yaml
   frames:
     membershipApi:
       clientIds:
         - usage-control
         - subscription-manager
       # Optional, recommended: the audience a service token must carry.
       audience: collab-hub-membership
   ```

   If you set `audience`, add an *Audience* protocol mapper with that value to
   each add-on client, so its tokens carry it. With `audience` empty the
   audience isn't checked; the signature, issuer, token type and client checks
   below still apply.

Removing every client id turns the API off again. Nothing is stored for it, so
there is nothing to clean up.

## Authentication

A request is accepted only when all of these hold:

- It carries `Authorization: Bearer <access token>`, signed by the realm the
  hub trusts (`frames.auth.bearer.jwksUrl`, `frames.auth.bearer.issuer`), with
  an expiry, and with `typ` `Bearer`. ID tokens and refresh tokens are refused.
- The token's `azp` is one of `frames.membershipApi.clientIds`, **and** the
  token carries the same id in Keycloak's `client_id` claim (`clientId` before
  Keycloak 25). Only the client-credentials grant sets that claim, so a person
  who signs in through an add-on's client is refused even though their token
  has the same `azp`. `azp` alone is never enough.
- The request carries **no cookies**. Services send none; a request with
  cookies comes from a browser, and a browser session never authenticates
  here, even beside a valid service token.

Forwarded user headers are ignored. A service caller is never resolved to a
person or an organization, and isn't recorded as a hub user.

| Outcome | Status | `error.code` |
| --- | --- | --- |
| No bearer token | 401 | `unauthorized` |
| Token invalid, expired, foreign-signed, wrong issuer or audience, not an access token, or no expiry | 401 | `unauthorized` |
| Valid token, but not a listed add-on's client-credentials token (another client, or a person's token) | 403 | `forbidden` |
| Any cookie on the request | 403 | `forbidden` |

A refused service token is logged as `membership_api_caller_refused` with the
`azp` it named (never the token), so a misconfigured add-on is visible in the
API's logs.

To get a token, an add-on uses the client-credentials grant on the realm's
token endpoint:

```sh
curl -s -d grant_type=client_credentials \
  -d client_id=usage-control -d client_secret="$SECRET" \
  https://<keycloak>/realms/<realm>/protocol/openid-connect/token
```

## Responses

Every response carries `observed_at`, the time the hub started the read (UTC,
ISO 8601). The data is at least as new as that time.

Roles are `owner` or `member`; there are no others. Owners are members too and
are included in `member_count`. Only **active** members are listed: a removed
member is absent, not listed with a status.

`display_name` and `email` are display and contact fields, never identifiers;
key everything on `subject`. They are the values the hub recorded when the
member joined (from the identity provider's token at invitation acceptance or
first sign-in), and either can be `null` when the hub holds none, for example a
member whose address the identity provider hadn't verified. A member is listed
whether or not the hub holds a name or email for them; the API never drops a
member for a missing label, and it makes no identity-provider lookups.

### `GET /v1/membership/subjects/{subject}`

```json
{
  "subject": "a1b2c3d4-1111-4111-8111-abcdefabcdef",
  "organizations": [{"org_id": "6f1c0e2a9b7d4c3e8f5a1b2c3d4e5f60", "role": "owner"}],
  "observed_at": "2026-11-10T17:03:12.481Z"
}
```

`organizations` is empty when the subject isn't an active member anywhere,
including after removal. A hub login has at most one organization today, so
the list holds zero or one entry; it is a list so that can change without a
new route.

### `GET /v1/membership/organizations/{org_id}`

```json
{
  "org_id": "6f1c0e2a9b7d4c3e8f5a1b2c3d4e5f60",
  "name": "Acme",
  "member_count": 2,
  "members": [
    {"subject": "22222222-2222-4222-8222-b0b0b0b0b0b0", "role": "member", "display_name": "Bob B", "email": "bob@example.com"},
    {"subject": "a1b2c3d4-1111-4111-8111-abcdefabcdef", "role": "owner", "display_name": "Alice A", "email": "alice@example.com"}
  ],
  "observed_at": "2026-11-10T17:03:12.481Z"
}
```

Members are ordered by `subject`. `name` is the organization's display name and
can be `null`. An organization whose members have all left answers `200` with
`member_count: 0`. An id that was never an organization answers `404` with
`error.code` `organization_not_found`.

### `GET /v1/membership/organizations`

Query parameters: `limit` (1–500, default 100) and `cursor`.

```json
{
  "organizations": [
    {"org_id": "...", "name": "Acme", "member_count": 2, "members": [...]}
  ],
  "next_cursor": "6f1c0e2a9b7d4c3e8f5a1b2c3d4e5f60",
  "observed_at": "2026-11-10T17:03:12.481Z"
}
```

Each entry has the same fields as the single-organization response, without
`observed_at`. Pass `next_cursor` as `cursor` to get the next page; it is
`null` on the last page. Treat the cursor as opaque. Organizations are
included even when they have no active members, so a full sync sees an
organization's last member leave.

Each page is one consistent read. Pages are read separately, so a change made
between two page reads can appear in a later page and not an earlier one; the
next sync reconciles it.

## What isn't exposed

Exactly the fields above, and nothing else: no frames, invitations, tokens,
credentials, removed members, audit history, platform (operator) roles or
connector state. The tests pin each response's field set.

## Freshness: the staleness bound

**The hub adds no staleness.** Nothing on this path is cached: every request
reads the membership table when it arrives, so a removal, an ownership change
or a join committed before a request is in that request's answer. This is the
same per-request read the hub's own authorization uses, which is why a removed
person loses hub access on their next request.

So the end-to-end bound is the add-on's own: **how often it reads, plus how
long it holds what it read**. We recommend:

- **Sync at least every 60 seconds** with the listing (or the per-organization
  route for the organizations you track). A removed member then stops drawing
  on, and being charged to, an organization's allowance within about a minute,
  and a share computed from `member_count` (for example an equal split of an
  organization's pool among members without an allocation) is at most that
  stale after a join or a removal.
- **Read live before a decision that matters.** A screen that lists an
  organization's members to its owners, or a check that decides whether a
  person may act for an organization, should call the per-organization or
  per-subject route at that moment instead of using a synced copy.
- **Fail closed when the hub can't answer.** `503` (`organizations_unavailable`
  or `database_unavailable`) means the hub can't read membership right now. It
  doesn't mean the organization is empty; keep the last known state and retry,
  rather than treating everyone as removed or everyone as present.

There is no change feed. Membership rows record when a member joined but not
when a role or status last changed, so a `changed_since` cursor would need a
schema change. At the sizes this API serves, a full sync each minute is cheap
(one query per page). Add a feed if that stops being true.
