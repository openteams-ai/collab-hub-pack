"""Installs (issue #105): pin a catalog Cog by digest on this hub, see what its install reached, remove it.

The API records intent and reads state; a run controller, over the same
store, moves the install (ADR-0002 D4, invariant 3): it fetches the
artifact, binds it through the Cog's own ``resolve`` over the hub's
inventory, and checks it on a worker with that binding delivered. The API
constructs no executor and never calls the controller.

- ``POST /v1/cogs/installs`` -- ask for an install: ``cog_id`` (and
  ``digest``, else the newest present version), or a pinned ``reference``
  from the catalog; at a development hub, ``package``, a package of the
  directory source. Answers 202 with the install, ``published``.
- ``GET /v1/cogs/installs``, ``GET /v1/cogs/installs/{id}`` -- the
  organization's installs and their state: ``published``, ``fetched``,
  ``bound``, ``invokable`` or ``uninstalled``; the single install also
  carries its events and, when a step failed, which (``failed_step``) and
  why (``failure``), so a Cog stuck before ``invokable`` is visible.
- ``DELETE /v1/cogs/installs/{id}`` -- ask for its removal: the controller
  tears down every runtime resource the install created and records it
  ``uninstalled``. Answers 202 with the install.

An install belongs to the organization that asked for it; to any other it
is a 404. Mounted before the catalog's ``/v1/cogs/{cog_id}`` routes, whose
``cog_id`` is a path, so ``installs`` is never read as a Cog id.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..cogs.catalog import STATUS_INDEXED, CogArtifact, CogCatalogStore
from ..cogs.models import CogErrorResponse, client_reference
from ..dependencies import get_cog_catalog_store
from ..frames.auth import AuthContext, get_auth_context
from .cogs import DIGEST_PATTERN, get_registry_host
from .frames import error_response
from .runs import (  # the execution package reaches the API through the run router alone
    ArtifactReference,
    InstallExists,
    InstallRow,
    PullError,
    RunService,
    get_run_service,
    new_install_id,
)

router = APIRouter(prefix="/cogs/installs", tags=["cogs"])

UNAVAILABLE = {
    503: {
        "model": CogErrorResponse,
        "description": "This hub records no installs: it has no database and no runs.track_path.",
    }
}
NOT_FOUND = {404: {"model": CogErrorResponse, "description": "No such install of the caller's organization."}}

INSTALL_ID_PATTERN = r"^inst-[0-9a-f]{12}$"
STATES = ("published", "fetched", "bound", "invokable", "uninstalled")


class InstallRequest(BaseModel):
    """What to install: a catalog Cog by id (its newest version, or ``digest``), a pinned reference, or, at a
    development hub, a package of the directory source."""

    model_config = ConfigDict(extra="forbid")

    cog_id: str | None = Field(default=None, min_length=1, max_length=512)
    digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    reference: str | None = Field(default=None, min_length=1, max_length=1024)
    package: str | None = Field(default=None, min_length=1, max_length=257)

    @model_validator(mode="after")
    def _one_of(self) -> InstallRequest:
        named = [name for name in ("cog_id", "reference", "package") if getattr(self, name)]
        if len(named) != 1:
            raise ValueError("an install names one of cog_id, reference or package")
        if self.digest and not self.cog_id:
            raise ValueError("digest goes with cog_id")
        return self


class InstallEventStatus(BaseModel):
    type: str
    occurred_at: str
    payload: dict[str, Any]


class InstallStatus(BaseModel):
    id: str
    reference: str
    """The pinned reference the install runs, or the package name at a development hub."""
    digest: str
    cog_id: str | None
    state: Literal["published", "fetched", "bound", "invokable", "uninstalled"]
    """``published``: asked for, until a controller takes it; ``fetched``: the artifact read at its digest;
    ``bound``: its binding admitted; ``invokable``: its check passed, a run may name it."""
    invokable: bool
    failed_step: str | None = None
    """When a step failed: ``fetch``, ``bind``, ``materialize`` or ``check``. The install stays where it was."""
    failure: str | None = None
    binding_id: str | None = None
    binding: dict[str, Any] | None = None
    """The binding record the Cog's own ``resolve`` produced: satisfiers by reference, no credential."""
    lock_digest: str | None = None
    picked_up_by: str | None = None
    requested_by: str | None = None
    uninstall_requested_by: str | None = None
    created_at: str
    updated_at: str
    kind: str | None = None
    events: list[InstallEventStatus] | None = None


class InstallPage(BaseModel):
    items: list[InstallStatus]
    next_offset: int | None = None


def _status(row: InstallRow, events=None) -> InstallStatus:
    card = row.card or {}
    kind = card.get("kind")
    return InstallStatus(
        id=row.id,
        reference=row.reference,
        digest=row.digest,
        cog_id=row.cog_id,
        state=row.state,
        invokable=row.invokable,
        failed_step=row.failed_step,
        failure=row.failure,
        binding_id=row.binding_id,
        binding=dict(row.binding) if row.binding is not None else None,
        lock_digest=row.lock_digest,
        picked_up_by=row.controller,
        requested_by=row.requested_by,
        uninstall_requested_by=row.uninstall_requested_by,
        created_at=_when(row.created_at),
        updated_at=_when(row.updated_at),
        kind=kind if isinstance(kind, str) else None,
        events=None
        if events is None
        else [
            InstallEventStatus(type=e.event_type, occurred_at=_when(e.occurred_at), payload=dict(e.payload))
            for e in events
        ],
    )


def _when(value: datetime) -> str:
    return value.isoformat()


AuthDep = Annotated[AuthContext, Depends(get_auth_context)]
ServiceDep = Annotated[RunService, Depends(get_run_service)]
CatalogDep = Annotated[CogCatalogStore, Depends(get_cog_catalog_store)]
RegistryHostDep = Annotated[str | None, Depends(get_registry_host)]
InstallId = Annotated[str, Path(pattern=INSTALL_ID_PATTERN)]


def _not_found(install_id: str) -> JSONResponse:
    return error_response(status.HTTP_404_NOT_FOUND, "install_not_found", f"No install {install_id}")


def _location_key(row: CogArtifact) -> tuple:
    pushed = row.pushed_at.timestamp() if row.pushed_at else 0.0
    return (row.removed_at is not None, row.pushed_at is None, -pushed, row.source_id, row.repository)


def _from_catalog(
    body: InstallRequest, store: CogCatalogStore, registry_host: str | None
) -> tuple[CogArtifact, str] | JSONResponse:
    """The catalog row an install pins, and the reference it will pull: the hub's own when it serves pulls."""
    if body.reference is not None:
        try:
            wanted = ArtifactReference.parse(body.reference)
        except PullError as exc:
            return error_response(status.HTTP_422_UNPROCESSABLE_CONTENT, "reference_invalid", str(exc))
        rows = [row for row in store.locations(wanted.digest) if row.status == STATUS_INDEXED]
        if not rows:
            return error_response(
                status.HTTP_404_NOT_FOUND, "cog_version_not_found", f"The catalog holds no Cog at {wanted.digest}"
            )
        preferred = sorted(rows, key=_location_key)
        row = next((r for r in preferred if r.repository == wanted.repository), preferred[0])
    else:
        versions = [
            row for row in store.list_versions(body.cog_id, include_removed=False) if row.status == STATUS_INDEXED
        ]
        if body.digest is not None:
            versions = [row for row in versions if row.digest == body.digest]
        if not versions:
            code = "cog_version_not_found" if body.digest else "cog_not_found"
            return error_response(
                status.HTTP_404_NOT_FOUND,
                code,
                f"The catalog holds no {'such version of ' if body.digest else ''}{body.cog_id}",
            )
        row = sorted(versions, key=_location_key)[0]
    if not row.present:
        return error_response(
            status.HTTP_409_CONFLICT,
            "cog_version_removed",
            f"{row.digest} has been removed from every registry the catalog indexes",
        )
    return row, client_reference(row, registry_host)


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=InstallStatus,
    summary="Install a Cog by digest",
    responses=UNAVAILABLE,
)
def install_cog(
    body: InstallRequest, auth: AuthDep, service: ServiceDep, catalog: CatalogDep, registry_host: RegistryHostDep
) -> InstallStatus | JSONResponse:
    """Ask for a Cog to be installed on this hub: pinned by digest from the catalog, fetched, bound by its own
    `resolve` and checked by a run controller. Answers at once, `published`; `GET` shows what it reaches."""
    if service.installs is None:
        return error_response(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "installs_unavailable",
            "this hub records no installs: it has no database and no runs.track_path",
        )
    if body.package is not None:
        refusal = service.package_refusal(body.package)
        if refusal is not None:
            return error_response(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "cog_not_launchable", f"Cannot install {body.package}: {refusal}"
            )
        package = service.packages.resolve(body.package)
        row = InstallRow(
            id=new_install_id(),
            org_id=auth.org_id,
            reference=package.name,
            digest=package.digest,
            cog_id=package.name,
            card={"kind": "package", "ops": {"lifecycle": ["serve"]}},
            requested_by=auth.user,
        )
    else:
        found = _from_catalog(body, catalog, registry_host)
        if isinstance(found, JSONResponse):
            return found
        artifact, reference = found
        row = InstallRow(
            id=new_install_id(),
            org_id=auth.org_id,
            reference=reference,
            digest=artifact.digest,
            cog_id=artifact.cog_id,
            card=dict(artifact.card or {}),
            requested_by=auth.user,
        )
    try:
        created = service.installs.create(row)
    except InstallExists:
        existing = service.installs.find(auth.org_id, digest=row.digest)
        held = f"{existing.id} ({existing.state})" if existing else "another install"
        return error_response(
            status.HTTP_409_CONFLICT,
            "already_installed",
            f"{row.digest} is installed as {held}",
            {"install": existing.id if existing else None},
        )
    return _status(created)


@router.get("", response_model=InstallPage, summary="List installs", responses=UNAVAILABLE)
def list_installs(
    auth: AuthDep,
    service: ServiceDep,
    install_state: Annotated[str | None, Query(alias="state", description="Only installs in this state.")] = None,
    include_uninstalled: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> InstallPage | JSONResponse:
    """The caller's organization's installs, newest first; `state` filters, and uninstalled ones are left out
    unless asked for."""
    if service.installs is None:
        return InstallPage(items=[])
    if install_state is not None and install_state not in STATES:
        return error_response(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "validation_error", f"state is one of {', '.join(STATES)}"
        )
    rows = service.installs.list(
        org_id=auth.org_id,
        state=install_state,
        include_uninstalled=include_uninstalled or install_state == "uninstalled",
        limit=limit + 1,
        offset=offset,
    )
    return InstallPage(
        items=[_status(row) for row in rows[:limit]], next_offset=offset + limit if len(rows) > limit else None
    )


def _visible(service: RunService, install_id: str, auth: AuthContext) -> InstallRow | None:
    if service.installs is None:
        return None
    row = service.installs.get(install_id)
    return row if row is not None and row.org_id == auth.org_id else None


@router.get(
    "/{install_id}",
    response_model=InstallStatus,
    summary="One install, with its events",
    responses={**NOT_FOUND, **UNAVAILABLE},
)
def get_install(install_id: InstallId, auth: AuthDep, service: ServiceDep) -> InstallStatus | JSONResponse:
    """The install as it stands, with every move its controller recorded: what it reached, and what failed."""
    row = _visible(service, install_id, auth)
    if row is None:
        return _not_found(install_id)
    return _status(row, service.installs.events(install_id))


@router.delete(
    "/{install_id}",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=InstallStatus,
    summary="Uninstall a Cog",
    responses={**NOT_FOUND, **UNAVAILABLE},
)
def uninstall_cog(install_id: InstallId, auth: AuthDep, service: ServiceDep) -> InstallStatus | JSONResponse:
    """Ask for the install's removal: the controller removes every runtime resource it created and records it
    `uninstalled`. Answers 202 with the install as it stands."""
    row = _visible(service, install_id, auth)
    if row is None:
        return _not_found(install_id)
    if not row.live:
        return error_response(status.HTTP_409_CONFLICT, "already_uninstalled", f"Install {install_id} is uninstalled")
    asked = service.installs.request_uninstall(install_id, auth.user)
    return _status(asked if asked is not None else row)
