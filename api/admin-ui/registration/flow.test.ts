import { describe, expect, it } from "vitest";

import { OUTCOME_KEY, REDEEM_URL, TOKEN_KEY, type Tab, claimInvitation, redeem, startingState } from "./flow";

const TOKEN = "S3cr3tTokenValueThatMustNeverBePrinted";

/** A browser tab reduced to the three things the flow touches. */
function tab(fragment = "", stored: Record<string, string> = {}) {
  const values = new Map(Object.entries(stored));
  const state = { fragment, cleared: 0 };
  const storage = {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => void values.set(key, value),
    removeItem: (key: string) => void values.delete(key),
  } as Storage;
  const browser: Tab = {
    readFragment: () => state.fragment,
    clearFragment: () => {
      state.fragment = "";
      state.cleared += 1;
    },
    storage: () => storage,
  };
  return { browser, values, state };
}

describe("claimInvitation", () => {
  it("takes the code from the link, removes it from the address bar and keeps it for this tab", () => {
    const { browser, values, state } = tab(`#token=${TOKEN}`);

    const claim = claimInvitation(browser);

    expect(claim).toEqual({ token: TOKEN, settled: "" });
    expect(state.fragment).toBe("");
    expect(values.get(TOKEN_KEY)).toBe(TOKEN);
    expect(values.has(OUTCOME_KEY)).toBe(false);
  });

  it("finds the code again after the sign-in round trip, when the link is no longer in the address", () => {
    const { browser } = tab("", { [TOKEN_KEY]: TOKEN });

    expect(claimInvitation(browser)).toEqual({ token: TOKEN, settled: "" });
  });

  it("refuses a code that is not the shape the server issues, and still clears the address bar", () => {
    const { browser, values, state } = tab("#token=<script>alert(1)</script>");

    expect(claimInvitation(browser)).toEqual({ token: "", settled: "" });
    expect(state.cleared).toBe(1);
    expect(values.has(TOKEN_KEY)).toBe(false);
  });

  it("remembers a result that ended the invitation, so a reload cannot redeem twice", () => {
    const { browser } = tab("", { [OUTCOME_KEY]: "accepted" });

    expect(claimInvitation(browser)).toEqual({ token: "", settled: "accepted" });
  });

  it("starts over when a new invitation link is opened in a tab that remembers an old result", () => {
    const { browser, values } = tab(`#token=${TOKEN}`, { [OUTCOME_KEY]: "invitation_expired" });

    expect(claimInvitation(browser)).toEqual({ token: TOKEN, settled: "" });
    expect(values.has(OUTCOME_KEY)).toBe(false);
  });

  it("still works for this page view when the browser withholds storage", () => {
    const refusing: Tab = {
      readFragment: () => `#token=${TOKEN}`,
      clearFragment: () => {},
      storage: () => {
        throw new Error("storage is blocked");
      },
    };

    expect(claimInvitation(refusing)).toEqual({ token: TOKEN, settled: "" });
  });
});

describe("startingState", () => {
  const signedIn = { signedIn: true, claimsCurrent: true };

  it("shows a remembered result before anything else", () => {
    expect(startingState({ token: "", settled: "accepted" }, signedIn)).toBe("accepted");
  });

  it("asks for the invitation link when the tab holds no code", () => {
    expect(startingState({ token: "", settled: "" }, signedIn)).toBe("no_token");
  });

  it("sends someone with no session to create an account or sign in", () => {
    expect(startingState({ token: TOKEN, settled: "" }, { signedIn: false, claimsCurrent: false })).toBe("signin");
  });

  it("asks for a fresh sign-in when the verified address was asserted too long ago", () => {
    expect(startingState({ token: TOKEN, settled: "" }, { signedIn: true, claimsCurrent: false })).toBe(
      "reauthentication_required",
    );
  });

  it("is ready to accept once there is a code and a current sign-in", () => {
    expect(startingState({ token: TOKEN, settled: "" }, signedIn)).toBe("ready");
  });
});

describe("redeem", () => {
  /** What the page holds once it has claimed the code from the link. */
  const HELD = { token: TOKEN, settled: "" };

  /** A fetch that records what it was asked and answers as told. */
  function answering(status: number, body?: unknown, type: ResponseType = "basic") {
    const calls: { url: string; init: RequestInit }[] = [];
    const fetchImpl = (async (url: string, init: RequestInit) => {
      calls.push({ url, init });
      return { status, type, json: async () => body } as Response;
    }) as unknown as typeof fetch;
    return { fetchImpl, calls };
  }

  it("sends the code in a POST body, with the CSRF token, and nowhere else", async () => {
    const { browser } = tab("", { [TOKEN_KEY]: TOKEN });
    const { fetchImpl, calls } = answering(200, { outcome: "accepted" });

    await redeem(browser, HELD, "csrf-value", fetchImpl);

    expect(calls).toHaveLength(1);
    const [{ url, init }] = calls;
    expect(url).toBe(REDEEM_URL);
    expect(url).not.toContain(TOKEN);
    expect(init.method).toBe("POST");
    expect(init.body).toBe(JSON.stringify({ token: TOKEN }));
    expect(init.headers).toEqual({ "Content-Type": "application/json", "X-CSRF-Token": "csrf-value" });
    expect(JSON.stringify(init.headers)).not.toContain(TOKEN);
    // A session that ended is answered with a redirect to sign-in, which must
    // not be followed: following it would re-send this request elsewhere.
    expect(init.redirect).toBe("manual");
    expect(init.credentials).toBe("same-origin");
  });

  it("drops the code and remembers the result once the invitation is accepted", async () => {
    const { browser, values } = tab("", { [TOKEN_KEY]: TOKEN });

    const result = await redeem(browser, HELD, "csrf", answering(200, { outcome: "accepted" }).fetchImpl);

    expect(result).toEqual({ state: "accepted", claim: { token: "", settled: "accepted" } });
    expect(values.has(TOKEN_KEY)).toBe(false);
    expect(values.get(OUTCOME_KEY)).toBe("accepted");
  });

  it("keeps the code when the refusal is something the person can fix", async () => {
    const { browser, values } = tab("", { [TOKEN_KEY]: TOKEN });

    const result = await redeem(
      browser,
      HELD,
      "csrf",
      answering(403, { outcome: "invitation_email_mismatch" }).fetchImpl,
    );

    expect(result).toEqual({ state: "invitation_email_mismatch", claim: HELD });
    expect(values.get(TOKEN_KEY)).toBe(TOKEN);
    expect(values.has(OUTCOME_KEY)).toBe(false);
  });

  it("asks for sign-in again when the session ended before the click", async () => {
    const { browser, values } = tab("", { [TOKEN_KEY]: TOKEN });

    const result = await redeem(browser, HELD, "csrf", answering(0, undefined, "opaqueredirect").fetchImpl);

    expect(result).toEqual({ state: "signin", claim: HELD });
    expect(values.get(TOKEN_KEY)).toBe(TOKEN);
  });

  it("reports a general failure for an answer it cannot read, and keeps the code", async () => {
    const { browser, values } = tab("", { [TOKEN_KEY]: TOKEN });
    const unreadable = (async () =>
      ({
        status: 502,
        type: "basic",
        json: async () => {
          throw new SyntaxError("not json");
        },
      }) as unknown as Response) as unknown as typeof fetch;
    const unreachable = (async () => {
      throw new TypeError("network");
    }) as unknown as typeof fetch;

    const failed = { state: "error", claim: HELD };
    expect(await redeem(browser, HELD, "csrf", unreadable)).toEqual(failed);
    expect(await redeem(browser, HELD, "csrf", unreachable)).toEqual(failed);
    expect(await redeem(browser, HELD, "csrf", answering(200, { unexpected: true }).fetchImpl)).toEqual(failed);
    expect(values.get(TOKEN_KEY)).toBe(TOKEN);
  });

  it("can still accept in a browser that withholds storage, from the code it was opened with", async () => {
    // Nothing can be banked, so the code this page view claimed from the link
    // is the only copy. It has to be the one that is sent.
    const refusing: Tab = {
      readFragment: () => `#token=${TOKEN}`,
      clearFragment: () => {},
      storage: () => {
        throw new Error("storage is blocked");
      },
    };
    const { fetchImpl, calls } = answering(200, { outcome: "accepted" });

    const result = await redeem(refusing, claimInvitation(refusing), "csrf", fetchImpl);

    expect(calls[0].init.body).toBe(JSON.stringify({ token: TOKEN }));
    expect(result).toEqual({ state: "accepted", claim: { token: "", settled: "accepted" } });
  });
});
