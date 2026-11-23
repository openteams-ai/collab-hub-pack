"""The API image is also the Hub's worker image, so its user must satisfy ``runAsNonRoot``."""

import re
from pathlib import Path


def test_the_runtime_image_user_is_a_numeric_non_root_id() -> None:
    dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile"
    # A new stage resets the user to its base image's, so only the final stage counts.
    final_stage = re.split(r"^FROM\s", dockerfile.read_text(encoding="utf-8"), flags=re.MULTILINE)[-1]
    users = re.findall(r"^USER\s+(\S+)\s*$", final_stage, flags=re.MULTILINE)

    assert users, "the final stage must set a USER"
    match = re.fullmatch(r"(\d+)(?::(\d+))?", users[-1])
    assert match, f"Kubernetes cannot verify runAsNonRoot for the user name {users[-1]!r}"
    assert int(match.group(1)) != 0
