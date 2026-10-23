"""The request budget, carried into blocking store calls (issue #179).

A ``/v2/`` request runs under one aggregate deadline, but its catalog and
credential lookups are synchronous psycopg calls on a worker thread, and
cancelling the coroutine that awaits a worker does not stop the worker: a
query the database is slow to answer would keep its thread and its pooled
connection after the request had already answered 503.

So the budget is handed to the database. The router records the request's
deadline in :data:`request_deadline` (a context variable, which the
threadpool hop copies), and every serving store call checks its connection
out through :func:`bounded_connection`: the pool wait is capped at what is
left, and a transaction-local ``statement_timeout`` makes the *server* abort
a statement that outlives it. The worker then unwinds, the transaction rolls
back, and the connection goes back to the pool.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar

DEFAULT_BUDGET_SECONDS = 10.0
"""Budget for a store call made outside a ``/v2/`` request (the credential exchange, the token endpoint)."""

request_deadline: ContextVar[float | None] = ContextVar("cog_serving_request_deadline", default=None)
"""``time.monotonic()`` at which the current request's budget runs out, or ``None`` outside one."""


class BudgetExhausted(TimeoutError):
    """The request's budget was already spent before a store call could start."""


def renew(seconds: float = DEFAULT_BUDGET_SECONDS) -> None:
    """Give the store calls that follow a fresh budget of ``seconds``.

    For a request whose own budget is long because it carries a blob: the
    store calls made *after* the bytes have been forwarded must not find the
    budget already spent by the transfer, and must not inherit minutes
    either. The request's aggregate timeout still bounds the whole.
    """

    request_deadline.set(time.monotonic() + seconds)


def remaining_seconds() -> float:
    """What is left of the current request's budget; the default budget when there is no request."""

    deadline = request_deadline.get()
    if deadline is None:
        return DEFAULT_BUDGET_SECONDS
    return deadline - time.monotonic()


class BudgetedConnection:
    """A pooled connection that spends from one deadline, statement by statement.

    Before **each** statement the remaining budget is recomputed: none left
    raises :class:`BudgetExhausted` without sending anything, and otherwise
    the statement runs under a transaction-local ``statement_timeout`` of
    exactly what is left. Time spent waiting for the connection, and time
    spent by earlier statements, is therefore never granted again.
    """

    # One residual, stated rather than engineered around: the ``set_config``
    # round trip that installs the timeout is itself a trivial statement with
    # no timeout of its own. If *it* stalls (a dead server or network, not a
    # lock: it touches no table), it is bounded by the pool's connection
    # settings -- TCP keepalive and the pool's liveness handling -- and not by
    # the request budget. The deadline is re-checked when it returns, so the
    # application statement never starts late.

    def __init__(self, conn, deadline: float) -> None:
        self._conn = conn
        self._deadline = deadline

    def execute(self, sql, params=None):
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise BudgetExhausted("the request's time budget is spent")
        self._conn.execute(
            "SELECT set_config('statement_timeout', %s, true)",
            (str(max(1, int(remaining * 1000))),),
        )
        # The setup round trip took time too: if it used up what was left,
        # the statement is not sent at all rather than started late.
        if self._deadline - time.monotonic() <= 0:
            raise BudgetExhausted("the request's time budget is spent")
        return self._conn.execute(sql, params)


@contextmanager
def bounded_connection(db):
    """A pooled connection whose acquisition and statements cannot outlive the request budget.

    Raises :class:`BudgetExhausted` without touching the pool when nothing is
    left. Otherwise the checkout waits at most the remaining budget (psycopg's
    ``PoolTimeout`` past it) and yields a :class:`BudgetedConnection`, whose
    every statement is bounded by what is left *at that moment*
    (``QueryCanceled`` past it). The timeout is transaction-local, so the
    connection returns to the pool unaltered.
    """

    remaining = remaining_seconds()
    if remaining <= 0:
        raise BudgetExhausted("the request's time budget is spent")
    deadline = time.monotonic() + remaining
    with db.connection(timeout=remaining) as conn:
        yield BudgetedConnection(conn, deadline)


def request_connection(db):
    """:func:`bounded_connection` inside a request that carries a deadline; the ordinary checkout otherwise.

    For stores with callers on both sides: the lookup behaves exactly as it
    always has unless a ``/v2/`` request has set :data:`request_deadline`.
    """

    if request_deadline.get() is None:
        return db.connection()
    return bounded_connection(db)
