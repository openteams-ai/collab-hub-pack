"""Tests for scripts/check_npm_audit.py: that it refuses what it exists to refuse.

    python3 -m unittest discover --start-directory scripts/tests --pattern test_check_npm_audit.py --verbose

Standard library only, like the check. The reports are synthetic, shaped like ``npm audit --json``
(auditReportVersion 2) output for api/admin-ui's picomatch -> fdir -> tinyglobby -> vite chain.
"""

from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "check_npm_audit.py"
_spec = importlib.util.spec_from_file_location("check_npm_audit", SCRIPT)
check = importlib.util.module_from_spec(_spec)
# dataclasses resolves the module's string annotations through sys.modules.
sys.modules[_spec.name] = check
_spec.loader.exec_module(check)

ALLOWED = "GHSA-fc5q-39xc-vjpf"
TODAY = "2026-11-17"


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
            "isDirect": False,
            "via": [advisory(ALLOWED, "picomatch")],
            "nodes": ["node_modules/picomatch"],
        },
        "fdir": {"name": "fdir", "isDirect": False, "via": ["picomatch"], "nodes": ["node_modules/fdir"]},
        "tinyglobby": {
            "name": "tinyglobby",
            "isDirect": False,
            "via": ["fdir", "picomatch"],
            "nodes": ["node_modules/tinyglobby"],
        },
        "vite": {"name": "vite", "isDirect": True, "via": ["picomatch", "tinyglobby"], "nodes": ["node_modules/vite"]},
    },
    "metadata": {},
}

# The matching package-lock.json: every one of those packages is a devDependency-only node.
LOCKFILE = {
    "lockfileVersion": 3,
    "packages": {
        "": {"name": "admin-ui"},
        **{f"node_modules/{name}": {"dev": True} for name in ("picomatch", "fdir", "tinyglobby", "vite")},
    },
}


def entry(ghsa: str = ALLOWED, expires: str | None = "2027-02-15", **overrides: object) -> dict:
    return {"id": ghsa, "expires": expires, "reason": "dev-only", "tracking": "https://example.invalid", **overrides}


class CheckTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.report = copy.deepcopy(REPORT)
        self.allowlist: object = {"allowlist": [entry()]}
        self.lockfile: object = copy.deepcopy(LOCKFILE)

    def write(self, name: str, data: object) -> Path:
        path = self.tmp / name
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        return path

    def run_main(self, *argv: str) -> tuple[int, str]:
        if self.lockfile is not None:
            self.write("package-lock.json", self.lockfile)
        args = [
            str(self.tmp),
            "--report",
            str(self.write("report.json", self.report)),
            "--allowlist",
            str(self.write("allowlist.json", self.allowlist)),
            "--today",
            TODAY,
            *argv,
        ]
        stderr = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            try:
                code = check.main(args)
            except SystemExit as error:
                code, message = 2, str(error.code)
                stderr.write(message)
        return code, stderr.getvalue()

    def add_advisory(self, package: str, ghsa: str, severity: str = "critical") -> None:
        self.report["vulnerabilities"][package]["via"].append(advisory(ghsa, package, severity))


class AdvisoryTests(CheckTestCase):
    def test_allowlisted_advisory_passes(self) -> None:
        self.assertEqual(self.run_main(), (0, ""))

    def test_clean_report_with_no_allowlist_passes(self) -> None:
        self.report["vulnerabilities"] = {}
        self.allowlist = {"allowlist": []}
        self.assertEqual(self.run_main(), (0, ""))

    def test_missing_allowlist_file_means_none(self) -> None:
        code, stderr = self.run_main("--allowlist", str(self.tmp / "absent.json"))
        self.assertEqual(code, 1)
        self.assertIn(f"{ALLOWED} (high, picomatch) is not allowlisted", stderr)

    def test_unlisted_advisory_fails(self) -> None:
        self.allowlist = {"allowlist": []}
        code, stderr = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn(f"{ALLOWED} (high, picomatch) is not allowlisted", stderr)

    def test_new_advisory_on_transitive_package_after_a_name_fails(self) -> None:
        # The case audit-ci 7.1.0 passes (IBM/audit-ci#360): tinyglobby is transitive and its via
        # list starts with the names of other vulnerable packages.
        self.add_advisory("tinyglobby", "GHSA-2222-3333-4444")
        code, stderr = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("GHSA-2222-3333-4444 (critical, tinyglobby) is not allowlisted", stderr)
        self.assertNotIn(ALLOWED, stderr)

    def test_new_advisory_on_allowlisted_package_fails(self) -> None:
        self.add_advisory("picomatch", "GHSA-2222-3333-4444")
        code, stderr = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("GHSA-2222-3333-4444 (critical, picomatch) is not allowlisted", stderr)

    def test_advisory_below_level_passes(self) -> None:
        self.add_advisory("vite", "GHSA-2222-3333-4444", "low")
        self.assertEqual(self.run_main("--level", "moderate"), (0, ""))
        self.assertEqual(self.run_main()[0], 1)

    def test_info_advisory_never_fails(self) -> None:
        self.add_advisory("vite", "GHSA-2222-3333-4444", "info")
        self.assertEqual(self.run_main(), (0, ""))

    def test_advisory_without_ghsa_url_fails(self) -> None:
        self.report["vulnerabilities"]["vite"]["via"].append({"name": "vite", "severity": "low", "url": ""})
        code, stderr = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("untitled advisory on vite", stderr)

    def test_unknown_severity_counts_as_critical(self) -> None:
        self.add_advisory("vite", "GHSA-2222-3333-4444", "severe")
        code, stderr = self.run_main("--level", "critical")
        self.assertEqual(code, 1)
        self.assertIn("GHSA-2222-3333-4444 (critical, vite)", stderr)


class AllowlistTests(CheckTestCase):
    def test_entry_is_valid_through_its_expiry_day(self) -> None:
        self.allowlist = {"allowlist": [entry(expires=TODAY)]}
        self.assertEqual(self.run_main(), (0, ""))

    def test_expired_entry_fails(self) -> None:
        self.allowlist = {"allowlist": [entry(expires="2026-11-16")]}
        code, stderr = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn(f"{ALLOWED}: allowlist entry expired on 2026-11-16", stderr)

    def test_entry_no_longer_reported_fails(self) -> None:
        self.report["vulnerabilities"] = {}
        code, stderr = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn(f"{ALLOWED}: allowlisted but no longer reported", stderr)

    def test_entry_without_expiry_never_expires(self) -> None:
        self.allowlist = {"allowlist": [entry(expires=None)]}
        self.assertEqual(self.run_main("--today", "2099-12-31"), (0, ""))

    def test_entry_without_expiry_still_fails_once_no_longer_reported(self) -> None:
        self.allowlist = {"allowlist": [entry(expires=None)]}
        self.report["vulnerabilities"] = {}
        code, stderr = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn(f"{ALLOWED}: allowlisted but no longer reported", stderr)

    def test_malformed_allowlists_are_refused(self) -> None:
        # Each case names the refusal it must hit, so one caught by the wrong check fails the test.
        not_a_list = 'expected an object with an "allowlist" list'
        wrong_keys = "expected the keys"
        bad_date = "neither a YYYY-MM-DD date nor null"
        cases = {
            "not an object": ([], not_a_list),
            "no allowlist list": ({"allowlist": {}}, not_a_list),
            "missing key": ({"allowlist": [{"id": ALLOWED, "expires": "2027-02-15", "reason": "x"}]}, wrong_keys),
            "unknown key": ({"allowlist": [entry(note="typo")]}, wrong_keys),
            "empty reason": ({"allowlist": [entry(reason=" ")]}, "id, reason and tracking must be non-empty strings"),
            "not a GHSA ID": ({"allowlist": [entry(ghsa="CVE-2026-112366")]}, "not a GHSA ID: 'CVE-2026-112366'"),
            "duplicate": ({"allowlist": [entry(), entry()]}, f"{ALLOWED} is listed twice"),
            "bad date": ({"allowlist": [entry(expires="15 February 2027")]}, bad_date),
            "empty expiry": ({"allowlist": [entry(expires="")]}, bad_date),
            "numeric expiry": ({"allowlist": [entry(expires=20270215)]}, bad_date),
            # No expiry has to be said with null; a forgotten key is not taken to mean it.
            "expiry omitted": ({"allowlist": [{"id": ALLOWED, "reason": "x", "tracking": "y"}]}, wrong_keys),
            "dev_only not a boolean": ({"allowlist": [entry(dev_only="yes")]}, "dev_only must be true or false"),
            "not JSON": ("{", "not valid JSON"),
        }
        for label, (allowlist, message) in cases.items():
            with self.subTest(label):
                self.allowlist = allowlist
                code, stderr = self.run_main()
                self.assertEqual(code, 2)
                self.assertIn(message, stderr)


class DevOnlyTests(CheckTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.allowlist = {"allowlist": [entry(expires=None, dev_only=True)]}

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
        self.assertIn("npm audit listed no installed copies", stderr)

    def test_entry_without_dev_only_ignores_the_lockfile(self) -> None:
        self.allowlist = {"allowlist": [entry(expires=None)]}
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
                self.assertIn("to check dev_only entries", stderr)


class ReportTests(CheckTestCase):
    def test_npm_error_report_is_refused(self) -> None:
        self.report = {"error": {"code": "ENOAUDIT", "summary": "registry unavailable"}}
        code, stderr = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("npm audit failed", stderr)

    def test_unrecognized_reports_are_refused(self) -> None:
        for label, report in {"v1 report": {"advisories": {}}, "not JSON": "", "not an object": []}.items():
            with self.subTest(label):
                self.report = report
                code, _ = self.run_main()
                self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
