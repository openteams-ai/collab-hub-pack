# Pull request and commit message style

PR descriptions help reviewers evaluate a change. Squash commit messages help
future readers understand why it was made. Structure descriptions so the
essential explanation is easy to reuse as the commit message.

These conventions apply to human-written, agent-generated, and dependency-update
PRs. Existing [contribution requirements](../CONTRIBUTING.md), including validation and code-owner review, still apply.

## Titles

Follow the repository's Conventional Commits title and release rules. Use a
short, specific description of the resulting change:

- Good: `fix(auth): preserve login when reopening the app`
- Avoid: `fix: authentication improvements and various fixes`

## Summary

Start with one to three short paragraphs explaining the problem, what changes,
and any lasting compatibility or migration implications. One or two sentences
are enough for a simple change. Include the relevant issue reference here, using
a closing keyword when the PR resolves it.

Describe the final implementation. Omit development narration, repeated
explanations of the diff, and abandoned approaches unless they explain an
important design decision. Update the title and summary when scope changes.

Keep release metadata in this summary: any `BREAKING CHANGE:` footer and, for a
revert, the exact `This reverts commit <sha>.` line must survive into the squash
commit. Follow the contributor guide's release rules when choosing the title.

## Reviewer context

Place this marker after the summary and any release metadata:

```markdown
<!-- reviewer-context -->
```

Below it, include only the additional information needed to assess the PR:

- Validation performed and results, including meaningful limitations.
- Screenshots or demos required by the contribution guidelines.
- Risks, rollout steps, or design tradeoffs that need reviewer attention.
- Links to detailed evidence or related work.

Use short bullets where helpful. Put lengthy logs and investigation notes in
linked artifacts or follow-up comments. Keep essential review information in
the description. Remove unused headings and instructional placeholders.

## Agent instructions

Before submitting or updating a PR, read the final diff and write the description
around its actual scope. Follow this guide and the repository's contribution
requirements. Report only checks actually performed; distinguish agent review,
human review, and automated test results. Remove generated boilerplate and
repeated material.

Before merging, inspect the final title and body using the rules below. These
instructions describe message preparation; they do not grant permission to
merge or bypass required review and checks.

## Squash merges

The merger is responsible for the final commit message, whether merging through
GitHub, a CLI, or an agent.

Keep the PR's Conventional Commits title as the subject. The merger must prepare
the body; GitHub does not strip the marker or reviewer context automatically.
Humans and agents merging through the CLI must run
[`scripts/strip_pr_body.py`](../scripts/strip_pr_body.py) and pass its output
explicitly to `gh pr merge --body-file`.

The script reads UTF-8 PR text from stdin and writes the cleaned body to stdout:

1. Remove the first `<!-- reviewer-context -->` marker and everything after it.
2. Remove everything from the first opening `<details>` tag through the last
   closing `</details>` tag in the remaining text, including the tags. This
   handles Dependabot release notes and commit lists, including nested blocks.
3. Trim surrounding whitespace while preserving the remaining text, including
   issue references, release footers, and any trailing Dependabot metadata.

Keep essential summary and release metadata outside `<details>` blocks and above
the marker. Text between separate details blocks is removed too. Unmatched
opening or closing details tags cause an error with no output; inspect and fix
the source rather than merging with an accidentally empty body.

From the repository root, prepare the message (replace `123` with the PR number).
Run each command only if the preceding command succeeded:

```sh
pr=123
body_file=$(mktemp)
gh pr view "$pr" --json body --jq .body > "$body_file.source"
python3 scripts/strip_pr_body.py < "$body_file.source" > "$body_file"
cat "$body_file"
```

Inspect the cleaned file and PR title before merging. If correct and the PR is
approved and ready to merge, use that exact file:

```sh
gh pr merge "$pr" --squash --body-file "$body_file"
rm "$body_file" "$body_file.source"
```

In the GitHub UI, run the same script on the PR description and paste its output
into the squash commit body. Keep the PR title as the subject and remove any
automatically appended list of intermediate commits.

For a Dependabot PR without the marker, the script still removes the details
blocks while preserving the dependency/version summary. For a legacy PR with
neither the marker nor details blocks, it preserves the description: first edit
the PR to place reviewer-only material below the marker, then run the script.
Do not assume an unmarked legacy description is already a concise commit body.

These steps do not grant permission to merge or bypass required reviews and
checks. Marker enforcement in CI is a separate follow-up; this change does not
require existing open PRs to adopt the marker immediately.
