import { describe, expect, it } from "vitest";

import { SIGNOUT_URL, signOut } from "./signout";

const REALM_LOGOUT =
  "https://keycloak.example/realms/nebari/protocol/openid-connect/logout?client_id=collab-web&id_token_hint=t";

function answering(response: Response, seen: { url?: string; init?: RequestInit } = {}): typeof fetch {
  return (async (url: string, init: RequestInit) => {
    seen.url = url;
    seen.init = init;
    return response;
  }) as unknown as typeof fetch;
}

describe("signOut", () => {
  it("posts to the sign-out route with the CSRF header, asking for JSON", async () => {
    const seen: { url?: string; init?: RequestInit } = {};

    await signOut("csrf-value", answering(Response.json({ location: REALM_LOGOUT }), seen));

    expect(seen.url).toBe(SIGNOUT_URL);
    expect(seen.init?.method).toBe("POST");
    const headers = seen.init?.headers as Record<string, string>;
    expect(headers["X-CSRF-Token"]).toBe("csrf-value");
    expect(headers.Accept).toBe("application/json");
  });

  // #335: clearing the hub's cookie alone left the realm session alive, so the
  // panel must send the browser on to the realm's logout, which fetch cannot
  // follow by itself.
  it("returns the realm logout URL the server answered with", async () => {
    expect(await signOut("csrf-value", answering(Response.json({ location: REALM_LOGOUT })))).toBe(REALM_LOGOUT);
  });

  it("asks for the signed-out page to send sign-in back to the panel", () => {
    // App-relative, as the sign-in redirect expects; the server checks it
    // against the same allowlist and carries it through the realm.
    expect(new URL(SIGNOUT_URL, "https://hub.example/admin/").searchParams.get("next")).toBe("/admin/");
  });

  // The signed-out page is static: it cannot tell anyone their session
  // survived. So a sign-out the server did not accept must not end there.
  it("reports a failure when the server did not confirm the sign-out", async () => {
    const failing = (async () => {
      throw new TypeError("network");
    }) as unknown as typeof fetch;

    expect(await signOut("csrf-value", failing)).toBeNull();
    expect(await signOut("csrf-value", answering(new Response(null, { status: 403 })))).toBeNull();
    expect(await signOut("csrf-value", answering(Response.json({})))).toBeNull();
    expect(await signOut("csrf-value", answering(new Response("<html>", { status: 200 })))).toBeNull();
  });

  it("never navigates anywhere but an https URL", async () => {
    expect(await signOut("csrf-value", answering(Response.json({ location: "javascript:alert(1)" })))).toBeNull();
    expect(await signOut("csrf-value", answering(Response.json({ location: "/web/signed-out" })))).toBeNull();
    expect(
      await signOut("csrf-value", answering(Response.json({ location: "http://keycloak.example/logout" }))),
    ).toBeNull();
  });

  it("accepts plain http only for a loopback realm, as the server does", async () => {
    const local = "http://localhost:8080/realms/nebari/protocol/openid-connect/logout?client_id=collab-web";
    expect(await signOut("csrf-value", answering(Response.json({ location: local })))).toBe(local);
  });
});
