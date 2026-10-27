#!/usr/bin/env python3
"""Read a PR body from stdin and write its squash commit body to stdout."""

import re
import sys

MARKER = "<!-- reviewer-context -->"
OPEN_DETAILS = re.compile(r"<details\b[^>]*>", re.IGNORECASE)
CLOSE_DETAILS = re.compile(r"</details\s*>", re.IGNORECASE)


def strip_pr_body(body: str) -> str:
    """Drop reviewer context, then the first-through-last details block span."""
    summary = body.split(MARKER, 1)[0]
    opening = OPEN_DETAILS.search(summary)
    closings = list(CLOSE_DETAILS.finditer(summary))
    if opening or closings:
        tags = [(match.start(), 1) for match in OPEN_DETAILS.finditer(summary)]
        tags.extend((match.start(), -1) for match in closings)
        depth = 0
        for _, delta in sorted(tags):
            depth += delta
            if depth < 0:
                raise ValueError("unmatched <details> tags; inspect the PR body manually")
        if depth:
            raise ValueError("unmatched <details> tags; inspect the PR body manually")
        summary = summary[: opening.start()] + summary[closings[-1].end() :]
    summary = summary.strip()
    return summary + "\n" if summary else ""


def main() -> int:
    try:
        result = strip_pr_body(sys.stdin.buffer.read().decode("utf-8"))
    except (ValueError, UnicodeError) as error:
        print(f"strip_pr_body: {error}", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(result.encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
