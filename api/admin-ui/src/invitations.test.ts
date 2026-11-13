import { describe, expect, it } from "vitest";

import type { Resource } from "./api";
import { invitationNotice, loadAllOrganizations, organizationLabel, organizationNotice } from "./invitations";

describe("invitationNotice", () => {
  it("reports a delivered invitation as sent", () => {
    expect(invitationNotice({ state: "ok", data: { outcome: "sent" } })).toEqual({
      text: "Invitation sent.",
      bad: false,
    });
  });

  it("does not call a created invitation whose email failed 'sent'", () => {
    const failed = invitationNotice({ state: "ok", data: { outcome: "send_failed" } });
    const unknown = invitationNotice({ state: "ok", data: { outcome: "send_unknown" } });

    expect(failed.bad).toBe(true);
    expect(failed.text).toMatch(/could not be sent/);
    expect(unknown.bad).toBe(true);
    expect(unknown.text).toMatch(/did not confirm delivery/);
  });

  it("uses the server's reason for a refusal", () => {
    expect(invitationNotice({ state: "refused", reason: "already_live" }).text).toMatch(/already has a live invitation/);
  });

  it("says why a single-organization hub will not issue the invitation", () => {
    const notice = invitationNotice({ state: "refused", reason: "organization_creation_refused" });

    expect(notice.bad).toBe(true);
    expect(notice.text).toMatch(/single organization/);
    expect(notice.text).not.toMatch(/did not go through/);
  });

  it("falls back when the request never got an answer", () => {
    expect(invitationNotice({ state: "error" })).toEqual({
      text: "That did not go through. Nothing was created.",
      bad: true,
    });
  });
});

describe("organizationLabel", () => {
  it("names the organization and says how many people are in it", () => {
    expect(organizationLabel({ name: "Acme Labs", members: 3 })).toBe("Acme Labs (3 people)");
    expect(organizationLabel({ name: "Acme Labs", members: 1 })).toBe("Acme Labs (1 person)");
    expect(organizationLabel({ name: null, members: 0 })).toBe("Unnamed organization (nobody yet)");
  });
});

describe("organizationNotice", () => {
  it("explains a refused name and a single-organization hub", () => {
    expect(organizationNotice({ state: "refused", reason: "invalid_name" }).bad).toBe(true);
    expect(organizationNotice({ state: "refused", reason: "invalid_name" }).text).toMatch(/not a name the hub will store/);
    expect(organizationNotice({ state: "refused", reason: "organization_creation_refused" }).text).toMatch(
      /single organization/,
    );
    expect(organizationNotice({ state: "error" })).toEqual({ text: "The organization was not created.", bad: true });
  });
});

describe("loadAllOrganizations", () => {
  type Page = { organizations: { id: string }[]; next_offset: number | null };

  it("follows every page, so an organization past the first hundred can still be picked", async () => {
    const asked: string[] = [];
    const get = async (path: string): Promise<Resource<Page>> => {
      asked.push(path);
      return path.endsWith("offset=100")
        ? { state: "ok", data: { organizations: [{ id: "org-101" }], next_offset: null } }
        : { state: "ok", data: { organizations: [{ id: "org-1" }], next_offset: 100 } };
    };

    const result = await loadAllOrganizations(get);

    expect(asked).toEqual(["api/organizations", "api/organizations?offset=100"]);
    expect(result).toEqual({ state: "ok", data: { organizations: [{ id: "org-1" }, { id: "org-101" }] } });
  });

  it("reports a later page's failure, never the first page as the whole list", async () => {
    const get = async (path: string): Promise<Resource<Page>> =>
      path === "api/organizations"
        ? { state: "ok", data: { organizations: [{ id: "org-1" }], next_offset: 100 } }
        : { state: "unavailable", reason: "invitations_unavailable" };

    expect(await loadAllOrganizations(get)).toEqual({ state: "unavailable", reason: "invitations_unavailable" });
  });
});
