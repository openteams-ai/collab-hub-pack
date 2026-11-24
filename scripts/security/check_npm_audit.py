#!/usr/bin/env python3
"""Fail on any npm audit advisory that an unexpired allowlist entry does not cover.

    python3 scripts/security/check_npm_audit.py DIRECTORY [--allowlist FILE] [--report FILE] [--level LEVEL]

Runs ``npm audit --json --package-lock-only`` in DIRECTORY (or reads a saved report with
``--report``), which audits DIRECTORY/package-lock.json, not whatever is installed, so nothing
needs to be installed and no dependency's code runs. It then checks each advisory at or above LEVEL (default ``low``, as ``npm audit --audit-level=low``) against
DIRECTORY/audit-allowlist.json. It fails on:

- an advisory at or above LEVEL whose GHSA ID no allowlist entry names;
- an allowlist entry past its ``expires`` date (if it has one), whether or not its advisory is
  still reported, so that exception is reviewed again;
- a ``dev_only`` allowlist entry whose advisory reaches a copy of a package that
  DIRECTORY/package-lock.json does not mark ``dev``, so it would ship;
- a report npm could not produce (a registry error, say), and a malformed allowlist.

It warns, without failing, on an unexpired allowlist entry no advisory in the report matches, so a
fixed or withdrawn advisory's exception is removed rather than left to cover a later recurrence.
That is a warning because it follows from a change upstream (a fix, a withdrawn advisory, a
narrowed range), not in this repo, and should not turn every open pull request red. Under GitHub
Actions it is a ``::warning::`` annotation on the entry's line in the allowlist.

It exists because ``npm audit`` cannot ignore an advisory, and an advisory with no patched release
otherwise blocks CI until upstream ships one. audit-ci is not used: its last release (7.1.0)
silently passes an advisory on a transitive package whose ``via`` list starts with another
vulnerable package (IBM/audit-ci#360), which is exactly the state allowlisting one package leaves
its dependents in. Standard library only. Tested in tests/test_check_npm_audit.py, next to this
script.

The allowlist is a JSON object whose ``allowlist`` list holds one entry per advisory::

    {"id": "GHSA-xxxx-xxxx-xxxx", "expires": "YYYY-MM-DD" or null,
     "reason": "why it does not affect us", "tracking": "URL of the upstream fix or issue",
     "dev_only": true}

An entry covers its advisory, wherever it appears in the dependency tree, through the end of its
``expires`` day (UTC), or, when ``expires`` is null, until the entry is removed (with a warning once
its advisory is no longer reported, as it would cover the advisory again if it came back). Null must
be written out: an entry without the key is refused, so no exception is permanent by omission.

``dev_only`` is optional (default false). When true, the entry covers its advisory only while every
vulnerable copy npm audit reports is a devDependency-only node in the lockfile (``"dev": true``; a
``devOptional`` node can install in production, so it does not count). Set it on any entry whose
reason is that the advisory does not ship, so that reason is checked rather than assumed.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ALLOWLIST_NAME = "audit-allowlist.json"
LOCKFILE_NAME = "package-lock.json"
SEVERITIES = ("info", "low", "moderate", "high", "critical")
GHSA_ID = re.compile(r"GHSA(?:-[23456789cfghjmpqrvwx]{4}){3}")
GHSA_URL = re.compile(r"https://github\.com/advisories/(" + GHSA_ID.pattern + r")")
REQUIRED_KEYS = {"id", "expires", "reason", "tracking"}
OPTIONAL_KEYS = {"dev_only"}


@dataclass
class Advisory:
    id: str
    severity: str
    title: str
    packages: set[str] = field(default_factory=set)
    # Lockfile paths (node_modules/...) of the vulnerable copies npm audit reports.
    nodes: set[str] = field(default_factory=set)


@dataclass
class Entry:
    id: str
    expires: datetime.date | None
    reason: str
    tracking: str
    dev_only: bool = False


def load_allowlist(path: Path) -> dict[str, Entry]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise SystemExit(f"error: {path}: {error}") from None
    except ValueError as error:
        raise SystemExit(f"error: {path}: not valid JSON: {error}") from None
    if not isinstance(data, dict) or not isinstance(data.get("allowlist"), list):
        raise SystemExit(f'error: {path}: expected an object with an "allowlist" list')
    entries = {}
    for index, raw in enumerate(data["allowlist"]):
        where = f"{path}: allowlist[{index}]"
        # A malformed entry (a typo'd ID, an unparseable date) would never match or never expire,
        # so refuse it rather than let it silently cover nothing or everything forever.
        if not isinstance(raw, dict) or not REQUIRED_KEYS <= set(raw) <= REQUIRED_KEYS | OPTIONAL_KEYS:
            raise SystemExit(
                f"error: {where}: expected the keys {sorted(REQUIRED_KEYS)}, plus optionally {sorted(OPTIONAL_KEYS)}"
            )
        if not all(isinstance(raw[key], str) and raw[key].strip() for key in REQUIRED_KEYS - {"expires"}):
            raise SystemExit(f"error: {where}: id, reason and tracking must be non-empty strings")
        if not GHSA_ID.fullmatch(raw["id"]):
            raise SystemExit(f"error: {where}: not a GHSA ID: {raw['id']!r}")
        if raw["id"] in entries:
            raise SystemExit(f"error: {where}: {raw['id']} is listed twice")
        expires = None
        if raw["expires"] is not None:
            try:
                expires = datetime.date.fromisoformat(raw["expires"])
            except (TypeError, ValueError):
                raise SystemExit(
                    f"error: {where}: expires is neither a YYYY-MM-DD date nor null: {raw['expires']!r}"
                ) from None
        dev_only = raw.get("dev_only", False)
        if not isinstance(dev_only, bool):
            raise SystemExit(f"error: {where}: dev_only must be true or false: {dev_only!r}")
        entries[raw["id"]] = Entry(raw["id"], expires, raw["reason"], raw["tracking"], dev_only)
    return entries


def load_lockfile(path: Path) -> dict[str, dict]:
    """Return the lockfile's ``packages`` map (lockfileVersion 2 or 3), keyed by node_modules path."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SystemExit(f"error: {path}: cannot read it to check dev_only entries: {error}") from None
    if not isinstance(data, dict) or not isinstance(data.get("packages"), dict):
        raise SystemExit(f'error: {path}: no "packages" map (lockfileVersion 2 or later) to check dev_only entries')
    return data["packages"]


def run_npm_audit(directory: Path) -> str:
    npm = shutil.which("npm")
    if npm is None:
        raise SystemExit("error: npm not found on PATH")
    # npm audit exits non-zero whenever it finds anything; the JSON on stdout is what counts, and
    # parse_report refuses it if npm could not audit at all. --package-lock-only audits the committed
    # lockfile, the same tree the dev_only check reads, even where node_modules is stale or absent.
    result = subprocess.run(
        [npm, "audit", "--json", "--package-lock-only"], cwd=directory, capture_output=True, text=True, check=False
    )
    if not result.stdout.strip():
        raise SystemExit(f"error: npm audit printed no report (exit {result.returncode}):\n{result.stderr}")
    return result.stdout


def parse_report(text: str) -> dict[str, Advisory]:
    try:
        report = json.loads(text)
    except ValueError as error:
        raise SystemExit(f"error: npm audit report is not JSON: {error}") from None
    if not isinstance(report, dict):
        raise SystemExit("error: npm audit report is not a JSON object")
    if "error" in report:
        raise SystemExit(f"error: npm audit failed: {json.dumps(report['error'])}")
    if report.get("auditReportVersion") != 2 or not isinstance(report.get("vulnerabilities"), dict):
        raise SystemExit("error: not an npm audit report (auditReportVersion 2 with vulnerabilities)")
    advisories: dict[str, Advisory] = {}
    # Every advisory object in every package's via list counts, wherever it sits in the list: the
    # strings there only name other vulnerable packages, whose own entries carry their advisories.
    for package, vulnerability in report["vulnerabilities"].items():
        for via in vulnerability.get("via", []):
            if not isinstance(via, dict):
                continue
            url = str(via.get("url", ""))
            match = GHSA_URL.fullmatch(url)
            # An advisory without a GHSA URL cannot be allowlisted, so it fails at any listed level.
            advisory_id = match.group(1) if match else url or f"untitled advisory on {package}"
            severity = via.get("severity") if via.get("severity") in SEVERITIES else "critical"
            advisory = advisories.setdefault(
                advisory_id, Advisory(advisory_id, severity, str(via.get("title", "")).strip())
            )
            if SEVERITIES.index(severity) > SEVERITIES.index(advisory.severity):
                advisory.severity = severity
            advisory.packages.add(str(via.get("name") or package))
            advisory.nodes.update(str(node) for node in vulnerability.get("nodes", []))
    return advisories


def shipped_nodes(advisory: Advisory, lock_packages: dict[str, dict]) -> list[str]:
    """Return the advisory's vulnerable copies the lockfile does not mark dev-only."""
    if not advisory.nodes:
        # Nothing to check against, so nothing proves the advisory stays out of production.
        return ["(npm audit listed no installed copies)"]
    return sorted(
        node
        for node in advisory.nodes
        if not (isinstance(lock_packages.get(node), dict) and lock_packages[node].get("dev") is True)
    )


def check(
    advisories: dict[str, Advisory],
    allowlist: dict[str, Entry],
    level: str,
    today: datetime.date,
    lock_packages: dict[str, dict] | None = None,
) -> tuple[list[str], list[str], dict[str, str]]:
    """Return (report lines, failures, warnings by GHSA ID).

    ``lock_packages`` is required if any entry is dev_only.
    """
    lines: list[str] = []
    failures: list[str] = []
    warnings: dict[str, str] = {}
    threshold = SEVERITIES.index(level)
    for advisory in sorted(advisories.values(), key=lambda a: (-SEVERITIES.index(a.severity), a.id)):
        packages = ", ".join(sorted(advisory.packages))
        lines.append(f"{advisory.severity:<8} {advisory.id} in {packages}: {advisory.title}")
        entry = allowlist.get(advisory.id)
        if entry is None:
            if SEVERITIES.index(advisory.severity) >= threshold:
                failures.append(f"{advisory.id} ({advisory.severity}, {packages}) is not allowlisted")
            else:
                lines.append(f"         below --level {level}, ignored")
            continue
        if entry.expires is not None and entry.expires < today:
            failures.append(f"{advisory.id}: allowlist entry expired on {entry.expires}; fix it or review it again")
            continue
        if entry.dev_only:
            shipped = shipped_nodes(advisory, lock_packages or {})
            if shipped:
                failures.append(
                    f"{advisory.id}: allowlisted as dev_only, but not dev-only in the lockfile: {', '.join(shipped)}"
                )
                continue
        until = f"until {entry.expires}" if entry.expires else "with no expiry"
        scope = " (dev-only, checked)" if entry.dev_only else ""
        lines.append(f"         allowlisted{scope} {until}: {entry.reason} ({entry.tracking})")
    for entry in allowlist.values():
        if entry.id in advisories:
            continue
        if entry.expires is not None and entry.expires < today:
            failures.append(f"{entry.id}: allowlist entry expired on {entry.expires}; no longer reported, so remove it")
        else:
            warnings[entry.id] = f"{entry.id}: allowlisted but no longer reported; remove its allowlist entry"
    return lines, failures, warnings


def annotation(message: str, allowlist_path: Path, advisory_id: str) -> str:
    """Return a GitHub Actions ``::warning::`` command for ``message``, on the entry's line if found."""

    def escape_data(value: str) -> str:
        return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")

    def escape_property(value: str) -> str:
        return escape_data(value).replace(":", "%3A").replace(",", "%2C")

    properties = [f"file={escape_property(allowlist_path.as_posix())}"]
    try:
        text = allowlist_path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    for number, line in enumerate(text.splitlines(), start=1):
        if f'"{advisory_id}"' in line:
            properties.append(f"line={number}")
            break
    properties.append("title=Stale npm audit allowlist entry")
    return f"::warning {','.join(properties)}::{escape_data(message)}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directory", type=Path, help="the npm project to audit (where package-lock.json is)")
    parser.add_argument(
        "--allowlist", type=Path, help=f"allowlist file (default: DIRECTORY/{ALLOWLIST_NAME}; none if absent)"
    )
    parser.add_argument("--report", type=Path, help="read this saved `npm audit --json` report instead of running it")
    parser.add_argument("--level", choices=SEVERITIES[1:], default="low", help="lowest severity that fails")
    parser.add_argument("--today", type=datetime.date.fromisoformat, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    allowlist_path = args.allowlist or args.directory / ALLOWLIST_NAME
    allowlist = load_allowlist(allowlist_path)
    lock_packages = None
    if any(entry.dev_only for entry in allowlist.values()):
        lock_packages = load_lockfile(args.directory / LOCKFILE_NAME)
    text = args.report.read_text(encoding="utf-8") if args.report else run_npm_audit(args.directory)
    advisories = parse_report(text)
    today = args.today or datetime.datetime.now(datetime.timezone.utc).date()
    lines, failures, warnings = check(advisories, allowlist, args.level, today, lock_packages)

    print(f"Advisories reported: {len(advisories)}; allowlisted: {len(allowlist)}; failing at {args.level} or above")
    for line in lines:
        print(line)
    in_github_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    for advisory_id, warning in warnings.items():
        # GitHub reads workflow commands from stdout only.
        if in_github_actions:
            print(annotation(warning, allowlist_path, advisory_id))
        else:
            print(f"WARNING: {warning}")
    sys.stdout.flush()
    for failure in failures:
        print(f"FAIL: {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
