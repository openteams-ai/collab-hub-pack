import { describe, expect, it } from "vitest";

import {
  accessLabel,
  auditSummary,
  connectorSummary,
  invitationSummary,
  modelSummary,
  shortDate,
  shares,
  sinceFor,
  tabLabel,
  usageSummary,
  userSummary,
} from "./summaries";

describe("shares", () => {
  it("gives each organization its whole-number share of the actions", () => {
    expect(
      shares([
        { org_id: "org-a", users: 2, events: 4 },
        { org_id: "org-b", users: 1, events: 1 },
      ]),
    ).toEqual([80, 20]);
  });

  it("is zero everywhere when nothing has been recorded, never a division by zero", () => {
    expect(shares([{ org_id: "org-a", users: 1, events: 0 }])).toEqual([0]);
  });
});

describe("sinceFor", () => {
  const now = new Date("2026-09-30T12:00:00Z");

  it("asks for everything when the range is all time", () => {
    expect(sinceFor("all", now)).toBeNull();
  });

  it("counts back whole days from now", () => {
    expect(sinceFor("30d", now)).toBe("2026-08-31T12:00:00.000Z");
    expect(sinceFor("7d", now)).toBe("2026-09-23T12:00:00.000Z");
  });
});

describe("tabLabel", () => {
  it("carries the count when every row is loaded", () => {
    expect(tabLabel("Pending", 2, true)).toBe("Pending 2");
  });

  it("drops the count when there are more rows on later pages, so it never undercounts", () => {
    expect(tabLabel("Pending", 2, false)).toBe("Pending");
  });
});

describe("accessLabel", () => {
  it("names where an administrator's role came from", () => {
    expect(accessLabel("manual")).toBe("Granted here");
    expect(accessLabel("idp")).toBe("Granted in Keycloak");
  });

  it("is empty for someone who is not an administrator", () => {
    expect(accessLabel(null)).toBeNull();
  });
});

describe("modelSummary", () => {
  it("names the one group the limited models share", () => {
    expect(
      modelSummary([
        { id: "a", owned_by: "hub", group_path: "/llm" },
        { id: "b", owned_by: "hub", group_path: "/llm" },
        { id: "c", owned_by: "hub", group_path: null },
      ]),
    ).toEqual({ figure: "3 models", detail: "2 limited to the /llm group" });
  });

  it("does not name a group when the limited models are split across several", () => {
    expect(
      modelSummary([
        { id: "a", owned_by: "hub", group_path: "/llm" },
        { id: "b", owned_by: "hub", group_path: "/gpu" },
      ]).detail,
    ).toBe("2 limited to a group");
  });

  it("says when nothing is limited", () => {
    expect(modelSummary([{ id: "a", owned_by: "hub", group_path: null }])).toEqual({
      figure: "1 model",
      detail: "Everyone who can sign in can use them",
    });
  });
});

describe("connectorSummary", () => {
  const connector = (label: string, configured: boolean, enabled: boolean) => ({
    key: label.toLowerCase(),
    label,
    configured,
    credential: null,
    probeable: false,
    enabled,
  });

  it("counts the connectors that are on and names one that is not set up", () => {
    expect(
      connectorSummary([connector("Google", true, true), connector("Slack", true, true), connector("GitHub", false, false)]),
    ).toEqual({ figure: "2 of 3 on", detail: "GitHub isn't set up yet" });
  });

  it("says so when every connector is set up", () => {
    expect(connectorSummary([connector("Google", true, false)]).detail).toBe("All set up");
  });
});

describe("userSummary", () => {
  const user = (id: string, role: string | null) => ({ id, username: id, email: null, role, role_source: null });

  it("counts people and administrators", () => {
    expect(userSummary([user("a", "operator"), user("b", "operator"), user("c", null)], true)).toEqual({
      figure: "3 people",
      detail: "2 administrators",
    });
  });

  it("says 'more than' when the directory has further pages", () => {
    expect(userSummary([user("a", "operator")], false)).toEqual({
      figure: "More than 1 person",
      detail: "1 administrator on the first page",
    });
  });
});

describe("invitationSummary", () => {
  const invitation = (status: string) => ({ status });

  it("counts pending and revoked invitations", () => {
    expect(invitationSummary([invitation("pending"), invitation("pending"), invitation("revoked")], true)).toEqual({
      figure: "2 pending",
      detail: "1 revoked",
    });
  });

  it("qualifies the figures when only the first page is loaded", () => {
    expect(invitationSummary([invitation("pending")], false)).toEqual({
      figure: "1 pending on the first page",
      detail: "0 revoked on the first page",
    });
  });
});

describe("usageSummary", () => {
  it("totals the actions and the organizations they came from", () => {
    expect(usageSummary(5, 2)).toEqual({ figure: "5 actions", detail: "Across 2 organizations" });
    expect(usageSummary(1, 1)).toEqual({ figure: "1 action", detail: "Across 1 organization" });
  });
});

describe("auditSummary", () => {
  it("dates the newest entry and says who did what to whom", () => {
    expect(
      auditSummary({
        at: "2026-09-17T02:18:28+00:00",
        actor: "u-1",
        actor_label: "alice@example.com",
        action: "connector.enable",
        target_label: "google",
        target_id: null,
      }),
    ).toEqual({ figure: "Last change 17 Sep, 02:18", detail: "alice@example.com connector.enable google" });
  });

  it("says when nothing has been recorded", () => {
    expect(auditSummary(undefined)).toEqual({ figure: "No changes yet", detail: "Nothing has been recorded" });
  });
});

describe("shortDate", () => {
  it("writes a timestamp as day, month and year in UTC", () => {
    expect(shortDate("2026-09-19T23:30:00+00:00")).toBe("19 Sep 2026");
  });

  it("is a dash when there is no date", () => {
    expect(shortDate(null)).toBe("\u2014");
  });
});
