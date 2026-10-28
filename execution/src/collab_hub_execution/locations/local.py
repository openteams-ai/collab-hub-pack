"""The ``local`` agent location: a Cog worker as a process on the controller's host.

``LocalProcessCogExecutor`` materializes a worker by running the Cog package's
declared ``serve`` task, in the package's own pixi environment, bound to a
loopback port the executor chooses. The worker is started through the launcher
(``launcher.py``), which kills the worker's process group when the controller
lets go of it or dies. The executor holds no lifecycle logic: it brings a
worker up, says where it is, and tears it down.

What a worker gets from the controller is its environment and nothing else: a
short allowlist of the controller's variables that a process needs to run at
all, the seam's variables (where to listen, its Cog, its run, its run token),
and what the binding delivers. Its output goes to a directory per run, never to
the Track.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import httpx

from .. import run_tokens
from ..kubernetes import TurnRefused, _KubernetesWorker
from .packages import CogPackage, DirectoryPackageSource

LOOPBACK = "127.0.0.1"
HOST_ENV, PORT_ENV, COG_ENV, RUN_ENV = "COLLAB_COG_HOST", "COLLAB_COG_PORT", "COLLAB_COG_ID", "COLLAB_RUN_ID"

# What a worker inherits from the controller: enough to find and run programs, and nothing that
# carries the controller's configuration or credentials.
INHERITED = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TZ", "PIXI_HOME", "PIXI_CACHE_DIR", "XDG_CACHE_HOME")

ENVIRONMENTS = ("pixi", "host")


__all__ = ["LocalProcessCogExecutor", "TurnRefused", "WorkerStartFailed"]


class WorkerStartFailed(RuntimeError):
    """A local worker that did not come up: it could not be started, exited, or never became ready."""


def _segment(value: str) -> str:
    """A run id or an instance as one path segment: a readable prefix, and a digest of the whole id.

    Ids are arbitrary, so the prefix alone would let two of them share a
    directory (``a/b`` and ``a_b``) and a long one exceed a file name. The
    digest keeps distinct ids apart, and the segment bounded.
    """
    prefix = re.sub(r"[^A-Za-z0-9._-]", "_", value)[:40].strip(".") or "_"
    return f"{prefix}-{hashlib.sha256(value.encode()).hexdigest()[:16]}"


Delivery = Callable[[str, str, str], Mapping[str, str]]
"""What the binding delivers to one worker: called with the Cog, the run and the instance."""


def _runs(pid: int, run_id: str) -> bool:
    """Whether the process ``pid`` is a worker of ``run_id``: its environment names the run.

    Read from ``/proc`` on Linux, and through ``ps`` elsewhere (macOS shows the
    environment of one's own processes); ``False`` when neither can tell.
    """
    wanted = f"{RUN_ENV}={run_id}"
    try:
        return wanted.encode() in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except FileNotFoundError:
        if Path("/proc/self/environ").exists():
            return False  # Linux, and the process is gone
    except OSError:
        return False
    try:
        shown = subprocess.run(["ps", "-wwE", "-o", "command=", "-p", str(pid)],  # noqa: S603, S607
                               capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return wanted in shown.split()


class _LocalWorker(_KubernetesWorker):
    """A worker process on this host, reached over loopback through the seam."""

    def __init__(self, cog: str, name: str, url: str, http: Any, *, launcher: subprocess.Popen,
                 details: Mapping[str, Any], run_token: str) -> None:
        super().__init__(cog, name, url, http)
        self.launcher = launcher
        self.details = dict(details)
        """Where the worker is, for the Track: its package, pid, process group and logs. No secret."""
        self._run_token = run_token  # held in memory for as long as the worker is up, and nowhere else
        self.run_token_digest = run_tokens.digest(run_token)
        """The sha256 of the worker's run token: what the Track records of it."""

    def _post_with_retry(self, url: str, payload: Any, **_: Any) -> Any:
        # The worker answered /healthz before it was handed over, so it is listening: one request,
        # and a worker that has gone since fails the step at once.
        return self.http.post(url, json=payload, headers={"Authorization": f"Bearer {self._run_token}"})

    def turn(self, turn: str, text: str) -> str:
        """One turn of a session the worker holds: ``POST /turn``, answered with ``{"text": ...}``."""
        response = self.http.post(f"{self.url}/turn", json={"turn": turn, "text": text},
                                  headers={"Authorization": f"Bearer {self._run_token}"})
        if response.status_code != 200:
            raise TurnRefused(f"the worker answered HTTP {response.status_code}"
                              + (": it holds no session" if response.status_code == 404 else ""),
                              status=response.status_code)
        try:
            answer = response.json()["text"]
        except (ValueError, KeyError, TypeError):
            answer = None
        if not isinstance(answer, str):
            raise TurnRefused("the worker's answer to a turn is not {\"text\": ...}")
        return answer


class LocalProcessCogExecutor:
    """Runs Cog packages from a directory source as processes on the controller's host.

    ``environment`` is how the ``serve`` task is run: ``pixi`` (the default) runs
    it in the package's own pixi environment, which is what isolates a Cog's
    dependencies from the controller's and from other Cogs'; ``host`` runs the
    task's command directly, with nothing installed for it, and is for testing
    the executor itself where pixi is not available.

    ``deliver`` is what the binding delivers to a worker — a model endpoint, a
    key the controller resolved from an ``auth_ref``. It is called once per
    materialization, with the Cog, the run and the instance, and what it
    returns enters that one child's environment only: never another worker's,
    never the Track, never a file. The executor keeps none of it.
    """

    location = "local"

    def __init__(
        self,
        *,
        source: DirectoryPackageSource,
        work_dir: str | Path,
        environment: str = "pixi",
        pixi: str = "pixi",
        deliver: Delivery | None = None,
        ready_timeout: float = 120.0,
        poll_interval: float = 0.1,
        interaction_timeout: float | None = 60.0,
        grace: float = 3.0,
    ) -> None:
        if environment not in ENVIRONMENTS:
            raise ValueError(f"unknown worker environment {environment!r}; it is one of {', '.join(ENVIRONMENTS)}")
        self.source = source
        self.work_dir = Path(work_dir)
        self.environment = environment
        self.pixi = pixi
        self.deliver = deliver
        self.ready_timeout = ready_timeout
        self.poll_interval = poll_interval
        self.grace = grace
        # trust_env=False: a proxy named in the controller's environment is never used to reach a
        # worker on loopback, so the run token and the payload go to the worker and nowhere else.
        self._http = httpx.Client(trust_env=False, timeout=httpx.Timeout(
            connect=5.0, read=interaction_timeout, write=interaction_timeout, pool=5.0))
        self._lock = threading.Lock()
        self._lock_start = threading.Lock()  # a port is chosen and its launcher recorded as one move
        self._ports: dict[int, subprocess.Popen] = {}  # the ports handed out, and the launcher each went to

    # --- materialize -------------------------------------------------------------------------

    def _command(self, package: CogPackage) -> list[str]:
        if self.environment == "host":
            return shlex.split(package.serve)
        pixi = shutil.which(self.pixi)
        if pixi is None:
            raise WorkerStartFailed(
                f"pixi is not on PATH: the local location runs a Cog in its own pixi environment "
                f"(https://pixi.sh); looked for {self.pixi!r}")
        # --locked: the environment is the one the lock describes, which is what the digest names;
        # pixi refuses to run rather than resolve again when the lock no longer matches the manifest.
        return [pixi, "run", "--locked", "--manifest-path", str(package.manifest), "serve"]

    def _port(self) -> int:
        """A free loopback port no worker of this executor was handed: chosen here, never by the Cog."""
        for _ in range(64):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind((LOOPBACK, 0))
                port = probe.getsockname()[1]
            with self._lock:
                self._ports = {p: launcher for p, launcher in self._ports.items() if launcher.poll() is None}
                if port not in self._ports:
                    return port
        raise WorkerStartFailed("no free loopback port for a worker")

    def _environment(self, package: CogPackage, run_id: str, instance: str, port: int,
                     token: str) -> dict[str, str]:
        env = {name: os.environ[name] for name in INHERITED if name in os.environ}
        if self.deliver is not None:
            env.update(self.deliver(package.name, run_id, instance))
        env.update({HOST_ENV: LOOPBACK, PORT_ENV: str(port), COG_ENV: package.name, RUN_ENV: run_id,
                    run_tokens.RUN_TOKEN_ENV: token})
        return env

    def materialize(self, cog: str, run_id: str, instance: str = "") -> _LocalWorker:
        package = self.source.resolve(cog)
        command = self._command(package)
        logs = self.work_dir / _segment(run_id) / _segment(instance or cog)
        logs.mkdir(parents=True, exist_ok=True)
        token = run_tokens.mint()
        with self._lock_start:
            port = self._port()
            launcher = self._launch(package, command, logs, run_id, instance, port, token)
            with self._lock:
                self._ports[port] = launcher
        return self._hand_over(cog, run_id, instance, package, logs, port, token, launcher)

    def _launch(self, package: CogPackage, command: list[str], logs: Path, run_id: str, instance: str,
                port: int, token: str) -> subprocess.Popen:
        with open(logs / "launcher.log", "ab") as launcher_log:
            return subprocess.Popen(  # noqa: S603 - this package's own launcher, with the Cog's serve command
                [sys.executable, "-m", "collab_hub_execution.locations.launcher",
                 "--stdout", str(logs / "stdout.log"), "--stderr", str(logs / "stderr.log"),
                 "--cwd", str(package.directory), "--grace", str(self.grace), "--", *command],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=launcher_log,
                env=self._environment(package, run_id, instance, port, token), start_new_session=True,
            )

    def _hand_over(self, cog: str, run_id: str, instance: str, package: CogPackage, logs: Path, port: int,
                   token: str, launcher: subprocess.Popen) -> _LocalWorker:
        worker = None
        try:
            started = json.loads(launcher.stdout.readline() or b"{}")
            if "pid" not in started:
                raise WorkerStartFailed(f"the worker of {cog!r} could not be started: {started.get('error', 'no pid')}")
            url = f"http://{LOOPBACK}:{port}"
            details = {
                "location": "local", "package": {"name": package.name, "digest": package.digest},
                "pid": started["pid"], "pgid": started["pgid"], "logs": str(logs),
            }
            worker = _LocalWorker(cog, f"{run_id}:{instance}", url, self._http, launcher=launcher, details=details,
                                  run_token=token)
            self._wait_ready(worker, logs)
            return worker
        except BaseException:
            self._stop(launcher, worker.details.get("pgid") if worker is not None else None)
            raise

    def _wait_ready(self, worker: _LocalWorker, logs: Path) -> None:
        deadline = time.monotonic() + self.ready_timeout
        while time.monotonic() < deadline:
            if worker.launcher.poll() is not None:
                raise WorkerStartFailed(f"the worker of {worker.cog!r} exited before it was ready"
                                        f"{self._tail(logs / 'stderr.log')}")
            try:
                if self._http.get(f"{worker.url}/healthz", timeout=2.0).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(self.poll_interval)
        raise WorkerStartFailed(f"the worker of {worker.cog!r} was not ready within {self.ready_timeout:g} seconds"
                                f"{self._tail(logs / 'stderr.log')}")

    @staticmethod
    def _tail(path: Path, lines: int = 5) -> str:
        try:
            text = path.read_text(errors="replace").strip().splitlines()[-lines:]
        except OSError:
            return ""
        return ": " + " | ".join(text) if text else ""

    # --- reaping what a stopped controller left -----------------------------------------------

    def reap(self, started: Mapping[str, Any], run_id: str) -> bool:
        """Kill a worker a stopped controller left alive, by the process group ``worker_started`` recorded.

        The launcher kills its worker when the controller dies, so a survivor is
        one whose launcher died with the controller. It is killed only if the
        recorded pid still leads the recorded group and is still this run's
        worker — its environment names the run — so a pid the system handed to
        another process since is left alone. Returns whether a worker was killed.
        """
        pid, pgid = started.get("pid"), started.get("pgid")
        if started.get("location") != "local" or not isinstance(pid, int) or not isinstance(pgid, int):
            return False
        try:
            if os.getpgid(pid) != pgid:
                return False
        except ProcessLookupError:
            return False  # gone already
        if not _runs(pid, run_id):
            return False
        try:
            os.killpg(pgid, signal.SIGTERM)
            deadline = time.monotonic() + self.grace
            while time.monotonic() < deadline:
                os.killpg(pgid, 0)
                time.sleep(0.05)
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            # The group has exited. macOS answers EPERM, not ESRCH, for a group left with only zombies,
            # to the probe and to SIGKILL alike.
            pass
        return True

    # --- teardown ----------------------------------------------------------------------------

    def teardown(self, worker: _LocalWorker) -> None:
        """Kill the worker's process tree and reap it. Tearing down twice is the same as once."""
        self._stop(worker.launcher, worker.details.get("pgid"))

    def _stop(self, launcher: subprocess.Popen, pgid: int | None) -> None:
        # Letting go of the pipe is the one signal: the launcher kills the worker's group and exits.
        for stream in (launcher.stdin, launcher.stdout):
            try:
                if stream is not None and not stream.closed:
                    stream.close()
            except OSError:
                pass
        try:
            launcher.wait(timeout=self.grace + 10.0)
        except subprocess.TimeoutExpired:
            # The launcher did not act: kill the worker's group here, then the launcher.
            if pgid is not None:
                try:
                    os.killpg(pgid, 9)
                except ProcessLookupError:
                    pass
            launcher.kill()
            launcher.wait(timeout=5.0)
