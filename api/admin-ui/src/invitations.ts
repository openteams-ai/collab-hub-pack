import type { Resource } from "./api";

const ISSUE_OUTCOMES: Record<string, string> = {
  sent: "Invitation sent.",
  send_unknown:
    "The invitation was created, but the mail provider did not confirm delivery. Check before sending another.",
  send_failed:
    "The invitation was created, but the email could not be sent. They will need the link another way.",
  already_live: "That address already has a live invitation, so a second one was not created.",
  already_member:
    "That address already belongs to an organization here, so nothing was sent. They can sign in as they are; an invitation could never be accepted by that account.",
  invalid_email: "That does not look like an email address.",
  unavailable: "This hub cannot issue invitations right now.",
};

/** Said after a send to somebody who registered and never finished. */
const SIGN_IN_COPY =
  "They already have an account, so the email tells them to sign in rather than register.";

/**
 * Said after a send that went out without the account check, because the hub
 * could not reach its directory. Without it, no remark would read as "they
 * have no account" when the truth is "nobody could ask".
 */
const ACCOUNT_UNCHECKED = "This hub could not check whether they already have an account.";

/**
 * What to tell an operator after issuing an invitation.
 *
 * The server names the outcome, on a success as well as a refusal: a 201 can
 * still mean the email did not go out. This only maps its word to a sentence,
 * and falls back for a failure that never reached the endpoint at all.
 */
export function invitationNotice(result: Resource<unknown>): { text: string; bad: boolean } {
  const data =
    result.state === "ok"
      ? (result.data as { outcome?: unknown; template?: unknown; account_check?: unknown } | null)
      : null;
  const outcome = result.state === "ok" ? data?.outcome : "reason" in result ? result.reason : undefined;
  const remarks = [
    data?.template === "existing_account" ? SIGN_IN_COPY : "",
    data?.account_check === "unavailable" ? ACCOUNT_UNCHECKED : "",
  ]
    .filter(Boolean)
    .map((remark) => ` ${remark}`)
    .join("");
  if (typeof outcome === "string" && outcome in ISSUE_OUTCOMES) {
    return { text: ISSUE_OUTCOMES[outcome] + remarks, bad: outcome !== "sent" };
  }
  if (result.state === "ok") return { text: ISSUE_OUTCOMES.sent + remarks, bad: false };
  return { text: "That did not go through. Nothing was created.", bad: true };
}

/** The views of the invitation list, in tab order. Outstanding first: it is what an operator acts on. */
export const INVITATION_TABS = [
  { status: "outstanding", label: "Outstanding" },
  { status: "accepted", label: "Accepted" },
  { status: "expired", label: "Expired" },
  { status: "revoked", label: "Revoked" },
  { status: "all", label: "All" },
] as const;

export type InvitationTab = (typeof INVITATION_TABS)[number]["status"];

/** The listing request for one tab, search and page. */
export function invitationsPath(status: InvitationTab, query: string, offset: number | null): string {
  const params = new URLSearchParams({ status });
  if (query) params.set("q", query);
  if (offset !== null) params.set("offset", String(offset));
  return `api/invitations?${params.toString()}`;
}

/** What to say when a tab has nothing in it, which depends on the tab. */
export function emptyListText(status: InvitationTab, query: string): string {
  if (query) return "No invitations match that search here.";
  switch (status) {
    case "outstanding":
      return "Nothing outstanding. Every invitation sent has been accepted, has expired or was revoked.";
    case "accepted":
      return "No invitation has been accepted yet.";
    case "expired":
      return "No invitation has expired.";
    case "revoked":
      return "No invitation has been revoked.";
    default:
      return "No invitations have been issued on this deployment yet.";
  }
}

/** What a template save or preview was refused for, as lines an operator can fix. */
export function templateProblems(result: Resource<unknown>): string[] | null {
  if (result.state !== "refused") return null;
  if (result.reason === "invalid_template") {
    const problems = result.body?.problems;
    return Array.isArray(problems) ? problems.filter((p): p is string => typeof p === "string") : [];
  }
  if (result.reason === "unavailable") {
    return ["This hub has no database to keep edited templates in, so it sends the built-in text."];
  }
  return null;
}
