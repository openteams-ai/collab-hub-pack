"""Frame and Suggestion persistence backends."""

from __future__ import annotations

import logging
import math
import os
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from . import codec
from .models import (
    Frame,
    FrameMetadata,
    Suggestion,
    SuggestionStatus,
    Visibility,
)


class FrameNotFoundError(KeyError):
    """Raised when a requested Frame id is not present in the backing store."""

    pass


class SuggestionNotFoundError(KeyError):
    """Raised when a requested Suggestion id is not present for its Frame."""

    pass


class ConcurrentFrameUpdateError(RuntimeError):
    """Raised when a backend cannot safely commit a concurrent Frame update."""

    pass


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp for persisted models."""

    return datetime.now(UTC)


def compute_token_estimate(body: str) -> int:
    """Estimate body tokens for future context-budgeting decisions."""

    # Prototype heuristic only: roughly four characters per token.
    return math.ceil(len(body) / 4)


def reader_write_fields(readers: list[str]) -> dict:
    """Metadata fields for a reader-list write, enforcing the reader invariant.

    A non-empty ``readers`` list forces ``visibility=private`` — readers only
    apply to private frames (Spec 1 §3.3). An empty list leaves visibility alone.
    """

    fields: dict = {"readers": readers}
    if readers:
        fields["visibility"] = Visibility.private
    return fields


def readers_after_visibility(visibility: Visibility, current_readers: list[str]) -> list[str]:
    """Readers to keep when visibility changes: cleared unless still ``private``.

    The other half of the invariant — setting ``internal``/``public`` clears the
    reader list so the two fields never contradict each other.
    """

    return current_readers if visibility == Visibility.private else []


logger = logging.getLogger("frames_server.store")


def metadata_matches_filters(
    item: FrameMetadata,
    org_id: str,
    workspace_id: str,
    name: str | None = None,
    tags: list[str] | None = None,
    owner: str | None = None,
) -> bool:
    """Return whether Frame metadata matches list endpoint filters."""

    if item.org_id != org_id or item.workspace_id != workspace_id:
        return False
    if name and name.casefold() not in item.name.casefold():
        return False
    if owner and owner not in item.owners:
        return False
    if tags and not set(tags).issubset(item.tags):
        return False
    return True


def close_suggestion_in_frame(frame: Frame, suggestion_id: str) -> Suggestion:
    """Close a Suggestion already loaded with its parent Frame."""

    for index, suggestion in enumerate(frame.suggestions):
        if suggestion.id != suggestion_id:
            continue
        closed = suggestion.model_copy(update={"status": SuggestionStatus.closed})
        frame.suggestions[index] = closed
        frame.updated_at = utc_now()
        return closed
    raise SuggestionNotFoundError(suggestion_id)


class FrameStore(ABC):
    """Persistence contract shared by the REST API and MCP server.

    Implementations own both Frame content and Suggestions. Frame Markdown bodies
    are stored separately from metadata where the backend supports that shape,
    but callers interact with a single typed contract.
    """

    @abstractmethod
    def list_frames(
        self,
        org_id: str,
        workspace_id: str,
        name: str | None = None,
        tags: list[str] | None = None,
        owner: str | None = None,
    ) -> list[FrameMetadata]:
        """Return scoped metadata filtered by name, owner, and required tags."""

        raise NotImplementedError

    @abstractmethod
    def get_frame(self, frame_id: str) -> Frame:
        """Return one Frame including its Markdown body."""

        raise NotImplementedError

    @abstractmethod
    def create_frame(
        self,
        org_id: str,
        workspace_id: str,
        created_by: str,
        owners: list[str],
        name: str,
        description: str,
        visibility: Visibility,
        tags: list[str],
        body: str,
    ) -> Frame:
        """Create a Frame with its creator and owner list."""

        raise NotImplementedError

    @abstractmethod
    def update_frame(
        self,
        frame_id: str,
        name: str,
        description: str,
        visibility: Visibility,
        tags: list[str],
        body: str,
    ) -> Frame:
        """Overwrite mutable Frame fields.

        Preserves ``owners``, ``published``, ``created_by``, and ``suggestions``
        (those are managed by dedicated setters/endpoints). ``readers`` is
        preserved only while ``visibility`` stays ``private``; setting
        ``visibility`` to ``internal``/``public`` **clears ``readers``** to keep
        the reader/visibility invariant (a non-empty reader list implies
        ``private`` — Spec 1 §3.3). Implementations must honor this.
        """

        raise NotImplementedError

    @abstractmethod
    def set_owners(self, frame_id: str, owners: list[str]) -> Frame:
        """Replace the owner list without touching the body."""

        raise NotImplementedError

    @abstractmethod
    def set_readers(self, frame_id: str, readers: list[str]) -> Frame:
        """Replace the reader list without touching the body."""

        raise NotImplementedError

    @abstractmethod
    def set_published(self, frame_id: str, published: bool) -> Frame:
        """Toggle the published gate without touching the body."""

        raise NotImplementedError

    @abstractmethod
    def delete_frame(self, frame_id: str) -> None:
        """Delete a Frame and all of its Suggestions."""

        raise NotImplementedError

    @abstractmethod
    def create_suggestion(
        self,
        frame_id: str,
        submitted_by: str,
        body: str,
    ) -> Suggestion:
        """Create an open Suggestion submitted by the authenticated user."""

        raise NotImplementedError

    @abstractmethod
    def list_suggestions(
        self,
        frame_id: str,
        status: SuggestionStatus | None = None,
    ) -> list[Suggestion]:
        """Return Suggestions for a Frame, optionally filtered by status."""

        raise NotImplementedError

    @abstractmethod
    def close_suggestion(self, frame_id: str, suggestion_id: str) -> Suggestion:
        """Mark a Suggestion closed after API-level permission checks."""

        raise NotImplementedError


class LocalFsFrameStore(FrameStore):
    """FrameStore that stores each Frame under one local directory.

    Layout:
      <root>/<frame_id>/body.md
      <root>/<frame_id>/metadata.json

    Suggestions live in metadata.json alongside their Frame so deleting the
    Frame directory deletes the suggestions as well. It is intended for dev,
    tests, and single-pod installs; production multi-replica deployments should
    use a shared backend such as S3.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._locks_guard = threading.Lock()
        self._locks: dict[str, threading.RLock] = {}

    def list_frames(
        self,
        org_id: str,
        workspace_id: str,
        name: str | None = None,
        tags: list[str] | None = None,
        owner: str | None = None,
    ) -> list[FrameMetadata]:
        """Return local metadata records matching scope and filters."""

        metadata = []
        for path in sorted(self.root.iterdir()):
            if not path.is_dir():
                continue
            try:
                item = self._read_metadata(path.name)
            except FrameNotFoundError:
                continue
            except codec.FrameDecodeError as exc:
                # One corrupt sidecar must not fail the whole list; a direct
                # GET of it still surfaces the structured 500.
                codec.undecodable_frame_log.skipped(exc, context="frame list")
                continue
            if not metadata_matches_filters(
                item,
                org_id,
                workspace_id,
                name,
                tags,
                owner,
            ):
                continue
            metadata.append(item)
        return metadata

    def get_frame(self, frame_id: str) -> Frame:
        """Read one local Frame with its Markdown body."""

        with self._frame_lock(frame_id):
            return self._read_frame(frame_id)

    def _read_metadata(self, frame_id: str) -> FrameMetadata:
        """Read one Frame's metadata *without* its Markdown body.

        Mirrors ``S3FrameStore._read_metadata``: listing needs only the
        sidecar, so a missing or corrupt ``body.md`` never hides a frame from
        one backend's list while the other still shows it.
        """

        metadata_path = self._frame_dir(frame_id) / "metadata.json"
        with self._frame_lock(frame_id):
            if not metadata_path.exists():
                raise FrameNotFoundError(frame_id)
            return codec.decode_metadata(metadata_path.read_bytes(), frame_id=frame_id)

    def _read_frame(self, frame_id: str) -> Frame:
        frame_dir = self._frame_dir(frame_id)
        metadata_path = frame_dir / "metadata.json"
        body_path = frame_dir / "body.md"
        if not metadata_path.exists() or not body_path.exists():
            raise FrameNotFoundError(frame_id)
        return codec.decode_frame(
            metadata_path.read_bytes(),
            body_path.read_bytes(),
            frame_id=frame_id,
        )

    def create_frame(
        self,
        org_id: str,
        workspace_id: str,
        created_by: str,
        owners: list[str],
        name: str,
        description: str,
        visibility: Visibility,
        tags: list[str],
        body: str,
    ) -> Frame:
        """Create a local Frame directory and metadata sidecar."""

        frame_id = uuid4().hex
        now = utc_now()
        frame = Frame(
            id=frame_id,
            org_id=org_id,
            workspace_id=workspace_id,
            name=name,
            description=description,
            visibility=visibility,
            created_by=created_by,
            owners=owners,
            tags=tags,
            body=body,
            token_estimate=compute_token_estimate(body),
            suggestions=[],
            created_at=now,
            updated_at=now,
        )
        with self._frame_lock(frame_id):
            self._write_frame(frame)
        return frame

    def update_frame(
        self,
        frame_id: str,
        name: str,
        description: str,
        visibility: Visibility,
        tags: list[str],
        body: str,
    ) -> Frame:
        """Update a local Frame body and mutable metadata."""

        with self._frame_lock(frame_id):
            current = self._read_frame(frame_id)
            updated = current.model_copy(
                update={
                    "name": name,
                    "description": description,
                    "visibility": visibility,
                    # Invariant: internal/public clears readers (§3.3).
                    "readers": readers_after_visibility(visibility, current.readers),
                    "tags": tags,
                    "body": body,
                    "token_estimate": compute_token_estimate(body),
                    "updated_at": utc_now(),
                }
            )
            self._write_frame(updated)
            return updated

    def set_owners(self, frame_id: str, owners: list[str]) -> Frame:
        """Replace a local Frame's owner list, preserving its body."""

        return self._set_metadata_fields(frame_id, {"owners": owners})

    def set_readers(self, frame_id: str, readers: list[str]) -> Frame:
        """Replace a local Frame's reader list (non-empty forces private), body intact."""

        return self._set_metadata_fields(frame_id, reader_write_fields(readers))

    def set_published(self, frame_id: str, published: bool) -> Frame:
        """Toggle a local Frame's published gate, preserving its body."""

        return self._set_metadata_fields(frame_id, {"published": published})

    def _set_metadata_fields(self, frame_id: str, fields: dict) -> Frame:
        with self._frame_lock(frame_id):
            current = self._read_frame(frame_id)
            updated = current.model_copy(update={**fields, "updated_at": utc_now()})
            self._write_frame(updated)
            return updated

    def delete_frame(self, frame_id: str) -> None:
        """Delete a local Frame directory and its sidecars."""

        with self._frame_lock(frame_id):
            frame_dir = self._frame_dir(frame_id)
            if not frame_dir.exists():
                raise FrameNotFoundError(frame_id)
            for child in frame_dir.iterdir():
                child.unlink()
            frame_dir.rmdir()

    def create_suggestion(
        self,
        frame_id: str,
        submitted_by: str,
        body: str,
    ) -> Suggestion:
        """Append a Suggestion to a local Frame metadata sidecar."""

        with self._frame_lock(frame_id):
            frame = self._read_frame(frame_id)
            suggestion = Suggestion(
                id=uuid4().hex,
                frame_id=frame_id,
                status=SuggestionStatus.open,
                submitted_by=submitted_by,
                body=body,
            )
            frame.suggestions.append(suggestion)
            frame.updated_at = utc_now()
            self._write_frame(frame)
            return suggestion

    def list_suggestions(
        self,
        frame_id: str,
        status: SuggestionStatus | None = None,
    ) -> list[Suggestion]:
        """Read local Suggestions, optionally filtered by status."""

        frame = self.get_frame(frame_id)
        if status is None:
            return frame.suggestions
        return [item for item in frame.suggestions if item.status == status]

    def close_suggestion(self, frame_id: str, suggestion_id: str) -> Suggestion:
        """Close a Suggestion stored in a local metadata sidecar."""

        with self._frame_lock(frame_id):
            frame = self._read_frame(frame_id)
            closed = close_suggestion_in_frame(frame, suggestion_id)
            self._write_frame(frame)
            return closed

    def _frame_dir(self, frame_id: str) -> Path:
        return self.root / frame_id

    def _frame_lock(self, frame_id: str) -> threading.RLock:
        with self._locks_guard:
            lock = self._locks.get(frame_id)
            if lock is None:
                lock = threading.RLock()
                self._locks[frame_id] = lock
            return lock

    def _write_frame(self, frame: Frame) -> None:
        frame_dir = self._frame_dir(frame.id)
        frame_dir.mkdir(parents=True, exist_ok=True)
        body_path = frame_dir / "body.md"
        metadata_path = frame_dir / "metadata.json"
        body_path.write_bytes(codec.encode_body(frame))
        self._write_bytes(metadata_path, codec.encode_metadata(frame))

    def _write_bytes(self, path: Path, data: bytes) -> None:
        """Atomically replace a sidecar on local filesystems.

        Writes the codec-produced bytes verbatim (trailing newline included) so
        the on-disk format is owned entirely by the codec.
        """

        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        try:
            with os.fdopen(fd, "wb") as tmp:
                tmp.write(data)
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)


class S3FrameStore(FrameStore):
    """FrameStore backed by S3 or an S3-compatible endpoint.

    It mirrors LocalFsFrameStore object names:
      frames/<frame_id>/body.md
      frames/<frame_id>/metadata.json

    Credentials are intentionally not modeled in chart values. In production
    this store relies on the ambient AWS credential chain, such as EKS
    ServiceAccount identity.
    """

    #: Concurrent metadata reads per list. Each listed Frame costs one GET, so a
    #: list is latency-bound on round trips rather than bytes -- 96 production
    #: Frames total under 600 KB. The botocore connection pool is sized to match;
    #: its default of 10 would serialise the surplus threads.
    LIST_WORKERS = 16

    def __init__(
        self,
        bucket: str,
        prefix: str = "frames",
        endpoint_url: str | None = None,
        region_name: str | None = None,
    ):
        try:
            import boto3
            from botocore.config import Config
            from botocore.exceptions import ClientError
        except ImportError as exc:
            raise RuntimeError("S3FrameStore requires boto3") from exc
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.client_error = ClientError
        config_kwargs: dict = {"max_pool_connections": self.LIST_WORKERS}
        if endpoint_url:
            config_kwargs["s3"] = {"addressing_style": "path"}
        self.s3 = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region_name,
            config=Config(**config_kwargs),
        )

    def list_frames(
        self,
        org_id: str,
        workspace_id: str,
        name: str | None = None,
        tags: list[str] | None = None,
        owner: str | None = None,
    ) -> list[FrameMetadata]:
        """Return S3 metadata records matching scope and filters."""

        frame_ids = set()
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=f"{self.prefix}/"):
            for item in page.get("Contents", []):
                key = item["Key"]
                if key.endswith("/metadata.json"):
                    parts = key.split("/")
                    if len(parts) >= 3:
                        frame_ids.add(parts[-2])

        def read(frame_id: str) -> FrameMetadata | None:
            try:
                return self._read_metadata(frame_id)
            except FrameNotFoundError:
                # Deleted between the LIST and this GET. Skipping matches the
                # local backend; raising would fail a whole page for one race.
                # Logged so the rate is measurable: a steady stream means
                # something other than ordinary deletes is removing objects.
                logger.warning(
                    "Frame %s listed but not readable; omitted from the list",
                    frame_id,
                )
                return None
            except codec.FrameDecodeError as exc:
                # One corrupt sidecar must not fail the whole list; a direct
                # GET of it still surfaces the structured 500.
                codec.undecodable_frame_log.skipped(exc, context="frame list")
                return None

        ordered = sorted(frame_ids)
        if not ordered:
            return []
        with ThreadPoolExecutor(max_workers=self.LIST_WORKERS) as pool:
            items = list(pool.map(read, ordered))
        return [
            item
            for item in items
            if item is not None
            and metadata_matches_filters(
                item,
                org_id,
                workspace_id,
                name,
                tags,
                owner,
            )
        ]

    def get_frame(self, frame_id: str) -> Frame:
        """Read one S3-backed Frame with its Markdown body."""

        frame, _metadata_etag = self._read_frame_with_metadata_etag(frame_id)
        return frame

    def _read_metadata(self, frame_id: str) -> FrameMetadata:
        """Read one Frame's metadata *without* its Markdown body.

        Listing never needs bodies, and fetching them doubles the round trips
        for data discarded on the next line.
        """

        try:
            metadata_obj = self.s3.get_object(
                Bucket=self.bucket,
                Key=self._key(frame_id, "metadata.json"),
            )
        except self.client_error as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in {"NoSuchKey", "404", "NotFound"}:
                raise FrameNotFoundError(frame_id) from exc
            raise
        return codec.decode_metadata(metadata_obj["Body"].read(), frame_id=frame_id)

    def _read_frame_with_metadata_etag(self, frame_id: str) -> tuple[Frame, str]:
        try:
            metadata_obj = self.s3.get_object(
                Bucket=self.bucket,
                Key=self._key(frame_id, "metadata.json"),
            )
            body_obj = self.s3.get_object(
                Bucket=self.bucket,
                Key=self._key(frame_id, "body.md"),
            )
        except self.client_error as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in {"NoSuchKey", "404", "NotFound"}:
                raise FrameNotFoundError(frame_id) from exc
            raise
        frame = codec.decode_frame(
            metadata_obj["Body"].read(),
            body_obj["Body"].read(),
            frame_id=frame_id,
        )
        return frame, metadata_obj["ETag"]

    def create_frame(
        self,
        org_id: str,
        workspace_id: str,
        created_by: str,
        owners: list[str],
        name: str,
        description: str,
        visibility: Visibility,
        tags: list[str],
        body: str,
    ) -> Frame:
        """Create S3 objects for a Frame body and metadata sidecar."""

        frame_id = uuid4().hex
        now = utc_now()
        frame = Frame(
            id=frame_id,
            org_id=org_id,
            workspace_id=workspace_id,
            name=name,
            description=description,
            visibility=visibility,
            created_by=created_by,
            owners=owners,
            tags=tags,
            body=body,
            token_estimate=compute_token_estimate(body),
            suggestions=[],
            created_at=now,
            updated_at=now,
        )
        self._write_frame(frame)
        return frame

    def update_frame(
        self,
        frame_id: str,
        name: str,
        description: str,
        visibility: Visibility,
        tags: list[str],
        body: str,
    ) -> Frame:
        """Update S3 objects for a Frame body and mutable metadata."""

        return self._retry_metadata_update(
            frame_id,
            lambda current: current.model_copy(
                update={
                    "name": name,
                    "description": description,
                    "visibility": visibility,
                    # Invariant: internal/public clears readers (§3.3).
                    "readers": readers_after_visibility(visibility, current.readers),
                    "tags": tags,
                    "body": body,
                    "token_estimate": compute_token_estimate(body),
                    "updated_at": utc_now(),
                }
            ),
            write_body=True,
        )

    def set_owners(self, frame_id: str, owners: list[str]) -> Frame:
        """Replace an S3 Frame's owner list via ETag-guarded metadata write."""

        return self._set_metadata_fields(frame_id, {"owners": owners})

    def set_readers(self, frame_id: str, readers: list[str]) -> Frame:
        """Replace an S3 Frame's reader list (non-empty forces private), ETag-guarded."""

        return self._set_metadata_fields(frame_id, reader_write_fields(readers))

    def set_published(self, frame_id: str, published: bool) -> Frame:
        """Toggle an S3 Frame's published gate via ETag-guarded metadata write."""

        return self._set_metadata_fields(frame_id, {"published": published})

    def _set_metadata_fields(self, frame_id: str, fields: dict) -> Frame:
        return self._retry_metadata_update(
            frame_id,
            lambda current: current.model_copy(update={**fields, "updated_at": utc_now()}),
            write_body=False,
        )

    def delete_frame(self, frame_id: str) -> None:
        """Delete S3 objects for a Frame body and metadata sidecar."""

        self.get_frame(frame_id)
        for name in ("metadata.json", "body.md"):
            self.s3.delete_object(Bucket=self.bucket, Key=self._key(frame_id, name))

    def create_suggestion(
        self,
        frame_id: str,
        submitted_by: str,
        body: str,
    ) -> Suggestion:
        """Append a Suggestion to a S3 Frame metadata sidecar."""

        suggestion = Suggestion(
            id=uuid4().hex,
            frame_id=frame_id,
            status=SuggestionStatus.open,
            submitted_by=submitted_by,
            body=body,
        )

        def append_suggestion(frame: Frame) -> Frame:
            updated = frame.model_copy(deep=True)
            updated.suggestions.append(suggestion)
            updated.updated_at = utc_now()
            return updated

        self._retry_metadata_update(frame_id, append_suggestion)
        return suggestion

    def list_suggestions(
        self,
        frame_id: str,
        status: SuggestionStatus | None = None,
    ) -> list[Suggestion]:
        """Read S3-backed Suggestions, optionally filtered by status."""

        frame = self.get_frame(frame_id)
        if status is None:
            return frame.suggestions
        return [item for item in frame.suggestions if item.status == status]

    def close_suggestion(self, frame_id: str, suggestion_id: str) -> Suggestion:
        """Close a Suggestion stored in S3 metadata."""

        closed: Suggestion | None = None

        def close_suggestion(frame: Frame) -> Frame:
            nonlocal closed
            updated = frame.model_copy(deep=True)
            closed = close_suggestion_in_frame(updated, suggestion_id)
            return updated

        self._retry_metadata_update(frame_id, close_suggestion)
        if closed is None:
            raise SuggestionNotFoundError(suggestion_id)
        return closed

    def _write_frame(self, frame: Frame) -> None:
        self._write_body(frame)
        self._write_metadata(frame)

    def _write_metadata(self, frame: Frame, metadata_etag: str | None = None) -> None:
        kwargs = {
            "Bucket": self.bucket,
            "Key": self._key(frame.id, "metadata.json"),
            "Body": codec.encode_metadata(frame),
            "ContentType": codec.METADATA_CONTENT_TYPE,
        }
        if metadata_etag is not None:
            kwargs["IfMatch"] = metadata_etag
        self.s3.put_object(**kwargs)

    def _write_body(self, frame: Frame) -> None:
        self.s3.put_object(
            Bucket=self.bucket,
            Key=self._key(frame.id, "body.md"),
            Body=codec.encode_body(frame),
            ContentType=codec.BODY_CONTENT_TYPE,
        )

    def _retry_metadata_update(
        self,
        frame_id: str,
        mutate,
        write_body: bool = False,
    ) -> Frame:
        for attempt in range(5):
            frame, metadata_etag = self._read_frame_with_metadata_etag(frame_id)
            updated = mutate(frame)
            try:
                self._write_metadata(updated, metadata_etag=metadata_etag)
            except self.client_error as exc:
                code = exc.response.get("Error", {}).get("Code")
                status_code = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                if code in {"PreconditionFailed", "412"} or status_code == 412:
                    time.sleep(0.05 * (attempt + 1))
                    continue
                raise
            if write_body:
                self._write_body(updated)
            return updated
        raise ConcurrentFrameUpdateError(f"Could not update Frame {frame_id} after concurrent write retries")

    def _key(self, frame_id: str, name: str) -> str:
        return f"{self.prefix}/{frame_id}/{name}"
