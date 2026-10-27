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
- ``GET /v1/runs/{id}/steps/{step}/payload`` -- a step's output, inline or
  kept by reference, under the run's own authorization; a failed step's error.
- ``POST /v1/runs/{id}/steps/{step}/decision`` -- approve, reject or send back
  the result a run waits at a Gate with, naming the escalation it answers; who
  may decide is the Gate's approvers, checked here.
- ``POST /v1/runs/{id}/retry`` -- record that an ended run is to run again; a
  controller takes it, resuming the attempt in flight after an interruption and
  starting a new one after a failure.

A step may name Frames: at submit each is checked to exist and be readable by
the caller, and its body snapshotted by reference on the Track, so the step,
retried or not, is given the Frame as it was at submit.

The Track is ``runs.track_path`` when set, a SQLite file for a hub without
Postgres, and the hub's own database otherwise; with neither, every route
answers 503. Runs are scoped to the organization that submitted them: another
organization's run is a 404, never a 403. Listing reads the Track's index of
runs, one query however many runs there are.

The execution package is imported here and nowhere else in the API.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal

from collab_hub_execution import intents
from collab_hub_execution.gates import POLICIES, Gate
from collab_hub_execution.locations.packages import DirectoryPackageSource, PackageNotFound, PackageRefused
from collab_hub_execution.ops import FrameRef, OpDefinition, OpStep
from collab_hub_execution.track import PostgresTrackStore, SqliteTrackStore, TrackEvent, TrackStore, upgrade
from fastapi import APIRouter, Depends, Header, Path, Query, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..config import RunsConfig
from ..frames.auth import AuthContext, get_auth_context
from ..frames.db import PostgresPools
from ..frames.store import FrameNotFoundError
from .frames import ID_PATTERN as FRAME_ID_PATTERN
from .frames import StoreDep, error_response, require_readable_frame

router = APIRouter(prefix="/runs", tags=["runs"])
_log = logging.getLogger(__name__)

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

        self._indexed = False

    def ensure_index(self) -> None:
        """Derive the Track's index of runs once, for a Track written before it had one; the store keeps it after.

        A database that does not answer is not an error here: the hub starts
        without it, as it does for every other store, and the index is checked
        again on the first listing.
        """
        if self._indexed or self.track is None:
            return
        try:
            if not self.track.runs(limit=1) and self.track.run_ids():
                self.track.reindex()
            self._indexed = True
        except Exception as exc:  # noqa: BLE001 - the database answers 503 through the routes until it is back
            _log.warning("the Track's index of runs could not be checked: %s", exc)

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
    service = RunService(track, packages, backend=config.backend, location=config.location)
    service.ensure_index()  # a database that does not answer at start is tried again on the first listing
    return service


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


MAX_FRAMES_PER_STEP = 16


class StepRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    cog: str = Field(min_length=1, max_length=257)
    entry_point: str = Field(min_length=1, max_length=128)
    input: Any = None
    gate: GateRequest = Field(default_factory=GateRequest)
    frames: list[Annotated[str, Field(pattern=FRAME_ID_PATTERN)]] = Field(default_factory=list,
                                                                          max_length=MAX_FRAMES_PER_STEP)
    """Frames the step is grounded in, by id: snapshotted at submit and placed beside the input."""


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


class DecisionStatus(BaseModel):
    escalation: str
    outcome: str
    actor: str


class StepFrame(BaseModel):
    id: str
    name: str
    digest: str
    """sha256 of the snapshot's text: what the step was given, whatever the Frame says now."""


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
    escalation: str | None = None
    """The escalation a run waiting at a Gate waits on, which a decision must name."""
    approvers: list[str] = Field(default_factory=list)
    """Who may decide that escalation: the roles its Gate declared, or the default ones."""
    decision: DecisionStatus | None = None
    """A decision asked for on that escalation and not yet delivered by the controller."""
    retry_asked_by: str | None = None
    """Who asked for an ended run to be retried, until a controller takes it."""
    frames: dict[str, list[StepFrame]] = Field(default_factory=dict)
    """The Frames each step is grounded in, by step name, as snapshotted at submit."""


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    escalation: str = Field(min_length=1, max_length=128)
    """The escalation this decision answers, from the run's ``escalation``: a decision on another is stale."""
    outcome: Literal["approve", "reject", "send_back"]
    findings: list[Any] = Field(default_factory=list, max_length=64)
    """What to send the step back with; read by the Cog as its signal."""


class StepPayload(BaseModel):
    step: str
    state: str
    payload: Any = None
    """What the step's Cog answered with, when the step completed; read back by reference when it was large."""
    error: dict[str, Any] | None = None
    """``{code, message}`` when the step failed."""


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
    "retry_asked": {"actor"},
    "retry_refused": {"error"},
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
    decision = view.decision
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
        escalation=view.escalation, approvers=list(view.approvers),
        decision=DecisionStatus(escalation=str(decision.get("escalation")), outcome=str(decision.get("outcome")),
                                actor=str(decision.get("actor"))) if decision else None,
        retry_asked_by=view.retry_asked_by,
        frames={step.name: [StepFrame(id=frame.id, name=frame.name, digest=frame.digest) for frame in step.frames]
                for step in view.op.steps if step.frames},
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


def _snapshot_frames(body: RunRequest, run_id: str, auth: AuthContext, store: Any,
                     service: RunService) -> dict[str, FrameRef] | JSONResponse:
    """Each Frame the steps name, checked and snapshotted once: its body kept by reference on the Track.

    A Frame that does not exist, or that the caller cannot read, is a 404 naming
    it, as the Frames API answers: never a 403, which would say it exists.
    """
    snapshots: dict[str, FrameRef] = {}
    for step in body.steps:
        for frame_id in step.frames:
            if frame_id in snapshots:
                continue
            try:
                frame = require_readable_frame(store.get_frame(frame_id), auth)
            except FrameNotFoundError:
                return error_response(status.HTTP_404_NOT_FOUND, "frame_not_found",
                                      f"No Frame {frame_id}, named by step {step.name}",
                                      {"frame": frame_id, "step": step.name})
            digest = hashlib.sha256(frame.body.encode()).hexdigest()
            ref = f"frame:{run_id}:{frame_id}"
            service.track.put_payload(run_id, ref, json.dumps(
                {"id": frame_id, "name": frame.name, "digest": digest, "text": frame.body}))
            snapshots[frame_id] = FrameRef(id=frame_id, name=frame.name, digest=digest, ref=ref,
                                           uri=f"frame:{frame_id}")
    return snapshots


@router.post("", status_code=status.HTTP_201_CREATED, response_model=RunStatus)
def submit_run(body: RunRequest, auth: AuthDep, service: ServiceDep, store: StoreDep) -> RunStatus | JSONResponse:
    refused = {step.cog: reason for step in body.steps if (reason := service.refusal(step.cog)) is not None}
    if refused:
        return error_response(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "cog_not_launchable",
            f"Cannot launch {', '.join(sorted(refused))}; the Cogs this hub launches are: "
            f"{', '.join(service.launchable()) or 'none'}",
            {"refused": refused, "launchable": list(service.launchable())})
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    snapshots = _snapshot_frames(body, run_id, auth, store, service)
    if isinstance(snapshots, JSONResponse):
        return snapshots
    op = OpDefinition(run_id, tuple(
        OpStep(name=step.name, cog=step.cog, entry_point=step.entry_point, input=step.input,
               gate=Gate(escalate=step.gate.escalate, approvers=tuple(step.gate.approvers)),
               frames=tuple(snapshots[frame_id] for frame_id in step.frames))
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
    # One indexed query for the page, plus one read per run on it; one more row says whether there is a next page.
    service.ensure_index()
    rows = service.track.runs(org_id=auth.org_id, status=run_status.upper() if run_status else None,
                              limit=limit + 1, offset=offset)
    items = []
    for row in rows[:limit]:
        try:
            view = service.views.view(row.run_id)
        except intents.RunUnreadable:
            continue
        if view is not None:
            items.append(_status(view, service))
    return RunPage(items=items, next_offset=offset + limit if len(rows) > limit else None)


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


def _step_error(service: RunService, run_id: str, step: str) -> dict[str, Any] | None:
    """What the last ``step_failed`` of a step recorded: its code and its message."""
    failed = None
    for event in service.views.events(run_id):
        if event.event_type == "step_failed" and event.payload.get("step") == step:
            failed = {"code": event.payload.get("error"), "message": event.payload.get("message") or ""}
    return failed


@router.get("/{run_id}/steps/{step}/payload", response_model=StepPayload)
def step_payload(run_id: RunId, step: Annotated[str, Path(min_length=1, max_length=128)], auth: AuthDep,
                 service: ServiceDep) -> StepPayload | JSONResponse:
    """A completed step's output, or a failed step's error, under the run's own authorization."""
    view = _visible(service, run_id, auth)
    if view is None:
        return _not_found(run_id)
    found = next((s for s in view.steps if s.name == step), None)
    if found is None:
        return error_response(status.HTTP_404_NOT_FOUND, "step_not_found", f"Run {run_id} has no step {step}")
    if found.state == "completed":
        payload = found.output
        if found.output_ref:
            try:
                payload = service.track.get_payload(found.output_ref)
            except KeyError:
                return error_response(status.HTTP_404_NOT_FOUND, "payload_not_found",
                                      f"The output of step {step} ({found.output_ref}) is not on the Track")
        return StepPayload(step=step, state=found.state, payload=payload)
    if found.state == "failed":
        return StepPayload(step=step, state=found.state, error=_step_error(service, run_id, step))
    return error_response(status.HTTP_409_CONFLICT, "step_not_finished",
                          f"Step {step} of run {run_id} is {found.state}: it has no output yet", {"state": found.state})


def _may_decide(view: intents.RunView, auth: AuthContext) -> bool:
    """Whether the caller may decide the run's open escalation.

    The Gate's approvers are role names (decision 1: organization owners and
    platform operators by default); the caller's roles are the two authority
    axes the hub resolves. A hub that resolves no roles (claims-sourced auth,
    where ``org_role`` and ``platform_role`` are both absent) has nobody with a
    role: there, the run's submitter decides the Gates of their own run.
    """
    roles = {role for role in (auth.org_role, auth.platform_role) if role}
    if roles & set(view.approvers):
        return True
    return not roles and auth.org_role is None and auth.platform_role is None \
        and view.submitted_by.get("user") == auth.user


@router.post("/{run_id}/steps/{step}/decision", status_code=status.HTTP_202_ACCEPTED, response_model=RunStatus)
def decide_step(run_id: RunId, step: Annotated[str, Path(min_length=1, max_length=128)], body: DecisionRequest,
                auth: AuthDep, service: ServiceDep) -> RunStatus | JSONResponse:
    """Decide the escalation a run waits on: the controller delivers it, and the run moves."""
    view = _visible(service, run_id, auth)
    if view is None:
        return _not_found(run_id)
    if view.state.ended:
        return error_response(status.HTTP_409_CONFLICT, "run_ended", f"Run {run_id} has ended {view.status}")
    waiting = next((s for s in view.steps if s.state == "waiting_at_gate"), None)
    if view.escalation is None or waiting is None:
        return error_response(status.HTTP_409_CONFLICT, "run_not_waiting",
                              f"Run {run_id} is {view.status}, and waits on no decision")
    if waiting.name != step:
        return error_response(status.HTTP_409_CONFLICT, "step_not_waiting",
                              f"Step {step} is not the one waiting at a Gate; {waiting.name} is",
                              {"waiting": waiting.name, "escalation": view.escalation})
    if body.escalation != view.escalation:
        return error_response(status.HTTP_409_CONFLICT, "stale_decision",
                              f"Escalation {body.escalation} is not the one run {run_id} waits on; "
                              f"{view.escalation} is", {"escalation": view.escalation})
    if not _may_decide(view, auth):
        return error_response(status.HTTP_403_FORBIDDEN, "not_an_approver",
                              f"Deciding this Gate takes one of the roles {', '.join(view.approvers)}",
                              {"approvers": list(view.approvers)})
    pending = view.decision or {}
    if pending and (pending.get("escalation"), pending.get("outcome"), pending.get("actor")) == (
            body.escalation, body.outcome, auth.user):
        return _status(view, service)  # the same decision, asked again: already recorded, not a conflict
    try:
        view = intents.request_decision(service.track, run_id, escalation=body.escalation, outcome=body.outcome,
                                        actor=auth.user, findings=body.findings)
    except intents.RunEnded as exc:
        return error_response(status.HTTP_409_CONFLICT, "run_ended", f"Run {run_id} has ended: {exc.reason}")
    except intents.StaleDecision as exc:
        if "already waiting" in str(exc):
            return error_response(status.HTTP_409_CONFLICT, "decision_pending",
                                  f"A decision on run {run_id} is already waiting to be delivered",
                                  {"escalation": body.escalation})
        fresh = _visible(service, run_id, auth)
        return error_response(status.HTTP_409_CONFLICT, "stale_decision", str(exc),
                              {"escalation": fresh.escalation if fresh else None})
    return _status(view, service)


@router.post("/{run_id}/retry", status_code=status.HTTP_202_ACCEPTED, response_model=RunStatus)
def retry_run(run_id: RunId, auth: AuthDep, service: ServiceDep) -> RunStatus | JSONResponse:
    """Ask for an ended run to run again: a controller takes it from its first unfinished step.

    After an interruption the step in flight continues under its attempt and
    key, so a committed claim answers instead of the work running twice; after
    a failure the step runs again as a new attempt.
    """
    if _visible(service, run_id, auth) is None:
        return _not_found(run_id)
    try:
        view = intents.request_retry(service.track, run_id, actor=auth.user)
    except intents.NotRetryable as exc:
        return error_response(status.HTTP_409_CONFLICT, "run_not_retryable",
                              f"Run {run_id} cannot be retried: {exc.reason}", {"status": exc.state.name})
    return _status(view, service)
