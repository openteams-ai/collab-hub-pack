import { describe, expect, it } from "vitest";

import { SESSION_URL, loadSession } from "./session";

function answering(status: number, body?: unknown) {
  const urls: string[] = [];
  const fetchImpl = (async (url: string) => {
    urls.push(url);
    return new Response(body === undefined ? null : JSON.stringify(body), {
      status,
      headers: { "content-type": "application/json" },
    });
  }) as unknown as typeof fetch;
  return { fetchImpl, urls };
}

describe("loadSession", () => {
  it("asks relative to the document, so a root-path prefix needs no configuration", async () => {
    const { fetchImpl, urls } = answering(200, { signed_in: false });

    await loadSession(fetchImpl);

    expect(urls).toEqual([SESSION_URL]);
    expect(SESSION_URL.startsWith("/")).toBe(false);
  });

  it("reports an invitee with no account as signed out, with nothing to post with", async () => {
    const { fetchImpl } = answering(200, {
      signed_in: false,
      claims_current: false,
      csrf_token: null,
      identity: null,
      require_verified_email: true,
      data_statement: "What we store.",
    });

    expect(await loadSession(fetchImpl)).toEqual({
      signedIn: false,
      claimsCurrent: false,
      csrfToken: "",
      identity: "",
      requireVerifiedEmail: true,
      dataStatement: "What we store.",
      accountUrl: "",
    });
  });

  it("carries the signed-in account and its CSRF token", async () => {
    const { fetchImpl } = answering(200, {
      signed_in: true,
      claims_current: true,
      csrf_token: "csrf-value",
      identity: "Alice Example",
      require_verified_email: false,
      data_statement: "What we store.",
      account_url: "https://billing.example.com/account",
    });

    expect(await loadSession(fetchImpl)).toMatchObject({
      signedIn: true,
      claimsCurrent: true,
      csrfToken: "csrf-value",
      identity: "Alice Example",
      requireVerifiedEmail: false,
      accountUrl: "https://billing.example.com/account",
    });
  });

  it("answers null when the hub cannot say, so the page reports a failure and not a sign-in prompt", async () => {
    const failing = (async () => {
      throw new TypeError("network");
    }) as unknown as typeof fetch;

    expect(await loadSession(answering(503, { error: "unavailable" }).fetchImpl)).toBeNull();
    expect(await loadSession(answering(200).fetchImpl)).toBeNull();
    expect(await loadSession(failing)).toBeNull();
  });
});
