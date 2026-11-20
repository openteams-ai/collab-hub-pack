import type { Resource } from "./api";

const ISSUE_OUTCOMES: Record<string, string> = {
  sent: "Invitation sent.",
  send_unknown:
    "The invitation was created, but the mail provider did not confirm delivery. Check before sending another.",
  send_failed:
    "The invitation was created, but the email could not be sent. They will need the link another way.",
  already_live: "That address already has a live invitation, so a second one was not created.",
  existing_account: "This email already has an account, so nothing was sent.",
  invalid_email: "That does not look like an email address.",
  unavailable: "This hub cannot issue invitations right now.",
};

/**
 * Added after a send that went out without the existing-account check, because
 * the hub could not reach its directory. Without it, no warning would read as
 * "they have no account" when the truth is "nobody could ask".
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
    result.state === "ok" ? (result.data as { outcome?: unknown; account_check?: unknown } | null) : null;
  const outcome = result.state === "ok" ? data?.outcome : "reason" in result ? result.reason : undefined;
  const unchecked = data?.account_check === "unavailable" ? ` ${ACCOUNT_UNCHECKED}` : "";
  if (typeof outcome === "string" && outcome in ISSUE_OUTCOMES) {
    return { text: ISSUE_OUTCOMES[outcome] + unchecked, bad: outcome !== "sent" };
  }
  if (result.state === "ok") return { text: ISSUE_OUTCOMES.sent + unchecked, bad: false };
  return { text: "That did not go through. Nothing was created.", bad: true };
}

/** What the server found when it stopped to warn about an address. */
export interface ExistingAccount {
  /** The identity provider's answer, or `null` when it could not be asked. */
  account: boolean | null;
  /**
   * Whether that account already has an organization here (active or removed),
   * or `null` when that cannot be known. Acceptance refuses an account that has
   * one, so this decides whether sending can help.
   */
  member: boolean | null;
  /** The day an earlier invitation to this address was accepted, if one was. */
  acceptedOn: string | null;
}

/**
 * The existing-account warning, if that is what this answer is.
 *
 * When it is, nothing was issued and nothing was sent. The server is waiting
 * for the operator to cancel or to send anyway.
 */
export function existingAccount(result: Resource<unknown>): ExistingAccount | null {
  if (result.state !== "refused" || result.reason !== "existing_account") return null;
  const body = result.body ?? {};
  return {
    account: typeof body.account === "boolean" ? body.account : null,
    member: typeof body.member === "boolean" ? body.member : null,
    acceptedOn: typeof body.accepted_at === "string" ? body.accepted_at.slice(0, 10) : null,
  };
}

/** What acceptance does with an account that already has an organization. */
const CANNOT_ACCEPT =
  "An account that already belongs to an organization here, or was removed from one, cannot accept another invitation.";

/**
 * The warning, worded by what was actually found: what this address already
 * has, whether sending can help, and what the confirming button should say.
 */
export function existingAccountWarning({ account, member, acceptedOn }: ExistingAccount): {
  found: string;
  consequence: string;
  send: string;
} {
  if (account && member === false) {
    // Registered but never joined -- typically an invitation that lapsed
    // before it was accepted. A new invitation is the right call; what the
    // operator needs is how to tell them to use it.
    return {
      found: "This email already has an account, but it is not in any organization here yet.",
      consequence:
        "This invitation is how they join. Tell them to sign in with that account instead of registering.",
      // Not "Send invitation": that is the form's own button, just above.
      send: "Send this invitation",
    };
  }
  const consequence =
    member === true
      ? "That account already belongs to an organization here, so it cannot accept another invitation."
      : CANNOT_ACCEPT;
  return { found: foundText(account, acceptedOn), consequence, send: "Send anyway" };
}

function foundText(account: boolean | null, acceptedOn: string | null): string {
  if (account) {
    return acceptedOn
      ? `This email already has an account (invitation accepted on ${acceptedOn}). They can sign in instead.`
      : "This email already has an account. They can sign in instead.";
  }
  // Only an earlier acceptance matched. The identity provider either said
  // there is no account now, or could not be asked.
  if (!acceptedOn) return "This email may already have an account. They can sign in instead.";
  if (account === false) {
    return `This email accepted an invitation on ${acceptedOn}, but no account uses it now.`;
  }
  return `This email accepted an invitation on ${acceptedOn}, so it probably has an account. They can sign in instead.`;
}
