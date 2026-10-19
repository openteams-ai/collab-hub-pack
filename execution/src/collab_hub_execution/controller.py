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
import fcntl
import json
import logging
import os
import signal
import socket
import sys
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import intents
from .binding import BindingResolutionError, ModelsBlock
from .locations import AGENT_LOCATIONS
from .runner import LifecycleRunner, RunBusy, RunOwnedElsewhere
from .states import InvalidTransition, RunState
from .track import PostgresTrackStore, SqliteTrackStore, TrackStore

_log = logging.getLogger("collab_hub_execution.controller")

POSTGRES_SCHEMES = ("postgresql://", "postgres://")
TRACK_TABLES = ("collab_track_events", "collab_track_payloads")
"""The Track's tables on Postgres, both from the hub's migrations: events, and payloads too large to keep inline."""


class RunController:
    """Watches a Track and advances what it finds there, on one lifecycle runner."""

    def __init__(self, runner: LifecycleRunner, *, poll_interval: float = 0.25, session_grace: float = 30.0) -> None:
        self.runner = runner
        self.poll_interval = poll_interval
        self.session_grace = session_grace
        """How long a worker that was invoked may answer a turn "no session" before the turn fails."""
        self._ended: set[str] = set()
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

    def _mine(self, view: intents.RunView) -> bool:
        """Whether this controller advances the run: it picked it up, or it is the only one on its Track."""
        return self.name is None or view.controller == self.name

    def _look_at(self, run_id: str) -> None:
        view = self.views.view(run_id)
        if view is None:
            return
        if view.state.ended:
            self._ended.add(run_id)
            self.views.forget(run_id)  # never read again here: keep nothing of it
            return
        mine = self._mine(view)
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


def open_track(spec: str) -> tuple[TrackStore, Any]:
    """The Track ``spec`` names, a SQLite file or a ``postgresql://`` URL, and what keeps it open."""
    if spec.startswith(POSTGRES_SCHEMES):
        try:
            from psycopg_pool import ConnectionPool
        except ImportError as exc:  # pragma: no cover - an install without the extra
            raise SystemExit("a Postgres Track needs psycopg: install collab-hub-execution[postgres]") from exc
        pool = ConnectionPool(spec, min_size=1, max_size=8, open=True)
        with pool.connection() as connection:
            # On Postgres the Track's tables come from the hub's migration registry, never from here.
            missing = [table for table in TRACK_TABLES if connection.execute(
                "SELECT to_regclass(%s) IS NULL", (table,)).fetchone()[0]]
        if missing:
            pool.close()
            raise SystemExit(f"this Postgres has no Track, or part of one (missing {', '.join(missing)}): the hub's "
                             "migrations create its tables (start the API on it once, e.g. `make -C dev api-pg`)")
        return PostgresTrackStore(pool), pool
    if "://" in spec:
        raise SystemExit(f"the Track is a SQLite file or a postgresql:// URL, not {spec.split('://')[0]}://...")
    path = Path(spec)
    SqliteTrackStore.ensure_schema(path)
    return SqliteTrackStore(path), None


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
    parser.add_argument("--poll-interval", type=float, default=float(env("COLLAB_CONTROLLER_POLL_INTERVAL", "0.25")))
    parser.add_argument("--interaction-timeout", type=float, metavar="SECONDS",
                        default=float(env("COLLAB_CONTROLLER_INTERACTION_TIMEOUT", "60")),
                        help="how long one interaction with a worker may take; 0 for no limit, which a Cog "
                             "holding a session for as long as someone talks to it needs")
    parser.add_argument("--environment", default=env("COLLAB_CONTROLLER_ENVIRONMENT", "pixi"), choices=("pixi", "host"),
                        help="how a package's serve task is run: in its own pixi environment, or directly")
    parser.add_argument("--health-port", type=int, default=int(env("COLLAB_CONTROLLER_HEALTH_PORT", "0")),
                        help="serve /healthz and /readyz on this port; none when 0")
    parser.add_argument("--health-host", default=env("COLLAB_CONTROLLER_HEALTH_HOST", "127.0.0.1"))
    parser.add_argument("--wait", type=float, default=float(env("COLLAB_CONTROLLER_WAIT", "0")), metavar="SECONDS",
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
    for required in ("track", "work_dir"):
        if not getattr(args, required):
            parser.error(f"--{required.replace('_', '-')} is required")
    if not args.packages:
        parser.error("--packages is required")
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
        except refusal:
            if time.monotonic() >= deadline:
                raise
            starting["waiting"] = waiting_for
            _log.info("waiting: %s", waiting_for)
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
    track, keep_open = _patiently(lambda: open_track(args.track), SystemExit, deadline,
                                  "the Track's tables are not there yet", starting)
    try:
        still_held = _patiently(lambda: hold_id(args.track, args.id), IdTaken, deadline,
                                f"another controller holds the id {args.id!r}", starting)
    except IdTaken as exc:
        print(f"error: {exc}: each controller on a Track has its own --id", file=sys.stderr)
        return 1

    runner = LifecycleRunner(
        track=track, backend=args.backend, location=args.location, controller=args.id,
        location_settings={"packages": args.packages, "allow": args.allow, "work_dir": args.work_dir,
                           "environment": args.environment,
                           "interaction_timeout": args.interaction_timeout or None,
                           "deliver": deliver})
    controller = RunController(runner, poll_interval=args.poll_interval)
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
