import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { Models, Users } from "./Sections";
import { useResource } from "./useResource";

vi.mock("./useResource", () => ({ useResource: vi.fn() }));

const resource = vi.mocked(useResource);

beforeEach(() => resource.mockReset());

function renderUsers(organization: unknown, reported = true) {
  resource.mockReturnValue({
    state: "ok",
    data: {
      users: [{ id: "subject-a", username: "alice", email: "alice@example.test", role: null,
        role_source: null, ...(reported ? { organization } : {}) }],
      manageable: false, next_first: null,
    },
  });
  return renderToStaticMarkup(<Users csrfToken="test" />);
}

describe("Users organization standing", () => {
  it("separates the organization role, membership status and Hub role", () => {
    const html = renderUsers({ id: "org-a", name: "Alpha <test>", role: "owner", status: "active" });
    expect(html).toContain("Alpha &lt;test&gt;");
    expect(html).toContain("org-a");
    expect(html).toContain("Owner");
    expect(html).toContain("Active");
    expect(html).toContain("Hub role");
    expect(html).toContain("Not an administrator");
  });

  it("does not present a removed row as active membership", () => {
    const html = renderUsers({ id: "org-a", name: null, role: "member", status: "removed" });
    expect(html).toContain("Removed");
    expect(html).toContain("Unnamed organization");
    expect(html).not.toContain(">Active<");
  });

  it("distinguishes no organization from an older response without membership data", () => {
    expect(renderUsers(null)).toContain("No organization");
    const missing = renderUsers(null, false);
    expect(missing).toContain("Membership not reported");
    expect(missing).not.toContain("No organization");
  });

  it("does not turn an unavailable read into an unassigned user", () => {
    resource.mockReturnValue({ state: "unavailable", reason: "organization_memberships_unavailable" });
    const html = renderToStaticMarkup(<Users csrfToken="test" />);
    expect(html).toContain("cannot show users right now");
    expect(html).not.toContain("No organization");
  });

  it("shows an empty directory explicitly", () => {
    resource.mockReturnValue({ state: "ok", data: { users: [], manageable: false, next_first: null } });
    expect(renderToStaticMarkup(<Users csrfToken="test" />)).toContain("No users reported by the directory");
  });
});

describe("Models group restriction is not inference entitlement", () => {
  it("does not promise inference to everyone who can sign in", () => {
    resource.mockReturnValue({ state: "ok", data: { models: [
      { id: "model-a", owned_by: "provider", group_path: null },
      { id: "model-b", owned_by: "provider", group_path: "/models/b" },
    ], manageable: false, catalog_error: null } });
    const html = renderToStaticMarkup(<Models csrfToken="test" />);
    expect(html).toContain("Group restriction");
    expect(html).toContain("No model-specific group");
    expect(html).toContain("organization and billing requirements");
    expect(html).not.toContain("Everyone who can sign in");
    expect(html).toContain("Group membership unavailable");
  });

  it("makes a successful but empty serving catalogue explicit", () => {
    resource.mockReturnValue({ state: "ok", data: { models: [], manageable: false, catalog_error: null } });
    expect(renderToStaticMarkup(<Models csrfToken="test" />)).toContain("No models reported by the serving layer");
  });

  it("does not call an unavailable catalogue empty", () => {
    resource.mockReturnValue({ state: "ok", data: { models: [], manageable: false, catalog_error: "unavailable" } });
    const html = renderToStaticMarkup(<Models csrfToken="test" />);
    expect(html).toContain("list of models is unavailable");
    expect(html).not.toContain("No models reported");
  });
});
