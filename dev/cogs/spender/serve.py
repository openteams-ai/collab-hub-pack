"""spender: reports the `tokens` it spent (100 by default), to run into a budget."""

import sys
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parent.parent)]

from fake_worker import envelope, serve  # noqa: E402


def handle(entry_point, value, **feedback):
    tokens = int((value or {}).get("tokens", 100))
    return envelope({"spent": tokens}, usage={"tokens": tokens, "cost": tokens / 10000})


if __name__ == "__main__":
    serve(handle)
