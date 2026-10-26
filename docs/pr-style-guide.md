# Pull request and commit message style

PR descriptions help reviewers evaluate a change. Squash commit messages help
future readers understand why it was made. Structure descriptions so the
essential explanation is easy to reuse as the commit message.

These conventions apply to human-written, agent-generated, and dependency-update
PRs. Existing contribution, validation, and code-owner requirements still apply.

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

Keep the PR's Conventional Commits title as the subject. Use the summary above
the marker as the body, preserving issue references and release metadata.
Inspect both before merging. Exclude the marker, reviewer context, template
instructions, checklists, and automatically appended lists of intermediate
commits. The marker is a convention; GitHub does not strip it automatically.

For older PRs or bot PRs without the marker, prepare a concise summary manually
rather than copying the entire description. For dependency updates, include
the dependency and version change, plus relevant compatibility information.

Merge automation should follow the same rules. A deterministic helper can be
added separately once the convention is agreed; this change does not introduce
a merge script or change merge permissions.
