"""Registry credentials and pull tokens for the Hub's own ``/v2/`` surface (issue #179).

A client never presents its Hub access token to the registry surface, and
never stores it as a registry login. It exchanges its Hub session for a
**registry credential** -- a username and a secret valid only at the Hub's
registry token endpoint -- and that endpoint turns the credential into a
short-lived, repository-scoped **pull token** each time a client pulls. Both
are opaque random strings; the Hub keeps only their SHA-256 digests, so
nothing in the database (or a backup of it) can be replayed, and there is no
signing key to configure, rotate or leak.

What the two are worth:

- A credential is bound to the user who exchanged it and expires
  (``cogs.serve.credential_ttl_seconds``). Revoking it deletes the row, and
  the tokens minted from it go with it, so revocation takes effect on the
  very next request.
- A token names the repositories it may pull and nothing else, and never
  outlives the credential it came from: its expiry is the earlier of
  ``cogs.serve.token_ttl_seconds`` from now and the credential's own expiry.
- Neither is a Hub credential. They are not JWTs and no Hub API verifier
  accepts them; the only code that reads these tables is the ``/v2/`` router
  and the exchange routes under ``/v1/cogs/registry-credentials``.

Three backends, following the catalog's pattern
(:mod:`.catalog`): Postgres over the shared pool (tables from migration 13
of :mod:`..frames.collab_schema`, never created here), in-memory for tests
and single-process development, and one that refuses when no database is
configured.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .deadline import bounded_connection

logger = logging.getLogger("frames_server.cogs.registry_credentials")

SCOPE_PULL = "pull"
CREDENTIAL_SCOPES = frozenset({SCOPE_PULL})
"""The scopes a credential may be exchanged for. The publish half (#180) adds its own."""

CREDENTIAL_ID_PREFIX = "crc-"
CREDENTIAL_SECRET_PREFIX = "chrs_"
PULL_TOKEN_PREFIX = "chrt_"
"""Greppable prefixes, so a leaked value can be recognised (and scanned for) as what it is."""

CREDENTIAL_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"
"""Every issued id is one URL-safe path segment of this shape, and clients rely on it:
the id goes into ``DELETE /v1/cogs/registry-credentials/{id}`` unescaped."""

SWEEP_INTERVAL_SECONDS = 300.0
"""How often a process sweeps expired rows; see ``PostgresRegistryCredentialStore._sweep_if_due``."""

SWEEP_BATCH_ROWS = 5000
"""Most rows one sweep deletes from each table; a full batch makes the next call sweep again."""

SWEEP_TIMEOUT_SECONDS = 5.0
"""What a sweep may spend waiting for a connection, and again on its statements."""

_SWEEP_STATEMENTS = (
    # Oldest first, in bounded batches, and never waiting on a row another
    # transaction holds (another replica's sweep, a mint checking this
    # credential): what is skipped is taken by the next sweep. Deleting a
    # credential cascades to its tokens.
    """
    DELETE FROM collab_cog_registry_credentials WHERE id IN (
        SELECT id FROM collab_cog_registry_credentials
        WHERE expires_at <= now() ORDER BY expires_at LIMIT %s FOR UPDATE SKIP LOCKED
    )
    """,
    """
    DELETE FROM collab_cog_registry_tokens WHERE token_hash IN (
        SELECT token_hash FROM collab_cog_registry_tokens
        WHERE expires_at <= now() ORDER BY expires_at LIMIT %s FOR UPDATE SKIP LOCKED
    )
    """,
)

CREDENTIAL_ISSUE_LOCK_CLASS = int.from_bytes(b"crc1", "big")
"""First key of the two-key advisory lock that serializes one user's credential issuance.

The second key is ``hashtext(user_id)``. The two-``int4`` form keeps these
locks in a key space of their own, apart from the single-``bigint`` keys the
schema migration and the index sweep take.
"""

MAX_CREDENTIALS_PER_USER = 20
"""Live credentials one user may hold; exchanging past it drops the oldest.

A client exchanges a fresh credential before each install and revokes at
sign-out, so a handful is normal. The cap bounds what a looping client (or a
hostile one) can make the table hold until the rows expire.
"""


class RegistryCredentialsUnavailableError(RuntimeError):
    """Raised when credentials are needed but no backend is configured."""


def new_credential_id() -> str:
    return CREDENTIAL_ID_PREFIX + secrets.token_hex(12)


def new_credential_secret() -> str:
    return CREDENTIAL_SECRET_PREFIX + secrets.token_urlsafe(32)


def new_pull_token() -> str:
    return PULL_TOKEN_PREFIX + secrets.token_urlsafe(32)


def secret_digest(secret: str) -> str:
    """What is stored in place of a credential secret or a pull token.

    A plain SHA-256, deliberately not a password hash: the input is 256 bits
    from the system CSPRNG, so there is nothing to brute-force and no reason
    to spend a KDF's time on every ``/v2/`` request.
    """

    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RegistryCredential:
    """A stored credential. The secret is not here: only its digest is kept, and only by the store."""

    id: str
    user_id: str
    scope: str
    expires_at: datetime
    session_id: str | None = None
    created_at: datetime | None = None


@dataclass(frozen=True)
class TokenGrant:
    """What one live pull token allows."""

    user_id: str
    repositories: tuple[str, ...]
    expires_at: datetime
    issued_at: datetime
    credential_id: str | None = None

    def allows_pull(self, repository: str) -> bool:
        return repository in self.repositories


class RegistryCredentialStore(ABC):
    """Persistence for registry credentials and the pull tokens minted from them.

    Every "find" answers only for a row that is live *now* -- unexpired, and
    for a token, whose credential still exists and is unexpired -- so callers
    never compare timestamps themselves.
    """

    @abstractmethod
    def create_credential(
        self,
        *,
        credential_id: str,
        user_id: str,
        secret_hash: str,
        scope: str,
        session_id: str | None,
        ttl_seconds: int,
    ) -> RegistryCredential:
        """Store a new credential; also drops the user's expired ones and their oldest past the cap."""

        raise NotImplementedError

    @abstractmethod
    def find_credential(self, credential_id: str, secret_hash: str) -> RegistryCredential | None:
        """The live credential with this id and secret digest, or ``None``."""

        raise NotImplementedError

    @abstractmethod
    def revoke_credential(self, credential_id: str, user_id: str) -> bool:
        """Delete this user's live credential and its tokens.

        ``False`` when there is none, it has expired, or it is not theirs.
        """

        raise NotImplementedError

    @abstractmethod
    def revoke_all(self, user_id: str) -> int:
        """Delete every credential and every pull token of this user; returns the credentials deleted."""

        raise NotImplementedError

    @abstractmethod
    def create_token(
        self,
        *,
        token_hash: str,
        user_id: str,
        credential_id: str | None,
        repositories: Iterable[str],
        ttl_seconds: int,
    ) -> TokenGrant | None:
        """Store a pull token and return what it grants.

        With a ``credential_id`` the token expires no later than that
        credential, and ``None`` is returned when the credential is gone or
        expired -- the mint and the liveness check are one statement, so a
        revoke racing a mint cannot leave a token behind.
        """

        raise NotImplementedError

    @abstractmethod
    def find_token(self, token_hash: str) -> TokenGrant | None:
        """The live grant for this token digest, or ``None``."""

        raise NotImplementedError


class UnavailableRegistryCredentialStore(RegistryCredentialStore):
    """Used when no shared frames Postgres is configured. Every call raises."""

    def _refuse(self) -> RegistryCredentialsUnavailableError:
        return RegistryCredentialsUnavailableError("Cog registry credential storage is not configured")

    def create_credential(self, **_kwargs) -> RegistryCredential:
        raise self._refuse()

    def find_credential(self, credential_id, secret_hash) -> RegistryCredential | None:
        raise self._refuse()

    def revoke_credential(self, credential_id, user_id) -> bool:
        raise self._refuse()

    def revoke_all(self, user_id) -> int:
        raise self._refuse()

    def create_token(self, **_kwargs) -> TokenGrant | None:
        raise self._refuse()

    def find_token(self, token_hash) -> TokenGrant | None:
        raise self._refuse()


@dataclass
class InMemoryRegistryCredentialStore(RegistryCredentialStore):
    """Process-local store for tests and single-process development; same semantics as Postgres."""

    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    _credentials: dict[str, tuple[RegistryCredential, str]] = field(default_factory=dict)
    _tokens: dict[str, TokenGrant] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _purge(self, now: datetime) -> None:
        for key in [key for key, (credential, _hash) in self._credentials.items() if credential.expires_at <= now]:
            self._drop_credential(key)
        for key in [key for key, grant in self._tokens.items() if grant.expires_at <= now]:
            del self._tokens[key]

    def _drop_credential(self, credential_id: str) -> None:
        del self._credentials[credential_id]
        for key in [key for key, grant in self._tokens.items() if grant.credential_id == credential_id]:
            del self._tokens[key]

    def create_credential(self, *, credential_id, user_id, secret_hash, scope, session_id, ttl_seconds):
        now = self.clock()
        credential = RegistryCredential(
            id=credential_id,
            user_id=user_id,
            scope=scope,
            session_id=session_id,
            created_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )
        with self._lock:
            self._purge(now)
            self._credentials[credential_id] = (credential, secret_hash)
            mine = sorted(
                (stored for stored, _hash in self._credentials.values() if stored.user_id == user_id),
                key=lambda stored: (stored.created_at, stored.id),
            )
            for stale in mine[:-MAX_CREDENTIALS_PER_USER]:
                self._drop_credential(stale.id)
        return credential

    def find_credential(self, credential_id, secret_hash):
        with self._lock:
            stored = self._credentials.get(credential_id)
            if stored is None:
                return None
            credential, known_hash = stored
            if credential.expires_at <= self.clock() or not hmac.compare_digest(known_hash, secret_hash):
                return None
            return credential

    def revoke_credential(self, credential_id, user_id):
        with self._lock:
            stored = self._credentials.get(credential_id)
            if stored is None or stored[0].user_id != user_id or stored[0].expires_at <= self.clock():
                return False
            self._drop_credential(credential_id)
            return True

    def revoke_all(self, user_id):
        with self._lock:
            mine = [key for key, (credential, _hash) in self._credentials.items() if credential.user_id == user_id]
            for key in mine:
                self._drop_credential(key)
            for key in [key for key, grant in self._tokens.items() if grant.user_id == user_id]:
                del self._tokens[key]
            return len(mine)

    def create_token(self, *, token_hash, user_id, credential_id, repositories, ttl_seconds):
        now = self.clock()
        expires_at = now + timedelta(seconds=ttl_seconds)
        with self._lock:
            self._purge(now)
            if credential_id is not None:
                stored = self._credentials.get(credential_id)
                if stored is None or stored[0].user_id != user_id:
                    return None
                expires_at = min(expires_at, stored[0].expires_at)
            grant = TokenGrant(
                user_id=user_id,
                credential_id=credential_id,
                repositories=tuple(repositories),
                issued_at=now,
                expires_at=expires_at,
            )
            self._tokens[token_hash] = grant
            return grant

    def find_token(self, token_hash):
        with self._lock:
            self._purge(self.clock())
            grant = self._tokens.get(token_hash)
            if grant is None or grant.expires_at <= self.clock():
                return None
            if grant.credential_id is not None:
                stored = self._credentials.get(grant.credential_id)
                if stored is None or stored[0].expires_at <= self.clock():
                    return None
            return grant


def _credential_from_row(row) -> RegistryCredential:
    return RegistryCredential(
        id=row["id"],
        user_id=row["user_id"],
        scope=row["scope"],
        session_id=row["session_id"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
    )


def _grant_from_row(row) -> TokenGrant:
    return TokenGrant(
        user_id=row["user_id"],
        credential_id=row["credential_id"],
        repositories=tuple(row["repositories"] or ()),
        issued_at=row["created_at"],
        expires_at=row["expires_at"],
    )


class PostgresRegistryCredentialStore(RegistryCredentialStore):
    """The two tables of migration 13, over the shared pool. Carries no DDL.

    Every comparison with "now" is the server's clock, so replicas agree on
    what is live whatever their own clocks say.
    """

    def __init__(self, db):
        self._db = db
        self._last_sweep = time.monotonic()
        self._sweep_guard = threading.Lock()
        self._sweep_thread: threading.Thread | None = None

    def _sweep_if_due(self) -> None:
        """Start a sweep of expired rows, at most once per :data:`SWEEP_INTERVAL_SECONDS` per process.

        Expired rows are housekeeping, never correctness: every lookup
        filters on ``expires_at`` itself. So no request deletes them. A call
        that finds a sweep due only starts one, on a thread and a pooled
        connection of its own, and carries on; the sweep is outside every
        request's transaction, ``statement_timeout`` and budget, and a sweep
        that fails or is slow costs no request anything. There is no timer:
        a Hub nobody is calling has nobody waiting on the rows either.
        """

        now = time.monotonic()
        with self._sweep_guard:
            if now - self._last_sweep < SWEEP_INTERVAL_SECONDS:
                return
            if self._sweep_thread is not None and self._sweep_thread.is_alive():
                return
            self._last_sweep = now
            self._sweep_thread = threading.Thread(
                target=self._sweep, name="cog-registry-credential-sweep", daemon=True
            )
            self._sweep_thread.start()

    def _sweep(self) -> None:
        try:
            full = False
            with self._db.connection(timeout=SWEEP_TIMEOUT_SECONDS) as conn:
                conn.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(int(SWEEP_TIMEOUT_SECONDS * 1000)),),
                )
                for statement in _SWEEP_STATEMENTS:
                    full = conn.execute(statement, (SWEEP_BATCH_ROWS,)).rowcount >= SWEEP_BATCH_ROWS or full
            if full:
                # More is waiting than one batch took: the next call sweeps again.
                with self._sweep_guard:
                    self._last_sweep = float("-inf")
        except Exception as exc:
            # Whatever went wrong is retried an interval from now. The class
            # only: a driver message can carry connection details.
            logger.warning("cog_registry_credential_sweep_failed", extra={"error": type(exc).__name__})

    def create_credential(self, *, credential_id, user_id, secret_hash, scope, session_id, ttl_seconds):
        self._sweep_if_due()
        with bounded_connection(self._db) as conn:
            # Issuance and pruning for one user are serialized: without this,
            # two concurrent exchanges each prune before the other commits and
            # the user ends up over the cap. Transaction-level, so it is
            # released at commit or rollback; the wait for it is a statement
            # like any other and is bounded by the request budget.
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
                (CREDENTIAL_ISSUE_LOCK_CLASS, user_id),
            )
            # This user's expired credentials go first (their tokens
            # cascade), so the cap below counts live ones. Only this user's:
            # everyone else's are the background sweep's, not this request's.
            conn.execute(
                "DELETE FROM collab_cog_registry_credentials WHERE user_id = %s AND expires_at <= now()",
                (user_id,),
            )
            row = conn.execute(
                """
                INSERT INTO collab_cog_registry_credentials (id, user_id, secret_hash, scope, session_id, expires_at)
                VALUES (%s, %s, %s, %s, %s, now() + make_interval(secs => %s))
                RETURNING id, user_id, scope, session_id, created_at, expires_at
                """,
                (credential_id, user_id, secret_hash, scope, session_id, ttl_seconds),
            ).fetchone()
            conn.execute(
                """
                DELETE FROM collab_cog_registry_credentials
                WHERE user_id = %s AND id NOT IN (
                    SELECT id FROM collab_cog_registry_credentials
                    WHERE user_id = %s
                    ORDER BY created_at DESC, id DESC
                    LIMIT %s
                )
                """,
                (user_id, user_id, MAX_CREDENTIALS_PER_USER),
            )
        return _credential_from_row(row)

    def find_credential(self, credential_id, secret_hash):
        with bounded_connection(self._db) as conn:
            row = conn.execute(
                """
                SELECT id, user_id, scope, session_id, created_at, expires_at
                FROM collab_cog_registry_credentials
                WHERE id = %s AND secret_hash = %s AND expires_at > now()
                """,
                (credential_id, secret_hash),
            ).fetchone()
        return _credential_from_row(row) if row else None

    def revoke_credential(self, credential_id, user_id):
        with bounded_connection(self._db) as conn:
            row = conn.execute(
                "DELETE FROM collab_cog_registry_credentials"
                " WHERE id = %s AND user_id = %s AND expires_at > now() RETURNING id",
                (credential_id, user_id),
            ).fetchone()
        return row is not None

    def revoke_all(self, user_id):
        with bounded_connection(self._db) as conn:
            rows = conn.execute(
                "DELETE FROM collab_cog_registry_credentials WHERE user_id = %s RETURNING id",
                (user_id,),
            ).fetchall()
            # Tokens minted straight from a Hub access token have no
            # credential to cascade from.
            conn.execute("DELETE FROM collab_cog_registry_tokens WHERE user_id = %s", (user_id,))
        return len(rows)

    def create_token(self, *, token_hash, user_id, credential_id, repositories, ttl_seconds):
        names = list(repositories)
        # Nothing is deleted on the mint: a client mints per request, and a
        # table-wide delete here would queue every concurrent mint behind the
        # same row locks.
        self._sweep_if_due()
        with bounded_connection(self._db) as conn:
            if credential_id is None:
                row = conn.execute(
                    """
                    INSERT INTO collab_cog_registry_tokens
                        (token_hash, user_id, credential_id, repositories, expires_at)
                    VALUES (%s, %s, NULL, %s, now() + make_interval(secs => %s))
                    RETURNING user_id, credential_id, repositories, created_at, expires_at
                    """,
                    (token_hash, user_id, names, ttl_seconds),
                ).fetchone()
            else:
                # One statement: the token exists only if the credential is
                # live at the instant it is written, and cannot outlive it.
                row = conn.execute(
                    """
                    INSERT INTO collab_cog_registry_tokens
                        (token_hash, user_id, credential_id, repositories, expires_at)
                    SELECT %s, c.user_id, c.id, %s, LEAST(now() + make_interval(secs => %s), c.expires_at)
                    FROM collab_cog_registry_credentials c
                    WHERE c.id = %s AND c.user_id = %s AND c.expires_at > now()
                    RETURNING user_id, credential_id, repositories, created_at, expires_at
                    """,
                    (token_hash, names, ttl_seconds, credential_id, user_id),
                ).fetchone()
        return _grant_from_row(row) if row else None

    def find_token(self, token_hash):
        self._sweep_if_due()
        with bounded_connection(self._db) as conn:
            row = conn.execute(
                """
                SELECT t.user_id, t.credential_id, t.repositories, t.created_at, t.expires_at
                FROM collab_cog_registry_tokens t
                LEFT JOIN collab_cog_registry_credentials c ON c.id = t.credential_id
                WHERE t.token_hash = %s
                  AND t.expires_at > now()
                  AND (t.credential_id IS NULL OR c.expires_at > now())
                """,
                (token_hash,),
            ).fetchone()
        return _grant_from_row(row) if row else None
