"""The worker side of the seam, served once: ``/healthz``, ``/invoke``, and ``/turn`` while a session is open.

A Cog built on the SDK declares its entry points on a ``Worker`` and calls
``serve()``; the run controller starts that process with the package's ``serve``
task, tells it where to listen (``COLLAB_COG_HOST``, ``COLLAB_COG_PORT``) and
hands it the run token (``COLLAB_RUN_TOKEN``), which it presents on every
request. Each entry point is handed a started ``Harness`` — the Cog's adapter
with the binding, the step's Frames, cancellation and usage accounting around
it — and answers with a payload; the worker wraps it in the envelope.

A session entry point holds its ``/invoke`` open and answers turns, each one
an interaction with the harness, until a farewell or until the run is
terminated and this process with it. The worker knows nothing of where it
runs: a process on the controller's host and a pod serve the same seam.
"""

from __future__ import annotations

import hmac
import json
import os
import signal as signals
import sys
import threading
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import binding as bindings
from .adapter import HarnessAdapter
from .envelope import Cancelled, Problem, Stopwatch, WorkerError, envelope, status_for
from .limits import check_fits
from .task import Context, Frame, Interaction, Prompt, Signal, Task, Usage, frames_from

HOST_ENV, PORT_ENV, COG_ENV, RUN_ENV, TOKEN_ENV = (
    "COLLAB_COG_HOST", "COLLAB_COG_PORT", "COLLAB_COG_ID", "COLLAB_RUN_ID", "COLLAB_RUN_TOKEN")
FAREWELLS = ("bye", "/bye")
Authenticate = Callable[[Mapping[str, str]], bool]
"""The ``/invoke`` authentication hook: the request's headers in, whether it may proceed out."""


class Harness:
    """What an entry point talks to: the Cog's started adapter, with everything the seam puts around it."""

    def __init__(self, adapter: HarnessAdapter, context: Context, frames: tuple[Frame, ...], cancel: Signal) -> None:
        self.adapter = adapter
        self.context = context
        self.frames = frames
        """The Frames the step carries: placed beside the first prompt, and any prompt that asks for them."""
        self.cancel = cancel
        self.usage = Usage()
        self.raws: list[str] = []
        self.problems: list[Problem] = []
        self.interactions = 0
        self._lock = threading.Lock()

    @property
    def binding(self) -> bindings.Binding:
        return self.context.binding

    @property
    def name(self) -> str:
        return getattr(self.adapter, "name", "the harness")

    def alive(self) -> bool:
        return self.adapter.alive()

    def interact(self, prompt: str | Prompt, *, frames: tuple[Frame, ...] | None = None) -> Interaction:
        """One prompt to the harness; the step's Frames ride with the first one unless ``frames`` says otherwise."""
        if isinstance(prompt, str):
            prompt = Prompt(prompt, self.frames if frames is None and self.interactions == 0 else (frames or ()))
        elif frames is not None:
            prompt = Prompt(prompt.text, frames)
        check_fits(prompt, self.binding)
        with self._lock:
            self.interactions += 1
            answer = self.adapter.interact(prompt, self.context, self.cancel)
        self.usage.add(answer.usage)
        if answer.raw:
            self.raws.append(answer.raw)
        return answer

    def problem(self, check: str, detail: str, severity: str = "error") -> None:
        """A contract-check finding of the Cog's own, reported on the envelope for Guards and Gates."""
        self.problems.append(Problem(check, detail, severity))

    def raw(self) -> str | None:
        return "\n\n".join(self.raws) or None


EntryPoint = Callable[[Task, Harness], Any]
TurnAnswer = Callable[[str, Harness], str]


class _Session:
    def __init__(self, answer: TurnAnswer | None) -> None:
        self.opened, self.ended = threading.Event(), threading.Event()
        self.harness: Harness | None = None
        self.answer = answer
        """How this session answers a turn: the function its entry point registered, or the harness's answer."""
        self.turns = 0
        self.lock = threading.Lock()


class Worker:
    """A Cog worker: its entry points, the harness they use, and the seam served around them.

    ``harness`` makes the Cog's adapter from the ``Context`` the worker starts
    it with (the binding among it); ``None`` for a Cog whose entry points need
    no harness. ``authenticate`` replaces the run-token check on ``/invoke`` and
    ``/turn``; the default admits the bearer of ``COLLAB_RUN_TOKEN``.
    """

    def __init__(self, cog: str | None = None, version: str = "0.1.0", *,
                 harness: Callable[[Context], HarnessAdapter] | None = None,
                 environ: Mapping[str, str] | None = None, authenticate: Authenticate | None = None,
                 session_start_timeout: float = 300.0, turn_grace: float = 1.0,
                 farewells: tuple[str, ...] = FAREWELLS, work_dir: str | None = None) -> None:
        self.environ = os.environ if environ is None else environ
        self.cog = cog or self.environ.get(COG_ENV, "cog")
        self.version = version
        self.run_id = self.environ.get(RUN_ENV, "")
        self.make_harness = harness
        self.authenticate = authenticate or self._run_token_check
        self.session_start_timeout = session_start_timeout
        """How long a turn waits for a session that is opening: a harness's first start can take a while."""
        self.turn_grace = turn_grace
        """How long a turn waits for a session to begin at all, before it is refused."""
        self._session_began = threading.Event()
        self.farewells = tuple(word.lower() for word in farewells)
        self.work_dir = work_dir
        self._entry_points: dict[str, EntryPoint] = {}
        self._sessions: dict[str, TurnAnswer | None] = {}
        self._session: _Session | None = None
        self._session_lock = threading.Lock()
        self._live: set[Harness] = set()
        self._live_lock = threading.Lock()
        self._binding: bindings.Binding | None = None
        self.stopping = Signal()

    # --- declaring entry points ---------------------------------------------------------------

    def task(self, name: str) -> Callable[[EntryPoint], EntryPoint]:
        """Register ``fn(task, harness) -> payload`` as the usage entry point ``name``.

        The harness is started before the call and closed after it. Returning
        an ``Interaction`` answers ``{"answer": text}``; raising ``WorkerError``
        fails the step with its code.
        """
        def register(fn: EntryPoint) -> EntryPoint:
            self._entry_points[name] = fn
            return fn
        return register

    def session(self, name: str = "session", answer: TurnAnswer | None = None) -> None:
        """Register an entry point that holds a session: each turn is one interaction with the harness.

        ``answer(text, harness) -> str`` replaces the default, which prompts the
        harness with the turn's text and answers with what it said. A farewell
        ends the session and completes the step with ``{"turns": n}``.
        """
        self._sessions[name] = answer

    @property
    def entry_points(self) -> tuple[str, ...]:
        return tuple(self._entry_points) + tuple(self._sessions)

    # --- the binding and the harness --------------------------------------------------------

    def binding(self) -> bindings.Binding:
        if self._binding is None:
            self._binding = bindings.load(self.environ)
        return self._binding

    def context(self) -> Context:
        return Context(binding=self.binding(), cog=self.cog, run_id=self.run_id, work_dir=self.work_dir)

    def _start_harness(self, frames: tuple[Frame, ...]) -> Harness:
        if self.make_harness is None:
            raise WorkerError("invalid-input", f"{self.cog} declares no harness for its entry points to use")
        context = self.context()
        adapter = self.make_harness(context)
        harness = Harness(adapter, context, frames, Signal())
        with self._live_lock:
            self._live.add(harness)  # from here on, a stop closes it, even while it is starting
        try:
            if harness.cancel.is_set():
                raise Cancelled()
            adapter.start(context)
            if harness.cancel.is_set():  # stopped while it was starting
                raise Cancelled()
        except BaseException:
            self._close(harness)
            raise
        return harness

    def _close(self, harness: Harness) -> None:
        harness.cancel.set()
        try:
            harness.adapter.cancel()
        except Exception:  # noqa: BLE001 - closing is best effort, and the adapter may be gone
            pass
        try:
            harness.adapter.close()
        finally:
            with self._live_lock:
                self._live.discard(harness)

    # --- invoking -----------------------------------------------------------------------------

    def invoke(self, entry_point: Any, input: Any = None, idempotency_key: Any = None, frames: Any = None,
               signal: Any = None) -> tuple[int, dict[str, Any]]:
        """``POST /invoke`` in process: the HTTP status and the envelope."""
        clock = Stopwatch()
        harness: Harness | None = None
        try:
            task = Task(entry_point=str(entry_point or ""), input=input, idempotency_key=idempotency_key,
                        frames=frames_from(frames), signal=signal)
            if task.entry_point in self._sessions:
                return self._hold_session(task, clock)
            fn = self._entry_points.get(task.entry_point)
            if fn is None:
                raise WorkerError("invalid-input", f"{self.cog} has no entry point {task.entry_point!r}: "
                                                   f"it has {', '.join(self.entry_points) or 'none'}")
            if self.make_harness is not None:
                harness = self._start_harness(task.frames)
            payload = fn(task, harness)  # type: ignore[arg-type]
            if isinstance(payload, Interaction):
                payload = {"answer": payload.text}
            return 200, self._envelope(task.entry_point, harness, clock, payload=payload)
        except WorkerError as error:
            return status_for(error), self._envelope(entry_point, harness, clock, error=error)
        except Cancelled:
            error = WorkerError("model-call-failed", "the interaction was cancelled before it answered")
            return status_for(error), self._envelope(entry_point, harness, clock, error=error)
        except Exception as exc:  # noqa: BLE001 - the seam's contract: every /invoke is answered with an envelope
            error = self._unexpected(exc)
            return status_for(error), self._envelope(entry_point, harness, clock, error=error)
        finally:
            if harness is not None:
                self._close(harness)

    @staticmethod
    def _unexpected(exc: BaseException) -> WorkerError:
        """What an exception that is neither a WorkerError nor a cancellation becomes: a failed call, named."""
        return WorkerError("model-call-failed", f"{type(exc).__name__}: {exc}"[:2000])

    def _envelope(self, entry_point: Any, harness: Harness | None, clock: Stopwatch, *, payload: Any = None,
                  error: WorkerError | None = None) -> dict[str, Any]:
        binding = self._binding
        return envelope(self.cog, self.version, entry_point if isinstance(entry_point, str) else None,
                        payload=payload, error=error, raw=harness.raw() if harness else None,
                        problems=harness.problems if harness else (),
                        binding=binding.record() if binding is not None and binding.bound else None,
                        usage=harness.usage.report() if harness else None, timing=clock.timing())

    def _hold_session(self, task: Task, clock: Stopwatch) -> tuple[int, dict[str, Any]]:
        with self._session_lock:
            if self._session is not None and not self._session.ended.is_set():
                raise WorkerError("invalid-input", f"{self.cog} already holds a session; one at a time")
            session = self._session = _Session(self._sessions[task.entry_point])
            self._session_began.set()
        harness = None
        try:
            try:
                harness = session.harness = self._start_harness(task.frames)
            finally:
                session.opened.set()  # a turn waiting on the session is answered now: by it, or by its absence
            # Until a farewell, or until the harness is gone: a session whose harness can never answer
            # again ends, failed, rather than holding the run open. A terminated run stops this process.
            while not session.ended.wait(1):
                if not harness.alive() or self.stopping.is_set():
                    break
            died = not session.ended.is_set() and not harness.alive()
            session.ended.set()
            if died:
                code = getattr(getattr(harness.adapter, "process", None), "returncode", None)
                raise WorkerError("model-call-failed", f"{harness.name} exited (code {code}) during the session, "
                                                       f"after {session.turns} turns")
            return 200, self._envelope(task.entry_point, harness, clock, payload={"turns": session.turns})
        except (WorkerError, Cancelled) as failure:
            error = failure if isinstance(failure, WorkerError) else \
                WorkerError("model-call-failed", "the session was stopped before it opened")
            return status_for(error), self._envelope(task.entry_point, harness, clock, error=error)
        except Exception as exc:  # noqa: BLE001 - every /invoke is answered with an envelope
            error = self._unexpected(exc)
            return status_for(error), self._envelope(task.entry_point, harness, clock, error=error)
        finally:
            session.ended.set()  # however it ended, the worker holds no session now, and can open another
            if harness is not None:
                self._close(harness)

    def turn(self, text: Any) -> tuple[int, dict[str, Any]]:
        """``POST /turn`` in process: one turn of the session the worker holds."""
        # A turn may arrive before the session's /invoke has: it waits a moment for one to begin, and then
        # for the session to open, which a harness's first start can make slow.
        self._session_began.wait(self.turn_grace)
        session = self._session
        if session is None or not session.opened.wait(self.session_start_timeout) or session.ended.is_set():
            return 404, {"error": "no session is open"}
        harness = session.harness
        if harness is None:
            return 503, {"error": "the session could not be opened"}
        text = str(text if text is not None else "")
        if text.strip().lower() in self.farewells:
            session.ended.set()
            return 200, {"text": f"Bye! The {self.cog} session ends, and the run completes."}
        try:
            with session.lock:
                said = session.answer(text, harness) if session.answer is not None else harness.interact(text).text
                session.turns += 1  # a turn that was answered; one that failed is not counted
            return 200, {"text": said}
        except WorkerError as error:
            return 502, {"error": f"{error.code}: {error.detail}"[:2000]}
        except Cancelled:
            return 502, {"error": "the turn was cancelled"}

    # --- serving ------------------------------------------------------------------------------

    def _run_token_check(self, headers: Mapping[str, str]) -> bool:
        token = self.environ.get(TOKEN_ENV, "")
        presented = headers.get("Authorization", "").removeprefix("Bearer ")
        return bool(token) and hmac.compare_digest(presented.encode(), token.encode())

    def stop(self) -> None:
        """Leave nothing of any harness behind: what SIGTERM does, and a test calls."""
        self.stopping.set()
        session = self._session
        if session is not None:
            session.ended.set()
        with self._live_lock:
            live = list(self._live)
        for harness in live:
            self._close(harness)

    def server(self, host: str | None = None, port: int | None = None) -> ThreadingHTTPServer:
        """The HTTP server of the seam, bound and not yet serving: ``serve_forever()`` runs it."""
        worker = self
        host = host or self.environ.get(HOST_ENV, "127.0.0.1")
        port = int(self.environ[PORT_ENV]) if port is None else port

        class Handler(BaseHTTPRequestHandler):
            def _send(self, code: int, body: Any) -> None:
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/healthz":
                    self._send(200, {"ok": True, "cog": worker.cog})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                if self.path not in ("/invoke", "/turn"):
                    return self._send(404, {"error": "not found"})
                if not worker.authenticate(self.headers):
                    return self._send(401, {"error": "not the run token of this worker"})
                try:
                    request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                    if not isinstance(request, dict):
                        raise ValueError("not an object")
                except ValueError as exc:
                    error = WorkerError("invalid-input", f"the request body is not a JSON object: {exc}")
                    return self._send(status_for(error), worker._envelope(None, None, Stopwatch(), error=error))
                if self.path == "/invoke":
                    return self._send(*worker.invoke(request.get("entry_point"), request.get("input"),
                                                     request.get("idempotency_key"), request.get("frames"),
                                                     request.get("signal")))
                return self._send(*worker.turn(request.get("text")))

            def log_message(self, *args: Any) -> None:
                pass  # the access log is noise in a run's stderr

        return ThreadingHTTPServer((host, port), Handler)

    def serve(self) -> None:
        """Serve the seam where the controller said to, until the process is stopped."""
        def stop(*_: Any) -> None:
            self.stop()
            os._exit(0)

        signals.signal(signals.SIGTERM, stop)
        server = self.server()
        print(f"{self.cog}: serving run {self.run_id} on {server.server_address[0]}:{server.server_address[1]}",
              file=sys.stderr, flush=True)
        server.serve_forever()
