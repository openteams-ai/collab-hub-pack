"""Organization membership for add-on services (issue #270).

Read-only, service-to-service routes under ``/v1/membership``. Add-on services
(for example usage control and a subscription manager) keep no organization
directory of their own; they ask the hub who belongs to an organization, in
which role, and how many members it has.

- ``GET /v1/membership/subjects/{subject}`` -- the organizations a subject
  belongs to and their role in each.
- ``GET /v1/membership/organizations/{org_id}`` -- one organization's members
  and owners, with a display name and email where the hub holds them, and the
  member count.
- ``GET /v1/membership/organizations`` -- every organization, a page at a
  time, for a caller that syncs them all.

Mounted only when ``frames.membership_api.client_ids`` names an add-on, and
only on a deployment whose organizations come from membership rows. Callers
authenticate as services (:mod:`..frames.service_callers`), never as people.

**Freshness.** Every response is read from the database when it is asked
for; nothing is cached on the hub side. A removal, a role change or a join
committed before a request is in that request's answer, so the only staleness
a caller sees is its own polling interval. ``observed_at`` is taken before
the read, so the answer is at least as new as it says.

**What is exposed** is exactly the fields of the response models below:
organization id and name, and per active member their subject, role, display
name and email. No frames, invitations, tokens, removed members or audit
data.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, Query, Request, status
from pydantic import BaseModel, Field

from ..frames import error_codes
from ..frames.orgs import OrgRoster, OrgStore
from ..frames.service_callers import ServiceCaller, get_service_caller
from .frames import error_response

MEMBERSHIP_API_PREFIX = "/v1/membership"

DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 500

router = APIRouter(prefix="/membership", tags=["membership"])

CallerDep = Annotated[ServiceCaller, Depends(get_service_caller)]


def membership_api_path(path: str) -> bool:
    """Whether *path* belongs to this API, segment-aware (``/v1/membershipx`` does not)."""

    return path == MEMBERSHIP_API_PREFIX or path.startswith(MEMBERSHIP_API_PREFIX + "/")


class OrganizationNotFoundError(Exception):
    """The requested organization id names no organization."""


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(OrganizationNotFoundError)
    async def organization_not_found_handler(_request: Request, _exc: OrganizationNotFoundError):
        return error_response(status.HTTP_404_NOT_FOUND, error_codes.ORGANIZATION_NOT_FOUND, "No such organization")


class SubjectOrganization(BaseModel):
    org_id: str
    role: Literal["owner", "member"]


class SubjectMemberships(BaseModel):
    subject: str
    organizations: list[SubjectOrganization] = Field(
        description="The organizations the subject is an active member of; empty when none. "
        "A hub login has at most one home organization today, so this holds zero or one entry."
    )
    observed_at: datetime


class Member(BaseModel):
    subject: str = Field(description="The member's OIDC subject.")
    role: Literal["owner", "member"]
    display_name: str | None = Field(description="Display only, never a principal; null when the hub holds none.")
    email: str | None = Field(description="Display/contact only, never a principal; null when the hub holds none.")


class Organization(BaseModel):
    org_id: str
    name: str | None
    member_count: int = Field(description="Active members, owners included.")
    members: list[Member]


class OrganizationRoster(Organization):
    observed_at: datetime


class OrganizationPage(BaseModel):
    organizations: list[Organization]
    next_cursor: str | None = Field(description="Pass as `cursor` for the next page; null on the last page.")
    observed_at: datetime


def _org_store(request: Request) -> OrgStore:
    return request.app.state.org_store


def _now() -> datetime:
    return datetime.now(UTC)


def _organization(roster: OrgRoster) -> dict:
    members = [
        Member(subject=m.user_id, role=m.role, display_name=m.display_name, email=m.email) for m in roster.members
    ]
    return {"org_id": roster.org_id, "name": roster.name, "member_count": len(members), "members": members}


@router.get(
    "/subjects/{subject}",
    response_model=SubjectMemberships,
    summary="The organizations a subject belongs to, and their role in each",
)
def subject_memberships(subject: str, request: Request, caller: CallerDep) -> SubjectMemberships:
    del caller
    observed_at = _now()
    membership = _org_store(request).get_membership(subject)
    organizations = []
    if membership is not None and membership.is_active:
        organizations.append(SubjectOrganization(org_id=membership.org_id, role=membership.role))
    return SubjectMemberships(subject=subject, organizations=organizations, observed_at=observed_at)


@router.get(
    "/organizations/{org_id}",
    response_model=OrganizationRoster,
    summary="An organization's members and owners",
)
def organization_roster(org_id: str, request: Request, caller: CallerDep) -> OrganizationRoster:
    del caller
    observed_at = _now()
    roster = _org_store(request).get_roster(org_id)
    if roster is None:
        raise OrganizationNotFoundError
    return OrganizationRoster(**_organization(roster), observed_at=observed_at)


@router.get(
    "/organizations",
    response_model=OrganizationPage,
    summary="Every organization with its members, a page at a time",
)
def list_organizations(
    request: Request,
    caller: CallerDep,
    cursor: Annotated[str | None, Query(min_length=1, description="`next_cursor` from the previous page.")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> OrganizationPage:
    del caller
    observed_at = _now()
    # One more than asked for: whether it comes back is whether there is a
    # next page, without a second query or a trailing empty page.
    rosters = _org_store(request).list_rosters(after=cursor, limit=limit + 1)
    next_cursor = rosters[limit - 1].org_id if len(rosters) > limit else None
    return OrganizationPage(
        organizations=[Organization(**_organization(r)) for r in rosters[:limit]],
        next_cursor=next_cursor,
        observed_at=observed_at,
    )
