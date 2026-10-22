from __future__ import annotations

import asyncio
import logging
import math
import re

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from collab_hub_api.config import ConnectorsConfig
from collab_hub_api.connectors.calendar_client import (
    CalendarSearchError,
    CalendarUpstreamError,
    GoogleCalendarClient,
)
from collab_hub_api.connectors.drive_client import DriveUpstreamError, GoogleDriveClient, UnsupportedDriveFileType
from collab_hub_api.connectors.github_client import (
    GitHubApiRequestError,
    GitHubClient,
    GitHubSearchError,
    GitHubUpstreamError,
)
from collab_hub_api.connectors.github_tokens import GitHubTokenProvider
from collab_hub_api.connectors.gmail_client import GmailClient, GmailSearchError, GmailUpstreamError
from collab_hub_api.connectors.google_tokens import (
    ConnectorNotConnected,
    ConnectorReconnectRequired,
    ConnectorTokenError,
    GoogleTokenProvider,
)
from collab_hub_api.connectors.models import (
    DRIVE_READONLY_SCOPE,
    GMAIL_READONLY_SCOPE,
    GOOGLE_CALENDAR_READONLY_SCOPE,
    NOTION_READONLY_CAPABILITIES,
    SLACK_READONLY_SCOPES,
    CalendarReadRequest,
    CalendarReadResponse,
    CalendarSearchRequest,
    CalendarSearchResponse,
    ConnectorSummary,
    DriveReadRequest,
    DriveReadResponse,
    DriveSearchRequest,
    DriveSearchResponse,
    GitHubApiGetRequest,
    GitHubApiGetResponse,
    GitHubFileReadRequest,
    GitHubFileReadResponse,
    GitHubItemReadRequest,
    GitHubItemReadResponse,
    GitHubProjectReadRequest,
    GitHubProjectReadResponse,
    GitHubProjectsListRequest,
    GitHubProjectsListResponse,
    GitHubReposListRequest,
    GitHubReposListResponse,
    GitHubSearchRequest,
    GitHubSearchResponse,
    GitHubStatus,
    GmailReadRequest,
    GmailReadResponse,
    GmailSearchRequest,
    GmailSearchResponse,
    GmailStatus,
    GoogleCalendarStatus,
    GoogleDriveStatus,
    NotionDatabaseQueryRequest,
    NotionDatabaseQueryResponse,
    NotionPageReadRequest,
    NotionPageReadResponse,
    NotionSearchRequest,
    NotionSearchResponse,
    NotionStatus,
    SlackChannelsResponse,
    SlackDmsResponse,
    SlackReadRequest,
    SlackReadResponse,
    SlackSearchRequest,
    SlackSearchResponse,
    SlackStatus,
    SlackThreadReadRequest,
    SlackThreadReadResponse,
)
from collab_hub_api.connectors.notion_client import (
    NotionClient,
    NotionSearchError,
    NotionUpstreamError,
    notion_time_bounds,
)
from collab_hub_api.connectors.notion_tokens import NotionTokenProvider
from collab_hub_api.connectors.slack_client import (
    SLACK_TOKEN_INVALID_ERRORS,
    SlackClient,
    SlackConversationNotAllowed,
    SlackUpstreamError,
)
from collab_hub_api.connectors.slack_tokens import SlackTokenProvider
from collab_hub_api.connectors.validation import has_control_or_nonprintable
from collab_hub_api.frames.auth import get_auth_context
from collab_hub_api.frames.connector_state import apply_disabled

router = APIRouter(prefix="/connectors", tags=["connectors"])
logger = logging.getLogger("frames_server.connectors")


def get_connectors_config(request: Request) -> ConnectorsConfig:
    """This deployment's connector configuration, minus anything switched off.

    The switch is applied here rather than in each handler because every
    connector route already takes this dependency: one place to enforce, and no
    route that can forget. A disabled connector arrives with its credentials
    blanked, so it reads as unconfigured to code that already handles that --
    see :mod:`..frames.connector_state`.

    A store that cannot answer disables nothing. Failing closed here would take
    every connector down over a database blip, which is a far worse outcome
    than briefly honouring a switch late.
    """

    config = request.app.state.connectors_config
    store = getattr(request.app.state, "connector_store", None)
    if store is None:
        return config
    try:
        disabled = store.disabled()
    except Exception:
        logger.warning("connector_state_unavailable")
        return config
    return apply_disabled(config, disabled)


@router.get("", response_model=list[ConnectorSummary])
async def list_connectors(
    request: Request,
    _auth=Depends(get_auth_context),
    config: ConnectorsConfig = Depends(get_connectors_config),
) -> list[ConnectorSummary]:
    return [
        await _google_drive_status(request, config),
        await _gmail_status(request, config),
        await _google_calendar_status(request, config),
        await _slack_status(request, config),
        await _github_status(request, config),
        await _notion_status(request, config),
    ]


@router.get("/google-drive/status", response_model=GoogleDriveStatus)
async def google_drive_status(
    request: Request,
    _auth=Depends(get_auth_context),
    config: ConnectorsConfig = Depends(get_connectors_config),
) -> GoogleDriveStatus:
    return await _google_drive_status(request, config)


@router.post("/google-drive/search", response_model=DriveSearchResponse)
async def search_google_drive(
    body: DriveSearchRequest,
    request: Request,
    _auth=Depends(get_auth_context),
    config: ConnectorsConfig = Depends(get_connectors_config),
) -> DriveSearchResponse:
    client = await _drive_client(request, config)
    try:
        files = await client.search(
            query=body.query,
            limit=body.limit,
            modified_after=body.modified_after,
            mime_types=body.mime_types,
        )
    except DriveUpstreamError as exc:
        logger.info(
            "google_drive_search_upstream_error",
            extra={"operation": exc.operation, "status_code": exc.status_code},
        )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Google Drive search failed") from exc
    return DriveSearchResponse(files=files)


@router.post("/google-drive/files/{file_id}/read", response_model=DriveReadResponse)
async def read_google_drive_file(
    file_id: str,
    body: DriveReadRequest,
    request: Request,
    _auth=Depends(get_auth_context),
    config: ConnectorsConfig = Depends(get_connectors_config),
) -> DriveReadResponse:
    client = await _drive_client(request, config)
    try:
        file = await client.metadata(file_id)
        text, truncated = await client.read_text(file, body.max_chars)
    except UnsupportedDriveFileType as exc:
        return DriveReadResponse(
            file=file,
            text="",
            truncated=False,
            unsupported=True,
            unsupported_reason=str(exc),
        )
    except DriveUpstreamError as exc:
        logger.info(
            "google_drive_read_upstream_error",
            extra={"operation": exc.operation, "status_code": exc.status_code, "file_id": file_id},
        )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Google Drive file read failed") from exc
    return DriveReadResponse(file=file, text=text, truncated=truncated)


@router.get("/gmail/status", response_model=GmailStatus)
async def gmail_status(
    request: Request,
    _auth=Depends(get_auth_context),
    config: ConnectorsConfig = Depends(get_connectors_config),
) -> GmailStatus:
    return await _gmail_status(request, config)


@router.post("/gmail/search", response_model=GmailSearchResponse)
async def search_gmail(
    body: GmailSearchRequest,
    request: Request,
    _auth=Depends(get_auth_context),
    config: ConnectorsConfig = Depends(get_connectors_config),
) -> GmailSearchResponse:
    client = await _gmail_client(request, config)
    try:
        messages, next_page_token, result_size_estimate = await client.search(
            query=body.query,
            limit=body.limit,
            label_ids=body.label_ids,
            include_spam_trash=body.include_spam_trash,
            days_back=body.days_back,
            since_date=body.since_date,
            until_date=body.until_date,
            time_zone=body.time_zone,
            page_token=body.page_token,
        )
    except GmailSearchError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except GmailUpstreamError as exc:
        logger.info("gmail_search_upstream_error", extra={"operation": exc.operation, "status_code": exc.status_code})
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Gmail search failed") from exc
    return GmailSearchResponse(
        messages=messages,
        next_page_token=next_page_token,
        result_size_estimate=result_size_estimate,
    )


@router.post("/gmail/messages/{message_id}/read", response_model=GmailReadResponse)
async def read_gmail_message(
    message_id: str,
    body: GmailReadRequest,
    request: Request,
    _auth=Depends(get_auth_context),
    config: ConnectorsConfig = Depends(get_connectors_config),
) -> GmailReadResponse:
    client = await _gmail_client(request, config)
    try:
        message, text, truncated, body_format, attachment_count = await client.read(message_id, body.max_chars)
    except GmailUpstreamError as exc:
        logger.info("gmail_read_upstream_error", extra={"operation": exc.operation, "status_code": exc.status_code})
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Gmail message read failed") from exc
    return GmailReadResponse(
        message=message,
        text=text,
        truncated=truncated,
        body_format=body_format,
        has_attachments=attachment_count > 0,
        attachment_count=attachment_count,
    )


@router.get("/google-calendar/status", response_model=GoogleCalendarStatus)
async def google_calendar_status(
    request: Request,
    _auth=Depends(get_auth_context),
    config: ConnectorsConfig = Depends(get_connectors_config),
) -> GoogleCalendarStatus:
    return await _google_calendar_status(request, config)


@router.post("/google-calendar/search", response_model=CalendarSearchResponse)
async def search_google_calendar(
    body: CalendarSearchRequest,
    request: Request,
    _auth=Depends(get_auth_context),
    config: ConnectorsConfig = Depends(get_connectors_config),
) -> CalendarSearchResponse:
    client = await _google_calendar_client(request, config)
    try:
        events, next_cursor = await client.search(
            query=body.query,
            limit=body.limit,
            calendar_ids=body.calendar_ids,
            time_min=body.time_min,
            time_max=body.time_max,
            days_back=body.days_back,
            days_ahead=body.days_ahead,
            since_date=body.since_date,
            until_date=body.until_date,
            time_zone=body.time_zone,
            cursor=body.cursor,
        )
    except CalendarSearchError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except CalendarUpstreamError as exc:
        logger.info(
            "google_calendar_search_upstream_error",
            extra={"operation": exc.operation, "status_code": exc.status_code},
        )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Google Calendar search failed") from exc
    return CalendarSearchResponse(events=events, next_cursor=next_cursor)


@router.post(
    "/google-calendar/calendars/{calendar_id}/events/{event_id}/read",
    response_model=CalendarReadResponse,
)
async def read_google_calendar_event(
    calendar_id: str,
    event_id: str,
    body: CalendarReadRequest,
    request: Request,
    _auth=Depends(get_auth_context),
    config: ConnectorsConfig = Depends(get_connectors_config),
) -> CalendarReadResponse:
    client = await _google_calendar_client(request, config)
    try:
        event, truncated = await client.read(calendar_id, event_id, body.max_chars)
    except CalendarUpstreamError as exc:
        logger.info(
            "google_calendar_read_upstream_error",
            extra={"operation": exc.operation, "status_code": exc.status_code},
        )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Google Calendar event read failed") from exc
    return CalendarReadResponse(event=event, truncated=truncated)


@router.get("/slack/status", response_model=SlackStatus)
async def slack_status(
    request: Request,
    _auth=Depends(get_auth_context),
    config: ConnectorsConfig = Depends(get_connectors_config),
) -> SlackStatus:
    return await _slack_status(request, config)


@router.get("/slack/channels", response_model=SlackChannelsResponse)
async def list_slack_channels(
    request: Request,
    limit: int = Query(default=100, ge=1, le=200),
    cursor: str = Query(default="", max_length=4096),
    _auth=Depends(get_auth_context),
    config: ConnectorsConfig = Depends(get_connectors_config),
) -> SlackChannelsResponse:
    client = await _slack_client(request, config)
    try:
        channels, next_cursor = await client.list_channels(limit=limit, cursor=cursor)
    except SlackUpstreamError as exc:
        logger.info(
            "slack_channels_upstream_error",
            extra={"operation": exc.operation, "status_code": exc.status_code},
        )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Slack channel list failed") from exc
    return SlackChannelsResponse(channels=channels, next_cursor=next_cursor)


@router.get("/slack/dms", response_model=SlackDmsResponse)
async def list_slack_dms(
    request: Request,
    limit: int = Query(default=100, ge=1, le=200),
    cursor: str = Query(default="", max_length=256),
    _auth=Depends(get_auth_context),
    config: ConnectorsConfig = Depends(get_connectors_config),
) -> SlackDmsResponse:
    client = await _slack_client(request, config)
    try:
        dms, next_cursor = await client.list_dms(limit=limit, cursor=cursor)
    except SlackUpstreamError as exc:
        logger.info(
            "slack_dms_upstream_error",
            extra={"operation": exc.operation, "status_code": exc.status_code},
        )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Slack DM list failed") from exc
    return SlackDmsResponse(dms=dms, next_cursor=next_cursor)


@router.post("/slack/search", response_model=SlackSearchResponse)
async def search_slack(
    body: SlackSearchRequest,
    request: Request,
    _auth=Depends(get_auth_context),
    config: ConnectorsConfig = Depends(get_connectors_config),
) -> SlackSearchResponse:
    client = await _slack_client(request, config)
    try:
        hits, next_page = await client.search_page(query=body.query, limit=body.limit, page=body.page)
    except SlackUpstreamError as exc:
        logger.info(
            "slack_search_upstream_error",
            extra={"operation": exc.operation, "status_code": exc.status_code},
        )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Slack search failed") from exc
    return SlackSearchResponse(hits=hits, next_page=next_page)


@router.post("/slack/channels/{channel_id}/read", response_model=SlackReadResponse)
async def read_slack_conversation(
    channel_id: str,
    body: SlackReadRequest,
    request: Request,
    _auth=Depends(get_auth_context),
    config: ConnectorsConfig = Depends(get_connectors_config),
) -> SlackReadResponse:
    _validate_slack_channel_id(channel_id)
    client = await _slack_client(request, config)
    try:
        messages, has_more, next_cursor = await client.read_conversation(
            channel_id=channel_id,
            limit=body.limit,
            oldest=body.oldest,
            latest=body.latest,
            cursor=body.cursor,
        )
    except SlackConversationNotAllowed as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except SlackUpstreamError as exc:
        logger.info(
            "slack_read_upstream_error",
            extra={"operation": exc.operation, "status_code": exc.status_code, "channel_id": channel_id},
        )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Slack conversation read failed") from exc
    return SlackReadResponse(channel_id=channel_id, messages=messages, has_more=has_more, next_cursor=next_cursor)


@router.post("/slack/channels/{channel_id}/threads/{message_ts}/read", response_model=SlackThreadReadResponse)
async def read_slack_thread(
    channel_id: str,
    message_ts: str,
    body: SlackThreadReadRequest,
    request: Request,
    _auth=Depends(get_auth_context),
    config: ConnectorsConfig = Depends(get_connectors_config),
) -> SlackThreadReadResponse:
    _validate_slack_channel_id(channel_id)
    _validate_slack_message_ts(message_ts)
    client = await _slack_client(request, config)
    try:
        messages, has_more, next_cursor = await client.read_thread(
            channel_id=channel_id,
            message_ts=message_ts,
            limit=body.limit,
            cursor=body.cursor,
        )
    except SlackConversationNotAllowed as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except SlackUpstreamError as exc:
        logger.info(
            "slack_thread_upstream_error",
            extra={"operation": exc.operation, "status_code": exc.status_code, "channel_id": channel_id},
        )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Slack thread read failed") from exc
    return SlackThreadReadResponse(
        channel_id=channel_id,
        message_ts=message_ts,
        messages=messages,
        has_more=has_more,
        next_cursor=next_cursor,
    )


@router.get("/github/status", response_model=GitHubStatus)
async def github_status(
    request: Request,
    _auth=Depends(get_auth_context),
    config: ConnectorsConfig = Depends(get_connectors_config),
) -> GitHubStatus:
    return await _github_status(request, config)


@router.post("/github/search", response_model=GitHubSearchResponse)
async def search_github(
    body: GitHubSearchRequest,
    request: Request,
    _auth=Depends(get_auth_context),
    config: ConnectorsConfig = Depends(get_connectors_config),
) -> GitHubSearchResponse:
    if body.repo.strip():
        _validate_github_repo(body.repo)
    client = await _github_client(request, config)
    try:
        hits, next_page_token, incomplete = await client.search(
            query=body.query,
            limit=body.limit,
            repo=body.repo,
            page_token=body.page_token,
        )
    except GitHubSearchError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except GitHubUpstreamError as exc:
        logger.info(
            "github_search_upstream_error",
            extra={"operation": exc.operation, "status_code": exc.status_code},
        )
        _raise_github_upstream(exc)
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "GitHub search failed") from exc
    return GitHubSearchResponse(hits=hits, next_page_token=next_page_token, incomplete_results=incomplete)


# Floor for the upstream-read deadline after the concurrency-permit wait has eaten
# into the shared request budget: keep a just-acquired permit from handing the read
# a near-zero timeout that would 502 immediately.
_API_GET_MIN_READ_BUDGET_SECONDS = 1.0


@router.post("/github/api/get", response_model=GitHubApiGetResponse)
async def github_api_get(
    body: GitHubApiGetRequest,
    request: Request,
    auth=Depends(get_auth_context),
    config: ConnectorsConfig = Depends(get_connectors_config),
) -> GitHubApiGetResponse:
    if not config.github.api_get_enabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "generic API read is disabled on this hub")
    # The generic read has a wider aperture than the curated tools, so its
    # request/refusal/truncation/upstream events are the alertable surface for
    # abuse (per-user via auth.user + the validated path).
    logger.info(
        "github_api_get_request",
        extra={"user": auth.user, "path": body.path, "media_type": body.media_type},
    )
    # Broker the token OUTSIDE the concurrency permit: _github_client does a live
    # Keycloak round-trip, and holding a permit across it would let a slow broker
    # fill every slot with requests that fail before GitHub is even contacted (and
    # the curated reads take no permit, so it bounds nothing). A token error then
    # surfaces its own status without ever queueing behind the semaphore.
    client = await _github_client(request, config)

    # Bound concurrent generic reads so an injected agent can't exhaust the hub's
    # sockets/memory by fanning out api_get calls. Waiting for a permit is itself
    # bounded (request_timeout_seconds): rather than queue unbounded and invisibly
    # until the pod OOMs, a saturated read sheds itself as a 429 and logs a throttle
    # event (with the queue depth) so contention is observable. The semaphore is
    # sized once at app startup (lifespan).
    semaphore = request.app.state.github_api_get_semaphore
    loop = asyncio.get_running_loop()
    wait_start = loop.time()
    request.app.state.github_api_get_waiters += 1
    try:
        async with asyncio.timeout(config.github.request_timeout_seconds):
            await semaphore.acquire()
    except TimeoutError as exc:
        logger.info(
            "github_api_get_throttled",
            extra={
                "user": auth.user,
                "path": body.path,
                # Queue depth at shed time. Read before the finally decrement, so it
                # includes this request — which WAS a waiter until it gave up here —
                # i.e. the honest snapshot of the state variable, not "others only".
                "waiters": request.app.state.github_api_get_waiters,
            },
        )
        # 429 Too Many Requests, with Retry-After so the client backs off ~the wait
        # budget instead of immediately re-queuing and re-saturating the pool. The
        # detail string ("busy") is distinct from the GitHub-upstream rate-limit
        # 429 ("GitHub rate limit exceeded"), and apollo-desktop surfaces both
        # generically by status+detail with no GitHub-specific 429 branch, so this
        # cannot be mistaken for a GitHub rate limit. Retry-After is integer seconds
        # (RFC 9110 delta-seconds), rounded up, floored at 1.
        retry_after = str(max(1, math.ceil(config.github.request_timeout_seconds)))
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "generic GitHub read is busy; retry shortly",
            headers={"Retry-After": retry_after},
        ) from exc
    finally:
        request.app.state.github_api_get_waiters -= 1
    try:
        # The permit wait and the upstream read draw on ONE budget
        # (request_timeout_seconds). Charge the read only the time left after the
        # wait so a saturated read is bounded to that budget total, not ~2x (a full
        # wait followed by a full read). Floor a near-zero remainder so a
        # just-acquired permit doesn't 502 the read on an empty deadline — but never
        # above the configured budget itself (which may be sub-second).
        budget = config.github.request_timeout_seconds
        read_budget = max(min(budget, _API_GET_MIN_READ_BUDGET_SECONDS), budget - (loop.time() - wait_start))
        result = await client.api_get(
            path=body.path,
            params=body.params,
            media_type=body.media_type,
            max_chars=body.max_chars,
            timeout_seconds=read_budget,
        )
    except GitHubApiRequestError as exc:
        logger.info(
            "github_api_get_refusal",
            extra={"reason": str(exc), "path": body.path, "media_type": body.media_type},
        )
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except GitHubUpstreamError as exc:
        logger.info(
            "github_api_get_upstream_error",
            extra={"operation": exc.operation, "status_code": exc.status_code, "path": body.path},
        )
        # A GitHub 400/422 on the generic read is model-correctable (malformed
        # params, or paging a search-backed endpoint past its 1000-result cap),
        # not a gateway failure — surface it as 422 so the model fixes the request
        # rather than blindly retrying a 502. str(exc) carries only GitHub's
        # structured message.
        if exc.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_CONTENT):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        _raise_github_upstream(exc, not_found_detail=_GITHUB_NOT_FOUND_DETAIL)
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "GitHub API read failed") from exc
    finally:
        semaphore.release()
    if result.truncated:
        logger.info(
            "github_api_get_truncation",
            extra={"path": body.path, "media_type": body.media_type, "content_type": result.content_type},
        )
    # api_get already returns the response model (sanitized + capped), so no
    # field-by-field copy is needed.
    return result


@router.post("/github/items/{number}/read", response_model=GitHubItemReadResponse)
async def read_github_item(
    number: int,
    body: GitHubItemReadRequest,
    request: Request,
    _auth=Depends(get_auth_context),
    config: ConnectorsConfig = Depends(get_connectors_config),
) -> GitHubItemReadResponse:
    _validate_github_repo(body.repo)
    if number < 1:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "A positive GitHub item number is required")
    client = await _github_client(request, config)
    try:
        item, text, truncated = await client.read_item(repo=body.repo, number=number, max_chars=body.max_chars)
    except GitHubUpstreamError as exc:
        logger.info(
            "github_item_read_upstream_error",
            extra={"operation": exc.operation, "status_code": exc.status_code},
        )
        _raise_github_upstream(exc, not_found_detail=_GITHUB_NOT_FOUND_DETAIL)
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "GitHub item read failed") from exc
    return GitHubItemReadResponse(item=item, text=text, truncated=truncated)


@router.post("/github/files/read", response_model=GitHubFileReadResponse)
async def read_github_file(
    body: GitHubFileReadRequest,
    request: Request,
    _auth=Depends(get_auth_context),
    config: ConnectorsConfig = Depends(get_connectors_config),
) -> GitHubFileReadResponse:
    _validate_github_repo(body.repo)
    _validate_github_path(body.path)
    _validate_github_ref(body.ref)
    client = await _github_client(request, config)
    try:
        file = await client.read_file(repo=body.repo, path=body.path, ref=body.ref, max_chars=body.max_chars)
    except GitHubUpstreamError as exc:
        logger.info(
            "github_file_read_upstream_error",
            extra={"operation": exc.operation, "status_code": exc.status_code},
        )
        _raise_github_upstream(exc, not_found_detail=_GITHUB_NOT_FOUND_DETAIL)
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "GitHub file read failed") from exc
    return GitHubFileReadResponse(
        repo=body.repo,
        path=body.path,
        ref=body.ref,
        content=file.content,
        truncated=file.truncated,
        too_large=file.too_large,
        binary=file.binary,
        unsupported_reason=file.unsupported_reason,
    )


@router.post("/github/repos/list", response_model=GitHubReposListResponse)
async def list_github_repos(
    body: GitHubReposListRequest,
    request: Request,
    _auth=Depends(get_auth_context),
    config: ConnectorsConfig = Depends(get_connectors_config),
) -> GitHubReposListResponse:
    _validate_github_owner(body.owner)
    client = await _github_client(request, config)
    try:
        repos = await client.list_repos(owner=body.owner)
    except GitHubUpstreamError as exc:
        logger.info(
            "github_repos_list_upstream_error",
            extra={"operation": exc.operation, "status_code": exc.status_code},
        )
        _raise_github_upstream(exc, not_found_detail=_GITHUB_NOT_FOUND_DETAIL)
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "GitHub repo list failed") from exc
    return GitHubReposListResponse(repos=repos)


@router.post("/github/projects/list", response_model=GitHubProjectsListResponse)
async def list_github_projects(
    body: GitHubProjectsListRequest,
    request: Request,
    _auth=Depends(get_auth_context),
    config: ConnectorsConfig = Depends(get_connectors_config),
) -> GitHubProjectsListResponse:
    _validate_github_owner(body.owner)
    client = await _github_client(request, config)
    try:
        projects = await client.list_projects(owner=body.owner)
    except GitHubUpstreamError as exc:
        logger.info(
            "github_projects_list_upstream_error",
            extra={"operation": exc.operation, "status_code": exc.status_code},
        )
        _raise_github_upstream(exc, not_found_detail=_GITHUB_NOT_FOUND_DETAIL)
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "GitHub project list failed") from exc
    return GitHubProjectsListResponse(projects=projects)


@router.post("/github/projects/{number}/read", response_model=GitHubProjectReadResponse)
async def read_github_project(
    number: int,
    body: GitHubProjectReadRequest,
    request: Request,
    _auth=Depends(get_auth_context),
    config: ConnectorsConfig = Depends(get_connectors_config),
) -> GitHubProjectReadResponse:
    _validate_github_owner(body.owner)
    if number < 1:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "A positive GitHub project number is required")
    client = await _github_client(request, config)
    try:
        # Counts run concurrently with the enumeration and never raise; only the
        # board read's own failure surfaces here to be mapped.
        project, items, counts = await client.read_project_with_counts(
            owner=body.owner, number=number, max_items=body.max_items
        )
    except GitHubUpstreamError as exc:
        logger.info(
            "github_project_read_upstream_error",
            extra={"operation": exc.operation, "status_code": exc.status_code},
        )
        _raise_github_upstream(exc, not_found_detail=_GITHUB_NOT_FOUND_DETAIL)
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "GitHub project read failed") from exc
    # project.items_count is the board's full size; flag when we returned fewer
    # so the caller doesn't reason over a partial board as if it were complete.
    total = project.items_count
    return GitHubProjectReadResponse(
        project=project,
        items=items,
        total_count=total,
        truncated=total > len(items),
        counts=counts,
    )


@router.get("/notion/status", response_model=NotionStatus)
async def notion_status(
    request: Request,
    _auth=Depends(get_auth_context),
    config: ConnectorsConfig = Depends(get_connectors_config),
) -> NotionStatus:
    return await _notion_status(request, config)


@router.post("/notion/search", response_model=NotionSearchResponse)
async def search_notion(
    body: NotionSearchRequest,
    request: Request,
    _auth=Depends(get_auth_context),
    config: ConnectorsConfig = Depends(get_connectors_config),
) -> NotionSearchResponse:
    client = await _notion_client(request, config)
    lower, upper = notion_time_bounds(
        days_back=body.days_back,
        since_date=body.since_date,
        until_date=body.until_date,
        time_zone=body.time_zone,
    )
    try:
        hits, next_page_token = await client.search(
            query=body.query,
            object_type=body.object_type,
            limit=body.limit,
            start_cursor=body.page_token,
            lower=lower,
            upper=upper,
        )
    except NotionSearchError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except NotionUpstreamError as exc:
        logger.info("notion_search_upstream_error", extra={"operation": exc.operation, "status_code": exc.status_code})
        _raise_notion_upstream(exc)
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Notion search failed") from exc
    return NotionSearchResponse(hits=hits, next_page_token=next_page_token)


@router.post("/notion/pages/{page_id}/read", response_model=NotionPageReadResponse)
async def read_notion_page(
    page_id: str,
    body: NotionPageReadRequest,
    request: Request,
    _auth=Depends(get_auth_context),
    config: ConnectorsConfig = Depends(get_connectors_config),
) -> NotionPageReadResponse:
    _validate_notion_id(page_id)
    client = await _notion_client(request, config)
    try:
        return await client.read_page(page_id=page_id, max_chars=body.max_chars)
    except NotionUpstreamError as exc:
        logger.info(
            "notion_page_read_upstream_error",
            extra={"operation": exc.operation, "status_code": exc.status_code},
        )
        _raise_notion_upstream(exc)
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Notion page read failed") from exc


@router.post("/notion/databases/{database_id}/query", response_model=NotionDatabaseQueryResponse)
async def query_notion_database(
    database_id: str,
    body: NotionDatabaseQueryRequest,
    request: Request,
    _auth=Depends(get_auth_context),
    config: ConnectorsConfig = Depends(get_connectors_config),
) -> NotionDatabaseQueryResponse:
    _validate_notion_id(database_id)
    client = await _notion_client(request, config)
    lower, upper = notion_time_bounds(
        days_back=body.days_back,
        since_date=body.since_date,
        until_date=body.until_date,
        time_zone=body.time_zone,
    )
    try:
        rows, next_page_token = await client.query_database(
            database_id=database_id,
            limit=body.limit,
            start_cursor=body.page_token,
            lower=lower,
            upper=upper,
        )
    except NotionSearchError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except NotionUpstreamError as exc:
        logger.info(
            "notion_database_query_upstream_error",
            extra={"operation": exc.operation, "status_code": exc.status_code},
        )
        _raise_notion_upstream(exc)
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Notion database query failed") from exc
    return NotionDatabaseQueryResponse(rows=rows, next_page_token=next_page_token)


async def _github_status(request: Request, config: ConnectorsConfig) -> GitHubStatus:
    provider = GitHubTokenProvider(config.github)
    try:
        token = await provider.access_token(request)
    except ConnectorTokenError as exc:
        return GitHubStatus(connected=False, state=exc.state, scopes=[], detail=str(exc))
    client = _new_github_client(token, config)
    try:
        access = await client.verify_access()
    except GitHubUpstreamError as exc:
        state = "reconnect_required" if exc.status_code in {401, 403} else "unavailable"
        return GitHubStatus(connected=False, state=state, scopes=[], detail=str(exc))
    except httpx.HTTPError:
        return GitHubStatus(
            connected=False,
            state="unavailable",
            scopes=[],
            detail="GitHub API access check failed.",
        )
    return GitHubStatus(
        connected=True,
        state="connected",
        scopes=access.scopes,
        account=access.login,
    )


def _new_github_client(token: str, config: ConnectorsConfig) -> GitHubClient:
    return GitHubClient(
        access_token=token,
        api_base_url=config.github.api_base_url,
        timeout_seconds=config.github.request_timeout_seconds,
        allowed_orgs=config.github.allowed_orgs,
    )


async def _github_client(request: Request, config: ConnectorsConfig) -> GitHubClient:
    provider = GitHubTokenProvider(config.github)
    try:
        token = await provider.access_token(request)
    except ConnectorNotConnected as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ConnectorReconnectRequired as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ConnectorTokenError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return _new_github_client(token, config)


# A GitHub 404 on a read is ambiguous: the resource may not exist, may be
# private and invisible to this token, or may live in an org that requires SSO
# authorization for the token. Never collapse it to a bare "not found".
_GITHUB_NOT_FOUND_DETAIL = (
    "GitHub returned not found. The item may not exist, may be private and not "
    "visible to your linked account, or may be in an organization that requires "
    "SSO authorization for this connection."
)


def _raise_github_upstream(exc: GitHubUpstreamError, *, not_found_detail: str = "") -> None:
    if exc.status_code == 429:
        headers = {"Retry-After": exc.retry_after} if exc.retry_after else None
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc), headers=headers) from exc
    if exc.status_code == 404 and not_found_detail:
        raise HTTPException(status.HTTP_404_NOT_FOUND, not_found_detail) from exc
    raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


_GITHUB_REPO_PATTERN = r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
# GitHub login: alphanumeric or single hyphens, up to 39 chars.
_GITHUB_OWNER_PATTERN = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})"


def _validate_github_owner(owner: str) -> None:
    if not re.fullmatch(_GITHUB_OWNER_PATTERN, owner.strip()):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "A GitHub org or user login is required")


def _validate_github_repo(repo: str) -> None:
    if not re.fullmatch(_GITHUB_REPO_PATTERN, repo.strip()):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "A GitHub repo of the form 'owner/name' is required")


def _validate_github_path(path: str) -> None:
    candidate = path.strip()
    # Reject traversal, absolute paths, backslashes, and non-printable characters
    # before the value is ever placed into an upstream URL. An ordinary ASCII space
    # is allowed (it stays printable), but non-printable Unicode — a non-breaking
    # space, zero-width/bidi marks, control/DEL — is rejected on purpose even though
    # GitHub may accept it in a name: on an LLM-facing surface those are
    # homoglyph/Trojan-Source spoofing vectors, so a rare legitimate filename takes a
    # clear 422 rather than resolving to a name that can disguise itself as another.
    if (
        not candidate
        or candidate.startswith("/")
        or "\\" in candidate
        or ".." in candidate.split("/")
        or has_control_or_nonprintable(candidate)
    ):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid GitHub file path")


def _validate_github_ref(ref: str) -> None:
    candidate = ref.strip()
    if not candidate:
        return  # empty ref = default branch
    # Same strict rejection as _validate_github_path, for the same reason: a ref
    # (branch/tag/sha) carrying a non-breaking space, zero-width/bidi mark, or
    # control/DEL is refused even though git may permit it, because those are
    # spoofing vectors on this surface. The two char checks are not redundant:
    # isspace() also rejects an ordinary ASCII space — legal in a file path but
    # never valid in a git ref — which isprintable() would otherwise let through.
    if (
        candidate.startswith("/")
        or candidate.startswith("-")
        or "\\" in candidate
        or ".." in candidate
        or any(ch.isspace() for ch in candidate)
        or has_control_or_nonprintable(candidate)
    ):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid GitHub ref")


async def _notion_status(request: Request, config: ConnectorsConfig) -> NotionStatus:
    provider = NotionTokenProvider(config.notion)
    try:
        token = await provider.access_token(request)
    except ConnectorTokenError as exc:
        return NotionStatus(connected=False, state=exc.state, scopes=[], detail=str(exc))
    client = _new_notion_client(token, config)
    try:
        access = await client.verify_access()
    except NotionUpstreamError as exc:
        state = "reconnect_required" if exc.status_code in {401, 403} else "unavailable"
        return NotionStatus(connected=False, state=state, scopes=[], detail=str(exc))
    except httpx.HTTPError:
        return NotionStatus(
            connected=False,
            state="unavailable",
            scopes=[],
            detail="Notion API access check failed.",
        )
    return NotionStatus(
        connected=True,
        state="connected",
        scopes=[*NOTION_READONLY_CAPABILITIES],
        account=access.workspace_name,
    )


def _new_notion_client(token: str, config: ConnectorsConfig) -> NotionClient:
    return NotionClient(
        access_token=token,
        api_base_url=config.notion.api_base_url,
        notion_version=config.notion.notion_version,
        timeout_seconds=config.notion.request_timeout_seconds,
    )


async def _notion_client(request: Request, config: ConnectorsConfig) -> NotionClient:
    provider = NotionTokenProvider(config.notion)
    try:
        token = await provider.access_token(request)
    except ConnectorNotConnected as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ConnectorReconnectRequired as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ConnectorTokenError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return _new_notion_client(token, config)


# A Notion id is 32 hex chars, optionally UUID dash-grouped (8-4-4-4-12).
_NOTION_ID_PATTERN = r"[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"


def _validate_notion_id(value: str) -> None:
    if not re.fullmatch(_NOTION_ID_PATTERN, value.strip()):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "A valid Notion id is required")


def _raise_notion_upstream(exc: NotionUpstreamError) -> None:
    # Notion returns 400 for caller-correctable input: invalid params or a
    # stale/garbage start_cursor. Surface those as 422; everything else
    # (timeout, 5xx, bad JSON, cursor cycle) is a provider failure -> 502.
    if exc.status_code == 400:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


async def _google_drive_status(request: Request, config: ConnectorsConfig) -> GoogleDriveStatus:
    provider = GoogleTokenProvider(config.google)
    try:
        await provider.access_token(request)
    except ConnectorTokenError as exc:
        return GoogleDriveStatus(
            connected=False,
            state=exc.state,
            scopes=[],
            detail=str(exc),
        )
    return GoogleDriveStatus(
        connected=True,
        state="connected",
        scopes=[DRIVE_READONLY_SCOPE],
    )


async def _drive_client(request: Request, config: ConnectorsConfig) -> GoogleDriveClient:
    token = await _google_connector_token(request, config)
    return GoogleDriveClient(
        access_token=token,
        api_base_url=config.google.drive_api_base_url,
        timeout_seconds=config.google.request_timeout_seconds,
    )


async def _gmail_status(request: Request, config: ConnectorsConfig) -> GmailStatus:
    provider = GoogleTokenProvider(config.google)
    try:
        token = await provider.access_token(request)
    except ConnectorTokenError as exc:
        return GmailStatus(connected=False, state=exc.state, scopes=[], detail=str(exc))
    client = _new_gmail_client(token, config)
    try:
        await client.verify_access()
    except GmailUpstreamError as exc:
        state = "reconnect_required" if exc.status_code in {401, 403} else "unavailable"
        return GmailStatus(connected=False, state=state, scopes=[], detail=str(exc))
    except httpx.HTTPError:
        return GmailStatus(
            connected=False,
            state="unavailable",
            scopes=[],
            detail="Gmail API access check failed.",
        )
    return GmailStatus(connected=True, state="connected", scopes=[GMAIL_READONLY_SCOPE])


def _new_gmail_client(token: str, config: ConnectorsConfig) -> GmailClient:
    return GmailClient(
        access_token=token,
        api_base_url=config.google.gmail_api_base_url,
        timeout_seconds=config.google.request_timeout_seconds,
    )


async def _gmail_client(request: Request, config: ConnectorsConfig) -> GmailClient:
    token = await _google_connector_token(request, config)
    return _new_gmail_client(token, config)


async def _google_calendar_status(request: Request, config: ConnectorsConfig) -> GoogleCalendarStatus:
    provider = GoogleTokenProvider(config.google)
    try:
        token = await provider.access_token(request)
    except ConnectorTokenError as exc:
        return GoogleCalendarStatus(connected=False, state=exc.state, scopes=[], detail=str(exc))
    client = _new_google_calendar_client(token, config)
    try:
        await client.verify_access()
    except CalendarUpstreamError as exc:
        state = "reconnect_required" if exc.status_code in {401, 403} else "unavailable"
        return GoogleCalendarStatus(connected=False, state=state, scopes=[], detail=str(exc))
    except httpx.HTTPError:
        return GoogleCalendarStatus(
            connected=False,
            state="unavailable",
            scopes=[],
            detail="Google Calendar API access check failed.",
        )
    return GoogleCalendarStatus(
        connected=True,
        state="connected",
        scopes=[GOOGLE_CALENDAR_READONLY_SCOPE],
    )


def _new_google_calendar_client(token: str, config: ConnectorsConfig) -> GoogleCalendarClient:
    return GoogleCalendarClient(
        access_token=token,
        api_base_url=config.google.calendar_api_base_url,
        timeout_seconds=config.google.request_timeout_seconds,
    )


async def _google_calendar_client(request: Request, config: ConnectorsConfig) -> GoogleCalendarClient:
    token = await _google_connector_token(request, config)
    return _new_google_calendar_client(token, config)


async def _google_connector_token(request: Request, config: ConnectorsConfig) -> str:
    provider = GoogleTokenProvider(config.google)
    try:
        return await provider.access_token(request)
    except ConnectorNotConnected as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ConnectorReconnectRequired as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ConnectorTokenError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc


async def _slack_status(request: Request, config: ConnectorsConfig) -> SlackStatus:
    provider = SlackTokenProvider(config.slack)
    try:
        token = await provider.access_token(request)
    except ConnectorTokenError as exc:
        return SlackStatus(
            connected=False,
            state=exc.state,
            scopes=[],
            detail=str(exc),
        )

    # A brokered token is not proof of a usable connector: Keycloak can broker a
    # Slack OpenID sign-in/identity token that links successfully yet fails every
    # Web API read. Validate it against auth.test so that case reports
    # reconnect_required here instead of silently 502-ing on the first read.
    client = _new_slack_client(token, config)
    try:
        access = await client.verify_access()
    except SlackUpstreamError as exc:
        if exc.message in SLACK_TOKEN_INVALID_ERRORS:
            return SlackStatus(
                connected=False,
                state="reconnect_required",
                scopes=[],
                detail=(
                    "Slack is linked but the brokered token is not a usable Slack Web API user token "
                    f"(auth.test returned '{exc.message}'). Reconnect Slack and make sure Keycloak brokers a "
                    "Slack user token with read scopes (search:read and the *:history scopes), not an OpenID "
                    "sign-in token."
                ),
            )
        # The token brokered fine but Slack could not be reached to verify it right
        # now; stay connected rather than flapping on a transient upstream error.
        logger.info(
            "slack_status_verify_upstream_error",
            extra={"operation": exc.operation, "status_code": exc.status_code},
        )
        return SlackStatus(
            connected=True,
            state="connected",
            scopes=list(SLACK_READONLY_SCOPES),
            detail="Connected; Slack API token verification was skipped after an upstream error.",
        )
    except httpx.HTTPError:
        logger.info("slack_status_verify_transport_error")
        return SlackStatus(
            connected=True,
            state="connected",
            scopes=list(SLACK_READONLY_SCOPES),
            detail="Connected; Slack API token verification was skipped after a network error.",
        )

    return SlackStatus(
        connected=True,
        state="connected",
        scopes=access.granted_scopes or list(SLACK_READONLY_SCOPES),
    )


def _new_slack_client(token: str, config: ConnectorsConfig) -> SlackClient:
    return SlackClient(
        access_token=token,
        api_base_url=config.slack.api_base_url,
        timeout_seconds=config.slack.request_timeout_seconds,
    )


async def _slack_client(request: Request, config: ConnectorsConfig) -> SlackClient:
    provider = SlackTokenProvider(config.slack)
    try:
        token = await provider.access_token(request)
    except ConnectorNotConnected as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ConnectorReconnectRequired as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ConnectorTokenError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return _new_slack_client(token, config)


_SLACK_CHANNEL_ID_PATTERN = r"[CGD][A-Z0-9]{2,}"
_SLACK_MESSAGE_TS_PATTERN = r"\d{10,}\.\d{3,}"


def _validate_slack_channel_id(channel_id: str) -> None:
    if not re.fullmatch(_SLACK_CHANNEL_ID_PATTERN, channel_id):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "A Slack channel or DM id is required")


def _validate_slack_message_ts(message_ts: str) -> None:
    if not re.fullmatch(_SLACK_MESSAGE_TS_PATTERN, message_ts):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "A Slack message timestamp is required")
