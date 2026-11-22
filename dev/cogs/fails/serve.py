"""fails: answers ok: false with an error code, so the step fails and the run with it."""

import sys
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parent.parent)]

from fake_worker import envelope, serve  # noqa: E402


def handle(entry_point, value, **feedback):
    return envelope(ok=False, error={"code": "model-call-failed", "detail": "the fake Cog always fails"})


if __name__ == "__main__":
    serve(handle)
