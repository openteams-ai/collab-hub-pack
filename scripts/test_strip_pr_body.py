"""Regression coverage for squash message cleanup (standard library only)."""

import subprocess
import sys
import unittest
from pathlib import Path

from strip_pr_body import strip_pr_body


class StripPrBodyTests(unittest.TestCase):
    def test_marker_preserves_release_metadata(self):
        summary = "Fixes #123\n\nBREAKING CHANGE: migrate config\nThis reverts commit abc123."
        self.assertEqual(strip_pr_body(summary + "\n\n<!-- reviewer-context -->\n- [ ] tests"), summary + "\n")

    def test_dependabot_without_marker(self):
        body = "Bump package from 1 to 2.\n\n<details>release notes</details>\n\n<details>commits</details>\n\nSigned-off-by: bot"
        self.assertEqual(strip_pr_body(body), "Bump package from 1 to 2.\n\n\n\nSigned-off-by: bot\n")

    def test_nested_details_and_attributes(self):
        self.assertEqual(strip_pr_body('Summary\n<details open><details>nested</details></details>'), "Summary\n")

    def test_marker_is_processed_before_details(self):
        self.assertEqual(strip_pr_body("Summary\n<!-- reviewer-context -->\n<details>unfinished"), "Summary\n")

    def test_first_to_last_span_is_removed(self):
        self.assertEqual(strip_pr_body("Summary\n<details>a</details>discard this too<DETAILS>b</DETAILS>\nFooter"), "Summary\n\nFooter\n")

    def test_legacy_description_is_preserved(self):
        self.assertEqual(strip_pr_body("  Legacy summary\n\nMore context  "), "Legacy summary\n\nMore context\n")

    def test_empty_and_marker_only(self):
        self.assertEqual(strip_pr_body(""), "")
        self.assertEqual(strip_pr_body("<!-- reviewer-context -->instructions"), "")

    def test_unmatched_details_fail(self):
        for body in ("Summary <details>notes", "Summary </details>", "</details><details>", "<details><details></details>"):
            with self.subTest(body=body), self.assertRaises(ValueError):
                strip_pr_body(body)

    def test_cli_utf8_and_failure_output(self):
        script = str(Path(__file__).with_name("strip_pr_body.py"))
        result = subprocess.run([sys.executable, script], input="Résumé\n<!-- reviewer-context -->notes".encode(), capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "Résumé\n".encode())
        result = subprocess.run([sys.executable, script], input=b"Summary <details>unfinished", capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertIn(b"unmatched", result.stderr)


if __name__ == "__main__":
    unittest.main()
