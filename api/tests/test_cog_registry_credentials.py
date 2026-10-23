"""Registry credentials and pull tokens (issue #179): the store contract, on every backend.

- the in-memory store, always;
- the Postgres store against a fake connection, always: each method issues
  the statement it claims with the parameters it claims, and maps rows back;
- the Postgres store against a live database, opt-in with
  ``COLLAB_HUB_TEST_POSTGRES_URL``, where the same contract is proven on the
  real tables -- expiry on the server's clock, the cascade that makes
  revocation immediate, and a token that cannot outlive its credential.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest

from collab_hub_api.cogs.registry_credentials import (
    CREDENTIAL_ID_PREFIX,
    CREDENTIAL_ISSUE_LOCK_CLASS,
    CREDENTIAL_SECRET_PREFIX,
    MAX_CREDENTIALS_PER_USER,
    PULL_TOKEN_PREFIX,
    InMemoryRegistryCredentialStore,
    PostgresRegistryCredentialStore,
    RegistryCredential,
    RegistryCredentialsUnavailableError,
    TokenGrant,
    UnavailableRegistryCredentialStore,
    new_credential_id,
    new_credential_secret,
    new_pull_token,
    secret_digest,
)
from collab_hub_api.config import Config, build_cog_registry_credential_store
from collab_hub_api.frames.collab_schema import run_collab_schema_migrations
from collab_hub_api.frames.db import PostgresPools

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def test_generated_values_are_distinct_prefixed_and_stored_as_digests():
    ids = {new_credential_id() for _ in range(50)}
    secrets_ = {new_credential_secret() for _ in range(50)}
    tokens = {new_pull_token() for _ in range(50)}
    assert len(ids) == len(secrets_) == len(tokens) == 50
    assert all(value.startswith(CREDENTIAL_ID_PREFIX) for value in ids)
    assert all(value.startswith(CREDENTIAL_SECRET_PREFIX) and len(value) > 40 for value in secrets_)
    assert all(value.startswith(PULL_TOKEN_PREFIX) and len(value) > 40 for value in tokens)
    secret = next(iter(secrets_))
    assert len(secret_digest(secret)) == 64 and secret not in secret_digest(secret)
    assert secret_digest(secret) == secret_digest(secret) != secret_digest(secret + "x")


def test_token_grant_allows_only_its_repositories():
    grant = TokenGrant(user_id="u", repositories=("cogs/a",), expires_at=T0, issued_at=T0)
    assert grant.allows_pull("cogs/a") and not grant.allows_pull("cogs/b")


def test_the_unavailable_store_refuses_every_call():
    store = UnavailableRegistryCredentialStore()
    calls = (
        lambda: store.create_credential(
            credential_id="c", user_id="u", secret_hash="h", scope="pull", session_id=None, ttl_seconds=1
        ),
        lambda: store.find_credential("c", "h"),
        lambda: store.revoke_credential("c", "u"),
        lambda: store.revoke_all("u"),
        lambda: store.create_token(token_hash="t", user_id="u", credential_id=None, repositories=(), ttl_seconds=1),
        lambda: store.find_token("t"),
    )
    for call in calls:
        with pytest.raises(RegistryCredentialsUnavailableError, match="not configured"):
            call()


def test_the_store_follows_the_catalog_backend(tmp_path):
    def build(cogs: dict, postgres_url: str = ""):
        config = Config.parse({"cogs": cogs, "frames": {"postgres": {"url": postgres_url}}})
        return build_cog_registry_credential_store(config, PostgresPools())

    assert isinstance(build({"catalog": {"backend": "memory"}}), InMemoryRegistryCredentialStore)
    assert isinstance(build({}), UnavailableRegistryCredentialStore)
    assert isinstance(build({}, "postgresql://db.example/collab"), PostgresRegistryCredentialStore)


# ---------------------------------------------------------------------------
# The contract, run against the in-memory store and (opt in) live Postgres.
# ---------------------------------------------------------------------------

POSTGRES_URL = os.environ.get("COLLAB_HUB_TEST_POSTGRES_URL", "")


class _Clock:
    """Moves "now" for the in-memory store; the live store's rows are moved instead."""

    def __init__(self, store, database=None) -> None:
        self.store = store
        self.database = database
        self.now = datetime.now(UTC)
        if database is None:
            store.clock = lambda: self.now

    def advance(self, seconds: float) -> None:
        if self.database is None:
            self.now += timedelta(seconds=seconds)
            return
        with self.database.connection() as conn:
            for table in ("collab_cog_registry_credentials", "collab_cog_registry_tokens"):
                conn.execute(
                    f"UPDATE {table} SET created_at = created_at - make_interval(secs => %s),"
                    " expires_at = expires_at - make_interval(secs => %s)",
                    (seconds, seconds),
                )


@pytest.fixture(params=["memory", "postgres"])
def backend(request):
    if request.param == "memory":
        store = InMemoryRegistryCredentialStore()
        yield store, _Clock(store)
        return
    if not POSTGRES_URL:
        pytest.skip("set COLLAB_HUB_TEST_POSTGRES_URL to a disposable database to run the live-Postgres tests")
    from test_collab_schema import COLLAB_TABLES

    from collab_hub_api.frames.db import PostgresDatabase

    database = PostgresDatabase(POSTGRES_URL, min_size=0, max_size=4, timeout_seconds=10.0)

    def drop_all() -> None:
        with database.connection() as conn:
            for table in COLLAB_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

    try:
        drop_all()
        run_collab_schema_migrations(database)
        store = PostgresRegistryCredentialStore(database)
        yield store, _Clock(store, database)
    finally:
        drop_all()
        database.close()


def _credential(store, user="alice", ttl=900, credential_id=None, secret="s3cret", session_id="sid-1"):
    return store.create_credential(
        credential_id=credential_id or new_credential_id(),
        user_id=user,
        secret_hash=secret_digest(secret),
        scope="pull",
        session_id=session_id,
        ttl_seconds=ttl,
    )


def _token(store, credential, *, user=None, repositories=("cogs/a",), ttl=300, token=None):
    token = token or new_pull_token()
    grant = store.create_token(
        token_hash=secret_digest(token),
        user_id=user or (credential.user_id if credential else "alice"),
        credential_id=credential.id if credential else None,
        repositories=repositories,
        ttl_seconds=ttl,
    )
    return token, grant


def test_a_credential_is_found_only_by_its_id_and_secret(backend):
    store, _clock = backend
    created = _credential(store)
    assert isinstance(created, RegistryCredential)
    assert (created.user_id, created.scope, created.session_id) == ("alice", "pull", "sid-1")
    assert timedelta(seconds=899) <= created.expires_at - created.created_at <= timedelta(seconds=901)

    found = store.find_credential(created.id, secret_digest("s3cret"))
    assert found == created
    assert store.find_credential(created.id, secret_digest("wrong")) is None
    assert store.find_credential("crc-nobody", secret_digest("s3cret")) is None


def test_a_credential_expires(backend):
    store, clock = backend
    created = _credential(store, ttl=60)
    clock.advance(59)
    assert store.find_credential(created.id, secret_digest("s3cret")) is not None
    clock.advance(2)
    assert store.find_credential(created.id, secret_digest("s3cret")) is None
    assert store.revoke_credential(created.id, "alice") is False, "an expired credential is already gone"
    token, grant = _token(store, created)
    assert grant is None and store.find_token(secret_digest(token)) is None


def test_revoking_is_owner_only_and_takes_the_tokens_with_it(backend):
    store, _clock = backend
    mine, theirs = _credential(store), _credential(store, user="bob")
    token, grant = _token(store, mine)
    their_token, _ = _token(store, theirs)
    assert grant.credential_id == mine.id and grant.repositories == ("cogs/a",)
    assert store.find_token(secret_digest(token)) == grant

    assert store.revoke_credential(mine.id, "bob") is False
    assert store.find_token(secret_digest(token)) is not None
    assert store.revoke_credential(mine.id, "alice") is True
    assert store.find_token(secret_digest(token)) is None
    assert store.find_credential(mine.id, secret_digest("s3cret")) is None
    assert store.revoke_credential(mine.id, "alice") is False
    assert store.find_token(secret_digest(their_token)) is not None


def test_revoke_all_covers_credentials_and_directly_minted_tokens(backend):
    store, _clock = backend
    first, second, theirs = _credential(store), _credential(store), _credential(store, user="bob")
    tokens = [_token(store, first)[0], _token(store, second)[0], _token(store, None, user="alice")[0]]
    their_token, _ = _token(store, theirs)
    their_direct, _ = _token(store, None, user="bob")

    assert store.revoke_all("alice") == 2
    assert [store.find_token(secret_digest(token)) for token in tokens] == [None, None, None]
    assert store.find_token(secret_digest(their_token)) is not None
    assert store.find_token(secret_digest(their_direct)) is not None
    assert store.revoke_all("alice") == 0


def test_a_token_never_outlives_its_credential(backend):
    store, clock = backend
    credential = _credential(store, ttl=120)
    clock.advance(60)
    token, grant = _token(store, credential, ttl=300)
    assert timedelta(seconds=58) <= grant.expires_at - grant.issued_at <= timedelta(seconds=61)
    assert grant.expires_at == store.find_credential(credential.id, secret_digest("s3cret")).expires_at
    clock.advance(61)
    assert store.find_token(secret_digest(token)) is None


def test_a_token_expires_on_its_own_and_a_direct_one_has_no_credential(backend):
    store, clock = backend
    credential = _credential(store, ttl=900)
    token, grant = _token(store, credential, ttl=60, repositories=("cogs/a", "cogs/b"))
    direct, direct_grant = _token(store, None, user="alice", ttl=60, repositories=())
    assert grant.repositories == ("cogs/a", "cogs/b")
    assert direct_grant.credential_id is None and direct_grant.repositories == ()
    assert timedelta(seconds=59) <= grant.expires_at - grant.issued_at <= timedelta(seconds=61)
    clock.advance(61)
    assert store.find_token(secret_digest(token)) is None
    assert store.find_token(secret_digest(direct)) is None
    assert store.find_token(secret_digest("never-issued")) is None
    # A token cannot be minted against somebody else's credential.
    _other, refused = _token(store, credential, user="bob")
    assert refused is None


def test_push_is_granted_only_to_tokens_of_a_publish_credential(backend):
    store, _clock = backend
    pull = _credential(store)
    publish = store.create_credential(
        credential_id=new_credential_id(),
        user_id="alice",
        secret_hash=secret_digest("s3cret"),
        scope="publish",
        session_id=None,
        ttl_seconds=900,
        org_id="org-a",
    )
    assert publish.org_id == "org-a" and pull.org_id is None
    assert store.find_credential(publish.id, secret_digest("s3cret")).org_id == "org-a"

    def mint(credential, push, repositories=("cogs/a",)):
        token = new_pull_token()
        grant = store.create_token(
            token_hash=secret_digest(token),
            user_id="alice",
            credential_id=credential.id if credential else None,
            repositories=repositories,
            ttl_seconds=300,
            push_repositories=push,
        )
        assert store.find_token(secret_digest(token)) == grant
        return grant

    asked = ("cogs/a",)
    assert mint(publish, asked).allows_push("cogs/a") and mint(publish, asked).org_id == "org-a"
    assert not mint(publish, ()).allows_push("cogs/a"), "not asked for, not given"
    assert not mint(pull, asked).allows_push("cogs/a"), "a pull credential never pushes"
    assert not mint(None, asked).allows_push("cogs/a"), "nor does a token minted from a Hub session"
    assert mint(pull, asked).allows_pull("cogs/a") and mint(None, asked).org_id is None


def test_push_is_stored_and_checked_per_repository(backend):
    """Push on one repository is never push on another the same token only pulls from."""

    store, _clock = backend
    publish = store.create_credential(
        credential_id=new_credential_id(),
        user_id="alice",
        secret_hash=secret_digest("s3cret"),
        scope="publish",
        session_id=None,
        ttl_seconds=900,
        org_id="org-a",
    )
    token = new_pull_token()
    store.create_token(
        token_hash=secret_digest(token),
        user_id="alice",
        credential_id=publish.id,
        repositories=("cogs/a", "cogs/b"),
        ttl_seconds=300,
        push_repositories=("cogs/a", "cogs/push-only"),
    )
    grant = store.find_token(secret_digest(token))
    assert grant.repositories == ("cogs/a", "cogs/b") and grant.push_repositories == ("cogs/a", "cogs/push-only")
    assert grant.allows_push("cogs/a") and grant.allows_pull("cogs/b")
    assert not grant.allows_push("cogs/b"), "asked for pull only: push on cogs/a grants nothing here"
    assert grant.allows_push("cogs/push-only") and not grant.allows_pull("cogs/push-only")
    assert grant.names("cogs/push-only") and grant.names("cogs/b") and not grant.names("cogs/c")


def test_exchanging_past_the_cap_drops_the_oldest(backend):
    store, clock = backend
    created = []
    for _ in range(MAX_CREDENTIALS_PER_USER + 2):
        created.append(_credential(store))
        clock.advance(1)
    other = _credential(store, user="bob")
    alive = [c for c in created if store.find_credential(c.id, secret_digest("s3cret")) is not None]
    assert alive == created[2:]
    assert store.find_credential(other.id, secret_digest("s3cret")) is not None


def test_expired_rows_are_swept(backend):
    store, clock = backend
    old = _credential(store, ttl=60)
    old_token, _ = _token(store, old, ttl=30)
    direct, _ = _token(store, None, user="alice", ttl=30)
    clock.advance(120)
    if isinstance(store, InMemoryRegistryCredentialStore):
        # A read is enough to sweep: a Hub that has gone quiet does not keep expired rows.
        assert store.find_token(secret_digest(old_token)) is None
        assert store._credentials == {} and store._tokens == {}
    _credential(store, user="bob")
    _token(store, None, user="bob")
    if isinstance(store, InMemoryRegistryCredentialStore):
        assert old.id not in store._credentials
        assert secret_digest(old_token) not in store._tokens and secret_digest(direct) not in store._tokens
        return

    def counts() -> tuple[int, int]:
        with store._db.connection() as conn:
            credentials = conn.execute("SELECT count(*) AS n FROM collab_cog_registry_credentials").fetchone()["n"]
            tokens = conn.execute("SELECT count(*) AS n FROM collab_cog_registry_tokens").fetchone()["n"]
        return credentials, tokens

    # Another user's writes delete nothing of alice's: no request sweeps the tables.
    assert counts() == (2, 3)
    assert store.find_credential(old.id, secret_digest("s3cret")) is None, "expired is not live, swept or not"
    assert store.find_token(secret_digest(direct)) is None
    # The sweep is what removes them, on its own connection, once it is due.
    store._last_sweep = float("-inf")
    store.find_token(secret_digest(direct))
    store._sweep_thread.join(timeout=30)
    assert counts() == (1, 1)


def test_live_an_exchange_drops_only_the_callers_own_expired_credentials(backend):
    store, clock = backend
    if isinstance(store, InMemoryRegistryCredentialStore):
        pytest.skip("the in-memory store purges everything under its one lock")
    mine = _credential(store, user="alice", ttl=60)
    theirs = _credential(store, user="bob", ttl=60)
    clock.advance(120)
    _credential(store, user="alice")
    with store._db.connection() as conn:
        left = {row["id"] for row in conn.execute("SELECT id FROM collab_cog_registry_credentials").fetchall()}
    assert mine.id not in left and theirs.id in left


def test_live_a_sweep_does_not_wait_for_a_row_another_transaction_holds(backend, monkeypatch):
    """A credential locked elsewhere (another replica's sweep) is skipped, not queued behind."""

    from collab_hub_api.cogs import registry_credentials

    store, clock = backend
    if isinstance(store, InMemoryRegistryCredentialStore):
        pytest.skip("row locks are Postgres's")
    monkeypatch.setattr(registry_credentials, "SWEEP_TIMEOUT_SECONDS", 2.0)
    held = _credential(store, user="alice", ttl=60)
    free = _credential(store, user="bob", ttl=60)
    clock.advance(120)
    with store._db.connection() as holder:
        holder.execute("SELECT 1 FROM collab_cog_registry_credentials WHERE id = %s FOR UPDATE", (held.id,))
        started = time.monotonic()
        store._sweep()
        assert time.monotonic() - started < 1.5, "the sweep waited on the held row"
        with store._db.connection() as conn:
            left = {row["id"] for row in conn.execute("SELECT id FROM collab_cog_registry_credentials").fetchall()}
        assert left == {held.id}, f"the free row was not swept, or the held one was: {left} (free={free.id})"


def test_live_concurrent_exchanges_never_leave_a_user_over_the_cap():
    """Many exchanges at once for one user: the cap holds, and another user is not held up or pruned."""

    if not POSTGRES_URL:
        pytest.skip("set COLLAB_HUB_TEST_POSTGRES_URL to a disposable database to run the live-Postgres tests")
    from concurrent.futures import ThreadPoolExecutor

    from test_collab_schema import COLLAB_TABLES

    from collab_hub_api.frames.db import PostgresDatabase

    database = PostgresDatabase(POSTGRES_URL, min_size=0, max_size=12, timeout_seconds=30.0)

    def drop_all() -> None:
        with database.connection() as conn:
            for table in COLLAB_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

    try:
        drop_all()
        run_collab_schema_migrations(database)
        store = PostgresRegistryCredentialStore(database)
        for _ in range(MAX_CREDENTIALS_PER_USER - 2):
            _credential(store)
        _credential(store, user="bob")

        def exchange(index: int) -> str:
            return _credential(store, user="alice" if index % 5 else "carol").id

        with ThreadPoolExecutor(max_workers=12) as pool:
            issued = list(pool.map(exchange, range(60)))
        assert len(set(issued)) == 60
        with database.connection() as conn:
            counts = {
                row["user_id"]: row["n"]
                for row in conn.execute(
                    "SELECT user_id, count(*) AS n FROM collab_cog_registry_credentials GROUP BY user_id"
                ).fetchall()
            }
        assert counts == {"alice": MAX_CREDENTIALS_PER_USER, "bob": 1, "carol": 12}
    finally:
        drop_all()
        database.close()


# ---------------------------------------------------------------------------
# The Postgres store against a fake connection.
# ---------------------------------------------------------------------------


class _FakeResult:
    rowcount = 0

    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConnection:
    """Records every statement; answers a statement whose text contains a scripted marker."""

    def __init__(self, answers: dict[str, list] | None = None):
        self.calls: list[tuple[str, tuple]] = []
        self.budgets: list[int] = []
        self.answers = answers or {}

    def execute(self, sql, params=None):
        text = " ".join(sql.split())
        if text.startswith("SELECT set_config('statement_timeout'"):
            # Every call's preamble (the request budget); asserted once, below.
            self.budgets.append(int(params[0]))
            return _FakeResult([])
        self.calls.append((text, tuple(params or ())))
        for marker, rows in self.answers.items():
            if marker in text:
                return _FakeResult(rows)
        return _FakeResult([])


class _FakeDb:
    def __init__(self, conn):
        self.conn = conn
        self.acquire_timeouts: list[float | None] = []

    @contextmanager
    def connection(self, timeout=None):
        self.acquire_timeouts.append(timeout)
        yield self.conn


def _fake(answers=None):
    conn = _FakeConnection(answers)
    return PostgresRegistryCredentialStore(_FakeDb(conn)), conn


CREDENTIAL_ROW = {
    "id": "crc-1",
    "user_id": "alice",
    "scope": "pull",
    "session_id": "sid-1",
    "org_id": "org-a",
    "created_at": T0,
    "expires_at": T0 + timedelta(minutes=15),
}
TOKEN_ROW = {
    "user_id": "alice",
    "credential_id": "crc-1",
    "repositories": ["cogs/a"],
    "push_repositories": None,
    "org_id": "org-a",
    "created_at": T0,
    "expires_at": T0 + timedelta(minutes=5),
}


def test_postgres_create_credential_sweeps_inserts_and_caps():
    store, conn = _fake({"INSERT INTO collab_cog_registry_credentials": [CREDENTIAL_ROW]})
    created = store.create_credential(
        credential_id="crc-1",
        user_id="alice",
        secret_hash="hash",
        scope="pull",
        session_id="sid-1",
        ttl_seconds=900,
        org_id="org-a",
    )
    assert created == RegistryCredential(
        id="crc-1",
        user_id="alice",
        scope="pull",
        session_id="sid-1",
        org_id="org-a",
        created_at=T0,
        expires_at=T0 + timedelta(minutes=15),
    )
    lock, own_expired, insert, cap = conn.calls
    # First, before anything is read or written: one user's issuance and pruning are serialized.
    assert lock == ("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (CREDENTIAL_ISSUE_LOCK_CLASS, "alice"))
    # Only the caller's own expired rows: nothing table-wide rides an exchange.
    assert own_expired == (
        "DELETE FROM collab_cog_registry_credentials WHERE user_id = %s AND expires_at <= now()",
        ("alice",),
    )
    assert "now() + make_interval(secs => %s)" in insert[0]
    assert insert[1] == ("crc-1", "alice", "hash", "pull", "sid-1", "org-a", 900)
    assert "ORDER BY created_at DESC, id DESC LIMIT %s" in cap[0]
    assert cap[1] == ("alice", "alice", MAX_CREDENTIALS_PER_USER)


def test_postgres_find_credential_matches_id_digest_and_liveness():
    store, conn = _fake({"FROM collab_cog_registry_credentials": [CREDENTIAL_ROW]})
    assert store.find_credential("crc-1", "hash").id == "crc-1"
    ((sql, params),) = conn.calls
    assert "WHERE id = %s AND secret_hash = %s AND expires_at > now()" in sql
    assert params == ("crc-1", "hash")
    empty, _ = _fake()
    assert empty.find_credential("crc-1", "hash") is None


def test_postgres_revocation_deletes_rows():
    store, conn = _fake({"DELETE FROM collab_cog_registry_credentials WHERE id": [{"id": "crc-1"}]})
    assert store.revoke_credential("crc-1", "alice") is True
    ((sql, params),) = conn.calls
    assert sql.endswith("WHERE id = %s AND user_id = %s AND expires_at > now() RETURNING id")
    assert params == ("crc-1", "alice")
    missing, _ = _fake()
    assert missing.revoke_credential("crc-1", "alice") is False

    store, conn = _fake({"DELETE FROM collab_cog_registry_credentials WHERE user_id": [{"id": "a"}, {"id": "b"}]})
    assert store.revoke_all("alice") == 2
    credentials, tokens = conn.calls
    assert credentials == ("DELETE FROM collab_cog_registry_credentials WHERE user_id = %s RETURNING id", ("alice",))
    assert tokens == ("DELETE FROM collab_cog_registry_tokens WHERE user_id = %s", ("alice",))


def test_postgres_create_token_is_one_statement_with_the_liveness_check():
    store, conn = _fake({"INSERT INTO collab_cog_registry_tokens": [TOKEN_ROW]})
    grant = store.create_token(
        token_hash="thash", user_id="alice", credential_id="crc-1", repositories=("cogs/a",), ttl_seconds=300
    )
    assert grant == TokenGrant(
        user_id="alice",
        credential_id="crc-1",
        repositories=("cogs/a",),
        issued_at=T0,
        expires_at=T0 + timedelta(minutes=5),
        org_id="org-a",
    )
    assert not grant.allows_push("cogs/a")
    (insert,) = conn.calls  # the mint has no housekeeping delete
    assert "LEAST(now() + make_interval(secs => %s), c.expires_at)" in insert[0]
    assert "WHERE c.id = %s AND c.user_id = %s AND c.expires_at > now()" in insert[0]
    assert insert[1] == ("thash", ["cogs/a"], [], 300, "crc-1", "alice")
    # Push is decided where the credential is read: the repositories asked for, and only for a publish credential.
    assert "CASE WHEN c.scope = 'publish' THEN %s::text[] ELSE '{}'::text[] END" in insert[0]
    pushing, conn = _fake(
        {"INSERT INTO collab_cog_registry_tokens": [{**TOKEN_ROW, "repositories": ["cogs/a", "cogs/b"],
                                                     "push_repositories": ["cogs/a"]}]}
    )
    granted = pushing.create_token(
        token_hash="t",
        user_id="alice",
        credential_id="crc-1",
        repositories=("cogs/a", "cogs/b"),
        ttl_seconds=300,
        push_repositories=iter(("cogs/a",)),
    )
    assert conn.calls[-1][1][1:3] == (["cogs/a", "cogs/b"], ["cogs/a"])
    assert granted.allows_push("cogs/a") and granted.allows_pull("cogs/b") and not granted.allows_push("cogs/b")
    # The credential went away between the caller's check and the insert.
    gone, _ = _fake()
    assert (
        gone.create_token(token_hash="t", user_id="alice", credential_id="crc-1", repositories=(), ttl_seconds=1)
        is None
    )


def test_postgres_create_token_without_a_credential():
    row = {**TOKEN_ROW, "credential_id": None, "repositories": None, "org_id": None}
    store, conn = _fake({"INSERT INTO collab_cog_registry_tokens": [row]})
    grant = store.create_token(
        token_hash="thash",
        user_id="alice",
        credential_id=None,
        repositories=iter(()),
        ttl_seconds=300,
        push_repositories=("cogs/a",),
    )
    assert grant.credential_id is None and grant.repositories == () and grant.org_id is None
    assert grant.push_repositories == ()
    (insert,) = conn.calls
    # A token minted straight from a Hub session is pull-only.
    assert "(token_hash, user_id, credential_id, repositories, expires_at)" in insert[0]
    assert "VALUES (%s, %s, NULL, %s, now() + make_interval(secs => %s))" in insert[0]
    assert insert[1] == ("thash", "alice", [], 300)


class _SweepDb:
    """Hands the request's connection to the caller's thread and a separate one to any other thread."""

    def __init__(self, request_conn, sweep_conn):
        self.request_conn = request_conn
        self.sweep_conn = sweep_conn
        self.caller = threading.get_ident()
        self.sweep_timeouts: list[float | None] = []

    @contextmanager
    def connection(self, timeout=None):
        if threading.get_ident() == self.caller:
            yield self.request_conn
            return
        self.sweep_timeouts.append(timeout)
        yield self.sweep_conn


def _sweeping(sweep_conn=None):
    request_conn, sweep_conn = _FakeConnection(), sweep_conn or _FakeConnection()
    database = _SweepDb(request_conn, sweep_conn)
    return PostgresRegistryCredentialStore(database), database


def test_postgres_sweeps_at_most_once_per_interval_and_never_on_the_requests_connection(monkeypatch):
    from collab_hub_api.cogs import registry_credentials

    clock = [1000.0]
    monkeypatch.setattr(registry_credentials.time, "monotonic", lambda: clock[0])
    store, database = _sweeping()

    def call_everything() -> None:
        store.find_token("thash")
        store.create_token(token_hash="t", user_id="alice", credential_id=None, repositories=(), ttl_seconds=1)
        if store._sweep_thread is not None:
            store._sweep_thread.join(timeout=10)

    call_everything()
    assert database.sweep_conn.calls == [], "nothing to sweep yet: the store was only just built"
    clock[0] += registry_credentials.SWEEP_INTERVAL_SECONDS + 1
    call_everything()
    swept = database.sweep_conn.calls
    assert [params for _sql, params in swept] == [(registry_credentials.SWEEP_BATCH_ROWS,)] * 2
    assert "DELETE FROM collab_cog_registry_credentials WHERE id IN" in swept[0][0]
    assert "DELETE FROM collab_cog_registry_tokens WHERE token_hash IN" in swept[1][0]
    for sql, _params in swept:
        assert "WHERE expires_at <= now() ORDER BY expires_at LIMIT %s FOR UPDATE SKIP LOCKED" in sql
    # Its own checkout and its own statement timeout, neither of them a request's.
    assert database.sweep_timeouts == [registry_credentials.SWEEP_TIMEOUT_SECONDS]
    assert database.sweep_conn.budgets == [int(registry_credentials.SWEEP_TIMEOUT_SECONDS * 1000)]
    call_everything()
    assert len(database.sweep_conn.calls) == 2, "and not again until the interval has passed"
    # The request's own connection never ran a delete.
    assert not [sql for sql, _ in database.request_conn.calls if sql.startswith("DELETE")]


def test_postgres_a_failing_sweep_costs_the_request_nothing(monkeypatch, caplog):
    from collab_hub_api.cogs import registry_credentials

    class _Broken(_FakeConnection):
        def execute(self, sql, params=None):
            raise RuntimeError("password=hunter2 host=db.internal")

    clock = [1000.0]
    monkeypatch.setattr(registry_credentials.time, "monotonic", lambda: clock[0])
    store, database = _sweeping(_Broken())
    database.request_conn.answers = {"FROM collab_cog_registry_tokens t": [TOKEN_ROW]}
    clock[0] += registry_credentials.SWEEP_INTERVAL_SECONDS + 1
    with caplog.at_level("WARNING", logger="frames_server.cogs.registry_credentials"):
        assert store.find_token("thash") is not None, "the read answered as if no sweep existed"
        store._sweep_thread.join(timeout=10)
    (record,) = [r for r in caplog.records if r.message == "cog_registry_credential_sweep_failed"]
    assert record.error == "RuntimeError"
    assert "hunter2" not in caplog.text and "db.internal" not in caplog.text


def test_postgres_a_full_batch_makes_the_next_call_sweep_again(monkeypatch):
    from collab_hub_api.cogs import registry_credentials

    class _Full(_FakeConnection):
        def execute(self, sql, params=None):
            result = super().execute(sql, params)
            result.rowcount = registry_credentials.SWEEP_BATCH_ROWS if "DELETE" in sql else 0
            return result

    clock = [1000.0]
    monkeypatch.setattr(registry_credentials.time, "monotonic", lambda: clock[0])
    store, database = _sweeping(_Full())
    clock[0] += registry_credentials.SWEEP_INTERVAL_SECONDS + 1
    for expected in (2, 4):
        store.find_token("thash")
        store._sweep_thread.join(timeout=10)
        assert len(database.sweep_conn.calls) == expected


def test_postgres_calls_are_bounded_by_the_request_budget():
    from collab_hub_api.cogs import deadline

    store, conn = _fake()
    calls = (
        lambda: store.find_token("t"),
        lambda: store.find_credential("c", "h"),
        lambda: store.revoke_credential("c", "u"),
        lambda: store.revoke_all("u"),
        lambda: store.create_token(token_hash="t", user_id="u", credential_id=None, repositories=(), ttl_seconds=1),
    )
    for call in calls:
        call()
    # Outside a request: the default budget, for the pool wait and before every single statement.
    default_ms = int(deadline.DEFAULT_BUDGET_SECONDS * 1000)
    assert len(conn.budgets) == len(conn.calls) and all(default_ms - 1000 < ms <= default_ms for ms in conn.budgets)
    assert store._db.acquire_timeouts == [deadline.DEFAULT_BUDGET_SECONDS] * len(calls)

    token = deadline.request_deadline.set(deadline.time.monotonic() + 0.5)
    try:
        store.find_token("t")
        assert 1 <= conn.budgets[-1] <= 500 and 0 < store._db.acquire_timeouts[-1] <= 0.5
        deadline.request_deadline.set(deadline.time.monotonic() - 1)
        before = len(store._db.acquire_timeouts)
        with pytest.raises(deadline.BudgetExhausted):
            store.find_token("t")
        assert len(store._db.acquire_timeouts) == before, "a spent budget does not even take a connection"
    finally:
        deadline.request_deadline.reset(token)


def test_the_budget_is_spent_once_across_the_pool_wait_and_every_statement(monkeypatch):
    """Waiting for a connection and running earlier statements both use up what later statements get."""

    from collab_hub_api.cogs import deadline

    clock = [100.0]
    monkeypatch.setattr(deadline.time, "monotonic", lambda: clock[0])
    statements: list[tuple[str, tuple]] = []

    class Connection:
        def execute(self, sql, params=None):
            statements.append((sql, tuple(params or ())))
            if not sql.startswith("SELECT set_config"):
                clock[0] += 4.0  # each statement takes four seconds
            return self

    class Database:
        @contextmanager
        def connection(self, timeout=None):
            assert timeout == 30.0
            clock[0] += 29.0  # the pool made us wait for nearly all of it
            yield Connection()

    token = deadline.request_deadline.set(clock[0] + 30.0)
    try:
        with pytest.raises(deadline.BudgetExhausted):
            with deadline.bounded_connection(Database()) as conn:
                conn.execute("SELECT 1")
                conn.execute("SELECT 2")
    finally:
        deadline.request_deadline.reset(token)
    # One second was left after the checkout, not a fresh thirty; the second statement was never sent.
    assert statements == [
        ("SELECT set_config('statement_timeout', %s, true)", ("1000",)),
        ("SELECT 1", ()),
    ]


def test_request_connection_is_the_ordinary_checkout_outside_a_request():
    from collab_hub_api.cogs import deadline

    conn = _FakeConnection()
    db = _FakeDb(conn)
    with deadline.request_connection(db) as plain:
        plain.execute("SELECT 1")
    assert plain is conn and conn.budgets == [] and db.acquire_timeouts == [None]

    token = deadline.request_deadline.set(deadline.time.monotonic() + 0.5)
    try:
        with deadline.request_connection(db) as bounded:
            bounded.execute("SELECT 1")
    finally:
        deadline.request_deadline.reset(token)
    assert isinstance(bounded, deadline.BudgetedConnection)
    assert 0 < db.acquire_timeouts[-1] <= 0.5 and 1 <= conn.budgets[-1] <= 500


def test_postgres_find_token_requires_a_live_credential_when_it_has_one():
    store, conn = _fake({"FROM collab_cog_registry_tokens t": [TOKEN_ROW]})
    assert store.find_token("thash").repositories == ("cogs/a",)
    ((sql, params),) = conn.calls
    assert "LEFT JOIN collab_cog_registry_credentials c ON c.id = t.credential_id" in sql
    assert "t.expires_at > now() AND (t.credential_id IS NULL OR c.expires_at > now())" in sql
    assert params == ("thash",)
    empty, _ = _fake()
    assert empty.find_token("thash") is None
