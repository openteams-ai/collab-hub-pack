/**
 * The invitation code, from the link to the redemption request.
 *
 * The one-time code arrives in the URL fragment (`…/invite/accept#token=…`). A
 * fragment is never put on a request line, never sent in a `Referer`, and
 * never reaches the server at all, which is why it was chosen and why this
 * page needs script: nothing else can read it.
 *
 * What this module may do with the code is short enough to state in full. It
 * is read from the fragment, the fragment is removed from the address bar in
 * the same step, the code is kept in `sessionStorage` so it survives the
 * sign-in round trip in the same tab, and it is sent to the server in a POST
 * body. It is never part of a URL this page builds, never a query parameter,
 * and never handed to anything that renders: the components receive a state
 * name and nothing else.
 */

import { SETTLED } from "./copy";

export const TOKEN_KEY = "nexus.invite.token";
export const OUTCOME_KEY = "nexus.invite.outcome";

/**
 * The redemption endpoint, relative to the document at `/invite/accept`. It is
 * relative so the app works under a root-path prefix without knowing it.
 */
export const REDEEM_URL = "accept/redeem";

/** What the server will accept as a code: its alphabet and its length bound. */
const TOKEN_SHAPE = /^[A-Za-z0-9_-]{1,512}$/;

/** The parts of a browser tab the flow touches, so the flow can be run without one. */
export interface Tab {
  /** The address's fragment including its leading `#`, or "" when there is none. */
  readFragment(): string;
  /** Remove the fragment from the address bar without navigating. */
  clearFragment(): void;
  /** This tab's session storage, or `null` where the browser withholds it. */
  storage(): Storage | null;
}

/** What this tab holds for the invitation: a code to redeem, or a result already reached. */
export interface Claim {
  token: string;
  settled: string;
}

function read(tab: Tab, key: string): string {
  try {
    return tab.storage()?.getItem(key) || "";
  } catch {
    return "";
  }
}

function write(tab: Tab, key: string, value: string): void {
  try {
    tab.storage()?.setItem(key, value);
  } catch {
    // A tab that cannot store the code still works until it navigates away.
  }
}

function drop(tab: Tab, key: string): void {
  try {
    tab.storage()?.removeItem(key);
  } catch {
    // Nothing to remove from a store that cannot be reached.
  }
}

/**
 * Read the code out of the fragment and strip the fragment, in one step, so
 * there is no path that banks the code and leaves it in the address bar.
 */
function takeFragment(tab: Tab): string {
  const fragment = tab.readFragment();
  let value = "";
  if (fragment.length > 1) {
    try {
      value = new URLSearchParams(fragment.slice(1)).get("token") || "";
    } catch {
      value = "";
    }
  }
  if (fragment) tab.clearFragment();
  return TOKEN_SHAPE.test(value) ? value : "";
}

/**
 * Bank the code this tab was opened with, and report what the tab now holds.
 *
 * A fresh link starts the flow over: its code replaces the stored one and any
 * remembered result is forgotten.
 */
export function claimInvitation(tab: Tab): Claim {
  const fresh = takeFragment(tab);
  if (fresh) {
    write(tab, TOKEN_KEY, fresh);
    drop(tab, OUTCOME_KEY);
  }
  const stored = fresh || read(tab, TOKEN_KEY);
  return { token: TOKEN_SHAPE.test(stored) ? stored : "", settled: read(tab, OUTCOME_KEY) };
}

/** What the server says about this browser's sign-in; see `session.ts`. */
export interface SignIn {
  signedIn: boolean;
  claimsCurrent: boolean;
}

/**
 * The state the page opens in. It never redeems on its own: with a code and a
 * current sign-in the answer is `ready`, which waits for a click.
 */
export function startingState(claim: Claim, signIn: SignIn): string {
  if (claim.settled) return claim.settled;
  if (!claim.token) return "no_token";
  if (!signIn.signedIn) return "signin";
  if (!signIn.claimsCurrent) return "reauthentication_required";
  return "ready";
}

/** What a redemption attempt leaves behind: the state to show, and what the page now holds. */
export interface Redemption {
  state: string;
  claim: Claim;
}

/**
 * Redeem the code this page view holds, for this browser's session.
 *
 * Called from the accept button and from nowhere else. Joining an organization
 * is permanent, so it takes a deliberate click: a page that redeemed on load
 * would let anyone able to issue an invitation bind a person's login to their
 * organization by getting them to open a link.
 *
 * The code comes from the claim the page made when it opened, never from a
 * second read of storage, so a browser that withholds storage can still accept
 * from the link it was opened with.
 *
 * The server answers one outcome word. An outcome that ends the invitation
 * drops the code and is remembered; every other answer keeps the code, so the
 * person can fix what was wrong and try again from this tab.
 */
export async function redeem(
  tab: Tab,
  claim: Claim,
  csrfToken: string,
  fetchImpl: typeof fetch = fetch,
): Promise<Redemption> {
  let response: Response;
  try {
    response = await fetchImpl(REDEEM_URL, {
      method: "POST",
      cache: "no-store",
      credentials: "same-origin",
      redirect: "manual",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
      body: JSON.stringify({ token: claim.token }),
    });
  } catch {
    return { state: "error", claim };
  }
  // The session guard answers an ended session with a redirect to sign-in.
  if (response.type === "opaqueredirect" || response.status === 0) return { state: "signin", claim };

  let outcome: unknown;
  try {
    outcome = ((await response.json()) as { outcome?: unknown } | null)?.outcome;
  } catch {
    return { state: "error", claim };
  }
  if (typeof outcome !== "string") return { state: "error", claim };

  if (!SETTLED.includes(outcome)) return { state: outcome, claim };
  drop(tab, TOKEN_KEY);
  write(tab, OUTCOME_KEY, outcome);
  return { state: outcome, claim: { token: "", settled: outcome } };
}
