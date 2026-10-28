"""The run controller: the process that advances runs, separate from the one that accepts them.

    collab-hub-run-controller --track FILE_OR_URL --packages DIR [--packages DIR ...] --work-dir DIR

ADR-0002 D4. The API writes intent to the Track (``intents.py``) — a
submission, a request to cancel, a turn, a decision on a Gate — and the
controller watches the Track and acts on it. Nothing calls the controller, and
it alone constructs an executor.

**Pickup.** A run submitted and not yet picked up is started on the lifecycle
runner, which records ``run_picked_up`` naming this controller only if, as the
record lands, nobody has picked the run up or cancelled it. Several controllers
share one Track, a SQLite file on one host or Postgres anywhere, and of those
picking up one run, one starts it.

**Ownership.** Under ``none`` a picked-up run belongs to the controller that
picked it up: it alone advances it, delivers its cancel, its turns and the
decision on its Gate, and when it stops, the run ends ``interrupted`` with it.
A controller is known by its id (``--id``, this host's name by default), which
it holds for as long as it runs — a lock beside a SQLite Track, an advisory lock
on Postgres — so two live controllers never share one. When it starts, it
records ``interrupted`` for every run it picked up and did not finish, and
reaps any of their ``local`` workers left alive. Under ``dbos`` and
``temporal`` the engine takes ownership after pickup (Phases 27 and 33).

**Models.** ``--models FILE`` is the hub's ``models:`` block: the models it
offers and which Cog talks to which; each worker receives its Cog's model, and
no other (``binding.ModelsBlock``).

**Health.** With ``--health-port``, ``GET /healthz`` answers 200 while the
controller is passing over the Track, and ``GET /readyz`` once it has started
and its last pass read the Track.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import logging
import os
import signal
import socket
import sys
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import intents
from .binding import BindingResolutionError, ModelsBlock
from .locations import AGENT_LOCATIONS
from .runner import LifecycleRunner, RetryTaken, RunBusy, RunOwnedElsewhere
from .states import InvalidTransition, RunState
from .track import PostgresTrackStore, SqliteTrackStore, TrackStore

_log = logging.getLogger("collab_hub_execution.controller")

POSTGRES_SCHEMES = ("postgresql://", "postgres://")
TRACK_TABLES = ("collab_track_events", "collab_track_payloads")
"""The Track's tables on Postgres, both from the hub's migrations: events, and payloads too large to keep inline."""


class RunController:
    """Watches a Track and advances what it finds there, on one lifecycle runner."""

    def __init__(self, runner: LifecycleRunner, *, poll_interval: float = 0.25, session_grace: float = 30.0,
                 track_spec: str | None = None, owner_check_interval: float = 30.0) -> None:
        self.runner = runner
        self.poll_interval = poll_interval
        self.session_grace = session_grace
        """How long a worker that was invoked may answer a turn "no session" before the turn fails."""
        self._ended: set[str] = set()
        self.track_spec = track_spec
        """The Track as `--track` names it, through which another controller's id can be probed; ``None`` leaves
        the runs of an owner that never returns to that owner."""
        self.owner_check_interval = owner_check_interval
        self._owners: dict[str, tuple[float, bool]] = {}  # controller id -> (when probed, still holds its id)
        self._holding: dict[str, threading.Lock] = {}  # controller id -> held by a takeover of this process
        self._holding_lock = threading.Lock()
        self._taking_over: dict[str, threading.Thread] = {}
        self._advancing: dict[str, threading.Thread] = {}
        self._cancelling: dict[str, threading.Thread] = {}
        self._turning: dict[str, threading.Thread] = {}
        self._first_refused: dict[str, float] = {}  # turn -> when its worker first said no session was open
        self.views = intents.RunViews(runner.track)
        self.started = False
        self.last_pass: float | None = None
        """When the last pass over the Track ended, on the monotonic clock; ``None`` before the first."""
        self.last_error: str | None = None
        """Why the last pass could not read the Track; ``None`` when it could."""

    @property
    def name(self) -> str | None:
        return self.runner.controller

    def start(self) -> tuple[str, ...]:
        """What a controller does first: record every run it left unfinished as interrupted, and reap its workers."""
        interrupted = self.runner.start()
        for run_id in interrupted:
            _log.info("interrupted %s: this controller stopped before it ended", run_id)
        self.started = True
        return interrupted

    def tick(self) -> None:
        """One pass over the Track: start what was submitted, deliver what was asked.

        Each run is read incrementally (``RunViews``), and each is handled on its
        own: a run whose Track cannot be read is logged and passed over, never
        stopping the pass for the runs after it.
        """
        for run_id in self.runner.track.run_ids():
            if run_id in self._ended:
                continue
            try:
                self._look_at(run_id)
            except intents.RunUnreadable:
                continue  # RunViews logged it once; it is read again only when its Track moves
            except Exception:  # noqa: BLE001 - one run's trouble never stops the pass
                _log.exception("handling %s failed; the next pass tries again", run_id)
        # An ended run is read no more, unless a retry was asked for it: the Track's index says which.
        for row in self.runner.track.runs(retry_asked=True):
            self._ended.discard(row.run_id)
            try:
                self._look_at(row.run_id)
            except intents.RunUnreadable:
                continue
            except Exception:  # noqa: BLE001
                _log.exception("handling %s failed; the next pass tries again", row.run_id)

    def _mine(self, view: intents.RunView) -> bool:
        """Whether this controller advances the run: it picked it up, or it is the only one on its Track."""
        return self.name is None or view.controller == self.name

    def _look_at(self, run_id: str) -> None:
        view = self.views.view(run_id)
        if view is None:
            return
        if view.state.ended:
            if view.retry_asked_by is not None:
                if not self._alive(self._advancing, run_id):
                    self._spawn(self._advancing, run_id, self._retry, run_id, view.retry_asked_by)
                return
            self._ended.add(run_id)
            self.views.forget(run_id)  # never read again here: keep nothing of it
            return
        mine = self._mine(view)
        if not mine and view.controller and not self._owner_holds_id(view.controller):
            # Picked up by a controller that never started again: a live one ends the run for it.
            if not self._alive(self._taking_over, run_id):
                self._spawn(self._taking_over, run_id, self._take_over, run_id, view.controller)
            return
        if view.cancel_requested_by is not None:
            # A run nobody picked up is cancelled by whichever controller gets there; any other, by its own.
            if (view.state is RunState.SUBMITTED or mine) and not self._alive(self._cancelling, run_id):
                self._spawn(self._cancelling, run_id, self._cancel, run_id, view.cancel_requested_by)
        elif view.state is RunState.SUBMITTED and not self._alive(self._advancing, run_id):
            self._spawn(self._advancing, run_id, self._advance, view.op)
        elif (view.state is RunState.WAITING_AT_GATE and view.decision is not None and mine
              and not self._alive(self._advancing, run_id)):
            self._spawn(self._advancing, run_id, self._decide, run_id, view.decision)
        if (mine and any(turn.state == "pending" for turn in self.views.turns(run_id).values())
                and not self._alive(self._turning, run_id)):
            self._spawn(self._turning, run_id, self._deliver_turns, run_id)

    @staticmethod
    def _alive(threads: dict[str, threading.Thread], run_id: str) -> bool:
        thread = threads.get(run_id)
        return thread is not None and thread.is_alive()

    @staticmethod
    def _spawn(threads: dict[str, threading.Thread], run_id: str, target, *args) -> None:
        threads[run_id] = threading.Thread(target=target, args=args, name=f"run-{run_id}", daemon=True)
        threads[run_id].start()

    def _advance(self, op) -> None:
        try:
            state = self.runner.submit(op)
        except Exception:  # noqa: BLE001 - one run's failure never stops the controller
            _log.exception("advancing %s failed", op.run_id)
            return
        view = self.views.view(op.run_id)
        if view is not None and view.controller not in (None, self.name):
            _log.info("%s was picked up by %s", op.run_id, view.controller)
            return
        _log.info("%s is %s", op.run_id, state.name)

    def _cancel(self, run_id: str, actor: str) -> None:
        try:
            self.runner.cancel(run_id, actor=actor)
        except InvalidTransition:
            pass  # it ended on its own before the request was delivered
        except RunOwnedElsewhere:
            return  # picked up by another controller as this one looked: that one delivers it
        except Exception:  # noqa: BLE001 - one run's failure never stops the controller
            _log.exception("cancelling %s failed", run_id)
            return
        _log.info("cancel of %s by %s delivered", run_id, actor)

    def _owner_holds_id(self, controller: str) -> bool:
        """Whether ``controller`` still holds its id on the Track; ``True`` when it cannot be known.

        Probed by trying its id's lock and letting it go at once: held, the
        owner is alive, and that is remembered for ``owner_check_interval``. A
        free id is never remembered: the owner may start again at any moment,
        and ``_take_over`` holds its id while it decides.
        """
        if self.track_spec is None:
            return True
        if self._owner_hold(controller).locked():
            return False  # a takeover of this process holds the id: free of its owner, by definition
        now = time.monotonic()
        probed = self._owners.get(controller)
        if probed is not None and now - probed[0] < self.owner_check_interval:
            return probed[1]
        try:
            holds = id_held(self.track_spec, controller)
        except Exception:  # noqa: BLE001 - a probe that fails says nothing: the owner is left its run
            _log.exception("probing whether controller %r holds its id failed", controller)
            holds = True
        if holds:
            self._owners[controller] = (now, holds)
        else:
            self._owners.pop(controller, None)
        return holds

    def _owner_hold(self, owner: str) -> threading.Lock:
        with self._holding_lock:
            return self._holding.setdefault(owner, threading.Lock())

    def _take_over(self, run_id: str, owner: str) -> None:
        """Take the run over while holding its owner's id, so the owner cannot start and pick it up meanwhile.

        Between a probe that found the id free and the write, the owner could
        start again, take its id back and carry the run on; the `interrupted`
        would then land on a run that is alive, and the Track would no longer
        replay. Held by this controller, the id stays free of the owner (one
        starting waits for it, or refuses) until the run is ended or found
        moved.
        """
        try:
            # One takeover of an owner's runs at a time in this process: a second thread trying the owner's
            # lock while a first holds it would read its own process as the owner, alive.
            with self._owner_hold(owner), holding_id(self.track_spec, owner) as held:
                if not held:
                    return  # the owner is back: the run is its
                state = self.runner.take_over(run_id, from_controller=owner)
        except Exception:  # noqa: BLE001 - one run's failure never stops the controller
            _log.exception("taking %s over from %s failed", run_id, owner)
            return
        if state is not None:
            _log.info("%s was picked up by %s, which no longer holds its id: it is %s", run_id, owner, state.name)

    def _retry(self, run_id: str, actor: str) -> None:
        """Take a retry asked for: the runner records `retry_requested` and advances the run; or say why not.

        Every controller on the Track may take an ask; the ask itself is the
        condition, so one takes it and the others find nothing to refuse.
        """
        try:
            state = self.runner.retry(run_id, asked=True)
        except (RunOwnedElsewhere, RetryTaken):
            return
        except ValueError as exc:
            intents.refuse_retry(self.runner.track, run_id, error=str(exc))
            return
        except Exception:  # noqa: BLE001 - one run's failure never stops the controller
            _log.exception("retrying %s failed", run_id)
            return
        _log.info("retry of %s asked by %s taken: it is %s", run_id, actor, state.name)

    def _decide(self, run_id: str, decision: dict[str, Any]) -> None:
        """Deliver a decision on a Gate to the runner, which records it and advances the run; or say why not."""
        escalation = decision.get("escalation")
        try:
            state = self.runner.decide(run_id, escalation=escalation, actor=decision.get("actor", ""),
                                       outcome=decision.get("outcome", ""), findings=decision.get("findings") or ())
        except ValueError as exc:
            if isinstance(exc.__cause__, RunBusy):
                return  # another call in this controller holds the run: the next pass delivers it
            intents.refuse_decision(self.runner.track, run_id, escalation=escalation, error=str(exc))
            return
        except InvalidTransition as exc:  # a stale escalation among them
            intents.refuse_decision(self.runner.track, run_id, escalation=escalation, error=str(exc))
            return
        except RunOwnedElsewhere:
            return
        except Exception:  # noqa: BLE001 - one run's failure never stops the controller
            _log.exception("deciding on %s failed", run_id)
            return
        _log.info("decision %s on %s by %s delivered: it is %s", decision.get("outcome"), run_id,
                  decision.get("actor"), state.name)

    def _deliver_turns(self, run_id: str) -> None:
        """Hand the run's waiting turns to its live worker, one at a time and in order.

        A turn asked before the worker is up waits for it: the next pass delivers it.
        """
        track = self.runner.track
        while True:
            waiting = [view for view in self.views.turns(run_id).values() if view.state == "pending"]
            if not waiting:
                return
            worker = self.runner.live_worker(run_id)
            if worker is None:
                return
            view = waiting[0]
            if not hasattr(worker, "turn"):
                intents.answer_turn(track, run_id, view.turn, error="this Cog's worker takes no turns")
                continue
            try:
                answer = worker.turn(view.turn, view.text)
            except Exception as exc:  # noqa: BLE001 - recorded on the Track as the turn's failure
                if getattr(exc, "status", None) == 404 and self._still_opening(view.turn):
                    # The worker was invoked and has not opened its session yet: the next pass tries again.
                    return
                intents.answer_turn(track, run_id, view.turn, error=f"{type(exc).__name__}: {exc}"[:1024])
                continue
            self._first_refused.pop(view.turn, None)
            intents.answer_turn(track, run_id, view.turn, text=answer)
            _log.info("turn %s of %s answered", view.turn, run_id)

    def _still_opening(self, turn: str) -> bool:
        """Whether a worker that holds no session yet is still within its grace to open one."""
        first = self._first_refused.setdefault(turn, time.monotonic())
        if time.monotonic() - first < self.session_grace:
            return True
        self._first_refused.pop(turn, None)
        return False

    def run(self, stop: threading.Event, *, still_held=None) -> None:
        """Watch the Track until ``stop`` is set, or until ``still_held`` says this controller lost its id."""
        while not stop.is_set():
            if still_held is not None and not still_held():
                _log.error("this controller no longer holds its id %r: stopping, so no other one shares it",
                           self.name)
                stop.set()
                break
            try:
                self.tick()
                self.last_error = None
            except Exception as exc:  # noqa: BLE001 - a Track that cannot be read now is read again next pass
                self.last_error = f"{type(exc).__name__}: {exc}"
                _log.exception("reading the Track failed")
            self.last_pass = time.monotonic()
            stop.wait(self.poll_interval)

    def idle(self) -> bool:
        """Whether nothing is being advanced or cancelled right now."""
        threads = (*self._advancing.values(), *self._cancelling.values(), *self._turning.values())
        return not any(thread.is_alive() for thread in threads)

    def health(self) -> dict[str, Any]:
        """Liveness and readiness: passing over the Track lately; started, and the last pass read it."""
        stale_after = max(30.0, 20 * self.poll_interval)
        live = self.last_pass is not None and time.monotonic() - self.last_pass < stale_after
        return {"controller": self.name, "live": live, "ready": live and self.started and self.last_error is None,
                "error": self.last_error}


def serve_health(health: Callable[[], dict[str, Any]], host: str, port: int) -> ThreadingHTTPServer:
    """``GET /healthz`` and ``GET /readyz`` from ``health()``, on a thread of their own."""

    class Health(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            state = health()
            wanted = {"/healthz": "live", "/readyz": "ready"}.get(self.path)
            if wanted is None:
                code, body = 404, {"error": "not found"}
            else:
                code, body = (200 if state[wanted] else 503), state
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer((host, port), Health)
    threading.Thread(target=server.serve_forever, name="health", daemon=True).start()
    return server


# --- the Track, and holding the controller's id on it -------------------------------------------


class IdTaken(RuntimeError):
    """Another live controller holds this id on this Track."""


class TrackNotReady(SystemExit):
    """The Track is not ready yet, where it may be soon: its database does not answer, or the API's
    migrations have not created its tables. What a controller started beside its API waits for; every
    other refusal of a Track is a misconfiguration, and stops it at once."""


def open_track(spec: str) -> tuple[TrackStore, Any]:
    """The Track ``spec`` names, a SQLite file or a ``postgresql://`` URL, and what keeps it open."""
    if spec.startswith(POSTGRES_SCHEMES):
        try:
            from psycopg_pool import ConnectionPool
        except ImportError as exc:  # pragma: no cover - an install without the extra
            raise SystemExit("a Postgres Track needs psycopg: install collab-hub-execution[postgres]") from exc
        import psycopg
        from psycopg_pool import PoolTimeout

        pool = ConnectionPool(spec, min_size=1, max_size=8, open=True)
        try:
            with pool.connection(timeout=10) as connection:
                # On Postgres the Track's tables come from the hub's migration registry, never from here.
                missing = [table for table in TRACK_TABLES if connection.execute(
                    "SELECT to_regclass(%s) IS NULL", (table,)).fetchone()[0]]
        except (PoolTimeout, psycopg.OperationalError) as exc:
            pool.close()
            raise TrackNotReady(f"the Track's database does not answer: {type(exc).__name__}") from exc
        if missing:
            pool.close()
            raise TrackNotReady(f"this Postgres has no Track, or part of one (missing {', '.join(missing)}): the "
                                "hub's migrations create its tables (start the API on it once, e.g. `make -C dev "
                                "api-pg`)")
        return PostgresTrackStore(pool), pool
    if "://" in spec:
        raise SystemExit(f"the Track is a SQLite file or a postgresql:// URL, not {spec.split('://')[0]}://...")
    path = Path(spec)
    SqliteTrackStore.ensure_schema(path)
    return SqliteTrackStore(path), None


def id_held(spec: str, controller: str) -> bool:
    """Whether a live controller holds ``controller`` on the Track: its lock is tried and let go at once."""
    if spec.startswith(POSTGRES_SCHEMES):
        import psycopg

        with psycopg.connect(spec, autocommit=True) as connection:
            got = connection.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0))",
                                     (f"collab-run-controller:{controller}",)).fetchone()[0]
            if got:
                connection.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                                   (f"collab-run-controller:{controller}",))
            return not got
    path = Path(spec)
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in controller)
    lock_path = path.with_name(f"{path.name}.{safe}.controller.lock")
    if not lock_path.exists():
        return False
    with open(lock_path) as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(lock, fcntl.LOCK_UN)
        return False


@contextlib.contextmanager
def holding_id(spec: str | None, controller: str) -> Iterator[bool]:
    """Hold ``controller``'s id for the block, when it is free: ``True`` inside when held, ``False`` when a live
    controller holds it (nothing is waited for). A ``None`` spec is a Track with no ids to hold: ``True``."""
    if spec is None:
        yield True
        return
    if spec.startswith(POSTGRES_SCHEMES):
        import psycopg

        with psycopg.connect(spec, autocommit=True) as connection:
            got = connection.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0))",
                                     (f"collab-run-controller:{controller}",)).fetchone()[0]
            try:
                yield bool(got)
            finally:
                if got:
                    connection.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                                       (f"collab-run-controller:{controller}",))
        return
    path = Path(spec)
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in controller)
    with open(path.with_name(f"{path.name}.{safe}.controller.lock"), "a") as lock:  # never truncated: flock only
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def hold_id(spec: str, controller: str):
    """Hold ``controller`` on the Track for as long as this process runs; returns a check that it still does.

    Beside a SQLite Track, a lock file per id; on Postgres, a session advisory
    lock on its own connection, released by Postgres when that connection
    ends — which the returned check notices. :class:`IdTaken` when another live
    controller holds it.

    The returned check holds the lock file or the connection: keep it for as
    long as the id must be held, since dropping it lets the lock go.
    """
    if spec.startswith(POSTGRES_SCHEMES):
        import psycopg

        connection = psycopg.connect(spec, autocommit=True)
        held = connection.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0))",
                                  (f"collab-run-controller:{controller}",)).fetchone()[0]
        if not held:
            connection.close()
            raise IdTaken(f"a controller named {controller!r} is running on this Track")

        def still_held() -> bool:
            try:
                connection.execute("SELECT 1")
                return True
            except psycopg.Error:
                return False

        return still_held
    path = Path(spec)
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in controller)
    lock = open(path.with_name(f"{path.name}.{safe}.controller.lock"), "w")  # noqa: SIM115 - held while running
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise IdTaken(f"a controller named {controller!r} is running on {path}") from None
    return lambda: not lock.closed


def _listed(name: str, separator: str) -> list[str] | None:
    """A repeatable option from its variable: ``separator``-separated values, or ``None`` when unset."""
    value = os.environ.get(name, "")
    return [item for item in value.split(separator) if item] or None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """The controller's configuration: its options, each defaulting to its ``COLLAB_CONTROLLER_*`` variable."""
    env = os.environ.get
    parser: argparse.ArgumentParser

    def number(name: str, default: str, cast):
        """A numeric option's default from its variable; one that is not a number is a usage error."""
        value = env(name) or default
        try:
            return cast(value)
        except ValueError:
            parser.error(f"{name} is not a number: {value!r}")

    parser = argparse.ArgumentParser(
        description="The run controller: advances the runs submitted to a Track. Each option also reads the "
                    "COLLAB_CONTROLLER_ variable named after it (COLLAB_CONTROLLER_HEALTH_PORT for --health-port); "
                    "--packages takes a os.pathsep-separated list there, and --allow a comma-separated one.")
    parser.add_argument("--track", default=env("COLLAB_CONTROLLER_TRACK"),
                        help="the Track the API writes submissions to: a SQLite file, or a postgresql:// URL")
    parser.add_argument("--id", default=env("COLLAB_CONTROLLER_ID") or socket.gethostname(),
                        help="this controller's name, which it holds while it runs and records on each run it "
                             "picks up; the same across its restarts, so a restart takes its runs back. This "
                             "host's name by default")
    parser.add_argument("--packages", action="append", metavar="DIR",
                        help="a directory Cog packages are found under; repeat for several")
    parser.add_argument("--allow", action="append", metavar="NAME",
                        help="a package that may run; every package under --packages when omitted")
    parser.add_argument("--work-dir", default=env("COLLAB_CONTROLLER_WORK_DIR"),
                        help="where each run's worker output goes")
    parser.add_argument("--backend", default=env("COLLAB_CONTROLLER_BACKEND", "none"))
    parser.add_argument("--location", default=env("COLLAB_CONTROLLER_LOCATION", "local"), choices=AGENT_LOCATIONS)
    parser.add_argument("--models", default=env("COLLAB_CONTROLLER_MODELS"), metavar="FILE",
                        help="the hub's models: block, in TOML: the models it offers, and which Cog talks to "
                             "which; each worker gets its Cog's model, and no other")
    parser.add_argument("--poll-interval", type=float, default=number("COLLAB_CONTROLLER_POLL_INTERVAL", "0.25", float))
    parser.add_argument("--interaction-timeout", type=float, metavar="SECONDS",
                        default=number("COLLAB_CONTROLLER_INTERACTION_TIMEOUT", "60", float),
                        help="how long one interaction with a worker may take; 0 for no limit, which a Cog "
                             "holding a session for as long as someone talks to it needs")
    parser.add_argument("--environment", default=env("COLLAB_CONTROLLER_ENVIRONMENT", "pixi"), choices=("pixi", "host"),
                        help="how a package's serve task is run: in its own pixi environment, or directly")
    parser.add_argument("--health-port", type=int, default=number("COLLAB_CONTROLLER_HEALTH_PORT", "0", int),
                        help="serve /healthz and /readyz on this port; none when 0")
    parser.add_argument("--health-host", default=env("COLLAB_CONTROLLER_HEALTH_HOST", "127.0.0.1"))
    parser.add_argument("--image", default=env("COLLAB_CONTROLLER_IMAGE"), metavar="IMAGE",
                        help="at the remote location, the image a worker pod runs: the hub's own, which ships "
                             "the Cog packages under /opt/collab-hub/cogs")
    parser.add_argument("--namespace", default=env("COLLAB_CONTROLLER_NAMESPACE", "collab-hub"),
                        help="at the remote location, the namespace worker pods are made in")
    parser.add_argument("--allow-ingress-from", default=env("COLLAB_CONTROLLER_ALLOW_INGRESS_FROM"), metavar="JSON",
                        help="at the remote location, the labels of the pods that may reach a worker's /invoke, "
                             "as a JSON object: the run controller's own; or --insecure-skip-network-policy")
    parser.add_argument("--worker-resources", default=env("COLLAB_CONTROLLER_WORKER_RESOURCES"), metavar="JSON",
                        help="at the remote location, a worker pod's resources (requests and limits), as JSON")
    parser.add_argument("--worker-cache-size-limit", default=env("COLLAB_CONTROLLER_WORKER_CACHE_SIZE_LIMIT"),
                        metavar="SIZE", help="at the remote location, the size limit of a worker's cache volume, "
                                             "where pixi builds the Cog's environment (10Gi unless given)")
    parser.add_argument("--worker-tmp-size-limit", default=env("COLLAB_CONTROLLER_WORKER_TMP_SIZE_LIMIT"),
                        metavar="SIZE", help="at the remote location, the size limit of a worker's /tmp, its "
                                             "HOME (2Gi unless given)")
    parser.add_argument("--insecure-skip-network-policy", action="store_true",
                        default=env("COLLAB_CONTROLLER_INSECURE_SKIP_NETWORK_POLICY", "") == "true",
                        help="at the remote location, make no NetworkPolicy for workers (a trusted namespace only)")
    parser.add_argument("--worker-start-timeout", type=float, metavar="SECONDS",
                        default=number("COLLAB_CONTROLLER_WORKER_START_TIMEOUT", "120", float),
                        help="how long a worker may take to start and answer /healthz, which includes pixi building "
                             "a Cog's environment the first time it is launched")
    parser.add_argument("--wait", type=float, default=number("COLLAB_CONTROLLER_WAIT", "0", float), metavar="SECONDS",
                        help="how long to wait at start for the Track's tables (the API's migrations create them) "
                             "and for this controller's id (held by the one a rollout replaces); 0 refuses at "
                             "once")
    args = parser.parse_args(argv)
    # Repeatable options: the command line's, or else their variables'.
    args.packages = args.packages or _listed("COLLAB_CONTROLLER_PACKAGES", os.pathsep)
    args.allow = args.allow or _listed("COLLAB_CONTROLLER_ALLOW", ",")
    # A default read from a variable is not checked against `choices` by argparse: checked here.
    if args.environment not in ("pixi", "host"):
        parser.error(f"--environment is pixi or host, not {args.environment!r}")
    if args.location not in AGENT_LOCATIONS:
        parser.error(f"--location is one of {', '.join(AGENT_LOCATIONS)}, not {args.location!r}")
    if not args.track:
        parser.error("--track is required")
    if args.location == "local":
        if not args.work_dir:
            parser.error("--work-dir is required at the local location")
        if not args.packages:
            parser.error("--packages is required at the local location")
    else:
        if not args.image:
            parser.error("--image is required at the remote location: the image a worker pod runs")
        if args.allow_ingress_from:
            try:
                args.allow_ingress_from = json.loads(args.allow_ingress_from)
            except ValueError:
                parser.error("--allow-ingress-from is a JSON object of labels")
            if not isinstance(args.allow_ingress_from, dict) or not args.allow_ingress_from:
                parser.error("--allow-ingress-from is a JSON object of labels, and not empty")
        elif not args.insecure_skip_network_policy:
            parser.error("the remote location needs --allow-ingress-from, the labels of the pods that may reach a "
                         "worker, or --insecure-skip-network-policy")
    return args


def _patiently(attempt, refusal: type[BaseException], deadline: float, waiting_for: str, starting: dict):
    """``attempt()``, tried again every few seconds while it is refused and ``deadline`` has not passed.

    How a controller started beside its API waits for what the API brings: the
    Track's tables, which its migrations create, and, in a rollout, the id the
    controller it replaces still holds until that one stops.
    """
    while True:
        try:
            return attempt()
        except refusal as exc:
            if time.monotonic() >= deadline:
                raise
            starting["waiting"] = f"{waiting_for}: {exc}"
            _log.info("waiting (%s): %s", waiting_for, exc)
            time.sleep(min(5.0, max(0.1, deadline - time.monotonic())))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format=f"%(asctime)s controller {args.id} %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per /healthz poll is noise here

    deliver = None
    if args.models:
        try:
            models = ModelsBlock.load(args.models)
            models.check(os.environ)
        except BindingResolutionError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        deliver = models.delivery()

    # Health is served from the start: a controller waiting for its Track or its id is alive, and not
    # ready, so a liveness probe leaves it to wait (a readiness probe on it would hold its pod back).
    starting: dict[str, Any] = {"controller": None, "waiting": None}

    def health() -> dict[str, Any]:
        if starting["controller"] is not None:
            return starting["controller"].health()
        return {"controller": args.id, "live": True, "ready": False, "error": starting["waiting"] or "starting"}

    if args.health_port:
        serve_health(health, args.health_host, args.health_port)
    deadline = time.monotonic() + args.wait
    # Only what may come soon is waited for: a Track misconfigured stops the controller at once.
    track, keep_open = _patiently(lambda: open_track(args.track), TrackNotReady, deadline,
                                  "the Track is not ready", starting)
    try:
        still_held = _patiently(lambda: hold_id(args.track, args.id), IdTaken, deadline,
                                f"another controller holds the id {args.id!r}", starting)
    except IdTaken as exc:
        print(f"error: {exc}: each controller on a Track has its own --id", file=sys.stderr)
        return 1

    if args.location == "local":
        location_settings: dict[str, Any] = {
            "packages": args.packages, "allow": args.allow, "work_dir": args.work_dir,
            "environment": args.environment}
    else:
        location_settings = {
            "image": args.image, "namespace": args.namespace, "controller": args.id,
            "allow_ingress_from": args.allow_ingress_from or None,
            "insecure_skip_network_policy": bool(args.insecure_skip_network_policy)}
        if args.worker_resources:
            try:
                location_settings["resources"] = json.loads(args.worker_resources)
            except ValueError:
                print("error: --worker-resources is a JSON object: {\"requests\": {...}, \"limits\": {...}}",
                      file=sys.stderr)
                return 2
        for option, setting in (("worker_cache_size_limit", "cache_size_limit"),
                                ("worker_tmp_size_limit", "tmp_size_limit")):
            if getattr(args, option):
                location_settings[setting] = getattr(args, option)
    runner = LifecycleRunner(
        track=track, backend=args.backend, location=args.location, controller=args.id,
        location_settings={**location_settings, "interaction_timeout": args.interaction_timeout or None,
                           "ready_timeout": args.worker_start_timeout, "deliver": deliver})
    controller = RunController(runner, poll_interval=args.poll_interval, track_spec=args.track)
    controller.start()
    starting["controller"] = controller

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    where = "Postgres" if args.track.startswith(POSTGRES_SCHEMES) else args.track
    _log.info("watching %s on the %r backend, workers at %r", where, runner.backend.name, args.location)
    controller.run(stop, still_held=still_held)
    # Stopping: runs still in flight are left as they are, and this controller's next start records them
    # interrupted.
    return 0 if still_held() else 1


if __name__ == "__main__":
    sys.exit(main())
