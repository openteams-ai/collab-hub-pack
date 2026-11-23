"""Realm sessions that have ended, so their web session cookies stop working.

The session cookie is stateless (``web.session``): any replica verifies it
with the shared secret and nothing else. That left one thing the Hub could not
do — end a session before its ``exp``. Logging out anywhere (the Hub's own Log
out, the Billing page's, an admin ending that session in Keycloak) ends the **realm** session, and
Keycloak tells every client that took part in it through OpenID Back-Channel
Logout, naming the realm session's ``sid`` (``POST
/web/oidc/backchannel-logout``, ``routers.web``). The cookie carries the
``sid`` it was minted for, so recording that ``sid`` here is what makes every
copy of the cookie stop working, on every replica
(openteams-ai/subscription-manager-pack#79).

A row is ``(sid, ended_at)``. It only has to outlive the cookies that name its
``sid``, and no cookie lives longer than the session lifetime, so each
:meth:`end` deletes the rows older than that: the table holds at most one
session lifetime's worth of logouts.

Two stores, chosen by :func:`~..config.build_ended_web_sessions`:

* :class:`PostgresEndedWebSessions` — ``collab_web_ended_sessions``
  (``collab_`` migration 18), on the shared ``frames.postgres``, so a logout
  any replica hears reaches every replica.
* :class:`InMemoryEndedWebSessions` — when the Hub has no database. Process
  local: a logout reaches only the replica that heard it, which is right for
  one replica in development and nowhere else.

Both are synchronous, like every store on the shared pool; the async callers
(``web.authz.get_web_session``, the session guard) run them on a worker
thread.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Protocol

ENDED_SESSIONS_TABLE = "collab_web_ended_sessions"


class EndedWebSessions(Protocol):
    """Where ended realm sessions are recorded and looked up."""

    def end(self, sid: str) -> None:
        """Record *sid* as ended (idempotent), and prune rows past the lifetime."""

    def has_ended(self, sid: str) -> bool:
        """Whether *sid* has been recorded as ended."""


class InMemoryEndedWebSessions:
    """Ended sessions in this process only: one replica, development and tests.

    Locked, because its callers run on the threadpool.
    """

    def __init__(self, lifetime_seconds: int, *, clock: Callable[[], float] = time.time) -> None:
        self._lifetime = lifetime_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._ended: dict[str, float] = {}

    def end(self, sid: str) -> None:
        now = self._clock()
        with self._lock:
            cutoff = now - self._lifetime
            self._ended = {s: at for s, at in self._ended.items() if at >= cutoff}
            self._ended.setdefault(sid, now)

    def has_ended(self, sid: str) -> bool:
        with self._lock:
            return sid in self._ended


class PostgresEndedWebSessions:
    """``collab_web_ended_sessions`` on the shared pool, read by every replica.

    ``ended_at`` is the database's clock, not the replica's, so pruning on one
    replica cannot delete a row another replica wrote a moment ago with a clock
    that runs behind. The table comes from the ``collab_`` migration runner
    (version 18), so there is no ``auto_migrate`` argument here.
    """

    def __init__(self, database, lifetime_seconds: int) -> None:
        self._database = database
        self._lifetime = lifetime_seconds

    def end(self, sid: str) -> None:
        with self._database.connection() as conn:
            conn.execute(
                f"INSERT INTO {ENDED_SESSIONS_TABLE} (sid, ended_at) VALUES (%s, now()) ON CONFLICT (sid) DO NOTHING",
                (sid,),
            )
            conn.execute(
                f"DELETE FROM {ENDED_SESSIONS_TABLE} WHERE ended_at < now() - make_interval(secs => %s)",
                (self._lifetime,),
            )

    def has_ended(self, sid: str) -> bool:
        with self._database.connection() as conn:
            row = conn.execute(f"SELECT 1 FROM {ENDED_SESSIONS_TABLE} WHERE sid = %s", (sid,)).fetchone()
        return row is not None


__all__ = [
    "ENDED_SESSIONS_TABLE",
    "EndedWebSessions",
    "InMemoryEndedWebSessions",
    "PostgresEndedWebSessions",
]
