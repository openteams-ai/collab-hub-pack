import { describe, expect, it } from "vitest";

import { getJson, postJson } from "./api";

function answering(status: number, body?: unknown): typeof fetch {
  return (async () =>
    new Response(body === undefined ? null : JSON.stringify(body), {
      status,
      headers: { "content-type": "application/json" },
    })) as unknown as typeof fetch;
}

describe("getJson", () => {
  it("returns the parsed body on success", async () => {
    const result = await getJson<{ users_total: number }>("api/usage", answering(200, { users_total: 3 }));

    expect(result).toEqual({ state: "ok", data: { users_total: 3 } });
  });

  it("separates a deployment that cannot answer from one that refuses", async () => {
    expect(await getJson("api/audit", answering(503, { error: "audit_log_unavailable" }))).toEqual({
      state: "unavailable",
      reason: "audit_log_unavailable",
    });
    expect(await getJson("api/audit", answering(403, { error: "forbidden" }))).toEqual({
      state: "forbidden",
    });
  });

  it("surfaces an upstream failure as its own state, not as an empty result", async () => {
    expect(await getJson("api/model-access", answering(502, { error: "group_unavailable" }))).toEqual({
      state: "upstream-error",
      reason: "group_unavailable",
    });
  });

  it("treats a network failure as an error", async () => {
    const failing = (async () => {
      throw new TypeError("network");
    }) as unknown as typeof fetch;

    expect(await getJson("api/usage", failing)).toEqual({ state: "error" });
  });
});

describe("refusals carry the server's own word", () => {
  it("keeps the outcome from a declined request", async () => {
    expect(await getJson("api/invitations", answering(409, { outcome: "already_live" }))).toEqual({
      state: "refused",
      reason: "already_live",
    });
    expect(await getJson("api/invitations", answering(400, { outcome: "invalid_email" }))).toEqual({
      state: "refused",
      reason: "invalid_email",
    });
  });

  it("prefers outcome over error when both could apply", async () => {
    expect(await getJson("x", answering(503, { outcome: "unavailable", error: "other" }))).toEqual({
      state: "unavailable",
      reason: "unavailable",
    });
  });
});

describe("postJson", () => {
  it("returns the body of a success, which can itself say something went wrong", async () => {
    // A 201 for an invitation that was created but whose email failed: the
    // request succeeded, and the body is the only place that says the rest.
    const result = await postJson("api/invitations", {}, "csrf", answering(201, { outcome: "send_failed" }));

    expect(result).toEqual({ state: "ok", data: { outcome: "send_failed" } });
  });

  it("treats a success with no body as ok with nothing to read", async () => {
    expect(await postJson("x", {}, "csrf", answering(204))).toEqual({ state: "ok", data: null });
  });

  it("reads the reason out of the hub's standard error body", async () => {
    // What a single-organization hub answers when asked to issue an invitation
    // that would create a second organization.
    const refusal = answering(409, {
      error: { code: "organization_creation_refused", message: "This deployment declares a single organization." },
    });

    expect(await postJson("api/invitations", {}, "csrf", refusal)).toEqual({
      state: "refused",
      reason: "organization_creation_refused",
    });
  });
});

describe("an expired session", () => {
  // The panel stays open after the session cookie runs out. Every later call
  // answers 401, and that has to read as "sign in again", not as a failure.
  it("reads as signed out on a GET", async () => {
    expect(await getJson("api/users", answering(401, { error: "unauthenticated" }))).toEqual({
      state: "signed-out",
    });
  });

  it("reads as signed out on a POST", async () => {
    expect(await postJson("api/users/u-2/role", {}, "csrf", answering(401))).toEqual({ state: "signed-out" });
  });
});
