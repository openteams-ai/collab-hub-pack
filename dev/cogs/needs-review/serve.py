"""needs-review: its first answer carries an `error` problem, so the step's default Gate escalates.

Sent back with findings, it answers again without the problem, so a person's
send back and approval can both be seen at dev level 1.
"""

import sys
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parent.parent)]

from fake_worker import envelope, serve  # noqa: E402


def handle(entry_point, value, **feedback):
    if "signal" in feedback:
        return envelope({"draft": value, "revised_for": feedback["signal"]})
    problem = {"check": "grounding", "detail": "a claim cites no source", "severity": "error"}
    return envelope({"draft": value}, problems=[problem])


if __name__ == "__main__":
    serve(handle)
