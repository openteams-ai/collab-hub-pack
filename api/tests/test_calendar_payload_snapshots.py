"""Keep the committed Calendar payload snapshots in step with the code (issue #138)."""

from __future__ import annotations

from pathlib import Path

from snapshot_calendar_payloads import main as write_snapshots

AFTER = Path(__file__).parent / "fixtures" / "calendar_payloads" / "after"


async def test_calendar_payload_snapshots_match_the_code(tmp_path, monkeypatch):
    # The generator sets these itself; registering them here restores them afterwards.
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_BEARER_ALLOW_UNSIGNED", "true")

    await write_snapshots(tmp_path)

    expected = sorted(path.name for path in AFTER.glob("*.json"))
    assert sorted(path.name for path in tmp_path.glob("*.json")) == expected
    for name in expected:
        assert (tmp_path / name).read_text(encoding="utf-8") == (AFTER / name).read_text(encoding="utf-8"), (
            f"{name} is out of date; regenerate it as described in {AFTER.parent / 'README.md'}"
        )
