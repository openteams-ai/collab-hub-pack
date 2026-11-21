import { describe, expect, it } from "vitest";

import { existingAccount, existingAccountWarning, invitationNotice } from "./invitations";

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

  it("falls back when the request never got an answer", () => {
    expect(invitationNotice({ state: "error" })).toEqual({
      text: "That did not go through. Nothing was created.",
      bad: true,
    });
  });

  it("says so when the account check could not run, without calling the send a failure", () => {
    const notice = invitationNotice({ state: "ok", data: { outcome: "sent", account_check: "unavailable" } });

    expect(notice.bad).toBe(false);
    expect(notice.text).toBe(
      "Invitation sent. This hub could not check whether they already have an account.",
    );
  });

  it("says nothing extra when the account check ran", () => {
    expect(invitationNotice({ state: "ok", data: { outcome: "sent" } }).text).toBe("Invitation sent.");
  });

  it("tells the operator a member can sign in, and that nothing was sent", () => {
    expect(invitationNotice({ state: "refused", reason: "already_member" })).toEqual({
      text: "Already a member here, so nothing was sent. They can sign in.",
      bad: true,
    });
  });
});

describe("existingAccount", () => {
  it("reads the warning and what the server found", () => {
    const found = existingAccount({
      state: "refused",
      reason: "existing_account",
      body: {
        outcome: "existing_account",
        email: "bob@example.com",
        account: true,
        member: false,
        accepted_at: "2026-08-15T14:03:00+00:00",
      },
    });

    expect(found).toEqual({ account: true, member: false, acceptedOn: "2026-08-15" });
  });

  it("keeps what the server could not know as unknown, not as no", () => {
    const found = existingAccount({
      state: "refused",
      reason: "existing_account",
      body: { account: null, member: null, accepted_at: "2026-08-15T14:03:00+00:00" },
    });

    expect(found).toEqual({ account: null, member: null, acceptedOn: "2026-08-15" });
  });

  it("is not a warning for any other answer", () => {
    expect(existingAccount({ state: "refused", reason: "already_live", body: {} })).toBeNull();
    expect(existingAccount({ state: "ok", data: { outcome: "sent" } })).toBeNull();
    expect(existingAccount({ state: "error" })).toBeNull();
  });
});

describe("existingAccountWarning", () => {
  it("names the account and when the earlier invitation was accepted", () => {
    const warning = existingAccountWarning({ account: true, member: null, acceptedOn: "2026-08-15" });

    expect(warning.found).toBe(
      "This email already has an account (invitation accepted on 2026-08-15). They can sign in instead.",
    );
    expect(warning.send).toBe("Send anyway");
  });

  it("names the account alone when no invitation was ever accepted", () => {
    expect(existingAccountWarning({ account: true, member: null, acceptedOn: null }).found).toBe(
      "This email already has an account. They can sign in instead.",
    );
  });

  it("says the invitation is how they join when the account has no organization", () => {
    // Registered, then the invitation lapsed before they accepted it: signing
    // in alone gets them nowhere, so "sign in instead" would be wrong advice.
    const warning = existingAccountWarning({ account: true, member: false, acceptedOn: null });

    expect(warning.found).toMatch(/not in any organization here yet/);
    expect(warning.consequence).toMatch(/This invitation is how they join/);
    expect(warning.found + warning.consequence).not.toMatch(/sign in instead/);
    expect(warning.send).toBe("Send this invitation");
  });

  it("does not claim what acceptance would do when the organization is unknown", () => {
    const warning = existingAccountWarning({ account: true, member: null, acceptedOn: null });

    expect(warning.consequence).toMatch(/An account that already belongs to an organization here/);
    expect(warning.send).toBe("Send anyway");
  });

  it("does not claim an account the directory could not confirm", () => {
    const { found } = existingAccountWarning({ account: null, member: null, acceptedOn: "2026-08-15" });

    expect(found).toMatch(/accepted an invitation on 2026-08-15/);
    expect(found).toMatch(/probably has an account/);
    expect(found).not.toMatch(/already has an account/);
  });

  it("does not suggest signing in when the directory says no account uses the address", () => {
    const { found } = existingAccountWarning({ account: false, member: null, acceptedOn: "2026-08-15" });

    expect(found).toBe("This email accepted an invitation on 2026-08-15, but no account uses it now.");
    expect(found).not.toMatch(/sign in/);
  });

  it("still words a warning that arrived with nothing found", () => {
    expect(existingAccountWarning({ account: null, member: null, acceptedOn: null }).found).toMatch(
      /may already have an account/,
    );
  });
});
