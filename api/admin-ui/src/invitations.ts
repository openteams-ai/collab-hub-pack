import { type Resource, getJson } from "./api";

const ISSUE_OUTCOMES: Record<string, string> = {
  sent: "Invitation sent.",
  send_unknown:
    "The invitation was created, but the mail provider did not confirm delivery. Check before sending another.",
  send_failed:
    "The invitation was created, but the email could not be sent. They will need the link another way.",
  already_live: "That address already has a live invitation, so a second one was not created.",
  organization_creation_refused:
    "This hub is set up for a single organization, and an invitation from this screen creates a new one, so the hub refused it. Nothing was created.",
  invalid_email: "That does not look like an email address.",
  organization_required: "Choose an organization first. Nothing was created.",
  organization_not_found: "That organization no longer exists, so nothing was created.",
  invalid_role: "That role cannot be granted there, so nothing was created.",
  unavailable: "This hub cannot issue invitations right now.",
};

const CREATE_OUTCOMES: Record<string, string> = {
  invalid_name:
    "That is not a name the hub will store. Use one line with at least one letter or digit, other than the placeholder.",
  organization_creation_refused:
    "This hub is set up for a single organization, so another cannot be created.",
  unavailable: "This hub cannot create organizations right now.",
};

/**
 * What to tell an operator after issuing an invitation.
 *
 * The server names the outcome, on a success as well as a refusal: a 201 can
 * still mean the email did not go out. This only maps its word to a sentence,
 * and falls back for a failure that never reached the endpoint at all.
 */
export function invitationNotice(result: Resource<unknown>): { text: string; bad: boolean } {
  const outcome =
    result.state === "ok"
      ? (result.data as { outcome?: unknown } | null)?.outcome
      : "reason" in result
        ? result.reason
        : undefined;
  if (typeof outcome === "string" && outcome in ISSUE_OUTCOMES) {
    return { text: ISSUE_OUTCOMES[outcome], bad: outcome !== "sent" };
  }
  if (result.state === "ok") return { text: ISSUE_OUTCOMES.sent, bad: false };
  return { text: "That did not go through. Nothing was created.", bad: true };
}

function outcomeWord(result: Resource<unknown>): string | undefined {
  if (result.state === "ok") return (result.data as { outcome?: unknown } | null)?.outcome as string | undefined;
  return "reason" in result ? result.reason : undefined;
}

/** An organization as the picker names it: its name, and how many people are in it. */
export function organizationLabel(org: { name: string | null; members: number }): string {
  const name = org.name ?? "Unnamed organization";
  const people = org.members === 0 ? "nobody yet" : org.members === 1 ? "1 person" : `${org.members} people`;
  return `${name} (${people})`;
}

/** What to tell an operator after creating an organization went wrong. */
export function organizationNotice(result: Resource<unknown>): { text: string; bad: boolean } {
  const word = outcomeWord(result);
  if (typeof word === "string" && word in CREATE_OUTCOMES) return { text: CREATE_OUTCOMES[word], bad: true };
  return { text: "The organization was not created.", bad: true };
}

/**
 * Every organization on the hub, read page by page. The picker filters and
 * chooses among all of them, so one page standing in for the list would hide
 * the rest; a failed page fails the whole read.
 */
export async function loadAllOrganizations<T>(
  get: (path: string) => Promise<Resource<{ organizations: T[]; next_offset: number | null }>> = getJson,
): Promise<Resource<{ organizations: T[] }>> {
  const organizations: T[] = [];
  let offset: number | null = 0;
  while (offset !== null) {
    const page = await get(offset === 0 ? "api/organizations" : `api/organizations?offset=${offset}`);
    if (page.state !== "ok") return page;
    organizations.push(...page.data.organizations);
    offset = page.data.next_offset;
  }
  return { state: "ok", data: { organizations } };
}
