/**
 * What the page says in each state.
 *
 * The words live in `states.json` rather than in this file because the server
 * has to agree with them: its redemption endpoint answers one outcome word, and
 * a test in the API suite reads that file to check every word it can answer has
 * a page here. A new outcome therefore fails a test instead of quietly showing
 * the general failure.
 */

import states from "./states.json";

export interface PageCopy {
  heading: string;
  paragraphs: string[];
}

interface Override {
  heading?: string;
  paragraphs?: Record<string, string>;
}

const PAGES: Record<string, PageCopy> = states.pages;

/**
 * Individual paragraphs, and the one heading, that differ where the deployment
 * does not require a verified address. Kept per paragraph so everything that
 * does not vary is written once.
 */
const RELAXED: Record<string, Override> = states.relaxed;

/** Every state the page can show. */
export const PAGE_STATES: string[] = Object.keys(PAGES);

/**
 * Outcomes after which the invitation can never work again: it was redeemed,
 * or it is dead. The tab then drops the code and remembers the result, so a
 * reload cannot send it a second time.
 *
 * The outcomes left out of this list did not consume the code and are fixable
 * by the person (sign in as the invited address, verify the mailbox, wait), so
 * the tab keeps the code and a reload retries.
 */
export const SETTLED: string[] = states.settled;

export function pageCopy(state: string, requireVerifiedEmail: boolean): PageCopy {
  const page = PAGES[state] ?? PAGES.error;
  const override = requireVerifiedEmail ? undefined : RELAXED[state];
  if (!override) return page;
  return {
    heading: override.heading ?? page.heading,
    paragraphs: page.paragraphs.map((paragraph, index) => override.paragraphs?.[index] ?? paragraph),
  };
}
