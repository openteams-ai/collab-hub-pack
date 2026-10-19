"""`make op OP=<name>`: run a fake Op at dev level 1 and print its Track; `make submit` hands it to a controller.

The Op is `dev/ops/<name>.yaml`; each step names a fake Cog under `dev/cogs/`,
a Cog package whose `serve.py` holds its `handle` function.

`make op` runs the Op itself, as the controller named `make-op`. Without a
location, `handle` answers in this process: no worker, no container, no
network. With `LOCATION=local` each step's worker is a real process: the
package's `serve` task, run in its own pixi environment through the local
executor, listening on a loopback port, and gone when the step ends or when this
host dies. Its output is under `dev/.local/runs/<run>/`. It starts the way a
controller does: a run a previous `make op` left unfinished — stopped mid-step
with Ctrl-C, or killed — is recorded `interrupted` before the new run starts,
and the runs of any other controller on the Track are left alone. One `make op`
at a time, since each holds the name `make-op`.

`make submit` launches the Op through the run API (`POST /v1/runs` on
`make api`), as any client does, and follows it until it ends or waits at a
Gate: a controller (`make controller`) picks it up. A budget is the runner's, so an Op that
declares one runs with `make op` only.

Either way the Track is the SQLite one in `dev/.local/`, or Postgres with
`TRACK=postgresql://...`, and the durability backend is `BACKEND` (`none` by
default, the only one built).
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import time
import uuid
from datetime import timedelta
from pathlib import Path

import yaml

from collab_hub_execution import (
    AGENT_LOCATIONS,
    Gate,
    InMemoryCogExecutor,
    LifecycleRunner,
    OpDefinition,
    OpStep,
    ResultEnvelope,
    RunBudget,
)
from collab_hub_execution.controller import IdTaken, hold_id, open_track

DEV = Path(__file__).resolve().parent


def _handler(cog: str):
    path = DEV / "cogs" / cog / "serve.py"
    if not path.exists():
        raise SystemExit(f"no fake Cog {cog!r}: expected {path.relative_to(DEV.parent)}")
    spec = importlib.util.spec_from_file_location(f"dev_cog_{cog.replace('-', '_')}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # In process the envelope is checked the way the seam checks one that arrived as JSON.
    return lambda entry_point, value, **feedback: ResultEnvelope.parse(module.handle(entry_point, value, **feedback))


def _op(name: str, run_id: str, in_process: bool) -> tuple[OpDefinition, RunBudget | None, dict]:
    path = DEV / "ops" / f"{name}.yaml"
    if not path.exists():
        known = sorted(p.stem for p in (DEV / "ops").glob("*.yaml"))
        raise SystemExit(f"no Op {name!r}: expected {path.relative_to(DEV.parent)}; the Ops are {', '.join(known)}")
    spec = yaml.safe_load(path.read_text())
    steps = tuple(
        OpStep(name=step["name"], cog=step["cog"], entry_point=step["entry_point"], input=step.get("input"),
               gate=Gate.from_dict(step.get("gate")))
        for step in spec["steps"]
    )
    budget = spec.get("budget")
    limits = None
    if budget:
        seconds = budget.get("max_seconds")
        limits = RunBudget(max_tokens=budget.get("max_tokens"), max_cost=budget.get("max_cost"),
                           max_duration=timedelta(seconds=seconds) if seconds else None)
    handlers = {step.cog: _handler(step.cog) for step in steps} if in_process else {}
    return OpDefinition(run_id, steps), limits, handlers


def _print_track(track, run_id: str) -> None:
    for event in track.replay(run_id):
        payload = event.payload
        detail = " ".join(
            f"{key}={payload[key]}"
            for key in ("step", "attempt", "error", "reason", "dimension", "actor", "backend", "location", "pid",
                        "controller", "reaped")
            if payload.get(key) not in (None, "")
        )
        print(f"  {event.sequence:>5}  {event.event_type:<20} {detail}".rstrip())


MAKE_OP = "make-op"
BY = {"user": "dev", "org_id": "dev"}


def _submit(api: str, op: OpDefinition, budget: RunBudget | None, timeout: float) -> int:
    """Launch the Op through the run API, as any client does, and follow it until it ends or waits at a Gate."""
    import httpx

    if budget is not None:
        raise SystemExit(f"{op.run_id.rsplit('-', 1)[0]} declares a budget, which is the runner's: run it with "
                         "`make op`")
    body = {"name": op.run_id.rsplit("-", 1)[0], "steps": [
        {"name": step.name, "cog": step.cog, "entry_point": step.entry_point, "input": step.input,
         "gate": {"escalate": step.gate.escalate, "approvers": list(step.gate.approvers)}}
        for step in op.steps]}
    with httpx.Client(base_url=api, timeout=30) as hub:
        try:
            answered = hub.post("/v1/runs", json=body)
        except httpx.HTTPError as exc:
            raise SystemExit(f"the run API at {api} cannot be reached ({exc}): is `make api` running?") from None
        if answered.status_code != 201:
            raise SystemExit(f"the run API refused it (HTTP {answered.status_code}): {answered.text[:500]}")
        run = answered.json()
        print(f"run {run['id']} submitted through {api}/v1/runs: waiting for a controller (make controller) "
              "to pick it up", flush=True)
        deadline = time.monotonic() + timeout
        while not (run["ended"] or run["status"] == "WAITING_AT_GATE"):
            if time.monotonic() > deadline:
                print(f"status: {run['status']} after {timeout:g} seconds"
                      + (": is a controller running on this Track?" if run["status"] == "SUBMITTED" else ""))
                return 1
            time.sleep(0.2)
            run = hub.get(f"/v1/runs/{run['id']}").raise_for_status().json()
        for event in hub.get(f"/v1/runs/{run['id']}/events").raise_for_status().json()["items"]:
            payload = event["payload"]
            detail = " ".join(f"{key}={payload[key]}" for key in ("step", "attempt", "error", "reason", "actor",
                                                                     "controller", "location", "reaped")
                              if payload.get(key) not in (None, ""))
            print(f"  {event['sequence']:>5}  {event['type']:<20} {detail}".rstrip())
    print(f"picked up by: {run['picked_up_by']}")
    print(f"status: {run['status']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("op", help="the Op to run: a file under dev/ops/, without .yaml")
    parser.add_argument("--backend", default=os.environ.get("BACKEND", "none"))
    parser.add_argument("--location", default=os.environ.get("LOCATION", ""), choices=("", *AGENT_LOCATIONS),
                        help="where each step's worker runs; without one, the fake Cogs answer in this process")
    parser.add_argument("--track", default=str(DEV / ".local" / "track.sqlite"),
                        help="a SQLite file, or a postgresql:// URL")
    parser.add_argument("--submit", action="store_true",
                        help="launch the Op through the run API for a controller to pick up, and follow it, "
                             "instead of running it here")
    parser.add_argument("--api", default=os.environ.get("API_URL", "http://127.0.0.1:8000"),
                        help="the hub whose run API --submit launches through")
    parser.add_argument("--timeout", type=float, default=120.0, help="how long --submit follows the run")
    args = parser.parse_args()

    run_id = f"{args.op}-{uuid.uuid4().hex[:8]}"
    op, budget, handlers = _op(args.op, run_id, in_process=not args.location and not args.submit)
    if args.submit:
        return _submit(args.api, op, budget, args.timeout)
    if not args.track.startswith(("postgresql://", "postgres://")):
        Path(args.track).parent.mkdir(parents=True, exist_ok=True)
    track, _keep_open = open_track(args.track)

    try:
        # Held for as long as this host runs: the hold lasts as long as the check it returns is kept.
        still_held = hold_id(args.track, MAKE_OP)
    except IdTaken:
        print("another `make op` is running on this Track: one at a time, since each is the controller "
              f"{MAKE_OP!r}", file=sys.stderr)
        return 1
    if args.location:
        track_dir = DEV / ".local" if args.track.startswith(("postgresql://", "postgres://")) else \
            Path(args.track).parent
        where = {"location": args.location, "location_settings": {
            "packages": [DEV / "cogs"], "work_dir": track_dir / "runs"}}
    else:
        where = {"executor": InMemoryCogExecutor(handlers)}
    runner = LifecycleRunner(track=track, budget=budget, backend=args.backend, controller=MAKE_OP, **where)
    for interrupted in runner.start():
        print(f"interrupted {interrupted}: a previous `make op` stopped before it ended", flush=True)

    print(f"run {run_id} on the {runner.backend.name!r} backend, workers "
          f"{'at ' + repr(args.location) if args.location else 'in process'}", flush=True)
    try:
        state = runner.submit(op)
    except KeyboardInterrupt:
        print(f"\nstopped mid-run: the next `make op` records {run_id} interrupted", file=sys.stderr)
        return 130
    _print_track(track, run_id)
    print(f"status: {state.name}")
    assert still_held(), "make op lost its hold on its name while it ran"
    escalation = runner.open_escalation(run_id)
    if escalation is not None:
        print(f"waiting at the Gate of step {escalation['step']!r}: {escalation['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
