"""The run API: launch a Cog, see what is running and what happened, stop it.

The API records intent on the Track and reads a run's status from it; a run
controller, a separate process over the same Track, advances the runs (ADR-0002
D4). The API constructs no executor and never calls the controller.

- ``POST /v1/runs`` -- submit an Op: steps naming a Cog package, an entry
  point, an input and a Gate. A step whose ``cog`` is not a package the
  controller can launch is refused with 422, naming the packages it can.
- ``GET /v1/runs``, ``GET /v1/runs/{id}`` -- the caller's organization's runs,
  status and each step's state replayed from the Track. The single run also
  carries each completed step's output.
- ``GET /v1/runs/{id}/events`` -- the run's Track: a page of events after a
  sequence, or, asked for ``text/event-stream``, a live stream of them until
  the run ends, resumable from ``Last-Event-ID``.
- ``POST /v1/runs/{id}/cancel`` -- record a request to cancel; the controller
  tears the worker down and the run ends ``CANCELLED``. A run that has ended
  answers 409, naming its status.
- ``POST /v1/runs/{id}/turns``, ``GET /v1/runs/{id}/turns/{turn}`` -- talk to a
  Cog whose step holds a session: the API records the turn, the controller
  delivers it to the run's live worker and records the answer, which the client
  reads back. Every turn and its answer are on the Track.
- ``GET /v1/runs/launchable`` -- the Cog packages a step may name.

The Track is ``runs.track_path`` when set, a SQLite file for a hub without
Postgres, and the hub's own database otherwise; with neither, every route
answers 503. Runs are scoped to the organization that submitted them: another
organization's run is a 404, never a 403. Payloads, decisions and retry are
later phases of the plan.

The execution package is imported here and nowhere else in the API.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal

from collab_hub_execution import intents
from collab_hub_execution.gates import POLICIES, Gate
from collab_hub_execution.locations.packages import DirectoryPackageSource, PackageNotFound, PackageRefused
from collab_hub_execution.ops import OpDefinition, OpStep
from collab_hub_execution.track import PostgresTrackStore, SqliteTrackStore, TrackEvent, TrackStore, upgrade
from fastapi import APIRouter, Depends, Header, Path, Query, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..config import RunsConfig
from ..frames.auth import AuthContext, get_auth_context
from ..frames.db import PostgresPools
from .frames import error_response

router = APIRouter(prefix="/runs", tags=["runs"])

RUN_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"


class RunsUnavailable(RuntimeError):
    """The run API has no Track to record runs on: the routes answer 503."""


RUNS_UNAVAILABLE = "runs_unavailable"


class RunService:
    """What the run routes need: the Track, the packages that can be launched, and what runs them."""

    def __init__(self, track: TrackStore | None, packages: DirectoryPackageSource | None, *, backend: str,
                 location: str) -> None:
        self.track = track
        """``None`` on a hub with neither Postgres nor ``runs.track_path``: every route answers 503."""
        # Read incrementally: a page costs what changed, not all history.
        self.views = intents.RunViews(track) if track is not None else None
        self.packages = packages
        self.backend = backend
        self.location = location

    def launchable(self) -> tuple[str, ...]:
        return () if self.packages is None else self.packages.names()

    def refusal(self, cog: str) -> str | None:
        """Why the controller could not launch ``cog``; ``None`` when it can."""
        if self.packages is None:
            return "no package directory is configured"
        try:
            self.packages.resolve(cog)
        except PackageRefused as exc:
            return str(exc)
        except PackageNotFound as exc:
            # The source's own message names the directories it looked in, which a client has no use for.
            return str(exc).split(" under ")[0]
        return None


def build_run_service(config: RunsConfig, postgres_url: str | None, pools: PostgresPools) -> RunService:
    """The run service: on the hub's own database, else on ``runs.track_path``, else on none.

    Both at once is refused: a hub with a database keeps its runs there, never
    in a file of the pod's own.

    On Postgres the Track's tables come from the hub's migrations, as every
    other table of the hub does, so nothing is created here.
    """
    if config.track_path and postgres_url:
        # A pod-local SQLite file would win over the hub's database, and vanish with the pod.
        raise RuntimeError(
            "runs.track_path is for a hub without Postgres: this hub has a database (frames.postgres.url), "
            "which is its Track; unset COLLAB_HUB_API__RUNS__TRACK_PATH")
    track: TrackStore | None = None
    if config.track_path:
        SqliteTrackStore.ensure_schema(config.track_path)
        track = SqliteTrackStore(config.track_path)
    elif postgres_url:
        track = PostgresTrackStore(pools.database(postgres_url))
    packages = DirectoryPackageSource(config.packages, config.allow) if config.packages else None
    return RunService(track, packages, backend=config.backend, location=config.location)


def get_run_service(request: Request) -> RunService:
    service: RunService = request.app.state.run_service
    if service.track is None:
        raise RunsUnavailable("this hub records no runs: it has no database (frames.postgres.url) and no "
                              "runs.track_path")
    return service


class GateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    escalate: str = "error"
    approvers: list[str] = Field(default_factory=list)

    @field_validator("escalate")
    @classmethod
    def _a_policy(cls, value: str) -> str:
        if value not in POLICIES:
            raise ValueError(f"a Gate escalates one of {', '.join(POLICIES)}")
        return value


class StepRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    cog: str = Field(min_length=1, max_length=257)
    entry_point: str = Field(min_length=1, max_length=128)
    input: Any = None
    gate: GateRequest = Field(default_factory=GateRequest)


class RunRequest(BaseModel):
    """An Op to run: its steps, in order."""

    model_config = ConfigDict(extra="forbid")

    steps: list[StepRequest] = Field(min_length=1, max_length=64)
    name: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$")
    """What to call the run in listings, e.g. ``hermes-on-claude``. A label: runs are found by their id."""

    @field_validator("steps")
    @classmethod
    def _distinct_names(cls, steps: list[StepRequest]) -> list[StepRequest]:
        names = [step.name for step in steps]
        if len(set(names)) != len(names):
            raise ValueError("each step of an Op has its own name")
        return steps


class StepStatus(BaseModel):
    name: str
    cog: str
    entry_point: str
    state: Literal["pending", "running", "completed", "failed", "waiting_at_gate", "cancelled", "interrupted",
                   "budget_exceeded", "rejected"]
    attempt: int | None = None
    error: str | None = None
    output: Any = None
    """What a completed step's Cog answered with. Only on ``GET /v1/runs/{id}``, and only when the
    Track holds it inline: a larger one is named by ``output_ref`` until the payload route exists."""
    output_ref: str | None = None


class RunStatus(BaseModel):
    id: str
    status: str
    """The run's state, as the Track tells it: SUBMITTED, RUNNING, WAITING_AT_GATE, COMPLETED, FAILED,
    REJECTED, CANCELLED, INTERRUPTED or BUDGET_EXCEEDED."""
    ended: bool
    steps: list[StepStatus]
    submitted_by: str | None
    name: str | None = None
    """What the run was called when it was launched, if anything."""
    submitted_by_name: str | None = None
    """Who submitted the run, for showing: their name or address when the sign-in carried one. Never
    compared with anything; ``submitted_by`` is the principal."""
    submitted_at: str
    updated_at: str
    cancel_requested_by: str | None = None
    picked_up_by: str | None = None
    """The run controller that picked the run up, which advances it; ``None`` until one does."""
    error: str | None = None
    reason: str | None = None
    backend: str
    """The durability backend the run is advanced on. ``none`` keeps nothing across a controller restart."""
    location: str
    """Where the run's workers are. A ``local`` worker shares the controller's host."""


class TurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=intents.MAX_TURN_TEXT)


class TurnStatus(BaseModel):
    turn: str
    text: str
    state: Literal["pending", "answered", "failed"]
    """``pending`` until the run's worker answers."""
    answer: str | None = None
    error: str | None = None
    asked_by: str | None = None


class Launchable(BaseModel):
    items: list[str]
    """The Cog packages a step's ``cog`` may name."""


class RunPage(BaseModel):
    items: list[RunStatus]
    next_offset: int | None = None


class RunEvent(BaseModel):
    sequence: int
    """The event's place on the Track: ``after`` takes it, and the stream names each event by it."""
    type: str
    occurred_at: str
    payload: dict[str, Any]


class RunEventPage(BaseModel):
    items: list[RunEvent]
    next_after: int | None = None
    """Pass as ``after`` for the events that follow, when there are more than this page holds."""


# What the run API serves of each event's payload, by event type: an allowlist, so a field a
# controller records later reaches clients only once it is named here. Where a local worker is on the
# controller's host (its pid, process group and log directory) is never among them: the controller
# needs it to reap the worker, and no client acts on it. An event type not listed is served with an
# empty payload.
_STEP = {"step", "attempt"}
_OUTCOME = {"problems", "binding"}
SERVED_FIELDS: dict[str, frozenset[str]] = {name: frozenset(fields) for name, fields in {
    "op_submitted": {"op", "submitted_by", "name"},
    "run_picked_up": {"controller"},
    "gate_escalated": _STEP | {"reason", "escalation", "envelope", "usage", "approvers", "gate"},
    "gate_decided": {"step", "outcome", "escalation", "actor", "value", "envelope_digest"},
    "completed": set(),
    "failed": {"step", "error", "reason", "worker_error", "revise_limit", "escalation", "actor", "value",
               "envelope_digest"} | _OUTCOME,
    "budget_exceeded": {"step", "dimension", "reason"},
    "cancelled": {"actor"},
    "interrupted": {"backend"},
    "retry_requested": {"from_status", "attempt", "budget_epoch", "controller"},
    "step_started": _STEP | {"cog", "digest"},
    "interaction_usage": _STEP | {"usage"},
    "step_completed": _STEP | {"cog", "digest", "usage", "frames", "escalation", "payload", "payload_ref"} | _OUTCOME,
    "step_failed": _STEP | {"key", "cog", "digest", "error", "message", "teardown_error"} | _OUTCOME,
    "worker_started": _STEP | {"instance", "location", "package", "run_token_sha256"},
    "worker_stopped": _STEP | {"instance", "reaped"},
    "materialized": {"cog", "digest"},
    "ready": {"cog"},
    "interaction_started": {"step", "entry_point"},
    "idle": {"step"},
    "teardown_started": {"step", "reason"},
    "teardown_failed": {"step", "error"},
    "cancel_requested": {"actor"},
    "turn_requested": {"turn", "text", "actor"},
    "turn_answered": {"turn", "text"},
    "turn_failed": {"turn", "error"},
    "decision_requested": {"escalation", "outcome", "actor", "findings"},
    "decision_refused": {"escalation", "error"},
}.items()}
STREAM_POLL_SECONDS = 0.5
STREAM_KEEPALIVE_SECONDS = 15.0


def _event(event: TrackEvent) -> RunEvent:
    event = upgrade(event)
    return RunEvent(sequence=event.sequence or 0, type=event.event_type, occurred_at=event.occurred_at.isoformat(),
                    payload={key: value for key, value in event.payload.items()
                             if key in SERVED_FIELDS.get(event.event_type, frozenset())})


def _status(view: intents.RunView, service: RunService, *, outputs: bool = False) -> RunStatus:
    entry_points = {step.name: step.entry_point for step in view.op.steps}
    return RunStatus(
        id=view.run_id, status=view.status, ended=view.state.ended,
        steps=[StepStatus(name=step.name, cog=step.cog, entry_point=entry_points[step.name], state=step.state,
                          attempt=step.attempt, error=step.error,
                          output=step.output if outputs else None, output_ref=step.output_ref if outputs else None)
               for step in view.steps],
        name=view.name, submitted_by=view.submitted_by.get("user"), submitted_by_name=view.submitted_by.get("name"),
        submitted_at=view.submitted_at.isoformat(), updated_at=view.updated_at.isoformat(),
        cancel_requested_by=view.cancel_requested_by, picked_up_by=view.controller, error=view.error,
        reason=view.reason, backend=service.backend, location=service.location,
    )


def _not_found(run_id: str) -> JSONResponse:
    return error_response(status.HTTP_404_NOT_FOUND, "run_not_found", f"No run {run_id}")


def _visible(service: RunService, run_id: str, auth: AuthContext) -> intents.RunView | None:
    """The run, when the caller's organization submitted it; a run that cannot be read is not shown."""
    try:
        view = service.views.view(run_id)
    except intents.RunUnreadable:
        return None
    if view is None or view.submitted_by.get("org_id") != auth.org_id:
        return None
    return view


AuthDep = Annotated[AuthContext, Depends(get_auth_context)]
ServiceDep = Annotated[RunService, Depends(get_run_service)]
RunId = Annotated[str, Path(pattern=RUN_ID_PATTERN)]


@router.post("", status_code=status.HTTP_201_CREATED, response_model=RunStatus)
def submit_run(body: RunRequest, auth: AuthDep, service: ServiceDep) -> RunStatus | JSONResponse:
    refused = {step.cog: reason for step in body.steps if (reason := service.refusal(step.cog)) is not None}
    if refused:
        return error_response(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "cog_not_launchable",
            f"Cannot launch {', '.join(sorted(refused))}; the Cogs this hub launches are: "
            f"{', '.join(service.launchable()) or 'none'}",
            {"refused": refused, "launchable": list(service.launchable())})
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    op = OpDefinition(run_id, tuple(
        OpStep(name=step.name, cog=step.cog, entry_point=step.entry_point, input=step.input,
               gate=Gate(escalate=step.gate.escalate, approvers=tuple(step.gate.approvers)))
        for step in body.steps))
    # `user` is the principal the run is scoped and checked by; `name` is only for showing who it was.
    by = {"user": auth.user, "org_id": auth.org_id, "workspace_id": auth.workspace_id}
    if auth.display.name or auth.display.email:
        by["name"] = auth.display.name or auth.display.email
    intents.submit(service.track, op, by=by, name=body.name)
    return _status(intents.describe(service.track, run_id), service)


@router.get("", response_model=RunPage)
def list_runs(
    auth: AuthDep, service: ServiceDep,
    run_status: Annotated[str | None, Query(alias="status", description="Only runs in this status.")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RunPage:
    views = service.views.views(org_id=auth.org_id, status=run_status.upper() if run_status else None)
    page = views[offset:offset + limit]
    return RunPage(items=[_status(view, service) for view in page],
                   next_offset=offset + limit if offset + limit < len(views) else None)


@router.get("/launchable", response_model=Launchable)
def launchable(auth: AuthDep, service: ServiceDep) -> Launchable:
    return Launchable(items=list(service.launchable()))


@router.get("/{run_id}", response_model=RunStatus)
def get_run(run_id: RunId, auth: AuthDep, service: ServiceDep) -> RunStatus | JSONResponse:
    view = _visible(service, run_id, auth)
    return _not_found(run_id) if view is None else _status(view, service, outputs=True)


@router.get("/{run_id}/events", response_model=RunEventPage,
            responses={200: {"content": {"text/event-stream": {}}}})
async def run_events(
    run_id: RunId, request: Request, auth: AuthDep, service: ServiceDep,
    after: Annotated[int, Query(ge=0, description="Only the events after this sequence.")] = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> RunEventPage | JSONResponse | StreamingResponse:
    """The run's events after ``after``: a page of them, or, for ``text/event-stream``, a live stream.

    The stream sends each event as it lands, named by its sequence, then an
    ``end`` event once the run has ended and nothing more is on its Track; a
    client that lost the connection reconnects with ``Last-Event-ID`` (or
    ``after``) and misses nothing and repeats nothing.
    """
    if await asyncio.to_thread(_visible, service, run_id, auth) is None:
        return _not_found(run_id)
    if "text/event-stream" in request.headers.get("accept", ""):
        start = int(last_event_id) if last_event_id and last_event_id.isdigit() else after
        return StreamingResponse(_stream(service, run_id, start, request), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
    # One more than the page holds: whether there is a next page, without reading the rest.
    events = await asyncio.to_thread(service.track.replay, run_id, after_sequence=after, limit=limit + 1)
    page = events[:limit]
    return RunEventPage(items=[_event(event) for event in page],
                        next_after=(page[-1].sequence if len(events) > limit else None))


async def _stream(service: RunService, run_id: str, after: int, request: Request) -> AsyncIterator[str]:
    """The run's events as they land, then `end`: one incremental read of the run per tick drives both,
    and the view it gives is of the events it gives, so a run that reads as ended has sent its last."""
    quiet = 0.0
    while not await request.is_disconnected():
        try:
            events, view = await asyncio.to_thread(service.views.after, run_id, after)
        except intents.RunUnreadable:
            events, view = (), None
        for event in events:
            served = _event(event)
            after = served.sequence
            yield f"id: {served.sequence}\nevent: {served.type}\ndata: {served.model_dump_json()}\n\n"
        if view is None or view.state.ended:
            yield f"event: end\ndata: {json.dumps({'status': view.status if view else None})}\n\n"
            return
        if events:
            quiet = 0.0
            continue  # read again at once: more may have landed while these were sent
        await asyncio.sleep(STREAM_POLL_SECONDS)
        quiet += STREAM_POLL_SECONDS
        if quiet >= STREAM_KEEPALIVE_SECONDS:
            quiet = 0.0
            yield ": keep-alive\n\n"  # a comment: proxies keep an idle stream open


@router.post("/{run_id}/cancel", status_code=status.HTTP_202_ACCEPTED, response_model=RunStatus)
def cancel_run(run_id: RunId, auth: AuthDep, service: ServiceDep) -> RunStatus | JSONResponse:
    if _visible(service, run_id, auth) is None:
        return _not_found(run_id)
    try:
        view = intents.request_cancel(service.track, run_id, actor=auth.user)
    except intents.RunEnded as exc:
        return error_response(status.HTTP_409_CONFLICT, "run_ended",
                              f"Run {run_id} cannot be cancelled: {exc.reason}")
    return _status(view, service)


def _turn(view: intents.TurnView) -> TurnStatus:
    return TurnStatus(turn=view.turn, text=view.text, state=view.state, answer=view.answer, error=view.error,
                      asked_by=view.actor)


@router.post("/{run_id}/turns", status_code=status.HTTP_202_ACCEPTED, response_model=TurnStatus)
def ask_turn(run_id: RunId, body: TurnRequest, auth: AuthDep, service: ServiceDep) -> TurnStatus | JSONResponse:
    if _visible(service, run_id, auth) is None:
        return _not_found(run_id)
    try:
        view = intents.request_turn(service.track, run_id, text=body.text, actor=auth.user)
    except intents.RunEnded as exc:
        return error_response(status.HTTP_409_CONFLICT, "run_ended", f"Run {run_id} takes no turns: {exc.reason}")
    except ValueError as exc:
        # The limit is in bytes; the request model bounds characters, which a non-ASCII text can pass.
        return error_response(status.HTTP_422_UNPROCESSABLE_CONTENT, "turn_too_long", str(exc))
    return _turn(view)


@router.get("/{run_id}/turns/{turn}", response_model=TurnStatus)
def get_turn(run_id: RunId, turn: Annotated[str, Path(pattern=r"^[0-9a-f]{12}$")], auth: AuthDep,
             service: ServiceDep) -> TurnStatus | JSONResponse:
    if _visible(service, run_id, auth) is None:
        return _not_found(run_id)
    view = service.views.turns(run_id).get(turn)
    if view is None:
        return error_response(status.HTTP_404_NOT_FOUND, "turn_not_found", f"No turn {turn} in run {run_id}")
    return _turn(view)
