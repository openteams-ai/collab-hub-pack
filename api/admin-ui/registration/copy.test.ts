import { describe, expect, it } from "vitest";

import { PAGE_STATES, SETTLED, pageCopy } from "./copy";
import states from "./states.json";

const text = (state: string, requireVerifiedEmail: boolean) => {
  const copy = pageCopy(state, requireVerifiedEmail);
  return [copy.heading, ...copy.paragraphs].join(" ");
};

describe("pageCopy", () => {
  it("has a heading and at least one paragraph for every state", () => {
    for (const state of PAGE_STATES) {
      const copy = pageCopy(state, true);
      expect(copy.heading, state).not.toBe("");
      expect(copy.paragraphs.length, state).toBeGreaterThan(0);
    }
  });

  it("falls back to the general failure for a word it has no page for", () => {
    expect(pageCopy("an_outcome_added_later", true)).toEqual(pageCopy("error", true));
  });

  it("does not promise a confirmation email where the deployment never sends one", () => {
    // Where a verified address is not required, `email_not_verified` is reached
    // only when the sign-in carried no address at all. Telling that person to
    // follow a confirmation link would send them looking for mail that does
    // not exist.
    expect(text("email_not_verified", true)).toMatch(/confirmation email/);
    expect(text("email_not_verified", false)).not.toMatch(/confirm/i);
    expect(pageCopy("email_not_verified", false).heading).toBe(
      "We could not read an email address for this account",
    );
  });

  it("mentions verification nowhere on a deployment that does not require it", () => {
    for (const state of PAGE_STATES) {
      expect(text(state, false), state).not.toMatch(/verif/i);
    }
  });

  it("changes only what it has to between the two kinds of deployment", () => {
    const changed = PAGE_STATES.filter((state) => text(state, true) !== text(state, false));

    expect(changed.sort()).toEqual(["email_not_verified", "signin"]);
    // The sign-in state keeps its heading and first paragraph: one copy of
    // everything that does not vary.
    expect(pageCopy("signin", false).heading).toBe(pageCopy("signin", true).heading);
    expect(pageCopy("signin", false).paragraphs[0]).toBe(pageCopy("signin", true).paragraphs[0]);
  });
});

describe("the relaxed copy table", () => {
  it("names only states and paragraphs that exist, and never restates the strict text", () => {
    const pages = states.pages as Record<string, { heading: string; paragraphs: string[] }>;
    const relaxed = states.relaxed as Record<string, { heading?: string; paragraphs?: Record<string, string> }>;

    for (const [state, override] of Object.entries(relaxed)) {
      expect(pages[state], state).toBeDefined();
      if (override.heading !== undefined) expect(override.heading).not.toBe(pages[state].heading);
      for (const [index, paragraph] of Object.entries(override.paragraphs ?? {})) {
        expect(pages[state].paragraphs[Number(index)], `${state}[${index}]`).toBeDefined();
        expect(paragraph).not.toBe(pages[state].paragraphs[Number(index)]);
      }
    }
  });
});

describe("the settled outcomes", () => {
  it("are all states the page can show", () => {
    for (const outcome of SETTLED) expect(PAGE_STATES).toContain(outcome);
  });
});
