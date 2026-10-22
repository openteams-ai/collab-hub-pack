"""Versioned, lock-guarded auto-migration for the ``collab_`` tables.

The pre-existing ``frames_server_`` stores each run their own idempotent
``CREATE TABLE IF NOT EXISTS`` DDL at startup, unlocked and with no version
history. ``replicaCount: 1`` has masked two problems with that idiom:

- ``CREATE TABLE IF NOT EXISTS`` is **not** concurrency-safe in Postgres. Two
  replicas starting at the same instant can both pass the existence check and
  then race the catalog insert, so one pod dies at startup with a duplicate-key
  error on ``pg_type``/``pg_class`` (issue #84).
- Create-only DDL has no mechanism for *changing* a table later: an ``ALTER``
  or a backfill has nowhere to live and no way to know whether it already ran.

The ``collab_`` tenancy tables (issue #62) therefore use a single runner rather
than per-store ``_ensure_schema`` methods:

- one transaction takes ``pg_advisory_xact_lock(COLLAB_SCHEMA_LOCK_KEY)``
  **before** any catalog access, so concurrent replica startups serialize; the
  lock is transaction-scoped, so it releases on commit or rollback with no
  unlock bookkeeping and no leak if the pod dies mid-migration;
- an ordered ``collab_schema_migrations`` table records what has been applied,
  so later non-idempotent changes are simply appended as new versions;
- lock, version reads, DDL, and version inserts all commit together — Postgres
  DDL is transactional, so a failed migration leaves nothing half-applied.

The existing ``frames_server_`` stores are deliberately untouched here: issue
#84 tracks converging them, and rewriting five shipped stores is not this
issue's scope. They keep their (masked) startup race.

Wiring follows the house pattern: the tables ride the shared
``frames.postgres.url`` and are migrated at app startup when
``frames.postgres.auto_migrate`` is set, over the shared psycopg pool —
never a fresh ``psycopg.connect``.
"""

from __future__ import annotations

import hashlib
import logging

collab_logger = logging.getLogger("frames_server.collab_schema")

# Fixed advisory-lock key for collab schema migrations:
# int.from_bytes(b"collab_1", "big"). Any 64-bit value would do as long as
# every writer of collab_ DDL uses the same one; deriving it from a name makes
# a collision with another advisory-lock user of the shared database unlikely,
# and makes the constant greppable.
COLLAB_SCHEMA_LOCK_KEY = int.from_bytes(b"collab_1", "big")

# Bookkeeping table recording which migrations this database has applied.
COLLAB_SCHEMA_VERSION_TABLE = "collab_schema_migrations"

NEUTRAL_ORG_NAME = "Unnamed organization"
"""The name every organization starts with, including one created by accepting
an invitation (issue #89).

Neutral by ratified decision — the dated Gate B revision of 2026-08-04
replaced the original "derive the name from login information" resolution.
This constant is the Python-side spelling of migration v1's column default;
:func:`~.invitations.PostgresInvitationService.accept` never supplies a name,
so the default is what applies and there is exactly one place the placeholder
is written. A test pins the two spellings together.
"""

# Ordered, append-only migration history for the collab_ tables.
#
# RULES:
#   1. Versions are append-only and are never renumbered.
#   2. The statements of a RELEASED version are frozen text. A deployment that
#      has recorded version N will never re-run it, so editing version N's SQL
#      forks reality between old and new databases. Fix a released version by
#      appending a new one.
#   3. Statements stay idempotent where the DDL allows it (IF NOT EXISTS), so a
#      database that was hand-patched between releases does not wedge the whole
#      migration transaction.
#
# Rule 2 is enforced mechanically, not just by review (issue #73): the registry
# stores a SHA-256 checksum of each applied version's statement text, and the
# runner verifies every already-applied version against the code's current text
# before applying anything. An in-place edit of a shipped version is therefore
# a startup failure naming the version, instead of a silent fork between old
# and new databases.
COLLAB_SCHEMA_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        1,
        (
            # Organizations. `id` is an opaque generated value and the public
            # identifier — there is no slug, and it is never derived from a
            # person's or a company's name. `name` is display-only: nullable,
            # non-unique, and defaulted to a neutral placeholder rather than
            # anything inferred from the creator. `created_by` is the OIDC sub.
            """
            CREATE TABLE IF NOT EXISTS collab_orgs (
                id          text PRIMARY KEY,
                name        text DEFAULT 'Unnamed organization',
                created_at  timestamptz NOT NULL DEFAULT now(),
                created_by  text NOT NULL
            )
            """,
            # One home organization per login. The PRIMARY KEY on user_id
            # (= OIDC sub) *is* that invariant, and it makes the hot query —
            # "which home org does this sub belong to?" — an exact PK lookup.
            #
            # `status` is load-bearing: removal keeps the row and flips it to
            # 'removed' so the home-org binding stays enforceable (a removed
            # user cannot re-register into a different org), while the auth
            # choke point resolves such a row to "no organization" (issue #63).
            # Nothing here ever deletes a membership row.
            #
            # `email`/`display_name` are display and contact fields only. They
            # are never ACL principals — authorization keys on `user_id`.
            """
            CREATE TABLE IF NOT EXISTS collab_org_members (
                user_id       text PRIMARY KEY,
                org_id        text NOT NULL REFERENCES collab_orgs(id),
                role          text NOT NULL CHECK (role IN ('owner', 'member')),
                email         text,
                display_name  text,
                created_at    timestamptz NOT NULL DEFAULT now(),
                status        text NOT NULL DEFAULT 'active'
                              CHECK (status IN ('active', 'removed'))
            )
            """,
            # Membership listing ("everyone in org X") is the only access path
            # that is not the user_id primary key.
            """
            CREATE INDEX IF NOT EXISTS collab_org_members_org
            ON collab_org_members (org_id)
            """,
        ),
    ),
    # No `collab_invitations` table here on purpose. Nothing in this phase
    # reads or writes invitations, and baking guessed columns into every
    # deployment that runs auto-migrate meanwhile buys nothing: with the
    # versioned runner above, adding that table later costs exactly one
    # appended migration version.
    #
    # `workspace_id` is not modeled anywhere: it is the literal constant
    # "default" and there is no workspaces table.
    (
        2,
        (
            # Platform-scoped roles (issue #87, Gate E). A second authority
            # axis from the org-scoped collab_org_members.role: an org owner
            # has authority inside one organization, an operator across the
            # deployment. Deliberately not merged into the membership table
            # and deliberately not a Keycloak realm role — Keycloak
            # authenticates, this server authorizes.
            #
            # `user_id` is the OIDC sub, like every other principal column.
            # `granted_by` is nullable because the bootstrap row is inserted
            # by hand (recorded as an `operator.manual` audit event — see the
            # runbook in docs/frames-operations.md); there are no grant/revoke
            # endpoints yet, on purpose. The CHECKs guard the actual write
            # path for this beta, which is psql: a typo'd role or status in a
            # hand-run insert is refused by the schema instead of silently
            # granting nothing. `status` defaults to 'active' to keep the
            # documented bootstrap insert a genuine one-liner, mirroring
            # collab_org_members.status.
            """
            CREATE TABLE IF NOT EXISTS collab_platform_roles (
                user_id     text PRIMARY KEY,
                role        text NOT NULL CHECK (role IN ('operator')),
                granted_at  timestamptz NOT NULL DEFAULT now(),
                granted_by  text,
                status      text NOT NULL DEFAULT 'active'
                            CHECK (status IN ('active', 'revoked'))
            )
            """,
            # The audit event log (issue #87). Every privileged action commits
            # its row here in the same transaction as the mutation it
            # describes — see frames/audit.py, the only writer.
            #
            # `actor` is the sub (immutable); `actor_label`/`target_label`
            # snapshot the human-readable name AT TIME OF ACTION, because a
            # row read months later showing only a UUID is useless and the
            # live display fields can change or vanish. `org_id` is nullable:
            # operator actions can be hub-scoped. `detail` is redacted by its
            # writers and must NEVER hold an invitation secret.
            #
            # `action` and `target_type` are CHECK-constrained to the ratified
            # closed vocabularies: rows are found by exact-match runbook
            # queries, so an unvocabularied value is a silent audit gap, and
            # the constraint also covers rows written from psql — which
            # includes every `operator.manual` entry. The lists here and in
            # frames/audit.py (AUDIT_ACTIONS / AUDIT_TARGET_TYPES) are pinned
            # together by a unit test; adding an action later is an appended
            # migration that replaces the constraint, plus the AUDIT_ACTIONS
            # change, on the issue that introduces the action.
            #
            # Append-only is a code convention, not an enforced boundary: the
            # application role owns this table (auto-migration creates it over
            # the runtime pool), so a REVOKE would be theater — an owner can
            # re-grant to itself. The enforceable claim, tested against the
            # code, is that no application code path updates or deletes rows.
            #
            # No indexes beyond the primary key, deliberately: the log is read
            # with psql per the runbook and beta volume is hundreds of rows.
            """
            CREATE TABLE IF NOT EXISTS collab_audit_events (
                id            bigserial PRIMARY KEY,
                at            timestamptz NOT NULL DEFAULT now(),
                actor         text NOT NULL,
                actor_label   text,
                action        text NOT NULL
                              CHECK (action IN ('invitation.send', 'invitation.redeem',
                                                'invitation.revoke', 'membership.create',
                                                'org.create', 'org.rename', 'operator.manual')),
                target_type   text
                              CHECK (target_type IN ('org', 'user', 'invitation')),
                target_id     text,
                target_label  text,
                org_id        text,
                detail        jsonb
            )
            """,
        ),
    ),
    (
        3,
        (
            # Invitations (issue #89, Gate B). Version 2 has shipped, so this
            # is an appended version and never an amendment of it — the rule
            # stated at the top of this list.
            #
            # `id` is an opaque generated value (uuid4 hex): it is quoted in
            # error messages, audit rows, and the SES `invitation_id` tag, so
            # it must carry nothing about the invitee. The **secret** is not
            # here in any form — `token_hash` is the SHA-256 hex of a 256-bit
            # random secret, and the raw secret exists only in memory between
            # minting and handing it to the email adapter. UNIQUE both
            # enforces single-issuance-per-secret and makes redemption an
            # index lookup.
            #
            # `org_id` NULL means an org-creating invitation: acceptance mints
            # a new organization with the accepter as its owner. A non-NULL
            # value is a join-this-org invitation and is immutable for the
            # row's whole life — the authorization checks on revoke read it,
            # and a mutable target would make those checks racy.
            #
            # `email` is the invited address **exactly as issued** (Gate B
            # chose exact match, 2026-08-03: there is no canonicalization
            # ruleset, so there is no canonical column and no second spelling
            # to keep in sync). Acceptance compares the verified `email`
            # claim to this string with `=`.
            #
            # `status` is the stored state only. `expired` is DERIVED
            # (`pending` past `expires_at`) so no sweeper has to keep a fourth
            # state true; see frames/invitations.py effective_status.
            #
            # The `accepted_*` columns are the replay record: they make a
            # redeemed token idempotent for the login that redeemed it, and
            # `accepted_org_id` records which organization the acceptance
            # produced (the created one, for a NULL-`org_id` invitation).
            """
            CREATE TABLE IF NOT EXISTS collab_invitations (
                id               text PRIMARY KEY,
                org_id           text REFERENCES collab_orgs(id),
                email            text NOT NULL,
                token_hash       text NOT NULL UNIQUE,
                status           text NOT NULL DEFAULT 'pending'
                                 CHECK (status IN ('pending', 'accepted', 'revoked')),
                created_at       timestamptz NOT NULL DEFAULT now(),
                created_by       text NOT NULL,
                expires_at       timestamptz NOT NULL,
                accepted_at      timestamptz,
                accepted_by      text,
                accepted_org_id  text REFERENCES collab_orgs(id),
                revoked_at       timestamptz,
                revoked_by       text
            )
            """,
            # "Every invitation for org X, newest first" is the owner's list
            # view and the only access path that is neither the primary key
            # nor the unique token hash. The operator's list is unfiltered and
            # sorts on created_at, which this index does not serve — beta
            # volume is hundreds of rows, so that sort stays a plain one.
            """
            CREATE INDEX IF NOT EXISTS collab_invitations_org
            ON collab_invitations (org_id, created_at DESC)
            """,
            # `org_id` immutability, enforced rather than assumed.
            #
            # Acceptance reads the invitation once outside its transaction to
            # decide the audited action and scope — `org.create` for a
            # NULL-`org_id` invitation, `invitation.redeem` otherwise — because
            # audited() fixes both before the body runs and refuses to let the
            # body rewrite them. It then re-reads FOR UPDATE and re-checks
            # everything. That design is safe only because `org_id` cannot
            # change between the two reads: if it could, an acceptance could
            # commit a join to an existing organization while its audit row
            # said `org.create` for an organization that was never created —
            # a log that describes something that did not happen, which is the
            # one failure the whole audited primitive exists to prevent.
            #
            # No application path updates this column, but "no code does it"
            # is not the same claim as "it cannot happen": the beta's write
            # path for corrections is psql, by hand, and this table's rows are
            # exactly the sort a person edits during an incident. The trigger
            # covers that case too, which is the point of putting it in the
            # database rather than in a comment.
            #
            # check_violation (23514) so it arrives as psycopg.errors.
            # CheckViolation — the same class an inline CHECK would raise, had
            # one been able to see the old row.
            """
            CREATE OR REPLACE FUNCTION collab_invitations_freeze_org_id()
            RETURNS trigger AS $$
            BEGIN
                IF NEW.org_id IS DISTINCT FROM OLD.org_id THEN
                    RAISE EXCEPTION
                        'collab_invitations.org_id is immutable (invitation %)', OLD.id
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """,
            # DROP-then-CREATE rather than CREATE OR REPLACE TRIGGER, which is
            # PostgreSQL 14+; the migration runner's idempotence rule should
            # not quietly raise this chart's minimum server version.
            """
            DROP TRIGGER IF EXISTS collab_invitations_org_id_immutable ON collab_invitations
            """,
            """
            CREATE TRIGGER collab_invitations_org_id_immutable
            BEFORE UPDATE ON collab_invitations
            FOR EACH ROW EXECUTE FUNCTION collab_invitations_freeze_org_id()
            """,
        ),
    ),
    (
        4,
        (
            # Account provisioning, keyed by the **folded** address (#172).
            #
            # This table is two things at once, and the second is why it is a
            # table rather than a column on `collab_invitations`:
            #
            # 1. the durable phase of setting an invitee's account up, so an
            #    interrupted run can be resumed rather than guessed at;
            # 2. **the claim that serializes provisioning.** The primary key is
            #    the folded address, so the first writer wins and a second
            #    concurrent request's insert conflicts. It learns it is not the
            #    creator and reads this row instead of creating a second
            #    account.
            #
            # `pg_advisory_xact_lock` cannot do that job here. Invitation rows
            # commit *before* the external calls (the audited claim is the
            # recoverable partial state), and a transaction-scoped lock is gone
            # at commit -- so the window this table closes is exactly the window
            # that lock leaves open.
            #
            # Keyed per **address**, not per invitation, because two live
            # invitations for one address are deliberate on the `/v1` routes and
            # pinned by a regression test. Provisioning happens at most once per
            # address; issuance may repeat. Keying it per invitation would make
            # two invitations own one account.
            #
            # The key is the folded form from `ascii_folded_bytes` (#157), the
            # same one Gate B matches on. Storing the raw address would let
            # `Alice@` and `alice@` each provision an account when only one of
            # them can ever accept.
            """
            CREATE TABLE IF NOT EXISTS collab_provisioned_accounts (
                email_folded     text PRIMARY KEY,
                email            text NOT NULL,
                phase            text NOT NULL
                                 CHECK (phase IN ('claimed', 'account_created',
                                                  'setup_sent', 'complete')),
                idp_user_id      text,
                first_invitation text NOT NULL REFERENCES collab_invitations(id),
                created_at       timestamptz NOT NULL DEFAULT now(),
                updated_at       timestamptz NOT NULL DEFAULT now(),
                -- A phase past `claimed` without an account id is not a state
                -- this workflow can be in, and it is the shape a half-written
                -- recovery would produce. Refused here rather than tolerated,
                -- because the recovery path reads this column to decide whether
                -- to adopt or create.
                CONSTRAINT collab_provisioned_accounts_id_present
                    CHECK (phase = 'claimed' OR idp_user_id IS NOT NULL)
            )
            """,
            # Cleanup and alerting both ask "what has been sitting unfinished",
            # which is a scan by phase over age.
            """
            CREATE INDEX IF NOT EXISTS collab_provisioned_accounts_phase_idx
            ON collab_provisioned_accounts (phase, updated_at)
            """,
        ),
    ),
    (
        5,
        (
            # Widen the audit action vocabulary by one: `service_access.grant`
            # (issue #180). The vocabulary is a CHECK constraint written by
            # version 2, whose text is frozen -- so the constraint is
            # *replaced* here rather than edited there, which is rule 2 of this
            # list applied to a constraint instead of a table.
            #
            # The DROP names the constraint PostgreSQL generated for version
            # 2's unnamed column CHECK, and the ADD gives the replacement that
            # same name -- so this migration is the last one that has to know
            # the generated spelling, and every future widening drops a name
            # this file chose. `IF EXISTS` keeps the statement safe on a
            # database where the constraint was already replaced by hand.
            #
            # Two things make a silently-wrong outcome impossible rather than
            # unlikely: the constraint is NOT VALID-free (it validates the
            # existing rows as it is added, and every existing row holds a
            # value that is still in the set), and a live test inserts one row
            # per action in `AUDIT_ACTIONS` plus one bogus action, so a
            # constraint left behind under a different name -- which would
            # keep refusing the new value -- fails that test rather than
            # production's first grant.
            """
            ALTER TABLE collab_audit_events
            DROP CONSTRAINT IF EXISTS collab_audit_events_action_check
            """,
            """
            ALTER TABLE collab_audit_events
            ADD CONSTRAINT collab_audit_events_action_check
            CHECK (action IN ('invitation.send', 'invitation.redeem',
                              'invitation.revoke', 'membership.create',
                              'org.create', 'org.rename', 'operator.manual',
                              'service_access.grant'))
            """,
        ),
    ),
    (
        6,
        (
            # What an acceptance owes somebody, durably (issue #180).
            #
            # The audit table records what happened; this records what is still
            # owed, and the two are not interchangeable. The `pending` row is
            # written INSIDE the acceptance's audited transaction, so it lands
            # if and only if the acceptance does -- which is what closes the
            # window between the acceptance committing and the identity-provider
            # call being recorded. Every crash point then resolves to `pending`,
            # and retrying is safe because adding a user to a group is
            # idempotent.
            #
            # PRIMARY KEY (user_id, group_path) because that pair IS the grant.
            # A second invitation to the same person does not create a second
            # thing to do, and `ON CONFLICT DO NOTHING` on the insert keeps the
            # first invitation as the recorded context rather than churning it.
            #
            # `invitation` references collab_invitations for the same reason
            # v4's claim does: the row is only meaningful as a consequence of an
            # invitation, and a dangling id would make the reconciliation list
            # unjoinable to the person it is about.
            """
            CREATE TABLE IF NOT EXISTS collab_service_access_grants (
                user_id    text NOT NULL,
                group_path text NOT NULL,
                state      text NOT NULL
                           CHECK (state IN ('pending', 'granted', 'failed')),
                invitation text NOT NULL REFERENCES collab_invitations(id),
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (user_id, group_path)
            )
            """,
            # The reconciliation read is "what is owed, oldest first", which is
            # a scan by state over age. Same shape as v4's index and the same
            # reason: this is the query a sweep and an operator page both run.
            """
            CREATE INDEX IF NOT EXISTS collab_service_access_grants_state_idx
            ON collab_service_access_grants (state, created_at)
            """,
        ),
    ),
    (
        7,
        (
            # Where a platform-role row came from, so that sync can remove its
            # own rows without ever removing a hand-administered one.
            #
            # Sync has to be able to revoke: a copy of the identity provider's
            # answer that only ever adds would leave a dropped admin's row
            # standing forever, and the deployment would believe the provider
            # had revoked them. But the bootstrap operator's row was inserted
            # by hand, and their subject may be in no group at all -- so a sync
            # that could revoke anything would lock the first admin out of the
            # deployment they had just bootstrapped, on their next sign-in.
            #
            # The default is `manual`, which is what makes this migration safe
            # on a live database: every row that exists when it runs was
            # inserted by hand, and every one of them keeps its authority.
            """
            ALTER TABLE collab_platform_roles
            ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'manual'
            CHECK (source IN ('manual', 'idp'))
            """,
            # Widen the audit action vocabulary by two: `platform_role.grant`
            # and `platform_role.revoke`. Same mechanics as v5 -- drop the
            # constraint by the name v5 chose, add the replacement under that
            # same name -- and the same test pins the effective constraint to
            # AUDIT_ACTIONS, so widening one without the other fails at unit
            # speed rather than on a production sign-in.
            """
            ALTER TABLE collab_audit_events
            DROP CONSTRAINT IF EXISTS collab_audit_events_action_check
            """,
            """
            ALTER TABLE collab_audit_events
            ADD CONSTRAINT collab_audit_events_action_check
            CHECK (action IN ('invitation.send', 'invitation.redeem',
                              'invitation.revoke', 'membership.create',
                              'org.create', 'org.rename', 'operator.manual',
                              'service_access.grant',
                              'platform_role.grant', 'platform_role.revoke'))
            """,
        ),
    ),
    (
        8,
        (
            # Version 2 shipped this table with no index beyond its primary
            # key, and said so deliberately: the log was read from psql at
            # beta volume. It now has a paginated reader behind the admin
            # panel, so the two filters that reader offers need to be seeks
            # rather than scans over a table that only grows.
            #
            # `id DESC` in both, matching the reader's ORDER BY exactly: an
            # index whose order disagrees with the query's is read forwards
            # and then sorted, which is the cost this exists to avoid. The
            # unfiltered listing is already served by the primary key.
            """
            CREATE INDEX IF NOT EXISTS collab_audit_events_actor_idx
            ON collab_audit_events (actor, id DESC)
            """,
            """
            CREATE INDEX IF NOT EXISTS collab_audit_events_action_idx
            ON collab_audit_events (action, id DESC)
            """,
        ),
    ),
    (
        9,
        (
            # Widen the action vocabulary by one: `service_access.revoke`.
            # Same mechanics as v5 and v7. It had no counterpart before because
            # nothing in this codebase could take service access away; the
            # admin panel can.
            """
            ALTER TABLE collab_audit_events
            DROP CONSTRAINT IF EXISTS collab_audit_events_action_check
            """,
            """
            ALTER TABLE collab_audit_events
            ADD CONSTRAINT collab_audit_events_action_check
            CHECK (action IN ('invitation.send', 'invitation.redeem',
                              'invitation.revoke', 'membership.create',
                              'org.create', 'org.rename', 'operator.manual',
                              'service_access.grant', 'service_access.revoke',
                              'platform_role.grant', 'platform_role.revoke'))
            """,
        ),
    ),
    (
        10,
        (
            # Which connectors an administrator has switched off.
            #
            # One bit per connector and nothing else. Credentials stay in
            # deployment configuration, which is what decides whether a
            # connector is possible at all; this decides whether it is
            # currently offered. A row here can only ever take a configured
            # connector away, never conjure an unconfigured one -- there is no
            # credential column for it to supply.
            #
            # Absence means enabled, so a deployment that never opens this
            # screen behaves exactly as it did before the table existed.
            """
            CREATE TABLE IF NOT EXISTS collab_connector_state (
                connector   text PRIMARY KEY,
                enabled     boolean NOT NULL,
                updated_at  timestamptz NOT NULL DEFAULT now(),
                updated_by  text
            )
            """,
            # Two more actions, and the first new target type since version 2:
            # a connector is not an org, a user or an invitation.
            """
            ALTER TABLE collab_audit_events
            DROP CONSTRAINT IF EXISTS collab_audit_events_action_check
            """,
            """
            ALTER TABLE collab_audit_events
            ADD CONSTRAINT collab_audit_events_action_check
            CHECK (action IN ('invitation.send', 'invitation.redeem',
                              'invitation.revoke', 'membership.create',
                              'org.create', 'org.rename', 'operator.manual',
                              'service_access.grant', 'service_access.revoke',
                              'platform_role.grant', 'platform_role.revoke',
                              'connector.enable', 'connector.disable'))
            """,
            """
            ALTER TABLE collab_audit_events
            DROP CONSTRAINT IF EXISTS collab_audit_events_target_type_check
            """,
            """
            ALTER TABLE collab_audit_events
            ADD CONSTRAINT collab_audit_events_target_type_check
            CHECK (target_type IN ('org', 'user', 'invitation', 'connector'))
            """,
        ),
    ),
    (
        11,
        (
            # The Cog catalog (issue #84): one row per artifact the indexer has
            # seen in a configured registry source, keyed by content digest.
            #
            # **Identity is the digest.** PRIMARY KEY (source_id, repository,
            # digest) because the same bytes may legitimately be published to
            # several repositories (and enumerated by several sources), and
            # each such location is its own row; `cog_id`/`name` are search
            # keys read out of the Cog's own declarations, and the repository
            # path is NOT identity -- published repository names carry an id
            # suffix and are free to change. `host` is the registry host of
            # the source's *external* URL, so `<host>/<repository>@<digest>`
            # is the pinned install reference and can be rebuilt from the row.
            #
            # `card` is the bundle reader's output, verbatim, as structured
            # JSON: the whole profile is preserved (requires/provides/io stay
            # objects, not strings), which is what the GIN index below makes
            # filterable. `status` says what kind of row this is:
            #   indexed  -- the reader produced a card (errors and all: a
            #               draft or a broken profile is still a Cog);
            #   non_cog  -- the manifest carries no COG.md (and no Prog
            #               pixi.toml), recorded with a reason so a
            #               repository full of images is not re-read every
            #               sweep;
            #   failed   -- the fetch or read failed; `read_errors` says how,
            #               and the next sweep retries it.
            # `read_errors` duplicates card.errors for indexed rows so a
            # "what is broken" query never has to open the card.
            #
            # `removed_at` is NULL while the digest is present in the
            # registry. Rows are never deleted by application code: an install
            # or a run may reference a digest long after its publisher removed
            # it, and "this once existed and is now gone" is an answer the
            # catalog must be able to give.
            """
            CREATE TABLE IF NOT EXISTS collab_cog_artifacts (
                source_id            text NOT NULL,
                host                 text NOT NULL,
                repository           text NOT NULL,
                digest               text NOT NULL,
                tags                 text[] NOT NULL DEFAULT '{}',
                pushed_at            timestamptz,
                indexed_at           timestamptz NOT NULL DEFAULT now(),
                manifest_media_type  text,
                status               text NOT NULL
                                     CHECK (status IN ('indexed', 'non_cog', 'failed')),
                card                 jsonb,
                cog_id               text,
                name                 text,
                version              text,
                kind                 text,
                publisher            text,
                manifest_schema      text,
                read_errors          jsonb NOT NULL DEFAULT '[]'::jsonb,
                removed_at           timestamptz,
                PRIMARY KEY (source_id, repository, digest)
            )
            """,
            # "Every version of this Cog" (removed ones included) starts from
            # cog_id, so that index is full; "every model Cog" from kind. The
            # catalog's other reads -- the newest present row per Cog, the
            # present rows per repository -- exclude removed rows, so a partial
            # index on the present rows keeps those cheap as the removed tail
            # grows, and it is what "WHERE removed_at IS NULL" plans against.
            """
            CREATE INDEX IF NOT EXISTS collab_cog_artifacts_cog_id_idx
            ON collab_cog_artifacts (cog_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS collab_cog_artifacts_kind_idx
            ON collab_cog_artifacts (kind)
            """,
            """
            CREATE INDEX IF NOT EXISTS collab_cog_artifacts_present_idx
            ON collab_cog_artifacts (cog_id, repository) WHERE removed_at IS NULL
            """,
            # Containment filters over the structured card ("requires
            # capability X", "provides Y", "accepts io Z") are `card @> ...`
            # queries; jsonb_path_ops is the GIN operator class built for
            # exactly that operator, and it is smaller than the default class.
            """
            CREATE INDEX IF NOT EXISTS collab_cog_artifacts_card_idx
            ON collab_cog_artifacts USING GIN (card jsonb_path_ops)
            """,
        ),
    ),
    (
        12,
        (
            # The Track (issue #5): the append-only record of every run, which is
            # the only source of a run's status (ADR-0002 D3). The execution
            # package's `PostgresTrackStore` reads and writes these tables; on the
            # hub they are created here and never by that store's `ensure_schema`,
            # which serves the standalone package and local use. That store's
            # `_SCHEMA` is this registry's Track statements in order, and
            # execution/tests/test_track_ddl.py holds the two equal. Like every
            # released version this one is frozen by its checksum: a change to the
            # Track tables is a new migration, with the same statements appended
            # to `_SCHEMA`, never an edit here.
            #
            # `sequence` is global, so events written by several replicas replay
            # in one stable order. `schema` is the version of the event's shape
            # (docs/cog-execution/track.md); rows written before events carried
            # one read as version 0, which the default records.
            "CREATE SEQUENCE IF NOT EXISTS collab_track_event_sequence",
            """
            CREATE TABLE IF NOT EXISTS collab_track_events (
                sequence    bigint PRIMARY KEY DEFAULT nextval('collab_track_event_sequence'),
                event_id    text NOT NULL UNIQUE,
                run_id      text NOT NULL,
                event_type  text NOT NULL,
                payload     jsonb NOT NULL DEFAULT '{}'::jsonb,
                occurred_at timestamptz NOT NULL,
                schema      integer NOT NULL DEFAULT 0
            )
            """,
            "ALTER TABLE collab_track_events ADD COLUMN IF NOT EXISTS schema integer NOT NULL DEFAULT 0",
            "CREATE INDEX IF NOT EXISTS collab_track_events_run_sequence ON collab_track_events (run_id, sequence)",
            # At most one submission per run: two API replicas cannot both start
            # the same run; the losing insert fails on this index.
            "CREATE UNIQUE INDEX IF NOT EXISTS collab_track_one_submission "
            "ON collab_track_events (run_id) WHERE event_type = 'op_submitted'",
            # A step's payload above the inline threshold, kept beside the Track
            # and named by the `payload_ref` of its `step_completed` event.
            """
            CREATE TABLE IF NOT EXISTS collab_track_payloads (
                ref        text PRIMARY KEY,
                run_id     text NOT NULL,
                payload    jsonb NOT NULL,
                stored_at  timestamptz NOT NULL DEFAULT now()
            )
            """,
            "CREATE INDEX IF NOT EXISTS collab_track_payloads_run ON collab_track_payloads (run_id)",
        ),
    ),
    (
        13,
        (
            # Pulls through the Hub (issue #179): the Hub serves the OCI read
            # API itself, and a client authenticates to that surface with a
            # *registry credential* exchanged from its Hub session, never with
            # the Hub token. Two tables, both holding only SHA-256 digests of
            # the secrets they stand for: the credential a client stores, and
            # the short-lived repository-scoped tokens minted from it.
            #
            # `user_id` is the ACL principal the credential was exchanged by.
            # `scope` is what the credential may ask for ('pull' today; the
            # publish half adds its own value, which is why there is no CHECK
            # to migrate later). `session_id` is the `sid` of the Hub session
            # it was exchanged from, when the token carried one. A credential
            # is revoked by deleting its row, and its tokens go with it
            # (ON DELETE CASCADE), which is what makes revocation immediate.
            """
            CREATE TABLE IF NOT EXISTS collab_cog_registry_credentials (
                id          text PRIMARY KEY,
                user_id     text NOT NULL,
                secret_hash text NOT NULL,
                scope       text NOT NULL,
                session_id  text,
                created_at  timestamptz NOT NULL DEFAULT now(),
                expires_at  timestamptz NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS collab_cog_registry_credentials_user_idx
            ON collab_cog_registry_credentials (user_id, created_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS collab_cog_registry_credentials_expiry_idx
            ON collab_cog_registry_credentials (expires_at)
            """,
            # `credential_id` is NULL for a token minted straight from a Hub
            # access token, which has no credential to be revoked with; such a
            # token is bounded by its own `expires_at` and by the per-user
            # revoke, which is why `user_id` is here as well.
            """
            CREATE TABLE IF NOT EXISTS collab_cog_registry_tokens (
                token_hash    text PRIMARY KEY,
                user_id       text NOT NULL,
                credential_id text REFERENCES collab_cog_registry_credentials (id) ON DELETE CASCADE,
                repositories  text[] NOT NULL DEFAULT '{}',
                actions       text NOT NULL DEFAULT 'pull',
                created_at    timestamptz NOT NULL DEFAULT now(),
                expires_at    timestamptz NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS collab_cog_registry_tokens_user_idx
            ON collab_cog_registry_tokens (user_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS collab_cog_registry_tokens_credential_idx
            ON collab_cog_registry_tokens (credential_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS collab_cog_registry_tokens_expiry_idx
            ON collab_cog_registry_tokens (expires_at)
            """,
            # Every request on the Hub's /v2/ surface is an exact lookup that
            # starts from the repository path, across sources: a digest, a
            # stored tag, or a blob. The primary key leads with source_id, so
            # those reads need their own index; partial, like the other
            # present-row index, so the removed tail costs it nothing.
            """
            CREATE INDEX IF NOT EXISTS collab_cog_artifacts_repository_idx
            ON collab_cog_artifacts (repository, digest) WHERE removed_at IS NULL
            """,
            # Which blobs a manifest references: its config and layer
            # descriptors, written once its bytes have been verified against
            # its digest (what a digest references never changes, so rows are
            # inserted and never updated; the catalog store deletes them when
            # their artifact is marked removed). A blob request names a repository and a
            # blob digest and nothing else; this is what ties it to a manifest
            # without reading registries at request time. The rows grant
            # nothing alone: a blob is pullable only while a row here joins
            # to a present, indexed artifact, so removing a version takes its
            # blobs with it. No foreign key to the artifacts table, because
            # that table's rows are replaced whole on reindex.
            """
            CREATE TABLE IF NOT EXISTS collab_cog_manifest_blobs (
                source_id       text NOT NULL,
                repository      text NOT NULL,
                manifest_digest text NOT NULL,
                blob_digest     text NOT NULL,
                size            bigint NOT NULL CHECK (size >= 0),
                media_type      text NOT NULL DEFAULT '',
                PRIMARY KEY (source_id, repository, manifest_digest, blob_digest)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS collab_cog_manifest_blobs_blob_idx
            ON collab_cog_manifest_blobs (repository, blob_digest)
            """,
        ),
    ),
    (
        14,
        (
            # Publishing through the Hub (issue #180): pushes arrive on the
            # Hub's /v2/ surface and are written through to one source.
            #
            # The organization a registry credential's owner acted in when it
            # was exchanged. On a membership-resolving deployment the owner's
            # organization is re-read on every push; this is what a publish
            # is attributed to where there is no membership table to re-read.
            "ALTER TABLE collab_cog_registry_credentials ADD COLUMN IF NOT EXISTS org_id text",
            # Which of a token's repositories it may push to. Per repository,
            # never per token: asking to push to one repository and pull from
            # another yields exactly that. Empty for every token that is not
            # minted from a publish credential. This, not version 13's
            # `actions` column (which stays 'pull' on every row), is what
            # decides a push.
            """
            ALTER TABLE collab_cog_registry_tokens
            ADD COLUMN IF NOT EXISTS push_repositories text[] NOT NULL DEFAULT '{}'
            """,
            # Who published an artifact *through the Hub*: the authenticated
            # Hub user and their organization, as opposed to the `publisher`
            # the bundle declares about itself. NULL for anything the indexer
            # found that was pushed to the registry directly. Not in the
            # indexer's upsert column list, so a sweep that rewrites the row
            # leaves both as they are.
            "ALTER TABLE collab_cog_artifacts ADD COLUMN IF NOT EXISTS published_by text",
            "ALTER TABLE collab_cog_artifacts ADD COLUMN IF NOT EXISTS published_org text",
            # A repository first published through the Hub belongs to the
            # publisher's organization: later pushes need membership of it.
            # One row per repository path, written *before* the first
            # manifest is forwarded to the registry: pending (`committed`
            # false) and already owned by the publisher's organization, then
            # committed once the registry has accepted a manifest. A pending
            # row is deleted only when the registry has definitely refused
            # every attempt that organization had in flight (`holders` counts
            # them); an unknown outcome leaves it pending and owned, so a
            # name is never handed to another organization automatically. A
            # platform operator releases a stuck one by hand (see
            # docs/cog-registry.md). Committed rows, and pending rows past a
            # grace period, extend their source's enumeration. `owner_org_id`
            # is NULL when a platform operator with no organization
            # published it.
            """
            CREATE TABLE IF NOT EXISTS collab_cog_repositories (
                repository   text PRIMARY KEY,
                source_id    text NOT NULL,
                owner_org_id text,
                created_by   text NOT NULL,
                committed    boolean NOT NULL DEFAULT false,
                holders      integer NOT NULL DEFAULT 0,
                created_at   timestamptz NOT NULL DEFAULT now()
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS collab_cog_repositories_source_idx
            ON collab_cog_repositories (source_id)
            """,
            # Who published a digest through the Hub, one row per attempt.
            # Written before the manifest is forwarded; `accepted_at` is set
            # only once the registry has accepted that attempt's manifest,
            # and only an accepted attempt ever attributes a catalog row --
            # the earliest, when there are several. An attempt that was
            # refused is deleted by its own request; one that never resolved
            # attributes nothing and is purged by age. Accepted attempts are
            # kept: they are the record of who published what. If acceptance
            # could not be recorded, the digest's publisher stays unknown.
            """
            CREATE TABLE IF NOT EXISTS collab_cog_publication_attempts (
                attempt_id    text PRIMARY KEY,
                source_id     text NOT NULL,
                repository    text NOT NULL,
                digest        text NOT NULL,
                published_by  text NOT NULL,
                published_org text,
                created_at    timestamptz NOT NULL DEFAULT now(),
                accepted_at   timestamptz
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS collab_cog_publication_attempts_digest_idx
            ON collab_cog_publication_attempts (source_id, repository, digest)
            """,
            # An upload in progress. The client only ever sees `id`; the
            # backing registry's own session URL stays here, so any replica
            # can continue an upload and no response names the backing host.
            # The row is written *before* the registry is asked to open its
            # session (`upstream_location` NULL until it has), held by its
            # opener's lease. `received` is the number of bytes forwarded so
            # far. `leased_until` is the mutation lease: one request at a
            # time, across replicas, may forward bytes to (or close, or
            # cancel) a session, and it gives the lease back only once the
            # outcome is recorded -- a lease that has run out marks a session
            # nobody can account for, which is dead. A dead or expired row is
            # kept until its registry session has been cancelled, or a day
            # has passed.
            """
            CREATE TABLE IF NOT EXISTS collab_cog_upload_sessions (
                id                text PRIMARY KEY,
                user_id           text NOT NULL,
                repository        text NOT NULL,
                source_id         text NOT NULL,
                upstream_location text,
                received          bigint NOT NULL DEFAULT 0 CHECK (received >= 0),
                leased_until      timestamptz,
                created_at        timestamptz NOT NULL DEFAULT now(),
                expires_at        timestamptz NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS collab_cog_upload_sessions_expiry_idx
            ON collab_cog_upload_sessions (expires_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS collab_cog_upload_sessions_user_idx
            ON collab_cog_upload_sessions (user_id)
            """,
        ),
    ),
)


def _validate_migrations() -> None:
    """Fail loudly at import if the migration list breaks its own rules.

    Cheap insurance for the append-only invariant: a duplicated or out-of-order
    version would otherwise be found by a production database applying the
    wrong thing (or nothing).
    """

    versions = [version for version, _ in COLLAB_SCHEMA_MIGRATIONS]
    if versions != sorted(set(versions)) or (versions and versions[0] != 1):
        raise RuntimeError(f"COLLAB_SCHEMA_MIGRATIONS must have unique, ascending versions starting at 1: {versions}")


_validate_migrations()

LATEST_COLLAB_SCHEMA_VERSION = COLLAB_SCHEMA_MIGRATIONS[-1][0] if COLLAB_SCHEMA_MIGRATIONS else 0
"""Highest migration version this build knows how to apply."""


def collab_migration_checksum(statements: tuple[str, ...]) -> str:
    """SHA-256 hex digest of a migration's statement text (issue #73).

    Hashes the **exact** text, statements NUL-separated so a statement boundary
    cannot be moved without changing the digest. Deliberately no whitespace or
    comment normalization: rule 2 above freezes the *text* of a released
    version, and the comments in this file are part of what review approved —
    a "cosmetic" edit to shipped SQL is exactly the kind of drift the checksum
    exists to surface. Cosmetic changes go in an appended version like any
    other change, or nowhere.
    """

    hasher = hashlib.sha256()
    for statement in statements:
        hasher.update(statement.encode("utf-8"))
        hasher.update(b"\x00")
    return hasher.hexdigest()


class CollabSchemaChecksumError(RuntimeError):
    """An already-applied migration's text no longer matches what was applied."""


def _verify_recorded_checksums(conn) -> dict[int, str]:
    """Check every applied version's recorded checksum against the code's text.

    Runs inside the runner's locked transaction, **before** anything is
    applied. Returns ``{version: recorded_checksum}`` for the applied rows —
    the caller only needs ``max()`` of the keys, but the full mapping is what
    tests (and any future caller reporting on checksum state) assert against,
    so don't simplify it to a version number.

    Three cases per applied row:

    * **Match** — fine.
    * **NULL** — a legacy row, applied by a build that predates checksums
      (issue #73). Nothing can be verified retroactively, so it is accepted
      and backfilled with the current text's checksum; from the next startup
      on it is a verified row like any other.
    * **Mismatch** — fail fast, naming the version. The database applied one
      text and the code now carries another; starting anyway would let old and
      new databases diverge silently, which is the failure mode the checksum
      exists to make mechanical. The fix is to restore the released text and
      append a new version (rule 2), never to edit the recorded row.

    A recorded version this build does not know (database ahead, mid-rolling-
    update) is skipped: there is no code text to compare against, and the
    version preflight already handles that case deliberately.
    """

    statements_by_version = dict(COLLAB_SCHEMA_MIGRATIONS)
    recorded: dict[int, str] = {}
    rows = conn.execute(f"SELECT version, checksum FROM {COLLAB_SCHEMA_VERSION_TABLE} ORDER BY version").fetchall()
    for row in rows:
        version, stored = row["version"], row["checksum"]
        recorded[version] = stored
        statements = statements_by_version.get(version)
        if statements is None:
            continue
        expected = collab_migration_checksum(statements)
        if stored is None:
            conn.execute(
                f"UPDATE {COLLAB_SCHEMA_VERSION_TABLE} SET checksum = %s WHERE version = %s",
                (expected, version),
            )
            recorded[version] = expected
            collab_logger.info(
                "collab_schema_checksum_backfilled",
                extra={"version": version, "checksum": expected},
            )
        elif stored != expected:
            raise CollabSchemaChecksumError(
                f"collab_ schema migration version {version} has been edited after it was applied: "
                f"the database recorded checksum {stored} but this build's text hashes to {expected}. "
                "Released migration text is frozen — restore the original statements for "
                f"version {version} and append the change as a new version."
            )
    return recorded


def run_collab_schema_migrations(db) -> None:
    """Apply any unapplied ``collab_`` migrations, safely under concurrency.

    ``db`` is a pooled :class:`~collab_hub_api.frames.db.PostgresDatabase`;
    ``db.connection()`` yields psycopg_pool's transaction-scoped context
    manager, so the advisory lock, the version bookkeeping, and the DDL all
    commit (or roll back) together.

    Concurrent replicas serialize on the advisory lock; whichever gets it
    second re-reads the version table *inside* the lock, finds the versions
    already recorded, and applies nothing. Failure semantics match the existing
    stores' ``auto_migrate``: an unreachable database raises here and the pod
    fails to start rather than serving against a schema it cannot verify.

    Before applying anything, every already-applied version's recorded checksum
    is verified against the code's current text (issue #73); a mismatch raises
    :class:`CollabSchemaChecksumError` naming the version, and a ``NULL``
    checksum from a pre-checksum build is accepted once and backfilled.
    """

    with db.connection() as conn:
        # Serialize before touching the catalog at all — including the version
        # table's own CREATE, which has exactly the same race as any other
        # CREATE TABLE IF NOT EXISTS.
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (COLLAB_SCHEMA_LOCK_KEY,))
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {COLLAB_SCHEMA_VERSION_TABLE} (
                version     integer PRIMARY KEY,
                applied_at  timestamptz NOT NULL DEFAULT now(),
                checksum    text
            )
            """
        )
        # A registry created before checksums existed (issue #73) lacks the
        # column; add it in place rather than as a numbered migration, because
        # the registry table is the runner's own bookkeeping — it exists before
        # the version list is even consulted. Idempotent and under the lock,
        # like everything else here. Nullable on purpose: NULL means "applied
        # by a pre-checksum build", which the verifier accepts once and
        # backfills.
        conn.execute(f"ALTER TABLE {COLLAB_SCHEMA_VERSION_TABLE} ADD COLUMN IF NOT EXISTS checksum text")
        # Verify (and backfill) every already-applied version before applying
        # anything: an edited released migration must fail the startup, not
        # half-run whatever follows it.
        recorded = _verify_recorded_checksums(conn)
        applied = max(recorded, default=0)
        for version, statements in COLLAB_SCHEMA_MIGRATIONS:
            if version <= applied:
                continue
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                f"INSERT INTO {COLLAB_SCHEMA_VERSION_TABLE} (version, checksum) VALUES (%s, %s)",
                (version, collab_migration_checksum(statements)),
            )


def applied_collab_schema_version(db) -> int:
    """Return the highest ``collab_`` migration version this database has applied.

    ``0`` means nothing has been applied — including the case where the
    bookkeeping table does not exist yet. Intended for operational checks and
    tests; the runner reads the version itself, inside its lock.
    """

    import psycopg

    try:
        with db.connection() as conn:
            row = conn.execute(
                f"SELECT COALESCE(MAX(version), 0) AS version FROM {COLLAB_SCHEMA_VERSION_TABLE}"
            ).fetchone()
    except psycopg.errors.UndefinedTable:
        return 0
    return row["version"] if row else 0


class CollabSchemaVersionError(RuntimeError):
    """The database's ``collab_`` schema is older than this build requires."""


def check_collab_schema_version(db, *, auto_migrate: bool) -> int | None:
    """Verify at startup that the ``collab_`` schema is new enough to serve.

    Called once from ``make_app``, and only when something actually reads these
    tables (issue #96: the check has no meaning on a deployment that does not).
    Three cases, decided deliberately:

    * **Behind** (``applied < LATEST_COLLAB_SCHEMA_VERSION``) — raises. This is
      the case the check exists for: with ``auto_migrate`` off and the
      migration never run out of band, the pod would otherwise start cleanly
      and fail later with ``relation "collab_org_members" does not exist``, at
      request time, in whichever endpoint happened to touch it first. An
      operator who has to run a migration should be told that, at startup,
      instead of reading a traceback out of a request log. (With
      ``auto_migrate`` on this is unreachable in the ordinary case — the
      migration has just run — so reaching it means the migration silently did
      not apply, which is also worth refusing to serve.)

    * **Ahead** (``applied > LATEST_COLLAB_SCHEMA_VERSION``) — logs, and is
      **not** fatal. A newer replica migrating the shared database while older
      replicas are still running is what an ordinary rolling update looks like;
      making that fatal would turn every deploy into an outage of the old
      replicas and would block rollbacks entirely. Migrations here are
      append-only, so an older build's statements remain valid against a newer
      schema. This is a deliberate decision, not an omission.

    * **Unreachable** — logs, and is **not** fatal *here*. Nothing is asserted
      about the schema in this case: the check simply could not run, and the
      version is reported as ``None``.

      Read that narrowly. It does **not** mean a membership deployment always
      survives a startup outage. With ``auto_migrate`` on,
      :func:`run_collab_schema_migrations` has already run — and already raised
      — before this function is called, so the pod fails to start and retries.
      That is deliberate and predates this check: a pod that skipped its
      migration and started anyway would come up against tables that do not
      exist, and would keep serving errors after the database returned, because
      migrations only run at startup. Crash-looping until Postgres is reachable,
      then migrating, is the correct outcome for that deployment.

      What this branch actually buys is the ``auto_migrate`` **off** case, where
      the pod touches no schema at startup and a reachable-database requirement
      would be a new dependency rather than an existing one.

    Returns the applied version, or ``None`` when the database could not be
    reached.
    """

    try:
        from .db import postgres_error_classes

        database_errors = postgres_error_classes()
    except ImportError:  # pragma: no cover - psycopg is a hard dependency
        database_errors = (Exception,)

    try:
        applied = applied_collab_schema_version(db)
    except database_errors as exc:
        collab_logger.warning(
            "collab_schema_version_check_skipped",
            extra={"reason": type(exc).__name__, "required": LATEST_COLLAB_SCHEMA_VERSION},
        )
        return None

    if applied < LATEST_COLLAB_SCHEMA_VERSION:
        raise CollabSchemaVersionError(
            f"The collab_ schema in this database is at version {applied}, but this build "
            f"requires version {LATEST_COLLAB_SCHEMA_VERSION}. "
            + (
                "Auto-migration is enabled, so the migration ran and did not take effect — "
                "check the startup logs for the failure."
                if auto_migrate
                else "Auto-migration is disabled "
                "(COLLAB_HUB_API__FRAMES__POSTGRES__AUTO_MIGRATE / frames.postgres.autoMigrate): "
                "apply the outstanding collab_ migrations out of band and record them in "
                f"{COLLAB_SCHEMA_VERSION_TABLE}, or enable auto-migration."
            )
        )

    if applied > LATEST_COLLAB_SCHEMA_VERSION:
        # Normal and transient mid-rollout; see the docstring.
        collab_logger.warning(
            "collab_schema_version_ahead",
            extra={"applied": applied, "required": LATEST_COLLAB_SCHEMA_VERSION},
        )
    return applied
