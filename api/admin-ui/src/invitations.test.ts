import { describe, expect, it } from "vitest";

import { emptyListText, invitationNotice, invitationsPath, templateProblems } from "./invitations";

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

  it("explains that an existing member was not sent anything and can sign in", () => {
    const notice = invitationNotice({ state: "refused", reason: "already_member" });
    expect(notice.bad).toBe(true);
    expect(notice.text).toMatch(/already belongs to an organization/);
    expect(notice.text).toMatch(/sign in/);
  });

  it("says when the sign-in copy went to somebody who already registered", () => {
    const notice = invitationNotice({ state: "ok", data: { outcome: "sent", template: "existing_account" } });
    expect(notice.bad).toBe(false);
    expect(notice.text).toBe(
      "Invitation sent. They already have an account, so the email tells them to sign in rather than register.",
    );
  });

  it("says when the account check could not run", () => {
    const notice = invitationNotice({ state: "ok", data: { outcome: "sent", account_check: "unavailable" } });
    expect(notice.text).toMatch(/could not check whether they already have an account/);
  });

  it("falls back when the request never got an answer", () => {
    expect(invitationNotice({ state: "error" })).toEqual({
      text: "That did not go through. Nothing was created.",
      bad: true,
    });
  });
});

describe("invitationsPath", () => {
  it("asks for one tab, with the search and page only when there are any", () => {
    expect(invitationsPath("outstanding", "", null)).toBe("api/invitations?status=outstanding");
    expect(invitationsPath("all", "bob & co", 100)).toBe("api/invitations?status=all&q=bob+%26+co&offset=100");
  });
});

describe("emptyListText", () => {
  it("does not say nothing was ever issued when only a tab is empty", () => {
    expect(emptyListText("outstanding", "")).toMatch(/Nothing outstanding/);
    expect(emptyListText("all", "")).toMatch(/No invitations have been issued/);
    expect(emptyListText("accepted", "bob")).toMatch(/match that search/);
  });
});

describe("templateProblems", () => {
  it("lists what the server found wrong with a template", () => {
    expect(
      templateProblems({
        state: "refused",
        reason: "invalid_template",
        body: { problems: ["The message must include [DATA STATEMENT].", 3] },
      }),
    ).toEqual(["The message must include [DATA STATEMENT]."]);
  });

  it("explains a hub that cannot keep edits", () => {
    expect(templateProblems({ state: "refused", reason: "unavailable" })?.[0]).toMatch(/no database/);
  });

  it("is not about templates otherwise", () => {
    expect(templateProblems({ state: "ok", data: null })).toBeNull();
  });
});
