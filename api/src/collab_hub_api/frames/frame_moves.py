"""Move a retired self-service organization's frames to where its owner went (issue #365).

When an invitation moves the owner of a self-service organization into another
organization, their frames go with them and become ``private``. Frames live in
object storage, which cannot commit with the database, so the acceptance writes
a ``collab_frame_moves`` row in its own transaction and this module performs
the move afterwards: right after the acceptance commits, and again from a
periodic sweep for any row a crash or an object-store error left ``pending``.

Every step is idempotent. Re-homing a frame sets two metadata fields to fixed
values, so a frame moved twice ends up exactly where a frame moved once does,
and a row is marked ``done`` only after every frame still in the old
organization has been moved. A frame that fails to move leaves the row
``pending`` for the next attempt.

Frame *groups* are not handled here: they live in the database and move inside
the acceptance transaction.
"""

from __future__ import annotations

import logging

from .auth import WORKSPACE_DEFAULT

logger = logging.getLogger("frames_server.frame_moves")

SWEEP_INTERVAL_SECONDS = 300.0
"""How often the app retries moves left ``pending``. Moves are rare and each
one lists the frame store, so this is deliberately unhurried."""

_SWEEP_BATCH = 20


def complete_frame_moves(db, frames_store, *, from_org_id: str | None = None) -> int:
    """Perform pending moves: one organization's, or up to a batch of any.

    Returns how many moves this call finished. Raises nothing for a single
    frame's failure (it is logged and the move stays pending); a database
    failure propagates, since nothing can be decided without the queue.
    """

    with db.connection() as conn:
        if from_org_id is not None:
            rows = conn.execute(
                "SELECT from_org_id, to_org_id FROM collab_frame_moves WHERE from_org_id = %s AND state = 'pending'",
                (from_org_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT from_org_id, to_org_id FROM collab_frame_moves
                WHERE state = 'pending' ORDER BY created_at LIMIT %s
                """,
                (_SWEEP_BATCH,),
            ).fetchall()
    finished = 0
    for row in rows:
        if _move_one(db, frames_store, from_org_id=row["from_org_id"], to_org_id=row["to_org_id"]):
            finished += 1
    return finished


def _move_one(db, frames_store, *, from_org_id: str, to_org_id: str) -> bool:
    try:
        frames = frames_store.list_frames(from_org_id, WORKSPACE_DEFAULT)
    except Exception:
        logger.exception("frame_move_list_failed", extra={"from_org": from_org_id})
        return False
    failed = 0
    for frame in frames:
        try:
            frames_store.move_to_org(frame.id, to_org_id)
        except Exception:
            failed += 1
            logger.exception("frame_move_failed", extra={"from_org": from_org_id, "frame": frame.id})
    with db.connection() as conn:
        if failed:
            conn.execute(
                "UPDATE collab_frame_moves SET moved = moved + %s, updated_at = now() WHERE from_org_id = %s",
                (len(frames) - failed, from_org_id),
            )
            return False
        conn.execute(
            """
            UPDATE collab_frame_moves SET state = 'done', moved = moved + %s, updated_at = now()
            WHERE from_org_id = %s
            """,
            (len(frames), from_org_id),
        )
    logger.info("frame_move_done", extra={"from_org": from_org_id, "to_org": to_org_id, "frames": len(frames)})
    return True
