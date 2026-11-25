# Admin organization and onboarding checks

Account registration, email delivery, invitation acceptance, organization membership and inference entitlement are separate outcomes. An existing account alone proves none of the later steps. Track the overall acceptance in [#363](https://github.com/openteams-ai/collab-hub-pack/issues/363).

## Users and Models

The operator-only `GET /admin/api/users` joins the current directory page to organization standing by subject id, not email. Each user has an `organization` object with `id`, nullable `name`, `role` and `status`, or `null` when no membership row exists. A removed row remains visible but grants no active membership. A failed organization read returns an error, never a successful list of unassigned people. An older response with no `organization` field is shown as "Membership not reported."

Users separates organization role and membership from the Hub administrator role and its grant source. This is read-only organization visibility, not an organization membership editor. Directory search/pagination retains its existing scope; this is not an organization directory or an organization-name search.

Models reports the serving catalogue and its model-specific identity-provider group restriction. "No model-specific group" does not mean everyone can infer: organization, billing and gateway requirements still apply. An empty serving catalogue, an unavailable catalogue and unavailable group management are different states. This view does not compute a person's effective billing entitlement. [#17](https://github.com/openteams-ai/collab-hub-pack/issues/17) owns wider model/access acceptance.

## Repeatable acceptance

Run on Dev first, then separately on Public after the deployed revision is confirmed. Use approved synthetic identities and organization fixtures, not existing staff or customer memberships. Record the URL, deployed image/revision, browser, time, expected result, actual result and sanitized evidence. A source merge or local screenshot does not prove deployed behavior.

| Check | Expected result |
| --- | --- |
| Operator opens Users | Each visible person has organization name/id, organization role and membership standing; Hub role is separate. |
| Active owner and member | Correct subject, organization and role; both marked Active. |
| Removed member | Bound organization remains visible, marked Removed; no implication that access was restored. |
| Registered account with no membership | No organization; no automatic admission or default shared organization. |
| Duplicate directory email on different subjects | Memberships stay attached to their subjects, not the shared label. |
| Next page | Only that page's subjects are enriched; no missing rows or changes to the cursor. |
| No directory users | Explicit empty state, not an unreadable blank table. |
| Organization backend unavailable | Error rather than "No organization"; no writes. Use an isolated test deployment, not a production outage. |
| Non-operator or ended session | Access refused by the existing browser/session and operator guards. |
| Models with/without groups | Restriction shown without promising billing or organization entitlement. |
| Empty/unavailable model catalogue | Empty result differs from failure; neither silently promises inference. |
| Group service unavailable | Group path remains visible, but membership controls are unavailable. |
| Desktop/mobile and keyboard | Readable table, bounded horizontal scroll, reachable navigation and controls. |

## Invitation-to-membership verification

These cases require the invitation implementation to be deployed. [#355](https://github.com/openteams-ai/collab-hub-pack/pull/355) owns existing-account classification and email templates, replacing the warning approach in [#350](https://github.com/openteams-ai/collab-hub-pack/pull/350). [#176](https://github.com/openteams-ai/collab-hub-pack/pull/176) owns organization-targeted admin invitations. Its required organization selection must be reconciled with the optional organization choice agreed in the November 25 discussion. This read-only change does not implement that selection or change self-registration policy.

1. Confirm the intended subject, optional target organization and intended role before issuing an approved fixture invitation. Record which deployed invitation implementation is under test.
2. Test a genuinely new account and a registered account with no organization separately. Check the registration versus sign-in email, receipt, acceptance and final Users standing; email delivery alone is not acceptance.
3. For a targeted invitation, verify the intended subject joined the selected organization with the selected role. For an untargeted invitation, verify the approved standalone-organization behavior, never a shared default organization.
4. Verify an active existing member is refused according to the current invitation policy. Keep removed membership distinct: it must not be silently restored or reassigned.
5. Exercise duplicate/live invitation and directory-unavailable outcomes. Report an unperformed account check as unknown, not "new account" certainty. Preserve the existing invitation and avoid duplicate mail unless the fixture explicitly calls for it.
6. Reopen Users after acceptance and verify the authoritative subject/org/role/status. Check an approved ordinary member's organization access independently of operator visibility. Inference requires its own eligible account and separate gateway result.

Stop on the first unexpected membership, role, cross-organization visibility or duplicate delivery outcome. Preserve evidence and report it on the owning issue. Do not repair a result by changing existing memberships, resetting accounts, granting credits or making a purchase. Mark checks without an approved fixture or deployed implementation NOT RUN.
