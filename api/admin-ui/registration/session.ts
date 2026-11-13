/**
 * What the hub knows about this browser, asked before the page shows a state.
 *
 * The hub knows three things this code cannot work out: whether the browser
 * holds a session, whether that session's verified email address was asserted
 * recently enough to accept an invitation on, and the CSRF token an acceptance
 * must carry. The token lives inside the HttpOnly session cookie, which this
 * code cannot read by design, so the hub hands it over here.
 *
 * The URL is relative to the document at `/invite/accept`: the hub can be
 * served under a root-path prefix, and an absolute path would miss it.
 */

import type { SignIn } from "./flow";

export const SESSION_URL = "accept/session";

export interface InviteSession extends SignIn {
  /** Empty for a browser with no session: there is nothing to bind one to. */
  csrfToken: string;
  /** The signed-in account as the person would recognise it, or "". */
  identity: string;
  /** Whether this deployment accepts an invitation only for a verified address. */
  requireVerifiedEmail: boolean;
  /** The data statement, shown in full beside the accept button. */
  dataStatement: string;
  /** The deployment's external account page, or "" when it sets none. */
  accountUrl: string;
}

/**
 * The hub's answer, or `null` when it could not give one.
 *
 * `null` is kept apart from "signed out" on purpose. Treating a hub that did
 * not answer as a browser with no session would send someone who is signed in
 * to create an account they already have.
 */
export async function loadSession(fetchImpl: typeof fetch = fetch): Promise<InviteSession | null> {
  try {
    const response = await fetchImpl(SESSION_URL, {
      headers: { accept: "application/json" },
      credentials: "same-origin",
      cache: "no-store",
    });
    if (!response.ok) return null;
    const body = (await response.json()) as Record<string, unknown>;
    return {
      signedIn: body.signed_in === true,
      claimsCurrent: body.claims_current === true,
      csrfToken: typeof body.csrf_token === "string" ? body.csrf_token : "",
      identity: typeof body.identity === "string" ? body.identity : "",
      requireVerifiedEmail: body.require_verified_email !== false,
      dataStatement: typeof body.data_statement === "string" ? body.data_statement : "",
      accountUrl: typeof body.account_url === "string" ? body.account_url : "",
    };
  } catch {
    return null;
  }
}
