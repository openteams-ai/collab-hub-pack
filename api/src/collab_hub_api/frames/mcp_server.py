from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .access import can_read
from .active_state import ActiveFrameStore, ActiveStateUnavailableError
from .auth import AuthContext, current_auth_context
from .codec import FrameDecodeError, undecodable_frame_log
from .models import validate_frame_id
from .store import FrameNotFoundError, FrameStore


def create_mcp_server(
    store: FrameStore,
    active_store: ActiveFrameStore | None = None,
) -> FastMCP:
    mcp = FastMCP(
        "frames",
        host="0.0.0.0",
        instructions=(
            "Expose Frames for deterministic model-context injection. Tools are read-only and never call a model."
        ),
    )

    def scoped_frame(frame_id: str, auth_context: AuthContext):
        validate_frame_id(frame_id)
        frame = store.get_frame(frame_id)
        # `can_read` owns scope: it scopes `internal`, allows `public` from any
        # tenant, and always allows owners. No separate org/workspace pre-gate
        # (that would block legitimate cross-tenant `public` reads — Spec 1 §6).
        if not can_read(frame, auth_context):
            raise FrameNotFoundError(frame_id)
        return frame

    @mcp.tool()
    def list_frames(
        name: str | None = None,
        tags: list[str] | None = None,
        owner: str | None = None,
    ) -> dict:
        auth_context = require_auth_context()
        # Mirrors REST list: the store query stays scoped to the caller's tenant,
        # so cross-tenant `public` frames are NOT listed here (no cross-tenant
        # discovery). `can_read` only refines within the tenant. Cross-tenant
        # `public` access is reachable by id via get_frame / the frame resource.
        return {
            "frames": [
                item.model_dump(mode="json", exclude={"suggestions"})
                for item in store.list_frames(
                    org_id=auth_context.org_id,
                    workspace_id=auth_context.workspace_id,
                    name=name,
                    tags=tags,
                    owner=owner,
                )
                if can_read(item, auth_context)
            ]
        }

    @mcp.tool()
    def get_frame(id: str) -> dict:
        frame = scoped_frame(id, require_auth_context())
        return {"id": frame.id, "body": frame.body}

    @mcp.tool()
    def get_active_frames(ids: list[str] | None = None) -> dict:
        frame_ids = ids
        # Treat both an omitted/None `ids` AND an empty list as "return the
        # caller's active Frame set". Models calling this tool frequently pass
        # `ids: []` (rather than omitting the optional arg); without this an
        # empty list would iterate over nothing and return zero Frames, which
        # silently defeats the whole point of get_active_frames.
        if not frame_ids:
            if active_store is None:
                raise ActiveStateUnavailableError("Active Frame state is not configured")
            auth_context = require_auth_context()
            frame_ids = active_store.get_active_frame_ids(
                auth_context.org_id,
                auth_context.workspace_id,
                auth_context.user,
            )
        else:
            auth_context = require_auth_context()
        frames = []
        for frame_id in frame_ids:
            # The active set should already be reconciled, but skip rather than
            # raise on a stale id the caller can no longer read so one bad entry
            # cannot break the whole call. Invalid ids still raise (via
            # validate_frame_id inside scoped_frame).
            validate_frame_id(frame_id)
            try:
                frame = scoped_frame(frame_id, auth_context)
            except FrameNotFoundError:
                continue
            except FrameDecodeError as exc:
                # A corrupt active frame is undecodable: skip it so one bad
                # entry cannot break the whole call.
                undecodable_frame_log.skipped(exc, context="get_active_frames")
                continue
            frames.append(
                {
                    "id": frame.id,
                    "body": frame.body,
                    "token_estimate": frame.token_estimate,
                }
            )
        return {"frames": frames}

    @mcp.resource("frame://{frame_id}", mime_type="text/markdown")
    def frame_resource(frame_id: str) -> str:
        return scoped_frame(frame_id, require_auth_context()).body

    return mcp


def require_auth_context() -> AuthContext:
    auth_context = current_auth_context.get()
    if auth_context is None:
        raise ActiveStateUnavailableError("Active Frame user is not available")
    return auth_context
