#!/usr/bin/env python3
"""Fail on any npm audit advisory that an allowlist entry does not cover.

    python3 scripts/security/check_npm_audit.py DIRECTORY [--allowlist FILE] [--level LEVEL]
                                                [--prod-level LEVEL] [--strict] [--report FILE]
    python3 scripts/security/check_npm_audit.py --list-projects [--allowlist SHARED_FILE]

Runs ``npm audit`` on DIRECTORY/package-lock.json alone (``--package-lock-only``), so nothing needs
to be installed and no dependency's code runs, or reads a saved ``npm audit --json`` report with
``--report``. Every npm setting that decides what is audited is passed as a flag, and flags override
the environment and any .npmrc, so offline mode (which skips the advisory lookup and reports
nothing), NODE_ENV=production or ``omit=dev`` (which drop dependency groups) cannot make the audit
report less than the lockfile holds.

Each advisory is checked against the allowlist: DIRECTORY/audit-allowlist.json, or the file
``--allowlist`` names (none if it is absent). The check fails (exit 1) on:

- an advisory no allowlist entry covers, at or above LEVEL (default ``low``), or at or above the
  production level (``--prod-level``, default LEVEL) where it reaches a copy the lockfile does not
  mark ``dev``, so it ships;
- a ``dev_only`` entry whose advisory reaches a copy the lockfile does not mark ``dev``;
- with ``--strict``, an entry due for review: past its ``expires`` date, or covering an advisory
  that is no longer reported.

Without ``--strict``, an entry due for review only warns, and an expired entry still covers its
advisory. Both follow from the calendar or from a change upstream (a fix, a withdrawn advisory, a
narrowed range), not from the pull request being checked, so they should not turn every open pull
request red; the weekly scheduled run passes ``--strict``, so no review is missed. Under GitHub
Actions each is an annotation (``::warning::``, or ``::error::`` with ``--strict``) on the entry's
line in the allowlist.

It refuses (exit 2) whatever it cannot trust: an npm run that ended abnormally or printed no report,
a report npm could not produce (a registry error, say), a report that is not internally consistent
(an entry no advisory stands behind, a package named in a ``via`` list that the report does not
describe, a summary that disagrees with the entries), and a malformed allowlist. An advisory with an
unknown severity counts as critical, and one without a GHSA URL cannot be allowlisted, so both fail
rather than refuse the report.

``--list-projects`` prints, as a JSON list, the directory of every committed package-lock.json
(``git ls-files``; ``.`` for the repository root), for a CI matrix that audits every npm project
without anyone having to list them. Given a shared allowlist, it also refuses (exit 2) unless that
allowlist's ``projects`` are exactly those directories.

It exists because ``npm audit`` cannot ignore an advisory, and an advisory with no patched release
otherwise blocks CI until upstream ships one. audit-ci is not used: its last release (7.1.0)
silently passes an advisory on a transitive package whose ``via`` list starts with another
vulnerable package (IBM/audit-ci#360), which is exactly the state allowlisting one package leaves
its dependents in. Standard library only. Tested in tests/test_check_npm_audit.py, next to this
script.

The allowlist is a JSON object whose ``allowlist`` list holds one entry per advisory::

    {"id": "GHSA-xxxx-xxxx-xxxx", "expires": "YYYY-MM-DD",
     "reason": "why it does not affect us", "tracking": "URL of the upstream fix or issue",
     "dev_only": true, "package": "name"}

An entry covers its advisory, wherever it appears in the dependency tree, through the end of its
``expires`` day (UTC). ``expires`` is required and at most 367 days after the day the check runs, so
every exception is reviewed at least yearly. ``dev_only`` and ``package`` are optional:

- ``dev_only`` (default false): the entry covers its advisory only while every vulnerable copy npm
  audit reports is a devDependency-only node in the lockfile (``"dev": true``; a ``devOptional``
  node can install in production, so it does not count). Set it on any entry whose reason is that
  the advisory does not ship, so that reason is checked rather than assumed.
- ``package``: the entry covers its advisory only in that package, for an advisory reported in more
  than one.

The object may also set ``level`` and ``prod_level``, the project's defaults for ``--level`` and
``--prod-level``; the command-line flags take precedence.

A shared allowlist holds the entries of several projects in one file. It has a ``projects`` object,
keyed by each project's directory relative to the repository root, whose values set that project's
``level`` and ``prod_level`` (``{}`` for the defaults); every entry then names its project's
``directory``, which must be one of ``projects``, and DIRECTORY must be given relative to the
repository root, as it is written there. Only the entries for DIRECTORY apply, but every entry is
checked, so a mistake in one project's entry fails every project's check.
"""

from __future__ import annotations

import argparse
import collections
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

ALLOWLIST_NAME = "audit-allowlist.json"
LOCKFILE_NAME = "package-lock.json"
SEVERITIES = ("info", "low", "moderate", "high", "critical")
LEVELS = SEVERITIES[1:]
GHSA_ID = re.compile(r"GHSA(?:-[23456789cfghjmpqrvwx]{4}){3}")
GHSA_URL = re.compile(r"https://github\.com/advisories/(" + GHSA_ID.pattern + r")")
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
MAX_EXPIRY_DAYS = 367
ENTRY_REQUIRED = frozenset({"id", "expires", "reason", "tracking"})
ENTRY_OPTIONAL = frozenset({"dev_only", "package", "directory"})
THRESHOLD_KEYS = frozenset({"level", "prod_level"})

# npm also takes its settings from the environment and from .npmrc files, and some of them make the
# audit report less than the lockfile holds: offline mode skips the advisory lookup and reports
# nothing, and NODE_ENV=production, `omit` and the older settings `omit` replaced (production, only,
# also, dev) drop whole dependency groups. Flags override both, so every setting that decides what
# is audited is given here, the older ones reset to their defaults. npm warns about each older one
# on every run, even reset, hence --loglevel=error; errors still print.
NPM_AUDIT_ARGS = (
    "audit",
    "--json",
    "--package-lock-only",
    "--offline=false",
    "--include=dev",
    "--include=optional",
    "--include=peer",
    "--production=null",
    "--only=null",
    "--also=null",
    "--dev=false",
    "--loglevel=error",
)


class Refusal(Exception):
    """Something the check cannot trust, so it exits 2 rather than pass or fail on it."""


@dataclass
class Advisory:
    id: str
    severity: str
    title: str
    # Each package the advisory is reported in, with the lockfile paths (node_modules/...) of the
    # vulnerable copies npm audit lists for it.
    packages: dict[str, set[str]] = field(default_factory=dict)


@dataclass
class Entry:
    id: str
    expires: datetime.date
    reason: str
    tracking: str
    dev_only: bool = False
    package: str | None = None
    # Which of the file's entries with this ID it is, counting from 0, to find its line.
    ordinal: int = 0


@dataclass
class Allowlist:
    path: Path
    entries: dict[str, Entry] = field(default_factory=dict)
    level: str | None = None
    prod_level: str | None = None
    # A shared allowlist's project directories; None for a single project's allowlist.
    projects: list[str] | None = None


def project_name(directory: str | PurePosixPath | Path) -> str:
    """Return a project directory as a shared allowlist writes it: POSIX, relative, "." for the root."""
    return PurePosixPath(str(directory).replace("\\", "/")).as_posix()


def rank(severity: str) -> int:
    return SEVERITIES.index(severity)


def read_thresholds(raw: object, where: str) -> tuple[str | None, str | None]:
    if not isinstance(raw, dict) or not set(raw) <= THRESHOLD_KEYS:
        raise Refusal(f"{where}: thresholds are an object with at most {sorted(THRESHOLD_KEYS)}")
    for key, value in raw.items():
        if value not in LEVELS:
            raise Refusal(f"{where}: {key} must be one of {', '.join(LEVELS)}: {value!r}")
    level, prod_level = raw.get("level"), raw.get("prod_level")
    if level and prod_level and rank(prod_level) > rank(level):
        raise Refusal(f"{where}: prod_level {prod_level} is above level {level}, so it would never apply")
    return level, prod_level


def load_allowlist(path: Path, today: datetime.date, directory: str | None) -> Allowlist:
    """Read and check the whole allowlist, keeping the entries for ``directory`` (none if None)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Allowlist(path)
    except OSError as error:
        raise Refusal(f"{path}: {error}") from None
    except ValueError as error:
        raise Refusal(f"{path}: not valid JSON: {error}") from None
    if not isinstance(data, dict) or not isinstance(data.get("allowlist"), list):
        raise Refusal(f'{path}: expected an object with an "allowlist" list')
    allowlist = Allowlist(path)
    shared = "projects" in data
    known_keys = {"$comment", "allowlist"} | ({"projects"} if shared else THRESHOLD_KEYS)
    if not set(data) <= known_keys:
        raise Refusal(f"{path}: unknown keys {sorted(set(data) - known_keys)}; expected only {sorted(known_keys)}")
    if shared:
        projects = data["projects"]
        if not isinstance(projects, dict) or not projects:
            raise Refusal(f'{path}: "projects" must be a non-empty object, keyed by project directory')
        for name, thresholds in projects.items():
            if project_name(name) != name:
                raise Refusal(f"{path}: projects: write {name!r} as {project_name(name)!r}")
            read_thresholds(thresholds, f"{path}: projects[{name!r}]")
        allowlist.projects = sorted(projects)
        if directory is not None:
            if directory not in projects:
                raise Refusal(
                    f"{path}: {directory!r} is not one of its projects ({', '.join(allowlist.projects)}); "
                    "give DIRECTORY relative to the repository root"
                )
            allowlist.level, allowlist.prod_level = read_thresholds(projects[directory], f"{path}: projects")
    else:
        thresholds = {key: data[key] for key in THRESHOLD_KEYS & set(data)}
        allowlist.level, allowlist.prod_level = read_thresholds(thresholds, str(path))

    latest = today + datetime.timedelta(days=MAX_EXPIRY_DAYS)
    ordinals: collections.Counter[str] = collections.Counter()
    seen: set[tuple[str | None, str]] = set()
    for index, raw in enumerate(data["allowlist"]):
        where = f"{path}: allowlist[{index}]"
        # A malformed entry (a typo'd ID, an unparseable date) would never match or never expire,
        # so refuse it rather than let it silently cover nothing or everything forever.
        if not isinstance(raw, dict) or not ENTRY_REQUIRED <= set(raw) <= ENTRY_REQUIRED | ENTRY_OPTIONAL:
            raise Refusal(
                f"{where}: expected the keys {sorted(ENTRY_REQUIRED)}, plus optionally {sorted(ENTRY_OPTIONAL)}"
            )
        if not all(isinstance(raw[key], str) and raw[key].strip() for key in ENTRY_REQUIRED - {"expires"}):
            raise Refusal(f"{where}: id, reason and tracking must be non-empty strings")
        if not GHSA_ID.fullmatch(raw["id"]):
            raise Refusal(f"{where}: not a GHSA ID: {raw['id']!r}")
        try:
            if not isinstance(raw["expires"], str) or not ISO_DATE.fullmatch(raw["expires"]):
                raise ValueError
            expires = datetime.date.fromisoformat(raw["expires"])
        except ValueError:
            raise Refusal(f"{where}: expires must be a YYYY-MM-DD date: {raw['expires']!r}") from None
        if expires > latest:
            raise Refusal(
                f"{where}: expires {expires} is more than {MAX_EXPIRY_DAYS} days away; "
                f"review it by {latest} at the latest"
            )
        dev_only = raw.get("dev_only", False)
        if not isinstance(dev_only, bool):
            raise Refusal(f"{where}: dev_only must be true or false: {dev_only!r}")
        package = raw.get("package")
        if package is not None and not (isinstance(package, str) and package.strip()):
            raise Refusal(f"{where}: package must be a non-empty string: {package!r}")
        entry_directory = raw.get("directory")
        if shared:
            if not isinstance(entry_directory, str) or entry_directory not in data["projects"]:
                raise Refusal(
                    f"{where}: directory must be one of the projects ({', '.join(allowlist.projects or [])}): "
                    f"{entry_directory!r}"
                )
        elif entry_directory is not None:
            raise Refusal(f'{where}: "directory" belongs only in a shared allowlist, one with "projects"')
        if (entry_directory, raw["id"]) in seen:
            raise Refusal(f"{where}: {raw['id']} is listed twice" + (f" for {entry_directory}" if shared else ""))
        seen.add((entry_directory, raw["id"]))
        ordinal = ordinals[raw["id"]]
        ordinals[raw["id"]] += 1
        if directory is None or (shared and entry_directory != directory):
            continue
        allowlist.entries[raw["id"]] = Entry(
            raw["id"], expires, raw["reason"], raw["tracking"], dev_only, package, ordinal
        )
    return allowlist


def load_lockfile(path: Path) -> dict[str, dict]:
    """Return the lockfile's ``packages`` map (lockfileVersion 2 or 3), keyed by node_modules path."""
    why = "to tell dev-only copies from production ones"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise Refusal(f"{path}: cannot read it {why}: {error}") from None
    if not isinstance(data, dict) or not isinstance(data.get("packages"), dict):
        raise Refusal(f'{path}: no "packages" map (lockfileVersion 2 or later) {why}')
    return data["packages"]


def run_npm_audit(directory: Path) -> str:
    npm = shutil.which("npm")
    if npm is None:
        raise Refusal("npm not found on PATH")
    try:
        result = subprocess.run(
            [npm, *NPM_AUDIT_ARGS],
            cwd=directory,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as error:
        raise Refusal(f"could not run npm audit in {directory}: {error}") from None
    if result.stderr.strip():
        sys.stderr.write(result.stderr if result.stderr.endswith("\n") else result.stderr + "\n")
    # npm exits 0 for a clean audit and 1 both for findings and for some failures, which
    # parse_report tells apart. Anything else means npm did not finish an audit.
    if result.returncode < 0:
        raise Refusal(f"npm audit was killed by signal {-result.returncode}")
    if result.returncode not in (0, 1):
        raise Refusal(f"npm audit exited {result.returncode}, so it did not finish an audit")
    if not result.stdout.strip():
        raise Refusal(f"npm audit printed no report (exit {result.returncode})")
    return result.stdout


def advisory_severity(via: dict) -> str:
    # Unknown counts as critical: the check fails on that advisory rather than refuse the report.
    severity = via.get("severity")
    return severity if severity in SEVERITIES else "critical"


def validate_report(vulnerabilities: dict, metadata: object) -> None:
    """Refuse a report that is not internally consistent: one entry read wrongly could be the one that matters."""
    for name, vulnerability in vulnerabilities.items():
        where = f"npm audit report: {name!r}"
        if not isinstance(vulnerability, dict):
            raise Refusal(f"{where} is not an object")
        if vulnerability.get("severity") not in SEVERITIES:
            raise Refusal(f"{where} has an unknown severity: {vulnerability.get('severity')!r}")
        via = vulnerability.get("via")
        if not isinstance(via, list) or not via:
            raise Refusal(f"{where} does not say what makes it vulnerable (no via list)")
        for item in via:
            if isinstance(item, str):
                # The name of another vulnerable package, whose own entry carries its advisories.
                if item not in vulnerabilities:
                    raise Refusal(f"{where} is vulnerable through {item!r}, which the report does not describe")
            elif not isinstance(item, dict):
                raise Refusal(f"{where} has a via item that is neither a package name nor an advisory: {item!r}")
        # The vulnerable copies, which the dev_only and --prod-level checks look up in the lockfile.
        nodes = vulnerability.get("nodes", [])
        if not isinstance(nodes, list) or not all(isinstance(node, str) for node in nodes):
            raise Refusal(f"{where} lists its copies (nodes) as something other than a list of paths: {nodes!r}")

    # npm rates a package by the worst advisory it is vulnerable through, directly or through the
    # packages it depends on. The check judges advisories, so an entry rated above every advisory
    # behind it, or with none behind it, would be a finding nobody looked at. via lists can loop.
    for name, vulnerability in vulnerabilities.items():
        worst, stack, visited = -1, [name], {name}
        while stack:
            for item in vulnerabilities[stack.pop()]["via"]:
                if isinstance(item, dict):
                    worst = max(worst, rank(advisory_severity(item)))
                elif item not in visited:
                    visited.add(item)
                    stack.append(item)
        if worst < 0:
            raise Refusal(f"npm audit report: {name!r} leads to no advisory")
        if rank(vulnerability["severity"]) > worst:
            raise Refusal(
                f"npm audit report: {name!r} is rated {vulnerability['severity']}, "
                f"but the worst advisory it leads to is {SEVERITIES[worst]}"
            )

    # npm's own summary must agree with the entries it lists.
    counts = metadata.get("vulnerabilities") if isinstance(metadata, dict) else None
    if not isinstance(counts, dict):
        raise Refusal("npm audit report has no metadata.vulnerabilities summary")

    def count(value: object) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    if count(counts.get("total")) != len(vulnerabilities):
        raise Refusal(
            f"npm audit report's summary counts {counts.get('total')!r} vulnerable packages, "
            f"but it lists {len(vulnerabilities)}"
        )
    for severity in SEVERITIES:
        listed = sum(vulnerability["severity"] == severity for vulnerability in vulnerabilities.values())
        if count(counts.get(severity, 0)) != listed:
            raise Refusal(
                f"npm audit report's summary counts {counts.get(severity)!r} {severity} vulnerable packages, "
                f"but it lists {listed}"
            )


def parse_report(text: str) -> dict[str, Advisory]:
    try:
        report = json.loads(text)
    except ValueError as error:
        raise Refusal(f"npm audit report is not JSON: {error}") from None
    if not isinstance(report, dict):
        raise Refusal("npm audit report is not a JSON object")
    if "error" in report:
        raise Refusal(f"npm audit failed: {json.dumps(report['error'])}")
    if report.get("auditReportVersion") != 2 or not isinstance(report.get("vulnerabilities"), dict):
        raise Refusal("not an npm audit report (auditReportVersion 2 with vulnerabilities)")
    validate_report(report["vulnerabilities"], report.get("metadata"))
    advisories: dict[str, Advisory] = {}
    # Every advisory object in every package's via list counts, wherever it sits in the list: the
    # strings there only name other vulnerable packages, whose own entries carry their advisories.
    for package, vulnerability in report["vulnerabilities"].items():
        nodes = set(vulnerability.get("nodes", []))
        for via in vulnerability["via"]:
            if not isinstance(via, dict):
                continue
            url = str(via.get("url", ""))
            match = GHSA_URL.fullmatch(url)
            # An advisory without a GHSA URL cannot be allowlisted, so it fails at any listed level.
            advisory_id = match.group(1) if match else url or f"untitled advisory on {package}"
            severity = advisory_severity(via)
            advisory = advisories.setdefault(
                advisory_id, Advisory(advisory_id, severity, str(via.get("title", "")).strip())
            )
            if rank(severity) > rank(advisory.severity):
                advisory.severity = severity
            advisory.packages.setdefault(str(via.get("name") or package), set()).update(nodes)
    return advisories


def production_copies(nodes: set[str], lock_packages: dict[str, dict]) -> list[str]:
    """Return the copies the lockfile does not mark dev-only, so they ship."""
    if not nodes:
        # Nothing to check against, so nothing proves the advisory stays out of production.
        return ["(npm audit listed no copies)"]
    return sorted(
        node
        for node in nodes
        if not (isinstance(lock_packages.get(node), dict) and lock_packages[node].get("dev") is True)
    )


def check(
    advisories: dict[str, Advisory],
    entries: dict[str, Entry],
    level: str,
    prod_level: str,
    today: datetime.date,
    lock_packages: dict[str, dict] | None = None,
) -> tuple[list[str], list[str], list[tuple[Entry, str]]]:
    """Return (report lines, failures, entries due for review with why).

    ``lock_packages`` is required if any entry is dev_only, or ``prod_level`` is below ``level``.
    """
    lines: list[str] = []
    failures: list[str] = []
    reviews: list[tuple[Entry, str]] = []
    lock = lock_packages or {}

    def fails(severity: str, nodes: set[str]) -> bool:
        if rank(severity) >= rank(level):
            return True
        return rank(severity) >= rank(prod_level) and bool(production_copies(nodes, lock))

    for advisory in sorted(advisories.values(), key=lambda a: (-rank(a.severity), a.id)):
        lines.append(
            f"{advisory.severity:<8} {advisory.id} in {', '.join(sorted(advisory.packages))}: {advisory.title}"
        )
        entry = entries.get(advisory.id)
        covered = {
            package: nodes
            for package, nodes in advisory.packages.items()
            if entry is not None and entry.package in (None, package)
        }
        uncovered = {package: nodes for package, nodes in advisory.packages.items() if package not in covered}
        if uncovered:
            names = ", ".join(sorted(uncovered))
            if fails(advisory.severity, set().union(*uncovered.values())):
                only = f"; its allowlist entry covers only {entry.package}" if entry is not None else ""
                failures.append(f"{advisory.id} ({advisory.severity}, {names}) is not allowlisted{only}")
            else:
                lines.append(f"         below the failing level in {names}, ignored")
        if entry is None or not covered:
            continue
        if entry.expires < today:
            reviews.append(
                (entry, f"{entry.id}: allowlist entry expired on {entry.expires}; fix the advisory or review it again")
            )
        if entry.dev_only:
            shipped = production_copies(set().union(*covered.values()), lock)
            if shipped:
                failures.append(
                    f"{advisory.id}: allowlisted as dev_only, but not dev-only in the lockfile: {', '.join(shipped)}"
                )
                continue
        scope = " (dev-only, checked)" if entry.dev_only else ""
        where = f" in {entry.package}" if entry.package else ""
        lines.append(f"         allowlisted{where}{scope} until {entry.expires}: {entry.reason} ({entry.tracking})")
    for entry in entries.values():
        advisory = advisories.get(entry.id)
        if advisory is not None and (entry.package is None or entry.package in advisory.packages):
            continue
        target = f"{entry.id} in {entry.package}" if entry.package else entry.id
        expired = f", and the entry expired on {entry.expires}" if entry.expires < today else ""
        reviews.append((entry, f"{target}: allowlisted but no longer reported{expired}; remove its allowlist entry"))
    return lines, failures, reviews


def annotation(message: str, allowlist_path: Path, advisory_id: str, ordinal: int = 0, level: str = "warning") -> str:
    """Return a GitHub Actions ``::{level}::`` command for ``message``, on the entry's line if found."""

    def escape_data(value: str) -> str:
        return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")

    def escape_property(value: str) -> str:
        return escape_data(value).replace(":", "%3A").replace(",", "%2C")

    properties = [f"file={escape_property(allowlist_path.as_posix())}"]
    try:
        text = allowlist_path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    matches = [number for number, line in enumerate(text.splitlines(), start=1) if f'"{advisory_id}"' in line]
    if ordinal < len(matches):
        properties.append(f"line={matches[ordinal]}")
    properties.append("title=npm audit allowlist entry due for review")
    return f"::{level} {','.join(properties)}::{escape_data(message)}"


def list_projects(allowlist_path: Path | None, today: datetime.date) -> int:
    # Relative to the repository root, wherever this runs from, as a shared allowlist's projects are.
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--full-name", "--", f":(top){LOCKFILE_NAME}", f":(top)*/{LOCKFILE_NAME}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as error:
        raise Refusal(f"could not run git: {error}") from None
    if result.returncode != 0:
        raise Refusal(f"git ls-files failed (not in a Git checkout?): {result.stderr.strip()}")
    directories = sorted({project_name(PurePosixPath(path).parent) for path in result.stdout.split("\0") if path})
    if not directories:
        raise Refusal(f"no committed {LOCKFILE_NAME} found")
    if allowlist_path is not None:
        allowlist = load_allowlist(allowlist_path, today, None)
        if allowlist.projects is None:
            raise Refusal(f'{allowlist_path}: not a shared allowlist (no "projects"), so there is nothing to compare')
        unlisted = sorted(set(directories) - set(allowlist.projects))
        uncommitted = sorted(set(allowlist.projects) - set(directories))
        if unlisted or uncommitted:
            problems = []
            if unlisted:
                problems.append(f"committed but not in its projects: {', '.join(unlisted)}")
            if uncommitted:
                problems.append(f"in its projects but with no committed {LOCKFILE_NAME}: {', '.join(uncommitted)}")
            raise Refusal(f"{allowlist_path}: {'; '.join(problems)}")
    print(json.dumps(directories))
    return 0


def audit(args: argparse.Namespace, today: datetime.date) -> int:
    directory: Path = args.directory
    allowlist = load_allowlist(args.allowlist or directory / ALLOWLIST_NAME, today, project_name(directory))
    level = args.level or allowlist.level or "low"
    # A production level above the overall one would never apply, so the lower of the two counts.
    prod_level = min(args.prod_level or allowlist.prod_level or level, level, key=rank)
    lock_packages = None
    if rank(prod_level) < rank(level) or any(entry.dev_only for entry in allowlist.entries.values()):
        lock_packages = load_lockfile(directory / LOCKFILE_NAME)
    if args.report:
        try:
            text = args.report.read_text(encoding="utf-8")
        except OSError as error:
            raise Refusal(f"{args.report}: {error}") from None
    else:
        text = run_npm_audit(directory)
    advisories = parse_report(text)
    lines, failures, reviews = check(advisories, allowlist.entries, level, prod_level, today, lock_packages)

    production = f", or {prod_level} or above in production copies" if prod_level != level else ""
    print(
        f"Advisories reported: {len(advisories)}; allowlisted: {len(allowlist.entries)}; "
        f"failing at {level} or above{production}"
    )
    for line in lines:
        print(line)
    in_github_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    for entry, message in reviews:
        # GitHub reads workflow commands from stdout only.
        if in_github_actions:
            level_name = "error" if args.strict else "warning"
            print(annotation(message, allowlist.path, entry.id, entry.ordinal, level_name))
        elif not args.strict:
            print(f"WARNING: {message}")
    if args.strict:
        failures.extend(message for _, message in reviews)
    sys.stdout.flush()
    for failure in failures:
        print(f"FAIL: {failure}", file=sys.stderr)
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directory", type=Path, nargs="?", help="the npm project to audit (where package-lock.json is)")
    parser.add_argument(
        "--list-projects",
        action="store_true",
        help="print the directories of every committed package-lock.json as a JSON list, and stop",
    )
    parser.add_argument(
        "--allowlist", type=Path, help=f"allowlist file (default: DIRECTORY/{ALLOWLIST_NAME}; none if absent)"
    )
    parser.add_argument("--report", type=Path, help="read this saved `npm audit --json` report instead of running it")
    parser.add_argument("--level", choices=LEVELS, help="lowest severity that fails (default: the allowlist's, or low)")
    parser.add_argument(
        "--prod-level",
        choices=LEVELS,
        help="lowest severity that fails where an advisory reaches a production (not dev-only) copy "
        "(default: the allowlist's, or LEVEL)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="fail, rather than warn, on an allowlist entry due for review: expired, or no longer reported",
    )
    parser.add_argument("--today", type=datetime.date.fromisoformat, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.list_projects == (args.directory is not None):
        parser.error("give either DIRECTORY or --list-projects")
    if args.list_projects and (args.report or args.level or args.prod_level or args.strict or args.today):
        parser.error("--list-projects takes only --allowlist")
    if args.level and args.prod_level and rank(args.prod_level) > rank(args.level):
        parser.error(f"--prod-level {args.prod_level} is above --level {args.level}, so it would never apply")
    # The date is fixed only for a saved report, so a live audit cannot keep an expired entry alive.
    if args.today and not args.report:
        parser.error("--today applies only to a saved --report")
    # timezone.utc, not datetime.UTC (3.11+): this also runs on older system Pythons, such as macOS's 3.9.
    today = args.today or datetime.datetime.now(datetime.timezone.utc).date()  # noqa: UP017
    try:
        if args.list_projects:
            return list_projects(args.allowlist, today)
        return audit(args, today)
    except Refusal as refusal:
        sys.stdout.flush()
        print(f"error: {refusal}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
