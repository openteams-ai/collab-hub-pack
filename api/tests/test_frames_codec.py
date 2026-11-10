from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import pytest

from collab_hub_api.frames import codec
from collab_hub_api.frames.codec import (
    FrameDecodeError,
    decode_frame,
    decode_metadata,
    encode_body,
    encode_metadata,
    normalize_metadata,
)
from collab_hub_api.frames.models import (
    FRAME_METADATA_SCHEMA_VERSION,
    Frame,
    FrameMetadata,
    Visibility,
)


def make_frame(**overrides) -> Frame:
    now = datetime(2026, 6, 1, tzinfo=UTC)
    kwargs = {
        "schema_version": FRAME_METADATA_SCHEMA_VERSION,
        "id": "0" * 32,
        "org_id": "org-a",
        "workspace_id": "workspace-a",
        "name": "Frame",
        "created_by": "alice",
        "owners": ["alice"],
        "tags": ["sales"],
        "body": "hello world",
        "token_estimate": 3,
        "suggestions": [],
        "created_at": now,
        "updated_at": now,
    }
    kwargs.update(overrides)
    return Frame(**kwargs)


# --- Round-trips -------------------------------------------------------------


def test_decode_frame_round_trips_encode():
    frame = make_frame()
    decoded = decode_frame(encode_metadata(frame), encode_body(frame))
    assert decoded == frame


def test_decode_metadata_round_trips_encode():
    frame = make_frame()
    decoded = decode_metadata(encode_metadata(frame))
    assert decoded == FrameMetadata(**frame.model_dump(exclude={"body"}))


# --- Format pin (stored-format regression guard) -----------------------------


def test_encode_metadata_pins_stored_byte_format():
    frame = make_frame()
    expected = (json.dumps(frame.model_dump(mode="json", exclude={"body"}), indent=2) + "\n").encode("utf-8")
    assert encode_metadata(frame) == expected


def test_encode_metadata_is_deterministic():
    frame = make_frame()
    assert encode_metadata(frame) == encode_metadata(frame)


def test_encode_metadata_keeps_ensure_ascii_escaping():
    # Non-ASCII must be \u-escaped: default ensure_ascii=True is part of the
    # stored contract, and changing it would rewrite every existing sidecar.
    frame = make_frame(name="Café Playbook")
    data = encode_metadata(frame)
    assert b"Caf\\u00e9" in data
    assert "Café".encode("utf-8") not in data
    # And it still decodes back to the original name.
    assert decode_metadata(data).name == "Café Playbook"


def test_encode_body_is_utf8_bytes():
    frame = make_frame(body="café ☕")
    assert encode_body(frame) == "café ☕".encode("utf-8")


# --- Legacy normalization via the codec --------------------------------------


def test_decode_migrates_legacy_owner_scalar():
    legacy = {
        "id": "0" * 32,
        "owner": "legacy-user",
        "tags": [],
        "token_estimate": 3,
        "suggestions": [],
        "created_at": "2026-06-01T00:00:00Z",
        "updated_at": "2026-06-01T00:00:00Z",
    }
    metadata = decode_metadata(json.dumps(legacy).encode("utf-8"))
    assert metadata.owners == ["legacy-user"]
    assert metadata.created_by == "legacy-user"


def test_decode_defaults_missing_governance_fields():
    legacy = {
        "id": "0" * 32,
        "owners": ["alice"],
        "tags": [],
        "token_estimate": 3,
        "suggestions": [],
        "created_at": "2026-06-01T00:00:00Z",
        "updated_at": "2026-06-01T00:00:00Z",
    }
    metadata = decode_metadata(json.dumps(legacy).encode("utf-8"))
    assert metadata.schema_version == FRAME_METADATA_SCHEMA_VERSION
    assert metadata.org_id == "dev-org"
    assert metadata.workspace_id == "default"
    assert metadata.name == "0" * 32  # defaults to id


def test_decode_repairs_reader_visibility_invariant():
    # internal + non-empty readers must be coerced to private on read so it can
    # never widen to whole-tenant access (Spec 1 §3.3).
    record = {
        "id": "1" * 32,
        "org_id": "org-a",
        "workspace_id": "workspace-a",
        "name": "Legacy Restricted",
        "created_by": "alice",
        "owners": ["alice"],
        "visibility": "internal",
        "readers": ["bob"],
        "tags": [],
        "token_estimate": 3,
        "suggestions": [],
        "created_at": "2026-06-01T00:00:00Z",
        "updated_at": "2026-06-01T00:00:00Z",
    }
    metadata = decode_metadata(json.dumps(record).encode("utf-8"))
    assert metadata.visibility == Visibility.private
    assert metadata.readers == ["bob"]


def test_normalize_metadata_mutates_in_place():
    # Callers (and identity_inventory's mirror) rely on in-place mutation.
    metadata = {
        "id": "0" * 32,
        "owner": "legacy-user",
        "tags": [],
        "token_estimate": 3,
        "suggestions": [],
        "created_at": "2026-06-01T00:00:00Z",
        "updated_at": "2026-06-01T00:00:00Z",
    }
    returned = normalize_metadata(metadata)
    assert returned is metadata
    assert metadata["owners"] == ["legacy-user"]
    assert "owner" not in metadata


# --- Error cases -------------------------------------------------------------


def _valid_metadata_bytes() -> bytes:
    return encode_metadata(make_frame())


def test_decode_metadata_wraps_invalid_json():
    with pytest.raises(FrameDecodeError) as exc_info:
        decode_metadata(b"{not json", frame_id="abc")
    assert exc_info.value.frame_id == "abc"
    assert isinstance(exc_info.value.__cause__, json.JSONDecodeError)


def test_decode_metadata_wraps_undecodable_bytes():
    # json.loads does its own encoding detection, so these bytes surface as a
    # JSONDecodeError rather than a UnicodeDecodeError; either way the codec
    # wraps it instead of leaking a raw decode error. (The UnicodeDecodeError
    # path is exercised directly by the body decode below.)
    with pytest.raises(FrameDecodeError) as exc_info:
        decode_metadata(b"\xff\xfe", frame_id="abc")
    assert exc_info.value.frame_id == "abc"
    assert exc_info.value.__cause__ is not None


def test_decode_metadata_rejects_top_level_array():
    with pytest.raises(FrameDecodeError) as exc_info:
        decode_metadata(b"[]", frame_id="abc")
    # An explicit FrameDecodeError, not a raw TypeError from Frame(**[]).
    assert exc_info.value.frame_id == "abc"


def test_decode_metadata_wraps_missing_id():
    data = json.dumps({"owners": ["alice"]}).encode("utf-8")
    with pytest.raises(FrameDecodeError) as exc_info:
        decode_metadata(data)
    assert isinstance(exc_info.value.__cause__, KeyError)


def test_decode_metadata_wraps_unknown_extra_key():
    record = json.loads(_valid_metadata_bytes())
    record["surprise"] = "value"  # extra="forbid" rejects this
    with pytest.raises(FrameDecodeError) as exc_info:
        decode_metadata(json.dumps(record).encode("utf-8"))
    assert exc_info.value.__cause__ is not None


def test_decode_metadata_wraps_bad_field_value():
    record = json.loads(_valid_metadata_bytes())
    record["tags"] = ["Bad Tag!"]  # fails tag pattern validation
    with pytest.raises(FrameDecodeError) as exc_info:
        decode_metadata(json.dumps(record).encode("utf-8"))
    assert exc_info.value.__cause__ is not None


def test_decode_frame_wraps_empty_body():
    # body has min_length=1, so an empty body.md is a decode error, not a 404.
    frame = make_frame()
    with pytest.raises(FrameDecodeError) as exc_info:
        decode_frame(encode_metadata(frame), b"", frame_id="abc")
    assert exc_info.value.frame_id == "abc"
    assert exc_info.value.__cause__ is not None


def test_decode_frame_wraps_invalid_body_utf8():
    frame = make_frame()
    with pytest.raises(FrameDecodeError) as exc_info:
        decode_frame(encode_metadata(frame), b"\xff\xfe")
    assert isinstance(exc_info.value.__cause__, UnicodeDecodeError)


def test_decode_frame_rejects_a_sidecar_carrying_a_body_key():
    """A ``body`` key in metadata.json is a corrupt sidecar, not a kwarg clash.

    Splatting it into ``Frame(**md, body=...)`` raised a raw ``TypeError`` that
    no skip path caught; it must be the same typed error ``decode_metadata``
    already gives for it (``extra="forbid"``).
    """

    record = json.loads(_valid_metadata_bytes())
    record["body"] = "smuggled"
    with pytest.raises(FrameDecodeError) as exc_info:
        decode_frame(json.dumps(record).encode("utf-8"), b"# Body", frame_id="abc")
    assert exc_info.value.frame_id == "abc"


def _malformed_sidecars() -> dict[str, bytes]:
    def with_record(mutate) -> bytes:
        record = json.loads(_valid_metadata_bytes())
        mutate(record)
        return json.dumps(record).encode("utf-8")

    return {
        "invalid-json": b"{not json",
        "undecodable-bytes": b"\xff\xfe",
        "top-level-array": b"[]",
        "top-level-string": b'"frame"',
        "missing-id": json.dumps({"owners": ["alice"]}).encode("utf-8"),
        "unknown-key": with_record(lambda r: r.update(surprise="value")),
        "body-key": with_record(lambda r: r.update(body="smuggled")),
        "bad-tag": with_record(lambda r: r.update(tags=["Bad Tag!"])),
        "non-string-id": with_record(lambda r: r.update(id=7)),
        "null-readers": with_record(lambda r: r.update(readers=None)),
        # normalize_metadata indexes owners[0] to default created_by.
        "int-owners": with_record(lambda r: (r.pop("created_by"), r.update(owners=5))),
        "future-schema-version": with_record(lambda r: r.update(schema_version=FRAME_METADATA_SCHEMA_VERSION + 1)),
    }


@pytest.mark.parametrize("case", sorted(_malformed_sidecars()))
def test_both_decoders_agree_on_every_malformed_sidecar(case):
    """The metadata-only and full-frame decoders must fail identically.

    Backends list with ``decode_metadata`` and GET with ``decode_frame``; if a
    sidecar decodes under one and escapes the other (or raises an untyped
    error), listing and GET diverge. Every malformed sidecar must raise
    ``FrameDecodeError`` from both.
    """

    data = _malformed_sidecars()[case]
    with pytest.raises(FrameDecodeError):
        decode_metadata(data, frame_id="abc")
    with pytest.raises(FrameDecodeError):
        decode_frame(data, b"# Body", frame_id="abc")


def _skip(log: codec.UndecodableFrameLog, frame_id: str, context: str = "frame list") -> None:
    try:
        decode_metadata(b"{not json", frame_id=frame_id)
    except FrameDecodeError as exc:
        log.skipped(exc, context=context)


def test_skip_log_warns_once_per_frame_per_interval_with_the_cause(caplog):
    """A corrupt frame read on every request must not flood the log.

    The first skip of a frame warns (with the frame id and the underlying
    cause attached); repeats inside the interval drop to DEBUG; once the
    interval passes, a still-corrupt frame warns again so it is not forgotten.
    """

    now = [1000.0]
    log = codec.UndecodableFrameLog(clock=lambda: now[0])

    with caplog.at_level(logging.DEBUG, logger="frames_server.codec"):
        _skip(log, "a" * 32)
        _skip(log, "a" * 32, context="group g1 projection")
        _skip(log, "a" * 32)
        _skip(log, "b" * 32)
        now[0] += codec.UNDECODABLE_WARN_INTERVAL_SECONDS + 1
        _skip(log, "a" * 32)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    debugs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert ["a" * 32 in r.getMessage() for r in warnings] == [True, False, True]
    assert "b" * 32 in warnings[1].getMessage()
    assert len(debugs) == 2
    # The cause travels with the warning, so the operator can diagnose it.
    assert warnings[0].exc_info is not None
    assert isinstance(warnings[0].exc_info[1], FrameDecodeError)


def test_a_type_error_bug_in_the_codec_is_not_mistaken_for_corrupt_data(monkeypatch):
    """Only bad *data* becomes FrameDecodeError; a code defect must crash loudly.

    If the decoders caught every TypeError, a bug in normalization would turn
    every frame into a "corrupt" one, and lists would quietly come back empty
    instead of failing.
    """

    def buggy(metadata: dict) -> dict:
        raise TypeError("bug in normalize_metadata")

    monkeypatch.setattr(codec, "normalize_metadata", buggy)
    with pytest.raises(TypeError):
        decode_metadata(_valid_metadata_bytes(), frame_id="abc")
    with pytest.raises(TypeError):
        decode_frame(_valid_metadata_bytes(), b"# Body", frame_id="abc")


def test_a_sidecar_from_a_newer_schema_version_is_a_decode_error_not_a_guess():
    """Schema-version drift: a sidecar written by a newer writer is not silently read as v1.

    A missing ``schema_version`` is legacy data and defaults to the current
    version (see ``test_decode_defaults_missing_governance_fields``). A *higher*
    version means fields this reader does not understand, so it fails as a
    typed decode error (a 500 on GET, skipped in lists) instead of being
    coerced or partially read.
    """

    record = json.loads(_valid_metadata_bytes())
    record["schema_version"] = FRAME_METADATA_SCHEMA_VERSION + 1
    data = json.dumps(record).encode("utf-8")

    with pytest.raises(FrameDecodeError) as exc_info:
        decode_metadata(data, frame_id="abc")
    assert exc_info.value.frame_id == "abc"
    with pytest.raises(FrameDecodeError):
        decode_frame(data, b"# Body", frame_id="abc")
