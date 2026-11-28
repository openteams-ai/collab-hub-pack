"""Tests for scripts/security/check_npm_audit.py: that it refuses what it exists to refuse.

    python3 -m unittest discover --start-directory scripts/security/tests --pattern test_check_npm_audit.py --verbose

Standard library only, like the check. Most reports are synthetic, shaped like ``npm audit --json``
(auditReportVersion 2) output for api/admin-ui's picomatch -> fdir -> tinyglobby -> vite chain.
RealNpmTests run the real npm client against a registry on localhost, and are skipped where npm is
not installed; ListProjectsTests need git.
"""

from __future__ import annotations

import contextlib
import copy
import gzip
import http.server
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "check_npm_audit.py"
_spec = importlib.util.spec_from_file_location("check_npm_audit", SCRIPT)
check = importlib.util.module_from_spec(_spec)
# dataclasses resolves the module's string annotations through sys.modules.
sys.modules[_spec.name] = check
_spec.loader.exec_module(check)

SEVERITIES = ("info", "low", "moderate", "high", "critical")
ALLOWED = "GHSA-fc5q-39xc-vjpf"
OTHER = "GHSA-2222-3333-4444"
TODAY = "2026-11-17"
EXPIRES = "2027-02-15"
LATEST = "2027-11-19"  # TODAY + 367 days, the latest expiry an entry may have.
# Keeps git away from the developer's own configuration.
GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def advisory(ghsa: str, name: str, severity: str = "high") -> dict:
    return {
        "source": 1,
        "name": name,
        "dependency": name,
        "title": f"{name} advisory",
        "url": f"https://github.com/advisories/{ghsa}",
        "severity": severity,
        "range": "*",
    }


# picomatch carries the advisory; its dependents list it by name only, as npm reports them.
REPORT = {
    "auditReportVersion": 2,
    "vulnerabilities": {
        "picomatch": {
            "name": "picomatch",
            "severity": "high",
            "isDirect": False,
            "via": [advisory(ALLOWED, "picomatch")],
            "nodes": ["node_modules/picomatch"],
        },
        "fdir": {
            "name": "fdir",
            "severity": "high",
            "isDirect": False,
            "via": ["picomatch"],
            "nodes": ["node_modules/fdir"],
        },
        "tinyglobby": {
            "name": "tinyglobby",
            "severity": "high",
            "isDirect": False,
            "via": ["fdir", "picomatch"],
            "nodes": ["node_modules/tinyglobby"],
        },
        "vite": {
            "name": "vite",
            "severity": "high",
            "isDirect": True,
            "via": ["picomatch", "tinyglobby"],
            "nodes": ["node_modules/vite"],
        },
    },
    "metadata": {
        "vulnerabilities": {"info": 0, "low": 0, "moderate": 0, "high": 4, "critical": 0, "total": 4},
    },
}

# The matching package-lock.json: every one of those packages is a devDependency-only node.
LOCKFILE = {
    "lockfileVersion": 3,
    "packages": {
        "": {"name": "admin-ui"},
        **{f"node_modules/{name}": {"dev": True} for name in ("picomatch", "fdir", "tinyglobby", "vite")},
    },
}


def summarize(report: object) -> object:
    """Rate each entry by the worst advisory it leads to, and write a summary to match, as npm does.

    Tests change a report's advisories freely; this keeps it consistent, so the consistency checks
    refuse only the reports built to be inconsistent.
    """
    vulnerabilities = report.get("vulnerabilities") if isinstance(report, dict) else None
    if not isinstance(vulnerabilities, dict) or not all(isinstance(v, dict) for v in vulnerabilities.values()):
        return report
    for name, vulnerability in vulnerabilities.items():
        worst, stack, visited = -1, [name], {name}
        while stack:
            for item in vulnerabilities[stack.pop()].get("via", []):
                if isinstance(item, dict):
                    severity = item.get("severity")
                    worst = max(worst, SEVERITIES.index(severity) if severity in SEVERITIES else len(SEVERITIES) - 1)
                elif isinstance(item, str) and item in vulnerabilities and item not in visited:
                    visited.add(item)
                    stack.append(item)
        if worst >= 0:
            vulnerability["severity"] = SEVERITIES[worst]
    counts = {severity: sum(v.get("severity") == severity for v in vulnerabilities.values()) for severity in SEVERITIES}
    report["metadata"] = {"vulnerabilities": {**counts, "total": len(vulnerabilities)}}
    return report


def entry(ghsa: str = ALLOWED, expires: str | None = EXPIRES, **overrides: object) -> dict:
    return {"id": ghsa, "expires": expires, "reason": "dev-only", "tracking": "https://example.invalid", **overrides}


class CheckTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        # Where the lockfile is written, and the DIRECTORY argument (that path unless set).
        self.project = self.tmp
        self.directory: str | None = None
        self.report: object = copy.deepcopy(REPORT)
        # Whether run_main rewrites the report's severities and summary to match its advisories.
        self.summarize = True
        self.allowlist: object = {"allowlist": [entry()]}
        self.lockfile: object = copy.deepcopy(LOCKFILE)
        # Whether the check runs as under GitHub Actions; these tests themselves run there in CI.
        self.github_actions = False
        self.stdout = ""
        # The real shared allowlist sits next to the script; tests that want one name their own.
        shared = mock.patch.object(check, "SHARED_ALLOWLIST", self.tmp / "no-shared-allowlist.json")
        shared.start()
        self.addCleanup(shared.stop)

    def write(self, name: str, data: object) -> Path:
        path = self.tmp / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data if isinstance(data, str) else json.dumps(data, indent=2), encoding="utf-8")
        return path

    def enter(self, directory: Path) -> None:
        """Run the rest of the test from ``directory``."""
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(directory)

    def call(self, *argv: str, environment: dict[str, str] | None = None) -> tuple[int, str]:
        """Run the check; return (exit code, stderr), and keep its stdout in ``self.stdout``."""
        stdout, stderr = io.StringIO(), io.StringIO()
        environment = {
            "GITHUB_ACTIONS": "true" if self.github_actions else "",
            "GITHUB_WORKSPACE": "",
            **(environment or {}),
        }
        with (
            mock.patch.dict(os.environ, environment),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            try:
                code = check.main(list(argv))
            except SystemExit as error:  # argparse
                code = error.code if isinstance(error.code, int) else 2
        self.stdout = stdout.getvalue()
        return code, stderr.getvalue()

    def run_main(self, *argv: str) -> tuple[int, str]:
        if self.lockfile is not None:
            self.project.mkdir(parents=True, exist_ok=True)
            lockfile = self.project / "package-lock.json"
            lockfile.write_text(json.dumps(self.lockfile), encoding="utf-8")
        report = summarize(copy.deepcopy(self.report)) if self.summarize else self.report
        return self.call(
            self.directory or str(self.project),
            "--report",
            str(self.write("report.json", report)),
            "--allowlist",
            str(self.write("allowlist.json", self.allowlist)),
            "--today",
            TODAY,
            *argv,
        )

    def add_advisory(self, package: str, ghsa: str, severity: str = "critical") -> None:
        self.report["vulnerabilities"][package]["via"].append(advisory(ghsa, package, severity))


class AdvisoryTests(CheckTestCase):
    def test_allowlisted_advisory_passes(self) -> None:
        self.assertEqual(self.run_main(), (0, ""))
        self.assertIn(f"allowlisted until {EXPIRES}", self.stdout)

    def test_clean_report_with_no_allowlist_passes(self) -> None:
        self.report["vulnerabilities"] = {}
        self.allowlist = {"allowlist": []}
        self.assertEqual(self.run_main(), (0, ""))

    def test_missing_default_allowlist_means_none(self) -> None:
        self.write("package-lock.json", LOCKFILE)
        report = self.write("report.json", REPORT)
        code, stderr = self.call(str(self.tmp), "--report", str(report), "--today", TODAY)
        self.assertEqual(code, 1)
        self.assertIn(f"{ALLOWED} (high, picomatch) is not allowlisted", stderr)

    def test_missing_explicit_allowlist_is_refused(self) -> None:
        # A mistyped path would otherwise mean no exceptions and the default level, silently.
        code, stderr = self.run_main("--allowlist", str(self.tmp / "absent.json"))
        self.assertEqual(code, 2)
        self.assertIn("absent.json: no such file", stderr)

    def test_default_allowlist_is_the_shared_one_next_to_the_script(self) -> None:
        # Ahead of the project's own file, which an older layout may have left behind.
        shared = {"projects": {"frontend": {"level": "critical"}}, "allowlist": []}
        self.enter(self.tmp)
        self.write("frontend/package-lock.json", LOCKFILE)
        self.write("frontend/audit-allowlist.json", {"allowlist": [entry()]})
        report = self.write("report.json", REPORT)
        with mock.patch.object(check, "SHARED_ALLOWLIST", self.write("shared.json", shared)):
            code, stderr = self.call("frontend", "--report", str(report), "--today", TODAY)
        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("allowlisted: 0; failing at critical or above", self.stdout)

    def test_default_allowlist_is_the_projects_own(self) -> None:
        self.write("audit-allowlist.json", {"allowlist": [entry()]})
        self.write("package-lock.json", LOCKFILE)
        report = self.write("report.json", REPORT)
        self.assertEqual(self.call(str(self.tmp), "--report", str(report), "--today", TODAY), (0, ""))

    def test_unlisted_advisory_fails(self) -> None:
        self.allowlist = {"allowlist": []}
        code, stderr = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn(f"{ALLOWED} (high, picomatch) is not allowlisted", stderr)

    def test_new_advisory_on_transitive_package_after_a_name_fails(self) -> None:
        # The case audit-ci 7.1.0 passes (IBM/audit-ci#360): tinyglobby is transitive and its via
        # list starts with the names of other vulnerable packages, then has an advisory of its own.
        self.add_advisory("tinyglobby", OTHER)
        code, stderr = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn(f"{OTHER} (critical, tinyglobby) is not allowlisted", stderr)
        self.assertNotIn(ALLOWED, stderr)

    def test_new_advisory_on_allowlisted_package_fails(self) -> None:
        self.add_advisory("picomatch", OTHER)
        code, stderr = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn(f"{OTHER} (critical, picomatch) is not allowlisted", stderr)

    def test_advisory_below_level_passes(self) -> None:
        self.add_advisory("vite", OTHER, "low")
        self.assertEqual(self.run_main("--level", "moderate"), (0, ""))
        self.assertIn("below the failing level in vite, ignored", self.stdout)
        self.assertEqual(self.run_main()[0], 1)

    def test_info_advisory_never_fails(self) -> None:
        self.add_advisory("vite", OTHER, "info")
        self.assertEqual(self.run_main(), (0, ""))

    def test_advisory_without_ghsa_url_fails(self) -> None:
        self.report["vulnerabilities"]["vite"]["via"].append({"name": "vite", "severity": "low", "url": ""})
        code, stderr = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("untitled advisory on vite", stderr)

    def test_unknown_severity_counts_as_critical(self) -> None:
        self.add_advisory("vite", OTHER, "severe")
        code, stderr = self.run_main("--level", "critical")
        self.assertEqual(code, 1)
        self.assertIn(f"{OTHER} (critical, vite)", stderr)


class ThresholdTests(CheckTestCase):
    def setUp(self) -> None:
        super().setUp()
        # A moderate advisory on vite, which the lockfile marks dev-only.
        self.add_advisory("vite", OTHER, "moderate")

    def make_vite_production(self) -> None:
        self.lockfile["packages"]["node_modules/vite"] = {}

    def test_prod_level_passes_a_dev_only_advisory_below_level(self) -> None:
        self.assertEqual(self.run_main("--level", "high", "--prod-level", "moderate"), (0, ""))
        self.assertIn("failing at high or above, or moderate or above in production copies", self.stdout)

    def test_prod_level_fails_an_advisory_reaching_a_production_copy(self) -> None:
        self.make_vite_production()
        code, stderr = self.run_main("--level", "high", "--prod-level", "moderate")
        self.assertEqual(code, 1)
        self.assertIn(f"{OTHER} (moderate, vite) is not allowlisted", stderr)

    def test_advisory_without_copies_counts_as_production(self) -> None:
        del self.report["vulnerabilities"]["vite"]["nodes"]
        self.assertEqual(self.run_main("--level", "high", "--prod-level", "moderate")[0], 1)

    def test_devoptional_copy_counts_as_production(self) -> None:
        self.lockfile["packages"]["node_modules/vite"] = {"devOptional": True}
        self.assertEqual(self.run_main("--level", "high", "--prod-level", "moderate")[0], 1)

    def test_allowlist_sets_the_levels(self) -> None:
        self.make_vite_production()
        self.allowlist = {"level": "high", "allowlist": [entry()]}
        self.assertEqual(self.run_main(), (0, ""))
        self.allowlist["prod_level"] = "moderate"
        self.assertEqual(self.run_main()[0], 1)

    def test_flags_override_the_allowlists_levels(self) -> None:
        self.allowlist = {"level": "critical", "allowlist": [entry()]}
        self.assertEqual(self.run_main("--level", "moderate")[0], 1)

    def test_prod_level_above_level_never_applies(self) -> None:
        # From different sources the lower one counts; from the same source it is refused.
        self.allowlist = {"level": "moderate", "allowlist": [entry()]}
        self.assertEqual(self.run_main("--prod-level", "critical")[0], 1)
        self.assertEqual(self.run_main("--level", "moderate", "--prod-level", "high")[0], 2)
        self.allowlist = {"level": "moderate", "prod_level": "high", "allowlist": [entry()]}
        code, stderr = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("prod_level high is above level moderate", stderr)

    def test_prod_level_needs_the_lockfile(self) -> None:
        self.lockfile = None
        code, stderr = self.run_main("--level", "high", "--prod-level", "moderate")
        self.assertEqual(code, 2)
        self.assertIn("to tell dev-only copies from production ones", stderr)

    def test_bad_levels_are_refused(self) -> None:
        for label, allowlist in {
            "unknown level": {"level": "severe", "allowlist": []},
            "info level": {"prod_level": "info", "allowlist": []},
            "unknown key": {"levels": "high", "allowlist": []},
        }.items():
            with self.subTest(label):
                self.allowlist = allowlist
                self.assertEqual(self.run_main()[0], 2)


class AllowlistTests(CheckTestCase):
    def test_entry_is_valid_through_its_expiry_day(self) -> None:
        self.allowlist = {"allowlist": [entry(expires=TODAY)]}
        self.assertEqual(self.run_main("--strict"), (0, ""))
        self.assertIn(f"allowlisted until {TODAY}", self.stdout)

    def test_entry_expiring_soon_warns_even_when_strict(self) -> None:
        # Seen on every pull request in the week before the weekly run fails on it, and never failing.
        for expires, when in (
            (TODAY, "today"),
            ("2026-11-18", "tomorrow"),
            ("2026-11-20", "in 3 days"),
            ("2026-11-24", "in 7 days"),
        ):
            for strict in ((), ("--strict",)):
                with self.subTest(expires=expires, strict=bool(strict)):
                    self.allowlist = {"allowlist": [entry(expires=expires)]}
                    self.assertEqual(self.run_main(*strict), (0, ""))
                    self.assertIn(
                        f"WARNING: {ALLOWED}: allowlist entry expires {when}, on {expires}; "
                        "fix the advisory or review the entry before then",
                        self.stdout,
                    )

    def test_entry_expiring_later_does_not_warn(self) -> None:
        self.allowlist = {"allowlist": [entry(expires="2026-11-25")]}  # 8 days away
        self.assertEqual(self.run_main(), (0, ""))
        self.assertNotIn("WARNING", self.stdout)

    def test_entry_expiring_soon_is_a_github_warning_on_its_line(self) -> None:
        self.github_actions = True
        self.allowlist = {"allowlist": [entry(expires="2026-11-20")]}
        self.assertEqual(self.run_main("--strict"), (0, ""))
        self.assertRegex(
            self.stdout,
            r"(?m)^::warning file=[^,\n]*allowlist\.json,line=4,title=npm audit allowlist entry expiring soon::"
            + ALLOWED
            + ": allowlist entry expires in 3 days",
        )
        self.assertNotIn("::error", self.stdout)

    def test_annotation_names_the_file_relative_to_the_checkout(self) -> None:
        self.github_actions = True
        self.report["vulnerabilities"] = {}
        self.assertEqual(self.run_main(), (0, ""))  # the allowlist is given as an absolute path
        self.assertRegex(self.stdout, r"(?m)^::warning file=[^,\n]+allowlist\.json,")
        code, _ = self.call(
            str(self.tmp),
            "--report",
            str(self.tmp / "report.json"),
            "--allowlist",
            str(self.tmp / "allowlist.json"),
            "--today",
            TODAY,
            environment={"GITHUB_WORKSPACE": str(self.tmp)},
        )
        self.assertEqual(code, 0)
        self.assertRegex(self.stdout, r"(?m)^::warning file=allowlist\.json,line=4,")

    def test_annotation_is_on_the_entrys_id_line(self) -> None:
        # Not on an earlier mention of the same ID as a bare JSON string, here in a comment list.
        self.github_actions = True
        self.report["vulnerabilities"] = {}
        self.allowlist = {"$comment": ["Remove once fixed:", ALLOWED], "allowlist": [entry()]}
        self.assertEqual(self.run_main(), (0, ""))
        lines = (self.tmp / "allowlist.json").read_text(encoding="utf-8").splitlines()
        id_line = next(number for number, line in enumerate(lines, start=1) if f'"id": "{ALLOWED}"' in line)
        self.assertGreater(id_line, 2)
        self.assertIn(f",line={id_line},", self.stdout)

    def test_expired_entry_warns_and_still_covers(self) -> None:
        # The calendar, not the pull request, made it due: it must not fail every open pull request.
        self.allowlist = {"allowlist": [entry(expires="2026-11-16")]}
        self.assertEqual(self.run_main(), (0, ""))
        self.assertIn(f"WARNING: {ALLOWED}: allowlist entry expired on 2026-11-16", self.stdout)

    def test_expired_entry_fails_when_strict(self) -> None:
        self.allowlist = {"allowlist": [entry(expires="2026-11-16")]}
        code, stderr = self.run_main("--strict")
        self.assertEqual(code, 1)
        self.assertIn(f"FAIL: {ALLOWED}: allowlist entry expired on 2026-11-16", stderr)
        self.assertNotIn("WARNING", self.stdout)

    def test_expired_entry_below_the_level_still_fails_when_strict(self) -> None:
        self.allowlist = {"allowlist": [entry(expires="2026-11-16")]}
        self.assertEqual(self.run_main("--level", "critical", "--strict")[0], 1)

    def test_expiry_may_be_up_to_367_days_away(self) -> None:
        self.allowlist = {"allowlist": [entry(expires=LATEST)]}
        self.assertEqual(self.run_main(), (0, ""))
        self.allowlist = {"allowlist": [entry(expires="2027-11-20")]}
        code, stderr = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn(f"more than 367 days away; review it by {LATEST}", stderr)

    def test_entry_no_longer_reported_warns(self) -> None:
        self.report["vulnerabilities"] = {}
        # A fix or a withdrawn advisory upstream must not fail every open pull request.
        self.assertEqual(self.run_main(), (0, ""))
        self.assertIn(f"WARNING: {ALLOWED}: allowlisted but no longer reported", self.stdout)

    def test_dev_only_entry_warns_once_no_longer_reported(self) -> None:
        # The shape of the picomatch entry after GHSA-fc5q-39xc-vjpf was withdrawn.
        self.allowlist = {"allowlist": [entry(dev_only=True)]}
        self.report["vulnerabilities"] = {}
        self.assertEqual(self.run_main(), (0, ""))
        self.assertIn(f"WARNING: {ALLOWED}: allowlisted but no longer reported", self.stdout)

    def test_expired_entry_no_longer_reported_warns_once(self) -> None:
        self.allowlist = {"allowlist": [entry(expires="2026-11-16")]}
        self.report["vulnerabilities"] = {}
        self.assertEqual(self.run_main(), (0, ""))
        self.assertEqual(self.stdout.count("WARNING"), 1)
        self.assertIn("no longer reported, and the entry expired on 2026-11-16", self.stdout)

    def test_stale_entry_beside_a_live_one_warns_only_for_itself(self) -> None:
        self.allowlist = {"allowlist": [entry(), entry(ghsa=OTHER)]}
        self.assertEqual(self.run_main(), (0, ""))
        self.assertIn(f"WARNING: {OTHER}: allowlisted but no longer reported", self.stdout)
        self.assertNotIn(f"WARNING: {ALLOWED}", self.stdout)

    def test_stale_entry_does_not_hide_a_failure(self) -> None:
        self.allowlist = {"allowlist": [entry(ghsa=OTHER)]}
        code, stderr = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn(f"{ALLOWED} (high, picomatch) is not allowlisted", stderr)
        self.assertIn(f"WARNING: {OTHER}: allowlisted but no longer reported", self.stdout)

    def test_entry_due_for_review_is_a_github_annotation_on_its_line(self) -> None:
        self.github_actions = True
        self.report["vulnerabilities"] = {}
        self.assertEqual(self.run_main(), (0, ""))
        # The allowlist is written indented, so the entry's "id" is on line 4.
        self.assertRegex(
            self.stdout,
            r"(?m)^::warning file=[^,\n]*allowlist\.json,line=4,title=npm audit allowlist entry due for review::"
            + ALLOWED
            + ": allowlisted but no longer reported; remove its allowlist entry$",
        )
        self.assertNotIn("WARNING:", self.stdout)

    def test_strict_fails_on_an_entry_no_longer_reported(self) -> None:
        self.report["vulnerabilities"] = {}
        code, stderr = self.run_main("--strict")
        self.assertEqual(code, 1)
        self.assertIn(f"FAIL: {ALLOWED}: allowlisted but no longer reported; remove its allowlist entry", stderr)
        self.assertNotIn("WARNING", self.stdout)

    def test_strict_passes_an_entry_still_reported(self) -> None:
        self.assertEqual(self.run_main("--strict"), (0, ""))

    def test_strict_is_a_github_error_annotation_on_its_line(self) -> None:
        self.github_actions = True
        self.allowlist = {"allowlist": [entry(expires="2026-11-16")]}
        code, stderr = self.run_main("--strict")
        self.assertEqual(code, 1)
        self.assertIn(f"FAIL: {ALLOWED}: allowlist entry expired on 2026-11-16", stderr)
        self.assertRegex(
            self.stdout,
            r"(?m)^::error file=[^,\n]*allowlist\.json,line=4,title=npm audit allowlist entry due for review::"
            + ALLOWED
            + ": allowlist entry expired on 2026-11-16; fix the advisory or review it again$",
        )
        self.assertNotIn("::warning", self.stdout)

    def test_annotation_escapes_workflow_command_characters(self) -> None:
        # Not written: an unreadable allowlist still yields an annotation, just without a line.
        command = check.annotation("50% done\nnext", Path("C:/odd,100%.json"), ALLOWED)
        self.assertEqual(
            command,
            "::warning file=C%3A/odd%2C100%25.json,title=npm audit allowlist entry due for review::50%25 done%0Anext",
        )

    def test_today_applies_only_to_a_saved_report(self) -> None:
        # Otherwise a live audit could be run with an old date to keep an expired entry alive.
        code, stderr = self.call(str(self.tmp), "--today", TODAY)
        self.assertEqual(code, 2)
        self.assertIn("--today applies only to a saved --report", stderr)

    def test_malformed_allowlists_are_refused(self) -> None:
        # Each case names the refusal it must hit, so one caught by the wrong check fails the test.
        not_a_list = 'expected an object with an "allowlist" list'
        wrong_keys = "expected the keys"
        bad_date = "expires must be a YYYY-MM-DD date"
        bad_package = "package must be a package name or a non-empty list of different package names"
        duplicate = "appears twice in one object, so which value counts is not clear"
        cases = {
            # json.loads would keep the last of each, while a reviewer reads the first.
            "repeated top-level key": ('{"allowlist": [], "allowlist": []}', f"the key 'allowlist' {duplicate}"),
            "repeated entry key": (
                '{"allowlist": [{"id": "' + ALLOWED + '", "expires": "2020-01-01", "expires": "' + EXPIRES + '", '
                '"reason": "x", "tracking": "y"}]}',
                f"the key 'expires' {duplicate}",
            ),
            "repeated project": (
                '{"projects": {"frontend": {"level": "high"}, "frontend": {"level": "critical"}}, "allowlist": []}',
                f"the key 'frontend' {duplicate}",
            ),
            "not an object": ([], not_a_list),
            "no allowlist list": ({"allowlist": {}}, not_a_list),
            "missing key": ({"allowlist": [{"id": ALLOWED, "expires": EXPIRES, "reason": "x"}]}, wrong_keys),
            "unknown key": ({"allowlist": [entry(note="typo")]}, wrong_keys),
            "empty reason": ({"allowlist": [entry(reason=" ")]}, "id, reason and tracking must be non-empty strings"),
            "not a GHSA ID": ({"allowlist": [entry(ghsa="CVE-2026-112366")]}, "not a GHSA ID: 'CVE-2026-112366'"),
            "duplicate": ({"allowlist": [entry(), entry()]}, f"{ALLOWED} is listed twice"),
            "bad date": ({"allowlist": [entry(expires="15 February 2027")]}, bad_date),
            "basic-format date": ({"allowlist": [entry(expires="20270215")]}, bad_date),
            "impossible date": ({"allowlist": [entry(expires="2027-02-30")]}, bad_date),
            "empty expiry": ({"allowlist": [entry(expires="")]}, bad_date),
            "numeric expiry": ({"allowlist": [entry(expires=20270215)]}, bad_date),
            # Every exception expires: null no longer means never.
            "null expiry": ({"allowlist": [entry(expires=None)]}, bad_date),
            "expiry omitted": ({"allowlist": [{"id": ALLOWED, "reason": "x", "tracking": "y"}]}, wrong_keys),
            "dev_only not a boolean": ({"allowlist": [entry(dev_only="yes")]}, "dev_only must be true or false"),
            "empty package": ({"allowlist": [entry(package="")]}, bad_package),
            "empty package list": ({"allowlist": [entry(package=[])]}, bad_package),
            "package list with an empty name": ({"allowlist": [entry(package=["picomatch", " "])]}, bad_package),
            "package list with a repeat": ({"allowlist": [entry(package=["picomatch", "picomatch"])]}, bad_package),
            "package list with a non-string": ({"allowlist": [entry(package=["picomatch", 3])]}, bad_package),
            "package not a name": ({"allowlist": [entry(package=3)]}, bad_package),
            # Not "every package": that is what leaving the key out means.
            "package null": ({"allowlist": [entry(package=None)]}, bad_package),
            "padded package": ({"allowlist": [entry(package=" picomatch")]}, bad_package),
            "padded package in a list": ({"allowlist": [entry(package=["picomatch", "fdir "])]}, bad_package),
            "directory outside a shared allowlist": (
                {"allowlist": [entry(directory="frontend")]},
                '"directory" belongs only in a shared allowlist',
            ),
            "unknown top-level key": ({"allowlist": [], "exceptions": []}, "unknown keys ['exceptions']"),
            "not JSON": ("{", "not valid JSON"),
        }
        for label, (allowlist, message) in cases.items():
            with self.subTest(label):
                self.allowlist = allowlist
                code, stderr = self.run_main()
                self.assertEqual(code, 2)
                self.assertIn(message, stderr)


class PackageTests(CheckTestCase):
    def test_entry_for_the_advisorys_package_covers_it(self) -> None:
        self.allowlist = {"allowlist": [entry(package="picomatch")]}
        self.assertEqual(self.run_main(), (0, ""))
        self.assertIn(f"allowlisted in picomatch until {EXPIRES}", self.stdout)

    def test_entry_for_another_package_does_not(self) -> None:
        self.allowlist = {"allowlist": [entry(package="minimatch")]}
        code, stderr = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn(
            f"{ALLOWED} (high, picomatch) is not allowlisted; its allowlist entry covers only minimatch", stderr
        )
        self.assertIn(f"WARNING: {ALLOWED} in minimatch: allowlisted but no longer reported", self.stdout)

    def test_advisory_in_a_second_package_fails_there_only(self) -> None:
        self.add_advisory("fdir", ALLOWED, "high")
        self.allowlist = {"allowlist": [entry(package="picomatch")]}
        code, stderr = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn(f"{ALLOWED} (high, fdir) is not allowlisted; its allowlist entry covers only picomatch", stderr)
        self.allowlist = {"allowlist": [entry()]}
        self.assertEqual(self.run_main(), (0, ""))

    def test_package_list_covers_each_package_named(self) -> None:
        self.add_advisory("fdir", ALLOWED, "high")
        self.allowlist = {"allowlist": [entry(package=["picomatch", "fdir"])]}
        self.assertEqual(self.run_main("--strict"), (0, ""))
        self.assertIn(f"allowlisted in fdir, picomatch until {EXPIRES}", self.stdout)

    def test_package_list_leaves_others_uncovered(self) -> None:
        self.add_advisory("fdir", ALLOWED, "high")
        self.add_advisory("vite", ALLOWED, "high")
        self.allowlist = {"allowlist": [entry(package=["picomatch", "fdir"])]}
        code, stderr = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn(
            f"{ALLOWED} (high, vite) is not allowlisted; its allowlist entry covers only fdir, picomatch", stderr
        )

    def test_package_no_longer_reported_in_a_list_is_due_for_review(self) -> None:
        # fdir was fixed, but the entry still names it; it must not hide behind picomatch.
        self.allowlist = {"allowlist": [entry(package=["picomatch", "fdir"])]}
        message = f"{ALLOWED}: allowlisted in fdir, but no longer reported there; remove them from its package list"
        self.assertEqual(self.run_main(), (0, ""))
        self.assertIn(f"WARNING: {message}", self.stdout)
        code, stderr = self.run_main("--strict")
        self.assertEqual(code, 1)
        self.assertIn(f"FAIL: {message}", stderr)

    def test_package_list_no_longer_reported_anywhere_is_due_for_review(self) -> None:
        self.report["vulnerabilities"] = {}
        self.allowlist = {"allowlist": [entry(package=["picomatch", "fdir"])]}
        self.assertEqual(self.run_main(), (0, ""))
        self.assertIn(f"WARNING: {ALLOWED} in fdir, picomatch: allowlisted but no longer reported", self.stdout)


class SharedAllowlistTests(CheckTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.enter(self.tmp)
        self.project = self.tmp / "frontend"
        self.directory = "frontend"
        self.projects = {"frontend": {}, "internal/piagent": {"level": "high"}}

    def shared(self, *entries: dict) -> dict:
        return {"projects": self.projects, "allowlist": list(entries)}

    def test_only_the_projects_own_entries_apply(self) -> None:
        self.allowlist = self.shared(entry(directory="internal/piagent"))
        code, stderr = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn(f"{ALLOWED} (high, picomatch) is not allowlisted", stderr)
        self.allowlist = self.shared(entry(directory="internal/piagent"), entry(directory="frontend"))
        self.assertEqual(self.run_main(), (0, ""))
        self.assertIn("allowlisted: 1;", self.stdout)

    def test_projects_set_their_levels(self) -> None:
        self.add_advisory("vite", OTHER, "moderate")
        self.projects["frontend"] = {"level": "high", "prod_level": "moderate"}
        self.allowlist = self.shared(entry(directory="frontend"))
        self.assertEqual(self.run_main(), (0, ""))
        self.lockfile["packages"]["node_modules/vite"] = {}
        self.assertEqual(self.run_main()[0], 1)

    def test_directory_is_normalized(self) -> None:
        self.directory = "./frontend/"
        self.allowlist = self.shared(entry(directory="frontend"))
        self.assertEqual(self.run_main(), (0, ""))

    def test_directory_must_be_one_of_the_projects(self) -> None:
        self.allowlist = self.shared(entry(directory="frontend"))
        self.directory = str(self.project)  # absolute, not as the projects write it
        code, stderr = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("is not one of its projects", stderr)

    def test_same_advisory_in_two_projects_annotates_the_right_line(self) -> None:
        self.github_actions = True
        self.report["vulnerabilities"] = {}
        self.allowlist = self.shared(entry(directory="internal/piagent"), entry(directory="frontend"))
        self.assertEqual(self.run_main(), (0, ""))
        lines = (self.tmp / "allowlist.json").read_text(encoding="utf-8").splitlines()
        second = [number for number, line in enumerate(lines, start=1) if f'"{ALLOWED}"' in line][1]
        self.assertIn(f",line={second},", self.stdout)
        self.assertEqual(self.stdout.count("::warning"), 1)

    def test_malformed_shared_allowlists_are_refused(self) -> None:
        cases = {
            "entry without a directory": (self.shared(entry()), "directory must be one of the projects"),
            "entry for an unknown directory": (
                self.shared(entry(directory="backend")),
                "directory must be one of the projects",
            ),
            "directory not a string": (self.shared(entry(directory=["frontend"])), "directory must be one of"),
            # Checked even though it is not this project's: a mistake anywhere fails every project.
            "bad entry in another project": (
                self.shared(entry(directory="internal/piagent", expires=None)),
                "expires must be a YYYY-MM-DD date",
            ),
            "duplicate in a project": (
                self.shared(entry(directory="frontend"), entry(directory="frontend")),
                f"{ALLOWED} is listed twice for frontend",
            ),
            "empty projects": ({"projects": {}, "allowlist": []}, '"projects" must be a non-empty object'),
            "project written loosely": (
                {"projects": {"./frontend": {}}, "allowlist": []},
                "write './frontend' as 'frontend'",
            ),
            "bad project thresholds": ({"projects": {"frontend": {"level": "x"}}, "allowlist": []}, "level must be"),
            "levels at the top": ({**self.shared(), "level": "high"}, "unknown keys ['level']"),
        }
        for label, (allowlist, message) in cases.items():
            with self.subTest(label):
                self.allowlist = allowlist
                code, stderr = self.run_main()
                self.assertEqual(code, 2)
                self.assertIn(message, stderr)

    def test_same_advisory_in_two_projects_is_allowed(self) -> None:
        self.allowlist = self.shared(entry(directory="frontend"), entry(directory="internal/piagent"))
        self.assertEqual(self.run_main(), (0, ""))


class DevOnlyTests(CheckTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.allowlist = {"allowlist": [entry(dev_only=True)]}

    def test_dev_only_advisory_passes(self) -> None:
        self.assertEqual(self.run_main(), (0, ""))

    def test_advisory_reaching_a_production_copy_fails(self) -> None:
        # A runtime dependency now pulls a second, nested copy of picomatch.
        self.report["vulnerabilities"]["picomatch"]["nodes"].append("node_modules/app-lib/node_modules/picomatch")
        self.lockfile["packages"]["node_modules/app-lib/node_modules/picomatch"] = {}
        code, stderr = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn(
            f"{ALLOWED}: allowlisted as dev_only, but not dev-only in the lockfile: "
            "node_modules/app-lib/node_modules/picomatch",
            stderr,
        )
        self.assertNotIn("node_modules/picomatch,", stderr)

    def test_dev_optional_copy_counts_as_production(self) -> None:
        self.lockfile["packages"]["node_modules/picomatch"] = {"devOptional": True}
        code, stderr = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("not dev-only in the lockfile: node_modules/picomatch", stderr)

    def test_copy_missing_from_lockfile_fails(self) -> None:
        del self.lockfile["packages"]["node_modules/picomatch"]
        code, stderr = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("not dev-only in the lockfile: node_modules/picomatch", stderr)

    def test_report_without_nodes_fails(self) -> None:
        del self.report["vulnerabilities"]["picomatch"]["nodes"]
        code, stderr = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("npm audit listed no copies", stderr)

    def test_entry_without_dev_only_ignores_the_lockfile(self) -> None:
        self.allowlist = {"allowlist": [entry()]}
        self.lockfile = None
        self.report["vulnerabilities"]["picomatch"]["nodes"].append("node_modules/app-lib/node_modules/picomatch")
        self.assertEqual(self.run_main(), (0, ""))

    def test_missing_or_old_lockfile_is_refused(self) -> None:
        for label, lockfile in {
            "missing": None,
            "lockfileVersion 1": {"lockfileVersion": 1, "dependencies": {}},
        }.items():
            with self.subTest(label):
                self.lockfile = lockfile
                (self.tmp / "package-lock.json").unlink(missing_ok=True)
                code, stderr = self.run_main()
                self.assertEqual(code, 2)
                self.assertIn("to tell dev-only copies from production ones", stderr)


class ReportTests(CheckTestCase):
    def test_npm_error_report_is_refused(self) -> None:
        self.report = {"error": {"code": "ENOAUDIT", "summary": "registry unavailable"}}
        code, stderr = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("npm audit failed", stderr)

    def test_report_that_is_not_utf8_is_refused(self) -> None:
        # What PowerShell 5's `npm audit --json > report.json` writes.
        report = self.tmp / "report-utf16.json"
        report.write_text(json.dumps(REPORT), encoding="utf-16")
        code, stderr = self.call(str(self.tmp), "--report", str(report), "--today", TODAY)
        self.assertEqual(code, 2)
        self.assertIn("cannot read it as UTF-8 text", stderr)

    def test_output_the_console_cannot_encode_is_escaped(self) -> None:
        # As through a Windows pipe, whose code page lacks the character; run as its own process.
        report = summarize(copy.deepcopy(REPORT))
        report["vulnerabilities"]["picomatch"]["via"][0]["title"] = "picomatch → ReDoS"
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                str(self.tmp),
                "--report",
                str(self.write("report.json", report)),
                "--allowlist",
                str(self.write("allowlist.json", {"allowlist": []})),
                "--today",
                TODAY,
            ],
            capture_output=True,
            env={**os.environ, "PYTHONIOENCODING": "ascii", "GITHUB_ACTIONS": ""},
            check=False,
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(rb"picomatch \u2192 ReDoS", result.stdout)
        self.assertNotIn(b"Traceback", result.stderr)

    def test_unrecognized_reports_are_refused(self) -> None:
        for label, report in {"v1 report": {"advisories": {}}, "not JSON": "", "not an object": []}.items():
            with self.subTest(label):
                self.report = report
                code, _ = self.run_main()
                self.assertEqual(code, 2)

    def test_inconsistent_reports_are_refused(self) -> None:
        self.summarize = False

        def report_with(change: callable) -> dict:
            report = summarize(copy.deepcopy(REPORT))
            change(report["vulnerabilities"], report)
            return report

        def counts(report: dict) -> dict:
            return report["metadata"]["vulnerabilities"]

        cases = {
            "entry not an object": (lambda v, r: v.update(vite=[]), "'vite' is not an object"),
            "entry with an unknown severity": (
                lambda v, r: v["fdir"].update(severity="severe"),
                "'fdir' has an unknown severity",
            ),
            "entry without a via list": (lambda v, r: v["vite"].pop("via"), "'vite' does not say what makes it"),
            "entry with an empty via list": (lambda v, r: v["vite"].update(via=[]), "'vite' does not say what"),
            "via naming an undescribed package": (
                lambda v, r: v["fdir"]["via"].append("rollup"),
                "'fdir' is vulnerable through 'rollup', which the report does not describe",
            ),
            "via item of the wrong type": (lambda v, r: v["fdir"]["via"].append(3), "neither a package name"),
            # Two packages vulnerable only through each other: no advisory stands behind either.
            "loop with no advisory": (
                lambda v, r: v.update(
                    a={"severity": "high", "via": ["b"]},
                    b={"severity": "high", "via": ["a"]},
                ),
                "'a' leads to no advisory",
            ),
            "entry rated above its advisories": (
                lambda v, r: v["vite"].update(severity="critical"),
                "'vite' is rated critical, but the worst advisory it leads to is high",
            ),
            "no summary": (lambda v, r: r.pop("metadata"), "no metadata.vulnerabilities summary"),
            "summary total disagrees": (
                lambda v, r: counts(r).update(total=5),
                "summary counts 5 vulnerable packages, but it lists 4",
            ),
            "summary severity disagrees": (
                lambda v, r: counts(r).update(high=3, moderate=1),
                "summary counts 1 moderate vulnerable packages, but it lists 0",
            ),
            "summary total not a number": (lambda v, r: counts(r).update(total=True), "summary counts True"),
            # A string would otherwise be read as one path per character.
            "nodes not a list": (lambda v, r: v["vite"].update(nodes=None), "'vite' lists its copies (nodes)"),
            "nodes a string": (lambda v, r: v["vite"].update(nodes="node_modules/vite"), "lists its copies"),
            "node not a string": (lambda v, r: v["vite"].update(nodes=[3]), "lists its copies"),
        }
        for label, (change, message) in cases.items():
            with self.subTest(label):
                self.report = report_with(change)
                code, stderr = self.run_main()
                self.assertEqual(code, 2)
                self.assertIn(message, stderr)

    def test_reports_that_once_passed_are_refused(self) -> None:
        # Each passed with exit 0 before the consistency checks (handoff from apollo-desktop#1193).
        self.summarize = False
        self.allowlist = {"allowlist": []}
        three_critical = {"info": 0, "low": 0, "moderate": 0, "high": 0, "critical": 3, "total": 3}
        one_critical = {"info": 0, "low": 0, "moderate": 0, "high": 0, "critical": 1, "total": 1}
        for label, vulnerabilities, summary in (
            ("empty via", {"x": {"severity": "critical", "via": []}}, one_critical),
            ("dangling via", {"x": {"severity": "critical", "via": ["missing"]}}, one_critical),
            ("summary of three, none listed", {}, three_critical),
        ):
            with self.subTest(label):
                self.report = {
                    "auditReportVersion": 2,
                    "vulnerabilities": vulnerabilities,
                    "metadata": {"vulnerabilities": summary},
                }
                self.assertEqual(self.run_main()[0], 2)


class NpmRunTests(CheckTestCase):
    """How the check runs npm, with npm itself replaced."""

    def run_npm(
        self, returncode: int = 1, stdout: str | None = None, stderr: str = "", environment: dict | None = None
    ) -> tuple[tuple[int, str], mock.MagicMock]:
        stdout = json.dumps(summarize(copy.deepcopy(REPORT))) if stdout is None else stdout
        completed = subprocess.CompletedProcess([], returncode, stdout, stderr)
        allowlist = str(self.write("allowlist.json", self.allowlist))
        with (
            mock.patch.object(check.shutil, "which", return_value="npm"),
            mock.patch.object(check.subprocess, "run", return_value=completed) as run,
        ):
            result = self.call(str(self.project), "--allowlist", allowlist, environment=environment)
        return result, run

    def test_every_setting_that_narrows_the_audit_is_pinned(self) -> None:
        (code, _), run = self.run_npm()
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.args[0], ["npm", *check.NPM_AUDIT_ARGS])
        self.assertEqual(run.call_args.kwargs["cwd"], self.project)
        for flag in (
            "--package-lock-only",
            "--offline=false",
            "--include=dev",
            "--include=optional",
            "--include=peer",
            "--production=null",
            "--only=null",
            "--also=null",
            "--dev=false",
        ):
            self.assertIn(flag, check.NPM_AUDIT_ARGS)

    def test_environment_does_not_change_the_command(self) -> None:
        _, run = self.run_npm(
            environment={"NODE_ENV": "production", "npm_config_omit": "dev", "npm_config_offline": "true"}
        )
        self.assertEqual(run.call_args.args[0], ["npm", *check.NPM_AUDIT_ARGS])

    def test_findings_pass_through_to_the_check(self) -> None:
        self.allowlist = {"allowlist": []}
        (code, stderr), _ = self.run_npm(returncode=1)
        self.assertEqual(code, 1)
        self.assertIn(f"{ALLOWED} (high, picomatch) is not allowlisted", stderr)

    def test_npms_stderr_is_passed_on(self) -> None:
        (code, stderr), _ = self.run_npm(stderr="npm error something odd")
        self.assertEqual(code, 0)
        self.assertIn("npm error something odd", stderr)

    def test_unfinished_audits_are_refused(self) -> None:
        error = json.dumps({"error": {"code": "ENOAUDIT", "summary": "no registry"}})
        for label, returncode, stdout, message in (
            ("unexpected exit status", 2, None, "npm audit exited 2"),
            ("killed by a signal", -15, None, "killed by signal 15"),
            ("error report", 1, error, "npm audit failed"),
            ("no report", 0, "  \n", "printed no report"),
        ):
            with self.subTest(label):
                (code, stderr), _ = self.run_npm(returncode=returncode, stdout=stdout)
                self.assertEqual(code, 2)
                self.assertIn(message, stderr)

    def test_missing_npm_is_refused(self) -> None:
        with mock.patch.object(check.shutil, "which", return_value=None):
            code, stderr = self.call(str(self.project))
        self.assertEqual(code, 2)
        self.assertIn("npm not found", stderr)


@unittest.skipUnless(shutil.which("git"), "needs git")
class ListProjectsTests(CheckTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.git("init", "-q")
        for path in ("package-lock.json", "a/package-lock.json", "b/c/package-lock.json", "a/package-lock.json.bak"):
            self.write(path, "{}")
        self.git("add", ".")
        self.write("untracked/package-lock.json", "{}")
        self.enter(self.tmp)

    def git(self, *args: str) -> None:
        subprocess.run(["git", *args], cwd=self.tmp, check=True, capture_output=True, env={**os.environ, **GIT_ENV})

    def list_projects(self, *argv: str) -> tuple[int, str]:
        return self.call("--list-projects", *argv, environment=GIT_ENV)

    def shared(self, *projects: str) -> str:
        return str(self.write("shared.json", {"projects": {name: {} for name in projects}, "allowlist": []}))

    def test_lists_every_committed_lockfile(self) -> None:
        self.assertEqual(self.list_projects(), (0, ""))
        self.assertEqual(json.loads(self.stdout), [".", "a", "b/c"])

    def test_lists_from_the_repository_root_wherever_it_runs(self) -> None:
        self.enter(self.tmp / "b")
        self.assertEqual(self.list_projects(), (0, ""))
        self.assertEqual(json.loads(self.stdout), [".", "a", "b/c"])

    def test_shared_allowlist_must_list_exactly_those(self) -> None:
        self.assertEqual(self.list_projects("--allowlist", self.shared(".", "a", "b/c")), (0, ""))
        code, stderr = self.list_projects("--allowlist", self.shared(".", "a", "d"))
        self.assertEqual(code, 2)
        self.assertIn("committed but not in its projects: b/c", stderr)
        self.assertIn("in its projects but with no committed package-lock.json: d", stderr)

    def test_a_single_projects_allowlist_is_refused(self) -> None:
        code, stderr = self.list_projects("--allowlist", str(self.write("single.json", {"allowlist": []})))
        self.assertEqual(code, 2)
        self.assertIn("not a shared allowlist", stderr)

    def test_no_lockfiles_is_refused(self) -> None:
        self.git("rm", "-q", "--cached", "-r", ".")
        code, stderr = self.list_projects()
        self.assertEqual(code, 2)
        self.assertIn("no committed package-lock.json", stderr)

    def test_missing_explicit_allowlist_is_refused(self) -> None:
        code, stderr = self.list_projects("--allowlist", str(self.tmp / "absent.json"))
        self.assertEqual(code, 2)
        self.assertIn("absent.json: no such file", stderr)

    def test_takes_no_directory_or_audit_options(self) -> None:
        self.assertEqual(self.call("--list-projects", ".")[0], 2)
        self.assertEqual(self.call("--list-projects", "--strict")[0], 2)
        self.assertEqual(self.call("--list-projects", "--all")[0], 2)
        self.assertEqual(self.call()[0], 2)


CLEAN_REPORT = json.dumps(
    {
        "auditReportVersion": 2,
        "vulnerabilities": {},
        "metadata": {"vulnerabilities": {**dict.fromkeys(SEVERITIES, 0), "total": 0}},
    }
)


@unittest.skipUnless(shutil.which("git"), "needs git")
class AllProjectsTests(CheckTestCase):
    """--all, with npm replaced by a canned report per project."""

    def setUp(self) -> None:
        super().setUp()
        subprocess.run(
            ["git", "init", "-q"], cwd=self.tmp, check=True, capture_output=True, env={**os.environ, **GIT_ENV}
        )
        for name in ("a", "b", "c"):
            self.write(f"{name}/package-lock.json", LOCKFILE)
        subprocess.run(
            ["git", "add", "."], cwd=self.tmp, check=True, capture_output=True, env={**os.environ, **GIT_ENV}
        )
        self.enter(self.tmp)
        # a passes, b has an advisory it does not allowlist, c's report cannot be trusted.
        self.reports = {"a": CLEAN_REPORT, "b": json.dumps(summarize(copy.deepcopy(REPORT))), "c": "{"}

    def audit_all(self, *argv: str) -> tuple[tuple[int, str], mock.MagicMock]:
        def run_npm_audit(directory: Path) -> str:
            return self.reports[directory.name]

        with mock.patch.object(check, "run_npm_audit", side_effect=run_npm_audit) as run:
            result = self.call("--all", *argv, environment=GIT_ENV)
        return result, run

    def test_runs_every_project_and_exits_with_the_worst(self) -> None:
        (code, stderr), run = self.audit_all()
        self.assertEqual(code, 2)
        self.assertEqual([call.args[0].name for call in run.call_args_list], ["a", "b", "c"])
        headings = [line for line in self.stdout.splitlines() if line.startswith("==> ")]
        self.assertEqual(headings, ["==> npm audit: a", "==> npm audit: b", "==> npm audit: c"])
        self.assertIn(f"{ALLOWED} (high, picomatch) is not allowlisted", stderr)
        self.assertIn("error: npm audit report is not JSON", stderr)
        self.assertIn("npm audit did not pass for: b (1), c (2)", stderr)

    def test_failure_ranks_above_a_pass(self) -> None:
        self.reports["c"] = CLEAN_REPORT
        (code, stderr), _ = self.audit_all()
        self.assertEqual(code, 1)
        self.assertIn("npm audit did not pass for: b (1)", stderr)
        self.reports["b"] = CLEAN_REPORT
        self.assertEqual(self.audit_all()[0], (0, ""))

    def test_uses_each_projects_own_allowlist(self) -> None:
        self.write("b/audit-allowlist.json", {"allowlist": [entry()]})
        self.reports["c"] = CLEAN_REPORT
        self.assertEqual(self.audit_all()[0], (0, ""))

    def test_uses_a_shared_allowlist_and_its_levels(self) -> None:
        self.reports["c"] = CLEAN_REPORT
        shared = {"projects": {"a": {}, "b": {"level": "critical"}, "c": {}}, "allowlist": []}
        self.assertEqual(self.audit_all("--allowlist", str(self.write("shared.json", shared)))[0], (0, ""))
        shared["projects"]["b"] = {}
        self.assertEqual(self.audit_all("--allowlist", str(self.write("shared.json", shared)))[0][0], 1)

    def test_uses_the_shared_allowlist_next_to_the_script_by_default(self) -> None:
        self.reports["c"] = CLEAN_REPORT
        shared = {"projects": {"a": {}, "b": {"level": "critical"}, "c": {}}, "allowlist": []}
        with mock.patch.object(check, "SHARED_ALLOWLIST", self.write("shared.json", shared)):
            self.assertEqual(self.audit_all()[0], (0, ""))
            # Its projects are checked against the lockfiles up front, as an explicit one's are.
            shared["projects"] = {"a": {}, "b": {}}
            self.write("shared.json", shared)
            (code, stderr), run = self.audit_all()
            self.assertEqual(code, 2)
            self.assertIn("committed but not in its projects: c", stderr)
            run.assert_not_called()

    def test_runs_from_anywhere_in_the_repository(self) -> None:
        self.enter(self.tmp / "b")
        (code, _), run = self.audit_all()
        self.assertEqual(code, 2)
        # Resolved: git gives the long form of a path Windows may hold in its short (8.3) form.
        called = [call.args[0].resolve() for call in run.call_args_list]
        self.assertEqual(called, [(self.tmp / name).resolve() for name in "abc"])

    def test_a_bad_shared_allowlist_is_refused_before_any_audit(self) -> None:
        for label, shared, message in (
            ("projects that differ", {"projects": {"a": {}, "b": {}}, "allowlist": []}, "committed but not in"),
            ("malformed", {"projects": {"a": {}, "b": {}, "c": {}}, "allowlist": [entry()]}, "directory must be"),
            ("missing", None, "no such file"),
        ):
            with self.subTest(label):
                path = self.tmp / "shared.json"
                path.unlink(missing_ok=True)
                if shared is not None:
                    self.write("shared.json", shared)
                (code, stderr), run = self.audit_all("--allowlist", str(path))
                self.assertEqual(code, 2)
                self.assertIn(message, stderr)
                self.assertEqual(stderr.count("error:"), 1)
                run.assert_not_called()

    def test_takes_no_directory_report_or_date(self) -> None:
        for argv in (("a",), ("--report", "r.json"), ("--report", "r.json", "--today", TODAY)):
            with self.subTest(argv=argv):
                self.assertEqual(self.audit_all(*argv)[0][0], 2)


FIXTURE_PACKAGE = {
    "name": "audit-fixture",
    "version": "1.0.0",
    "private": True,
    "dependencies": {"fixture-prod": "1.0.0"},
    "devDependencies": {"fixture-dev": "1.0.0"},
}


class FakeRegistry(http.server.BaseHTTPRequestHandler):
    """The npm registry's bulk advisory endpoint, reporting one advisory, against fixture-dev."""

    # do_POST, do_GET and log_message are the names http.server calls.
    def do_POST(self) -> None:
        body = self.read_body()
        if self.path != "/-/npm/v1/security/advisories/bulk":
            self.reply(404, {})
            return
        if self.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        names = sorted(json.loads(body))
        self.server.asked.append(names)
        advisories = {}
        if "fixture-dev" in names:
            advisories["fixture-dev"] = [
                {
                    "id": 1,
                    "url": f"https://github.com/advisories/{OTHER}",
                    "title": "fixture-dev advisory",
                    "severity": "moderate",
                    "vulnerable_versions": "<=1.0.0",
                }
            ]
        self.reply(200, advisories)

    def do_GET(self) -> None:
        self.reply(404, {})

    def read_body(self) -> bytes:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            chunks = []
            while size := int(self.rfile.readline().split(b";")[0].strip(), 16):
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
            self.rfile.readline()
            return b"".join(chunks)
        return self.rfile.read(int(self.headers.get("Content-Length", 0)))

    def reply(self, status: int, payload: object) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        pass


@unittest.skipUnless(shutil.which("npm"), "needs npm")
class RealNpmTests(CheckTestCase):
    """The real npm client, against a registry on localhost: the pinned settings work, not only get passed."""

    def setUp(self) -> None:
        super().setUp()
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeRegistry)
        server.asked = []
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.server = server
        self.project = self.tmp / "project"
        self.write("project/package.json", FIXTURE_PACKAGE)
        self.write(
            "project/package-lock.json",
            {
                "name": "audit-fixture",
                "version": "1.0.0",
                "lockfileVersion": 3,
                "requires": True,
                "packages": {
                    "": {key: FIXTURE_PACKAGE[key] for key in ("name", "version", "dependencies", "devDependencies")},
                    "node_modules/fixture-dev": {"version": "1.0.0", "dev": True},
                    "node_modules/fixture-prod": {"version": "1.0.0"},
                },
            },
        )
        # npm's own configuration, isolated from the developer's; tests write settings into it.
        self.npmrc = self.write("npmrc", "")

    def run_live(self, *argv: str, registry: str | None = None, **settings: str) -> tuple[int, str]:
        environment = {
            "npm_config_registry": registry or f"http://127.0.0.1:{self.server.server_address[1]}/",
            "npm_config_userconfig": str(self.npmrc),
            "npm_config_cache": str(self.tmp / "cache"),
            "npm_config_fetch_retries": "0",
            "npm_config_update_notifier": "false",
            **settings,
        }
        self.server.asked.clear()
        allowlist = str(self.write("none.json", {"allowlist": []}))
        return self.call(str(self.project), "--allowlist", allowlist, *argv, environment=environment)

    def assert_audits_everything(self, code: int, stderr: str) -> None:
        self.assertEqual(code, 1, stderr)
        self.assertIn(f"{OTHER} (moderate, fixture-dev) is not allowlisted", stderr)
        self.assertEqual(self.server.asked, [["fixture-dev", "fixture-prod"]])

    def test_audits_every_dependency(self) -> None:
        self.assert_audits_everything(*self.run_live())

    def test_prod_level_judges_a_dev_only_advisory_by_level(self) -> None:
        self.assertEqual(self.run_live("--level", "high", "--prod-level", "moderate")[0], 0)
        self.assertEqual(self.server.asked, [["fixture-dev", "fixture-prod"]])

    def test_environment_cannot_narrow_the_audit(self) -> None:
        for label, settings in {
            "offline": {"npm_config_offline": "true"},
            "NODE_ENV=production": {"NODE_ENV": "production"},
            "settings that omit dev": {
                "npm_config_omit": "dev",
                "npm_config_only": "production",
                "npm_config_production": "true",
            },
        }.items():
            with self.subTest(label):
                self.assert_audits_everything(*self.run_live(**settings))

    def test_npmrc_cannot_narrow_the_audit(self) -> None:
        self.npmrc.write_text("offline=true\nomit=dev\nproduction=true\n", encoding="utf-8")
        self.assert_audits_everything(*self.run_live())

    def test_unreachable_registry_is_refused(self) -> None:
        # Nothing listens on the discard port, so the lookup cannot succeed.
        code, stderr = self.run_live(registry="http://127.0.0.1:9/")
        self.assertEqual(code, 2, stderr)


if __name__ == "__main__":
    unittest.main()
