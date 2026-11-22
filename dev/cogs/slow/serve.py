"""slow: takes `seconds` (2 by default) to answer, long enough to stop its host mid-step."""

import sys
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parent.parent)]

from fake_worker import envelope, serve  # noqa: E402

import time  # noqa: E402


def handle(entry_point, value, **feedback):
    seconds = float((value or {}).get("seconds", 2))
    time.sleep(seconds)
    return envelope({"slept": seconds})


if __name__ == "__main__":
    serve(handle)
