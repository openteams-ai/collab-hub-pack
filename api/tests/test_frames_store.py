from __future__ import annotations

import io
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from collab_hub_api.frames import codec
from collab_hub_api.frames.models import (
    FRAME_METADATA_SCHEMA_VERSION,
    Frame,
    FrameMetadata,
    SuggestionStatus,
    Visibility,
)
from collab_hub_api.frames.store import (
    ConcurrentFrameUpdateError,
    LocalFsFrameStore,
    S3FrameStore,
    compute_token_estimate,
)


def _create(store, **overrides):
    kwargs = {
        "org_id": "org-a",
        "workspace_id": "workspace-a",
        "created_by": "alice",
        "owners": ["alice"],
        "name": "Sales Playbook",
        "description": "",
        "visibility": Visibility.private,
        "tags": ["sales"],
        "body": "hello world",
    }
    kwargs.update(overrides)
    return store.create_frame(**kwargs)


def test_local_store_crud_and_suggestion_cleanup(tmp_path):
    store = LocalFsFrameStore(tmp_path)

    frame = _create(store)
    metadata = json.loads((tmp_path / frame.id / "metadata.json").read_text())
    assert frame.owners == ["alice"]
    assert frame.created_by == "alice"
    assert frame.visibility == Visibility.private
    assert frame.published is False
    assert frame.readers == []
    assert frame.name == "Sales Playbook"
    assert frame.schema_version == FRAME_METADATA_SCHEMA_VERSION
    assert metadata["schema_version"] == FRAME_METADATA_SCHEMA_VERSION
    assert metadata["name"] == "Sales Playbook"
    assert metadata["owners"] == ["alice"]
    assert "owner" not in metadata
    assert frame.org_id == "org-a"
    assert frame.workspace_id == "workspace-a"
    assert frame.token_estimate == compute_token_estimate("hello world")
    assert (tmp_path / frame.id / "body.md").read_text() == "hello world"
    assert store.list_frames("org-a", "workspace-a", tags=["sales"])[0].id == frame.id
    assert store.list_frames("org-a", "workspace-a", name="play")[0].id == frame.id
    assert store.list_frames("org-a", "workspace-a", owner="alice")[0].id == frame.id
    assert store.list_frames("org-a", "workspace-b") == []

    updated = store.update_frame(
        frame.id,
        "Legal Playbook",
        "Legal guidance",
        Visibility.internal,
        ["sales", "legal"],
        "updated body",
    )
    assert updated.name == "Legal Playbook"
    assert updated.description == "Legal guidance"
    assert updated.visibility == Visibility.internal
    assert updated.body == "updated body"
    assert updated.token_estimate == compute_token_estimate("updated body")

    # update_frame preserves governance fields.
    assert updated.owners == ["alice"]
    assert updated.created_by == "alice"

    owners_set = store.set_owners(frame.id, ["alice", "bob"])
    assert owners_set.owners == ["alice", "bob"]
    readers_set = store.set_readers(frame.id, ["carol"])
    assert readers_set.readers == ["carol"]
    published = store.set_published(frame.id, True)
    assert published.published is True
    # Body-preserving setters leave the body untouched.
    assert store.get_frame(frame.id).body == "updated body"

    suggestion = store.create_suggestion(frame.id, "bob", "try this")
    assert suggestion.status == SuggestionStatus.open
    assert store.list_suggestions(frame.id, SuggestionStatus.open)[0].id == suggestion.id

    closed = store.close_suggestion(frame.id, suggestion.id)
    assert closed.status == SuggestionStatus.closed
    assert store.list_suggestions(frame.id, SuggestionStatus.open) == []
    assert store.list_suggestions(frame.id, SuggestionStatus.closed)[0].id == suggestion.id

    store.delete_frame(frame.id)
    assert not (tmp_path / frame.id).exists()


def test_local_store_reads_legacy_metadata_without_schema_version(tmp_path):
    frame_id = "0" * 32
    frame_dir = tmp_path / frame_id
    frame_dir.mkdir()
    (frame_dir / "body.md").write_text("legacy body", encoding="utf-8")
    (frame_dir / "metadata.json").write_text(
        json.dumps(
            {
                "id": frame_id,
                "owner": "legacy-user",
                "tags": [],
                "token_estimate": 3,
                "suggestions": [],
                "created_at": "2026-06-01T00:00:00Z",
                "updated_at": "2026-06-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    frame = LocalFsFrameStore(tmp_path).get_frame(frame_id)

    assert frame.schema_version == FRAME_METADATA_SCHEMA_VERSION
    assert frame.name == frame_id
    assert frame.org_id == "dev-org"
    assert frame.workspace_id == "default"
    assert frame.body == "legacy body"
    # Legacy `owner` migrates to `owners`/`created_by` and new fields default.
    assert frame.owners == ["legacy-user"]
    assert frame.created_by == "legacy-user"
    assert frame.visibility == Visibility.private
    assert frame.published is False
    assert frame.readers == []
    assert frame.group_ids == []
    assert frame.description == ""


def test_normalize_metadata_repairs_reader_visibility_invariant(tmp_path):
    # A record persisted under the earlier "readers restrict internal" semantics
    # (internal + non-empty readers) must be coerced to private on read, so it
    # cannot widen to whole-tenant access once can_read ignores readers on
    # internal/public.
    frame_id = "1" * 32
    frame_dir = tmp_path / frame_id
    frame_dir.mkdir()
    (frame_dir / "body.md").write_text("legacy body", encoding="utf-8")
    (frame_dir / "metadata.json").write_text(
        json.dumps(
            {
                "id": frame_id,
                "org_id": "org-a",
                "workspace_id": "workspace-a",
                "name": "Legacy Restricted",
                "created_by": "alice",
                "owners": ["alice"],
                "visibility": "internal",
                "published": True,
                "readers": ["bob"],
                "tags": [],
                "token_estimate": 3,
                "suggestions": [],
                "created_at": "2026-06-01T00:00:00Z",
                "updated_at": "2026-06-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    frame = LocalFsFrameStore(tmp_path).get_frame(frame_id)

    # Coerced to private; the reader grant is preserved.
    assert frame.visibility == Visibility.private
    assert frame.readers == ["bob"]


@pytest.mark.parametrize(
    ("body", "estimate"),
    [
        ("", 0),
        ("a", 1),
        ("abcd", 1),
        ("abcde", 2),
    ],
)
def test_token_estimate_heuristic(body, estimate):
    assert compute_token_estimate(body) == estimate


def test_local_store_concurrent_suggestions_are_preserved(tmp_path):
    store = LocalFsFrameStore(tmp_path)
    frame = _create(store, name="Team Frame", tags=["team"], body="body")

    def submit(index: int):
        return store.create_suggestion(frame.id, f"user-{index}", f"suggestion {index}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        created = list(pool.map(submit, range(25)))

    suggestions = store.list_suggestions(frame.id)
    assert len(suggestions) == 25
    assert {item.id for item in suggestions} == {item.id for item in created}


class FakeS3ClientError(Exception):
    def __init__(self, code: str, status_code: int):
        self.response = {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status_code},
        }


def make_frame(frame_id: str = "0" * 32) -> Frame:
    now = datetime.now(UTC)
    return Frame(
        schema_version=FRAME_METADATA_SCHEMA_VERSION,
        id=frame_id,
        org_id="org-a",
        workspace_id="workspace-a",
        name="Frame",
        created_by="alice",
        owners=["alice"],
        tags=[],
        body="body",
        token_estimate=1,
        suggestions=[],
        created_at=now,
        updated_at=now,
    )


def test_s3_metadata_update_retries_on_etag_conflict():
    store = S3FrameStore.__new__(S3FrameStore)
    store.client_error = FakeS3ClientError
    reads = [("stale", make_frame()), ("fresh", make_frame())]
    writes = []
    body_writes = []

    def read_frame_with_etag(_frame_id):
        etag, frame = reads.pop(0)
        return frame, etag

    def write_metadata(_frame, metadata_etag=None):
        writes.append(metadata_etag)
        if metadata_etag == "stale":
            raise FakeS3ClientError("PreconditionFailed", 412)

    store._read_frame_with_metadata_etag = read_frame_with_etag
    store._write_metadata = write_metadata
    store._write_body = lambda frame: body_writes.append(frame.body)

    updated = store._retry_metadata_update(
        "0" * 32,
        lambda frame: frame.model_copy(update={"body": "updated"}),
        write_body=True,
    )

    assert updated.body == "updated"
    assert writes == ["stale", "fresh"]
    assert body_writes == ["updated"]


def test_s3_metadata_update_reports_exhausted_conflicts():
    store = S3FrameStore.__new__(S3FrameStore)
    store.client_error = FakeS3ClientError
    store._read_frame_with_metadata_etag = lambda _frame_id: (make_frame(), "stale")
    store._write_metadata = lambda _frame, metadata_etag=None: (_ for _ in ()).throw(
        FakeS3ClientError("PreconditionFailed", 412)
    )

    with pytest.raises(ConcurrentFrameUpdateError):
        store._retry_metadata_update("0" * 32, lambda frame: frame)


class RecordingFakeS3:
    """Minimal S3 client that records every key a caller fetches."""

    def __init__(self, metadata_by_id: dict, missing: set[str] | None = None):
        self.metadata_by_id = metadata_by_id
        self.missing = missing or set()
        self.requested: list[str] = []
        self._lock = threading.Lock()

    def get_paginator(self, operation: str):
        assert operation == "list_objects_v2"
        keys = []
        for frame_id in self.metadata_by_id:
            keys.append(f"frames/{frame_id}/metadata.json")
            keys.append(f"frames/{frame_id}/body.md")

        class _Paginator:
            def paginate(self, **_kwargs):
                return [{"Contents": [{"Key": key} for key in keys]}]

        return _Paginator()

    def get_object(self, Bucket: str, Key: str):  # noqa: N803 - boto3 casing
        del Bucket
        with self._lock:
            self.requested.append(Key)
        frame_id = Key.split("/")[-2]
        if frame_id in self.missing:
            raise FakeS3ClientError("NoSuchKey", 404)
        payload = json.dumps(self.metadata_by_id[frame_id]).encode("utf-8")
        return {"Body": io.BytesIO(payload), "ETag": '"etag"'}


def _metadata_payload(frame_id: str) -> dict:
    return make_frame(frame_id).model_dump(mode="json", exclude={"body"})


def _s3_store_with(fake: RecordingFakeS3) -> S3FrameStore:
    store = S3FrameStore.__new__(S3FrameStore)
    store.bucket = "bucket"
    store.prefix = "frames"
    store.client_error = FakeS3ClientError
    store.s3 = fake
    return store


def test_s3_list_frames_does_not_read_bodies():
    """Listing is latency-bound on round trips; bodies are never needed."""

    ids = [f"{index:032x}" for index in range(5)]
    fake = RecordingFakeS3({frame_id: _metadata_payload(frame_id) for frame_id in ids})
    store = _s3_store_with(fake)

    listed = store.list_frames("org-a", "workspace-a")

    assert [item.id for item in listed] == sorted(ids)
    assert all(key.endswith("/metadata.json") for key in fake.requested)
    assert len(fake.requested) == len(ids)


def test_s3_list_frames_skips_a_frame_deleted_mid_list():
    """A delete between the LIST and a GET must not fail the whole page."""

    ids = [f"{index:032x}" for index in range(4)]
    gone = ids[2]
    fake = RecordingFakeS3(
        {frame_id: _metadata_payload(frame_id) for frame_id in ids},
        missing={gone},
    )
    store = _s3_store_with(fake)

    listed = store.list_frames("org-a", "workspace-a")

    assert [item.id for item in listed] == sorted(set(ids) - {gone})


def test_s3_list_frames_warns_when_it_skips(caplog):
    """The skip is logged, so its rate is measurable rather than invisible."""

    ids = [f"{index:032x}" for index in range(3)]
    gone = ids[1]
    fake = RecordingFakeS3(
        {frame_id: _metadata_payload(frame_id) for frame_id in ids},
        missing={gone},
    )
    store = _s3_store_with(fake)

    with caplog.at_level(logging.WARNING, logger="frames_server.store"):
        store.list_frames("org-a", "workspace-a")

    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert gone in warnings[0].getMessage()


def test_s3_list_frames_returns_empty_without_reading_anything():
    """An empty store short-circuits before the thread pool."""

    fake = RecordingFakeS3({})
    store = _s3_store_with(fake)

    assert store.list_frames("org-a", "workspace-a") == []
    assert fake.requested == []


def test_s3_read_metadata_reraises_errors_that_are_not_missing_objects():
    """A permissions failure must not masquerade as a deleted Frame.

    ``list_frames`` skips FrameNotFoundError. If anything else were folded into
    it, an AccessDenied would silently shorten every page instead of failing.
    """

    frame_id = "0" * 32

    class DeniedS3(RecordingFakeS3):
        def get_object(self, Bucket: str, Key: str):  # noqa: N803 - boto3 casing
            raise FakeS3ClientError("AccessDenied", 403)

    store = _s3_store_with(DeniedS3({frame_id: _metadata_payload(frame_id)}))

    with pytest.raises(FakeS3ClientError):
        store._read_metadata(frame_id)
    with pytest.raises(FakeS3ClientError):
        store.list_frames("org-a", "workspace-a")


def test_s3_list_frames_skips_a_corrupt_frame(caplog):
    """A corrupt sidecar must not fail the whole page: skip it and log the skip.

    The object exists but is undecodable, so ``_read_metadata`` raises
    ``FrameDecodeError``. A direct GET of that frame still surfaces a 500; here,
    in a list, one bad sidecar must not hide every healthy frame.
    """

    ids = [f"{index:032x}" for index in range(4)]
    corrupt = ids[2]

    class CorruptingFakeS3(RecordingFakeS3):
        def get_object(self, Bucket: str, Key: str):  # noqa: N803 - boto3 casing
            if Key.split("/")[-2] == corrupt:
                with self._lock:
                    self.requested.append(Key)
                return {"Body": io.BytesIO(b"{not valid json"), "ETag": '"etag"'}
            return super().get_object(Bucket, Key)

    fake = CorruptingFakeS3({frame_id: _metadata_payload(frame_id) for frame_id in ids})
    store = _s3_store_with(fake)

    with caplog.at_level(logging.WARNING, logger="frames_server.codec"):
        listed = store.list_frames("org-a", "workspace-a")

    assert [item.id for item in listed] == sorted(set(ids) - {corrupt})
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert corrupt in warnings[0].getMessage()


def test_local_list_frames_skips_a_corrupt_frame(tmp_path, caplog):
    """The local backend isolates a corrupt sidecar the same way S3 does."""

    good = make_frame(frame_id="a" * 32)
    bad = make_frame(frame_id="b" * 32)
    store = LocalFsFrameStore(tmp_path)
    store._write_frame(good)
    store._write_frame(bad)
    (tmp_path / bad.id / "metadata.json").write_text("{not valid json", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="frames_server.codec"):
        listed = store.list_frames("org-a", "workspace-a")

    assert [item.id for item in listed] == [good.id]
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert bad.id in warnings[0].getMessage()


def test_local_list_frames_keeps_a_frame_whose_body_is_corrupt(tmp_path):
    """List reads metadata only, on both backends, so a bad body.md never hides a frame.

    S3 lists from metadata.json alone; the local list used to decode the body
    too, so the same corrupt frame vanished locally but not on S3. It must stay
    listed (discoverable) while a direct GET surfaces the decode error.
    """

    good = make_frame(frame_id="a" * 32)
    bad_body = make_frame(frame_id="b" * 32)
    store = LocalFsFrameStore(tmp_path)
    store._write_frame(good)
    store._write_frame(bad_body)
    (tmp_path / bad_body.id / "body.md").write_bytes(b"\xff\xfe")

    listed = store.list_frames("org-a", "workspace-a")

    assert [item.id for item in listed] == [good.id, bad_body.id]
    with pytest.raises(codec.FrameDecodeError):
        store.get_frame(bad_body.id)


def test_local_list_frames_keeps_a_frame_with_metadata_but_no_body(tmp_path):
    """Parity with S3, which discovers frames by their metadata.json key alone."""

    frame = make_frame(frame_id="a" * 32)
    store = LocalFsFrameStore(tmp_path)
    store._write_frame(frame)
    (tmp_path / frame.id / "body.md").unlink()

    assert [item.id for item in store.list_frames("org-a", "workspace-a")] == [frame.id]


def test_repeated_lists_warn_about_a_corrupt_frame_once(tmp_path, caplog):
    """A corrupt frame is skipped on every list; only the first skip warns."""

    bad = make_frame(frame_id="b" * 32)
    store = LocalFsFrameStore(tmp_path)
    store._write_frame(bad)
    (tmp_path / bad.id / "metadata.json").write_text("{not valid json", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="frames_server.codec"):
        for _ in range(5):
            assert store.list_frames("org-a", "workspace-a") == []

    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_s3_store_sizes_the_connection_pool_to_the_read_fan_out():
    """Botocore's default pool of 10 would serialise the surplus list workers."""

    store = S3FrameStore(bucket="bucket", region_name="us-east-1")

    assert store.s3.meta.config.max_pool_connections == S3FrameStore.LIST_WORKERS
    assert getattr(store.s3.meta.config, "s3", None) in (None, {})


def test_s3_store_keeps_path_addressing_for_s3_compatible_endpoints():
    store = S3FrameStore(
        bucket="bucket",
        endpoint_url="http://localhost:9000",
        region_name="us-east-1",
    )

    assert store.s3.meta.config.s3["addressing_style"] == "path"
    assert store.s3.meta.config.max_pool_connections == S3FrameStore.LIST_WORKERS


def test_s3_list_frames_still_applies_filters():
    ids = [f"{index:032x}" for index in range(3)]
    fake = RecordingFakeS3({frame_id: _metadata_payload(frame_id) for frame_id in ids})
    store = _s3_store_with(fake)

    assert store.list_frames("org-a", "workspace-a", owner="alice")
    assert store.list_frames("other-org", "workspace-a") == []


class CapturingFakeS3:
    """In-memory S3 client that round-trips the exact bytes it is handed.

    Stores whatever ``put_object`` writes and serves it back verbatim from
    ``get_object`` so a write→read cycle exercises the real encode *and* decode
    paths (no shortcut through a hand-built payload).
    """

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def put_object(self, Bucket, Key, Body, ContentType, **kwargs):  # noqa: N803 - boto3 casing
        del Bucket, ContentType, kwargs
        self.objects[Key] = Body

    def get_object(self, Bucket, Key):  # noqa: N803 - boto3 casing
        del Bucket
        if Key not in self.objects:
            raise FakeS3ClientError("NoSuchKey", 404)
        return {"Body": io.BytesIO(self.objects[Key]), "ETag": '"etag"'}


def _capturing_s3_store(fake: CapturingFakeS3) -> S3FrameStore:
    store = S3FrameStore.__new__(S3FrameStore)
    store.bucket = "bucket"
    store.prefix = "frames"
    store.client_error = FakeS3ClientError
    store.s3 = fake
    return store


def test_s3_store_round_trips_a_frame_through_the_codec():
    """A frame written to S3 reads back equal, exercising encode and decode.

    Covers the real ``_read_frame_with_metadata_etag`` path (both objects fetched
    and handed to the codec), which the ETag-retry tests stub out.
    """

    fake = CapturingFakeS3()
    store = _capturing_s3_store(fake)
    frame = make_frame(frame_id="b" * 32).model_copy(update={"name": "Café Playbook", "body": "hello café ☕"})

    store._write_frame(frame)

    assert store.get_frame(frame.id) == frame
    assert store._read_metadata(frame.id) == FrameMetadata(**frame.model_dump(exclude={"body"}))


def test_local_and_s3_backends_write_identical_bytes(tmp_path):
    """Issue #60 requirement (c): both backends persist the same bytes.

    Writing one Frame — with a non-ASCII name, so ``ensure_ascii`` escaping is in
    play — through each backend must produce byte-identical ``metadata.json`` and
    ``body.md``. The shared codec is what guarantees it.
    """

    frame = make_frame(frame_id="a" * 32).model_copy(update={"name": "Café Playbook", "body": "hello café ☕"})

    local = LocalFsFrameStore(tmp_path)
    local._write_frame(frame)
    local_metadata = (tmp_path / frame.id / "metadata.json").read_bytes()
    local_body = (tmp_path / frame.id / "body.md").read_bytes()

    fake = CapturingFakeS3()
    s3 = S3FrameStore.__new__(S3FrameStore)
    s3.bucket = "bucket"
    s3.prefix = "frames"
    s3.s3 = fake
    s3._write_frame(frame)

    assert fake.objects[f"frames/{frame.id}/metadata.json"] == local_metadata
    assert fake.objects[f"frames/{frame.id}/body.md"] == local_body
