"""echo: answers with its input. The Op that always completes."""

import sys
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parent.parent)]

from fake_worker import envelope, serve  # noqa: E402


def handle(entry_point, value, **feedback):
    return envelope({"echo": value})


if __name__ == "__main__":
    serve(handle)
