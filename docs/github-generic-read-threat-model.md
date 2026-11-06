# Threat model — generic GitHub read (`github_api_get`)

Security sign-off doc for the generic GitHub REST read (`github_api_get`), added
in this PR and its desktop-client counterpart change. Live-ship
is gated on this sign-off **and** the hub deploy carrying this change.

The feature adds ONE new capability: a single GET against an arbitrary GitHub
REST path, exposed to the model through both apollo tool paths (Hermes plugin +
ravnar ChatAgent). Everything else in this doc exists to bound that one capability.

## What ships (commit map, for verification)

Commit SHAs are as of sign-off; verify against this PR's diff.

Collab Hub (`collab-hub-pack/api`):
- `ff2120a` — `sanitize_github_api_text` (connectors/connector_text.py)
- `535d0aa` — `GitHubClient.api_get` + helpers (connectors/github_client.py)
- `aad63c9` — route `POST /v1/connectors/github/api/get`, request/response models,
  `api_get_enabled` config, structured logging, docs (routers/connectors.py,
  connectors/models.py, config.py, docs/github-connector.md)

Apollo (`apollo-desktop`):
- `c352846` — proxy allow-list (internal/hubauth/proxy_policy.go)
- `1984ec5` — Hermes plugin tool (internal/hermesagent/plugins/apollo_github/)
- `98ec29a` — ravnar ChatAgent tool (internal/intelligencehub/ravnar_apollo/agents.py)
- `9ed8272`, `55237f1` — plugin-harness CI wiring (see "Discovered gap" below)

## Invariants (the boundary — each enforced nexus-side, in the client)

None of these are model-controllable: the request model exposes only
`path` / `params` / `media_type` / `max_chars`.

1. **GET-only (verb lock).** `api_get` hardcodes `client.stream("GET", …)`. The
   model cannot select the verb. This — not any denylist — is what excludes
   GitHub **writes** (the brokered `repo` scope is write-capable) and **GraphQL**
   (which executes only via POST). Enforced: `github_client.py::api_get`.
   Tested: the whole api_get suite issues GET only; token-scope reasoning in the
   `GitHubClient` docstring.

2. **Origin lock (SSRF).** The host cannot be escaped. `httpx.URL(base + path)`
   resolves within the base authority (live-probed: `//evil`, `/@evil`,
   `%2f%2fevil`, `https://evil` all stay on / never leave the API host). The raw
   path is validated (must start `/`; no `?`/`#`/`%`/`..`/`//`/whitespace/control/DEL/non-printable;
   ≤500 chars) AND the constructed URL is re-checked (authority unchanged, no `..`);
   a still-malformed path is caught as `httpx.InvalidURL` and refused (422), never a 500.
   Redirects are followed ONLY back to the same origin — scheme `https`, same
   host, same port, no userinfo — resolved through `httpx.URL.join` (never a
   string prefix match), ≤3 hops, auth kept. With an EMPTY `allowed_orgs`, a
   renamed-repo 301 (stays on the API host) is followed; a codeload/storage hop,
   an `https→http` downgrade, or a `host@evil` userinfo spoof is refused. Note
   the org-scope interaction: a real rename/transfer 301 targets the numeric
   `/repositories/{id}` route, which a NONEMPTY allowlist refuses (owner not
   name-resolvable) — the right fail-closed trade, so renamed-repo follow is an
   empty-allowlist-only property. Enforced: `_validate_api_path`,
   `_resolve_api_redirect`, `_enforce_api_get_org_scope` (re-run per hop). Tested: `test_api_get_rejects_bad_paths` (18 cases),
   the five redirect tests (same-host follow, hop cap, cross-host refuse,
   downgrade refuse, userinfo-spoof refuse).

3. **Link-shape masking — NOT general injection-neutralization.**
   `sanitize_github_api_text` rewrites link/email/markdown *shapes* so they can't
   linkify in the renderer — scheme/`www.`/markdown/`mailto` die — MINUS
   bare-domain masking, so code-shaped text (diffs, `config.py`, SHAs, refs)
   survives. It runs over every JSON string **value AND key** (a key is an
   untrusted channel too — the Gists API keys its `files` object by
   attacker-chosen filename), over diff/patch/text wholesale, AND over the
   upstream error `message`. Enforced: `_sanitize_json_values`, `_text_result`,
   `_github_error_message`. Tested: `test_api_get_sanitizes_nested_string_values`,
   `test_api_get_sanitizes_link_shaped_object_keys`,
   `test_api_get_error_message_is_sanitized`, `test_api_get_diff_media`, S0 suite.
   **Scope, precisely — this is NOT an injection defense.** Plain text,
   forged response-framing (`</result>`, `<system>…`, fabricated `tool_result`
   JSON), and homoglyph prompt injection pass through **by design** — code-shaped
   bodies (the net-new surface) are deliberately left readable, so they are the
   richest injection carrier and the masker does nothing to them. The residual
   injection control is the prompt-level `content_trust=external_untrusted` +
   `security_notice` envelope on every response (`models.py`) — a mitigation, not
   a guarantee, which the signer must weigh as load-bearing.

4. **Size cap + explicit truncation.** `max_chars` (media-aware default: 20k json
   / 50k diff-patch) bounds the returned text; `truncated` is set whenever the
   body is cut. JSON over the cap degrades to a text prefix rather than a partial
   object. The 50k ceiling / ≥1 floor is enforced by the request model
   (`GitHubApiGetRequest.max_chars = Field(ge=1, le=50_000)`) AND a defensive
   `max(1, min(resolved_max, 50_000))` clamp in `api_get`, so a direct caller
   can't exceed it either. Enforced: `models.py`, `_json_result`, `_text_result`.
   Tested:
   `test_api_get_json_over_max_chars_becomes_text_prefix`,
   `test_api_get_diff_trimmed_to_last_line`, `test_api_get_*_default_max_chars`.

5. **Pre-parse byte cap (memory-safety).** The body is STREAMED and aborted
   past `max_chars*4 + 1` bytes, so parse-then-truncate never buffers an unbounded
   upstream (`/git/trees?recursive=1` is the attack). Enforced: `read_capped`
   (in `http_stream.py`, shared with drive_client's `aiter_bytes` pattern). Tested:
   `test_api_get_aborts_oversized_body` asserts the stream is abandoned early, not
   fully drained.

6. **Concurrency cap (hub-side DoS).** Each `api_get` opens its own HTTP client
   plus a token-broker fetch, so unbounded concurrent calls from an injected agent
   could exhaust the hub's sockets/FDs/memory. A process-wide `asyncio.Semaphore`
   sized by `api_get_max_concurrency` (default 8) bounds concurrent generic reads;
   curated tools are unaffected. Enforced: `app.state.github_api_get_semaphore`
   (created in `core.py`'s lifespan) + the `async with` around the client call in
   `routers/connectors.py`.

Two more properties fall out of the above:
- **Content-Type keying, not status keying.** Dispatch is on
  `content_type.split(";")[0]` (live GitHub always appends `; charset=utf-8`), and
  the parsed `content_type` is echoed — the model's only signal that a `diff`
  request silently degraded to JSON (issues do this). A `202` empty body is a
  retry signal, not an error. Anything non-JSON/diff/patch/text is refused as
  binary.
- **Token never surfaced.** The access token lives only in the request
  `Authorization` header; it appears in no response field across happy /
  truncated / diff / error / refusal paths. Tested:
  `test_api_get_never_exposes_token` + the route-level token test.

## GraphQL exclusion — BY CONSTRUCTION, not a denylist

GitHub GraphQL executes queries/mutations ONLY via POST. The GET-only verb lock
(invariant 1) therefore excludes it on every path, alias, and future endpoint
shape — the same way the host lock works. The `/graphql` path block in
`_validate_api_path` is retained ONLY as a courtesy: it returns a clearer
model-facing error than upstream would and cuts noise from harmless GET
schema-introspection. **It is NOT load-bearing and must not be weighed as a
security control** — reviewers should evaluate the verb lock, not the string match.

## Sanitizer scope — additive, zero cross-connector impact

`sanitize_github_api_text` is a NEW function used ONLY by `api_get`. The shared
`sanitize_connector_text` and all its callers (Gmail/Calendar/Drive/Slack + the
curated GitHub tools) are byte-identical — no other-connector behavior or doc
changed (guarded by `test_shared_sanitizer_still_masks_bare_domains` + the
unchanged Google Workspace suite). Retiring the shared bare-domain masking
globally is a SEPARATE, security-flagged future ticket, explicitly out of scope
here (its #365 premise has expired, but changing a shared function warrants its
own review).

## Explicit decisions for sign-off (please ratify or redirect)

- **(a) `api_get_enabled` default = False (fail-closed, opt-in).** The generic
  read ships OFF: an upgraded hub gains full-token-visibility long-tail read only
  after an operator explicitly enables it, ideally alongside a set `allowed_orgs`
  — so the default posture on upgrade is no wider aperture, not a wider one. The
  curated tools are unaffected (they gate on `broker_token_url`). **Containment
  latency:** the flag is read once at startup, so toggling it — on to adopt, or
  off during an incident — requires a **redeploy/restart**, not a live config
  reload. Ratify opt-in-default knowing enabling and the kill switch are both
  deploy-time, not instant.
- **(b) Org allowlist enforced; residual scope is the token's within it.** The
  generic read honors the same `connectors.github.allowed_orgs` allowlist as the
  curated search (issue #64): with it set, `api/get` admits ONLY
  owner-qualified paths (`/repos`, `/orgs`, `/users`) under an allowed owner and
  refuses everything that can read across orgs — `/search/*`, the `/user/*`
  self-endpoints, `/issues`, `/gists`, `/notifications`, etc. (positive allowlist,
  so a future cross-org endpoint is refused without a code change). This closes
  the earlier gap where the generic read walked around the curated allowlist.
  Residual, ratified: (1) an EMPTY allowlist == the token's full visibility (the
  project-wide default — real deploys set `allowed_orgs`); (2)
  within an allowed org, nothing further binds the reachable `path` to the agent's
  frame/repo, and the actor is the possibly-injected agent, not the user, so an
  injected agent still reaches every in-allowlist endpoint the token scopes cover.
  A per-agent/frame repo binding remains a possible future narrowing.
- **(c) 50k ceiling + media-aware defaults** (json 20k / diff 50k) accepted as the
  v1 limit; the fallback for a bigger diff is `/pulls/{n}/files` (paginated).
- **(d) Abuse observability = the structured events; GitHub-side rate budgets
  deferred** until a diagnosed failure justifies them. Hub-side resource
  exhaustion is NOT deferred — it is bounded now by the `api_get_max_concurrency`
  cap (invariant 6). Ratify the default (8) and whether an added per-user budget
  is warranted.
- **(e) Known rot-prone edge, ACCEPTED.** A secondary rate-limit `403` carrying
  NEITHER `Retry-After` NOR `x-ratelimit-remaining: 0` falls through to `502`. We
  deliberately refuse to string-match GitHub's "secondary rate limit" prose (it's
  human copy GitHub changes without notice); `502` at least reads as
  retryable-later. The header-based cases ARE normalized to `429` with the
  `retry_after` folded into the detail string. Note: the echoed `status` plus the
  404-vs-403→502 mapping is a finer existence/permission **oracle** over arbitrary
  paths than the curated shapes — accepted as `curl`-equivalent, but named here as
  an intentional info-disclosure the signer ratifies.

## Post-ship blast radius, detection & response

**Aperture, honestly stated.** Everything reachable is bounded by the calling
user's OWN GitHub access — **provided `static_access_token` stays unset**; if an
operator sets it, all callers share one token and this bound no longer holds. The
brokered token carries the write-capable `repo` scope (GitHub has no
read-only-private scope) plus whatever `read:org`/`user` the link grants, so the
reachable READ surface is far wider than the six curated shapes: every private
repo of every accessible org, org membership, collaborator/team lists, secret
*names*, deploy-key metadata, `/user/emails`. The GET lock excludes writes; it
does not by itself narrow this read set — but a configured `allowed_orgs`
allowlist does (decision (b)): with it set the reachable set collapses to
owner-qualified paths under the allowed orgs, and `/user/emails` and the other
cross-org endpoints above are refused outright. The NET-NEW risk vs the curated tools is
**injection-driven exfiltration with a wider aperture** — and the ACTOR driving
the path is the (possibly prompt-injected) agent, not the user (decision (b)).
Sanitization masks link *shapes* in what comes back; it does NOT neutralize
injection and does NOT change what is *reachable*.

**Masking is fully bypassed for file contents — named explicitly.** The single
most common long-tail read, `/repos/{o}/{r}/contents/{path}` via
`media_type: "json"`, returns the file as a **base64 `content` field**. The
base64 alphabet cannot contain `://` or any link shape, so `sanitize_github_api_text`
provably cannot mask it: the decoded file text reaches the model with links,
markdown, and any injection payload intact. Only the curated `files/read` sanitizes
decoded file contents; through the generic read the masking layer does nothing for
file bodies. This is accepted (code-shaped content is deliberately left readable —
invariant 3), but the signer must weigh it knowing file fetches are unmasked.
*Future trap:* if a `raw` media type is ever added to `_ACCEPT_BY_MEDIA` to return
decoded bodies, GitHub labels them `Content-Type: application/vnd.github.raw+json`
— `_is_json_content`'s `endswith("+json")` test would misroute that raw text into
`json.loads` (failing into the truncated-prefix fallback with `truncated=True` on a
complete file). The Accept table's first extension needs a matching content-type
exception.

**Detection.** The structured events are the alertable surface, with distinct
names so ops can build per-user/agent volume and anomaly alerts:
`github_api_get_request` (validated path + auth identity `auth.user` +
media_type), `github_api_get_refusal` (reason), `github_api_get_truncation`,
`github_api_get_upstream_error` (operation, status_code, path). The logged `path`
is model-controlled (a token/email/private-repo name can land in it); `params` are
deliberately NOT logged — confirm the log store's trust boundary. **HONEST GAP:**
nexus-pack has no in-repo alerting framework — wiring these into alerts is an
infra follow-up (see "Open items" below), not shippable in this change.

**Response runbook.**
1. Flip `connectors.github.apiGetEnabled: false` in the Helm values and
   **redeploy/restart** the hub — the flag is read at startup, so this is NOT a
   live reload; new calls then `403`, and the curated reads are unaffected
   throughout.
2. Grep the structured events for the offending user/agent's `path` history to
   scope what was read.
3. If warranted, revoke the user's GitHub OAuth grant (invalidates the broker's
   tokens) and rotate.
4. Transcript-review the agent session for the injection source.

## Discovered gap (unrelated, routed out)

Wiring the plugin harness into CI (`55237f1`) surfaced that **apollo_slack**
enforces DM/mpim exclusion on list/search but NOT on read-by-id — a pre-existing
gap that stayed red unnoticed because the harness ran in no workflow. It is NOT
part of this feature; the failing assertion is a strict xfail so the harness can
land and guard everything else, and the fix is filed with the desktop
client's Slack-connector owner.

## Open items for sign-off

- [ ] Sign-off decisions (a)–(e) ratified.
- [ ] Infra alerting ticket for the structured `github_api_get_*` events — to file.
- [ ] `scripts/integration_e2e` model-contract evals — deferred (a model-behavior
      guard, not a connector-contract test; separate follow-up).
