"""Tests for scripts/check_emudhra_trust.py: that it refuses what it exists to refuse (#285).

    python3 -m unittest discover --start-directory scripts/tests --verbose

Standard library only, like the check. The certificates are synthetic: the check hashes each PEM
block's DER bytes without parsing them, so any bytes stand in for a root.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import os
import ssl
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "check_emudhra_trust.py"
_spec = importlib.util.spec_from_file_location("check_emudhra_trust", SCRIPT)
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

INCIDENT = b"stand-in for an incident root"
UNRELATED = b"stand-in for an unrelated root"


def pem(*ders: bytes) -> str:
    return "".join(ssl.DER_cert_to_PEM_cert(der) for der in ders)


def sha256(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest().upper()


class CheckTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.fixture = self.write("roots.txt", f"# incident roots\n{sha256(INCIDENT)} Incident Root G1\n")

    def write(self, name: str, text: str) -> Path:
        path = self.tmp / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def run_main(self, files: list[Path] | None = None, argv: tuple[str, ...] = ()) -> tuple[int, str]:
        """Run the check against this fixture; ``files`` replaces the store lookup unless None."""
        patches = [mock.patch.object(check, "FIXTURE", self.fixture)]
        if files is not None:
            patches.append(mock.patch.object(check, "store_files", return_value=[str(f) for f in files]))
        stderr = io.StringIO()
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.redirect_stderr(stderr))
            code = check.main(list(argv))
        return code, stderr.getvalue()


class TrustStoreTests(CheckTestCase):
    def test_store_without_incident_roots_passes(self) -> None:
        code, stderr = self.run_main([self.write("bundle.pem", pem(UNRELATED))])
        self.assertEqual((code, stderr), (0, ""))

    def test_incident_root_in_a_bundle_fails(self) -> None:
        bundle = self.write("bundle.pem", pem(UNRELATED, INCIDENT))
        code, stderr = self.run_main([bundle])
        self.assertEqual(code, 1)
        self.assertIn(f"{bundle} trusts Incident Root G1", stderr)

    def test_incident_root_in_any_one_store_fails(self) -> None:
        clean = self.write("certifi.pem", pem(UNRELATED))
        stale = self.write("os.pem", pem(INCIDENT))
        code, stderr = self.run_main([clean, stale])
        self.assertEqual(code, 1)
        self.assertIn(str(stale), stderr)
        self.assertNotIn(str(clean), stderr)

    def test_lowercase_fixture_digest_still_matches(self) -> None:
        self.fixture = self.write("roots.txt", f"{sha256(INCIDENT).lower()} Incident Root G1\n")
        code, _ = self.run_main([self.write("bundle.pem", pem(INCIDENT))])
        self.assertEqual(code, 1)

    def test_no_store_files_fails(self) -> None:
        code, stderr = self.run_main([])
        self.assertEqual(code, 1)
        self.assertIn("no certificates found", stderr)

    def test_store_files_without_certificates_fail(self) -> None:
        code, stderr = self.run_main([self.write("empty.pem", ""), self.write("notes.txt", "not a certificate\n")])
        self.assertEqual(code, 1)
        self.assertIn("no certificates found", stderr)


class StoreDiscoveryTests(CheckTestCase):
    """The real store lookup, pointed at temporary stores, finds an incident root wherever it is."""

    def stores(self, cafile: Path | None, capath: Path | None, certifi_bundle: Path | None) -> contextlib.ExitStack:
        paths = ssl.DefaultVerifyPaths(
            cafile=str(cafile) if cafile else None,
            capath=str(capath) if capath else None,
            openssl_cafile_env="SSL_CERT_FILE",
            openssl_cafile=str(self.tmp / "missing.pem"),
            openssl_capath_env="SSL_CERT_DIR",
            openssl_capath=str(self.tmp / "missing"),
        )
        # None in sys.modules makes `import certifi` raise ImportError, as when it isn't installed.
        certifi = types.SimpleNamespace(where=lambda: str(certifi_bundle)) if certifi_bundle else None
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(check.ssl, "get_default_verify_paths", return_value=paths))
        stack.enter_context(mock.patch.dict(sys.modules, {"certifi": certifi}))
        return stack

    def test_incident_root_only_in_capath_fails(self) -> None:
        capath = self.tmp / "certs"
        self.write("certs/unrelated.pem", pem(UNRELATED))
        stale = self.write("certs/incident.pem", pem(INCIDENT))
        with self.stores(self.write("ca.pem", pem(UNRELATED)), capath, None):
            code, stderr = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn(f"{os.path.realpath(stale)} trusts Incident Root G1", stderr)

    def test_incident_root_only_in_certifi_fails(self) -> None:
        bundle = self.write("certifi/cacert.pem", pem(INCIDENT))
        with self.stores(self.write("ca.pem", pem(UNRELATED)), None, bundle):
            code, stderr = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn(f"{os.path.realpath(bundle)} trusts Incident Root G1", stderr)

    def test_missing_stores_fail(self) -> None:
        with self.stores(None, None, None):
            code, stderr = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("no certificates found", stderr)

    def test_capath_links_are_read_once(self) -> None:
        capath = self.tmp / "certs"
        target = self.write("certs/incident.pem", pem(INCIDENT))
        try:
            os.symlink(target, capath / "0a1b2c3d.0")
        except OSError as error:  # Windows without the symlink privilege
            self.skipTest(f"cannot create a symlink here: {error}")
        with self.stores(None, capath, None):
            files = check.store_files()
        self.assertEqual(files, [os.path.realpath(target)])


class FixtureTests(CheckTestCase):
    def assert_refused(self, text: str, message: str) -> None:
        with self.assertRaises(SystemExit) as raised:
            check.load_fingerprints(self.write("roots.txt", text))
        self.assertIn(message, str(raised.exception))

    def test_shipped_fixture_loads(self) -> None:
        self.assertTrue(check.load_fingerprints(check.FIXTURE))

    def test_colon_separated_digest_is_refused(self) -> None:
        colons = ":".join(sha256(INCIDENT)[i : i + 2] for i in range(0, 64, 2))
        self.assert_refused(f"{colons} Incident Root G1\n", "not a 64-hex-digit SHA-256")

    def test_short_digest_is_refused(self) -> None:
        self.assert_refused(f"{sha256(INCIDENT)[:63]} Incident Root G1\n", "not a 64-hex-digit SHA-256")

    def test_non_hex_digest_is_refused(self) -> None:
        self.assert_refused(f"{'G' * 64} Incident Root G1\n", "not a 64-hex-digit SHA-256")

    def test_sha1_digest_is_refused(self) -> None:
        self.assert_refused(f"{hashlib.sha1(INCIDENT).hexdigest()} Incident Root G1\n", "not a 64-hex-digit SHA-256")

    def test_empty_fixture_is_refused(self) -> None:
        self.assert_refused("", "no fingerprints")

    def test_comments_only_fixture_is_refused(self) -> None:
        self.assert_refused("# nothing listed yet\n\n", "no fingerprints")

    def test_malformed_fixture_stops_the_check(self) -> None:
        self.fixture = self.write("roots.txt", "not-a-digest Incident Root G1\n")
        with self.assertRaises(SystemExit):
            self.run_main([self.write("bundle.pem", pem(UNRELATED))])


class HardenedPythonTests(CheckTestCase):
    def test_each_minor_passes_at_its_floor_and_fails_below_it(self) -> None:
        for minor, floor in check.HARDENED_PYTHON.items():
            with self.subTest(minor=minor):
                self.assertIsNone(check.python_failure(floor))
                self.assertIsNone(check.python_failure((*minor, floor[2] + 1)))
                self.assertIsNotNone(check.python_failure((*minor, floor[2] - 1)))
                self.assertIsNotNone(check.python_failure((*minor, 0)))

    def test_floors_keep_the_verified_releases(self) -> None:
        # The first release of each minor with gh-160040's denylist, checked against the release tags.
        # A new minor may be added; none of these may be dropped or lowered.
        verified = {
            (3, 11): (3, 11, 18),
            (3, 12): (3, 12, 16),
            (3, 13): (3, 13, 18),
            (3, 14): (3, 14, 10),
            (3, 15): (3, 15, 2),
        }
        for minor, floor in verified.items():
            with self.subTest(minor=minor):
                self.assertGreaterEqual(check.HARDENED_PYTHON.get(minor, (0, 0, 0)), floor)

    def test_minor_without_a_hardened_release_fails(self) -> None:
        for version in ((3, 10, 99), (3, 16, 0), (4, 0, 0)):
            with self.subTest(version=version):
                self.assertIn("no hardened release listed", check.python_failure(version))

    def test_flag_fails_an_interpreter_below_its_floor(self) -> None:
        bundle = self.write("bundle.pem", pem(UNRELATED))
        current = tuple(sys.version_info[:3])
        above = {current[:2]: (*current[:2], current[2] + 1)}
        with mock.patch.object(check, "HARDENED_PYTHON", above):
            self.assertEqual(self.run_main([bundle])[0], 0)
            code, stderr = self.run_main([bundle], ("--require-hardened-python",))
        self.assertEqual(code, 1)
        self.assertIn("Python is older than", stderr)

    def test_flag_passes_an_interpreter_at_its_floor(self) -> None:
        bundle = self.write("bundle.pem", pem(UNRELATED))
        current = tuple(sys.version_info[:3])
        with mock.patch.object(check, "HARDENED_PYTHON", {current[:2]: current}):
            self.assertEqual(self.run_main([bundle], ("--require-hardened-python",)), (0, ""))


if __name__ == "__main__":
    unittest.main()
