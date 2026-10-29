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
        body = "Bump package from 1 to 2.\n\n<details>\nrelease notes\n</details>\n\n<details>\ncommits\n</details>\n\nSigned-off-by: bot"
        self.assertEqual(strip_pr_body(body), "Bump package from 1 to 2.\n\n\n\nSigned-off-by: bot\n")

    def test_nested_details_and_attributes(self):
        body = "Summary\n<details open>\n  <details>\n  nested\n  </details>\n</details>"
        self.assertEqual(strip_pr_body(body), "Summary\n")

    def test_marker_is_processed_before_details(self):
        self.assertEqual(strip_pr_body("Summary\n<!-- reviewer-context -->\n<details>unfinished"), "Summary\n")

    def test_first_to_last_span_is_removed(self):
        body = "Summary\n<details>a\n</details>discard this too\n<DETAILS>b\n</DETAILS>\nFooter"
        self.assertEqual(strip_pr_body(body), "Summary\n\nFooter\n")

    def test_inline_details_mention_is_preserved(self):
        body = "Strip `<details>` blocks; a literal </details> in prose stays."
        self.assertEqual(strip_pr_body(body), body + "\n")

    def test_truncated_details_run_to_end(self):
        # GitHub truncates large grouped Dependabot bodies before the closing tag.
        body = "Bumps the deps group with 9 updates.\n<details>\n<summary>Release notes</summary>\n<details>\ncut off"
        self.assertEqual(strip_pr_body(body), "Bumps the deps group with 9 updates.\n")

    def test_comments_and_headings_are_removed(self):
        body = "## What's Changed\n\nSummary <!-- inline -->\n<!-- multi\nline -->\n### Notes\nFixes #12"
        self.assertEqual(strip_pr_body(body), "Summary \n\nFixes #12\n")

    def test_crlf_is_normalized(self):
        body = "Summary\r\n\r\n<details>\r\nnotes\r\n</details>\r\nFooter\r\n"
        self.assertEqual(strip_pr_body(body), "Summary\n\n\nFooter\n")

    def test_legacy_description_is_preserved(self):
        self.assertEqual(strip_pr_body("  Legacy summary\n\nMore context  "), "Legacy summary\n\nMore context\n")

    def test_empty_and_marker_only(self):
        self.assertEqual(strip_pr_body(""), "")
        self.assertEqual(strip_pr_body("<!-- reviewer-context -->instructions"), "")

    def test_unedited_template_fails(self):
        for body in ("Fixes #\n\nReal summary", "Fixes #12\n\nSUMMARY_HERE", "Fixes #\n<!-- reviewer-context -->"):
            with self.subTest(body=body), self.assertRaises(ValueError):
                strip_pr_body(body)

    def test_unmatched_closing_details_fail(self):
        for body in ("Summary\n</details>", "</details>\n<details>"):
            with self.subTest(body=body), self.assertRaises(ValueError):
                strip_pr_body(body)

    def test_cli_utf8_and_failure_output(self):
        script = str(Path(__file__).with_name("strip_pr_body.py"))
        result = subprocess.run([sys.executable, script], input="Résumé\n<!-- reviewer-context -->notes".encode(), capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "Résumé\n".encode())
        result = subprocess.run([sys.executable, script], input=b"Summary\n</details>", capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertIn(b"unmatched", result.stderr)


if __name__ == "__main__":
    unittest.main()
