"""Move a retired self-service organization's frames to where its owner went (issue #365).

When an invitation moves the owner of a self-service organization into another
organization, their frames go with them and become ``private``. Frames live in
object storage, which cannot commit with the database, so the acceptance writes
a ``collab_frame_moves`` row in its own transaction and this module performs
the move afterwards: right after the acceptance commits, and again from a
periodic sweep for any row a crash or an object-store error left ``pending``.

Every step is idempotent. A frame moves only if it is still in the old
organization (the store decides that on its own guarded write), so two movers
working from overlapping listings move each frame once and never undo a change
the owner made after the first move.

A row is marked ``done`` only when a pass moved or found every frame, could
read every record in the store, and ran at least :data:`SETTLE_SECONDS` after
the acceptance. The last condition covers a frame or group write that was
authorized against the old organization just before the acceptance and lands
just after it; :data:`SETTLE_SECONDS` explains the bound. Until then
the row stays ``pending`` and the sweep keeps coming back. ``moved`` counts
frames this module moved; it is informational, and a crash between a move and
the counter update leaves it short.

Frame *groups* live in the database and move inside the acceptance
transaction; every pass here moves any that arrived since, for the same late
write.
"""

from __future__ import annotations

import logging

from .auth import WORKSPACE_DEFAULT

logger = logging.getLogger("frames_server.frame_moves")

SWEEP_INTERVAL_SECONDS = 300.0
"""How often the app retries moves left ``pending``. Moves are rare and each
one lists the frame store, so this is deliberately unhurried."""

_SWEEP_BATCH = 20

SETTLE_SECONDS = 3600
"""How long after the acceptance a move must keep looking before it can be
``done``. It has to outlast a frame or group write that was authorized against
the old organization before it was retired. Such a write is a handful of
object-store calls, and each is bounded by the S3 client's defaults (60 s to
connect, 60 s to read, five attempts): about ten minutes per call at worst. An
hour covers several of them."""


def complete_frame_moves(db, frames_store, *, from_org_id: str | None = None) -> int:
    """Perform pending moves: one organization's, or up to a batch of any.

    Returns how many moves this call marked ``done``. A single frame's
    failure is logged and leaves the move pending; a database failure
    propagates, since nothing can be decided without the queue. The sweep
    takes the least recently attempted moves first, so a move that cannot
    finish (an unreadable record in the store) does not starve the others.
    """

    columns = "from_org_id, to_org_id, (created_at <= now() - make_interval(secs => %s)) AS settled"
    with db.connection() as conn:
        if from_org_id is not None:
            rows = conn.execute(
                f"SELECT {columns} FROM collab_frame_moves WHERE from_org_id = %s AND state = 'pending'",
                (SETTLE_SECONDS, from_org_id),
            ).fetchall()
        else:
            rows = conn.execute(
                f"""
                SELECT {columns} FROM collab_frame_moves
                WHERE state = 'pending' ORDER BY updated_at, created_at LIMIT %s
                """,
                (SETTLE_SECONDS, _SWEEP_BATCH),
            ).fetchall()
    finished = 0
    for row in rows:
        if _move_one(
            db, frames_store, from_org_id=row["from_org_id"], to_org_id=row["to_org_id"], settled=row["settled"]
        ):
            finished += 1
    return finished


def _move_one(db, frames_store, *, from_org_id: str, to_org_id: str, settled: bool) -> bool:
    undecodable: list[str] = []
    try:
        frames = frames_store.list_frames(from_org_id, WORKSPACE_DEFAULT, undecodable=undecodable)
    except Exception:
        logger.exception("frame_move_list_failed", extra={"from_org": from_org_id})
        frames, undecodable = [], ["<listing failed>"]
    moved = failed = 0
    for frame in frames:
        try:
            if frames_store.move_to_org(frame.id, from_org_id, to_org_id):
                moved += 1
        except Exception:
            failed += 1
            logger.exception("frame_move_failed", extra={"from_org": from_org_id, "frame": frame.id})
    if undecodable:
        logger.warning(
            "frame_move_incomplete_listing",
            extra={"from_org": from_org_id, "unreadable": len(undecodable)},
        )
    complete = settled and not failed and not undecodable
    with db.connection() as conn:
        # A group created against the old organization after the acceptance's
        # own move (see the module docstring). Same database as the queue, so
        # this rides the same connection.
        if conn.execute("SELECT to_regclass('frames_server_groups') IS NOT NULL AS present").fetchone()["present"]:
            conn.execute(
                """
                UPDATE frames_server_groups SET org_id = %s, visibility = 'private', updated_at = now()
                WHERE org_id = %s
                """,
                (to_org_id, from_org_id),
            )
        conn.execute(
            """
            UPDATE collab_frame_moves
            SET state = CASE WHEN %s THEN 'done' ELSE state END, moved = moved + %s, updated_at = now()
            WHERE from_org_id = %s AND state = 'pending'
            """,
            (complete, moved, from_org_id),
        )
    if complete:
        logger.info("frame_move_done", extra={"from_org": from_org_id, "to_org": to_org_id})
    return complete
