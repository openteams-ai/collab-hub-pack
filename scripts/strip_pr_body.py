#!/usr/bin/env python3
"""Read a PR body from stdin and write its squash commit body to stdout."""

import re
import sys

MARKER = "<!-- reviewer-context -->"
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
HEADING = re.compile(r"^##+ .*(?:\n|$)", re.MULTILINE)
# Only tags at the start of a line count, so a literal <details> in prose survives.
OPEN_DETAILS = re.compile(r"^[ \t]*<details\b[^>]*>", re.IGNORECASE | re.MULTILINE)
CLOSE_DETAILS = re.compile(r"^[ \t]*</details\s*>", re.IGNORECASE | re.MULTILINE)
UNEDITED = re.compile(r"SUMMARY_HERE|Fixes #(?!\d)")


def strip_pr_body(body: str) -> str:
    """Drop reviewer context, comments, headings, then the details block span."""
    summary = body.replace("\r\n", "\n").split(MARKER, 1)[0]
    summary = HEADING.sub("", HTML_COMMENT.sub("", summary))
    opening = OPEN_DETAILS.search(summary)
    closings = list(CLOSE_DETAILS.finditer(summary))
    if opening or closings:
        tags = [(match.start(), 1) for match in OPEN_DETAILS.finditer(summary)]
        tags.extend((match.start(), -1) for match in closings)
        depth = 0
        for _, delta in sorted(tags):
            depth += delta
            if depth < 0:
                raise ValueError("unmatched </details> tag; inspect the PR body manually")
        # GitHub truncates long Dependabot bodies, leaving details unclosed.
        end = len(summary) if depth else closings[-1].end()
        summary = summary[: opening.start()] + summary[end:]
    if UNEDITED.search(summary):
        raise ValueError("PR template placeholders are unedited; fill in the summary and issue number")
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
