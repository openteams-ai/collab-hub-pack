import { Activity, Building2, Search, UserRound, Users as UsersIcon, X } from "lucide-react";
import { useState } from "react";

import { ConnectorIcon, ModelIcon } from "./BrandIcon";
import { postJson } from "./api";
import { groupMembersPath, loadGroupSizes } from "./groups";
import type { Resource } from "./api";
import { invitationNotice, loadAllOrganizations, organizationLabel, organizationNotice } from "./invitations";
import { Pager, useDebounced, usePages } from "./paging";
import { type UsageRange, accessLabel, shares, shortDate, sinceFor, tabLabel, usageRows } from "./summaries";
import type { AuditRow, ConnectorRow, HubUsage, InvitationRow, ModelRow, UserRow } from "./types";
import { Failure, Loading, PageHeader, Person, Tabs } from "./ui";
import { useLoaded, useResource } from "./useResource";

export function Models({ csrfToken }: { csrfToken: string }) {
  const [reload, setReload] = useState(0);
  const resource = useResource<{ models: ModelRow[]; catalog_error: string | null; manageable: boolean }>(
    "api/models",
    reload,
  );
  const [openGroup, setOpenGroup] = useState<string | null>(null);
  const gated = resource?.state === "ok" ? resource.data.models.flatMap((model) => model.group_path ?? []) : [];
  const sizes = useLoaded<Map<string, number>>(
    gated.length ? () => loadGroupSizes(gated).then((data) => ({ state: "ok" as const, data })) : null,
    gated.join("\n"),
  );
  const groupSizes = sizes?.state === "ok" ? sizes.data : new Map<string, number>();

  const header = (
    <PageHeader title="Models and access">
      The models this hub serves, read live from the serving layer. Some are limited to a group in your identity
      provider. Select a group to see who is in it, and to add or remove people.
    </PageHeader>
  );

  if (resource === null) return <>{header}<Loading subject="models" /></>;
  if (resource.state !== "ok") return <>{header}<Failure resource={resource} subject="models" /></>;

  const { models, catalog_error: catalogError, manageable } = resource.data;

  if (catalogError === "not_configured") {
    return (
      <>
        {header}
        <p className="empty">This hub has no model catalogue endpoint configured, so there is nothing to show here yet.</p>
      </>
    );
  }
  if (catalogError) {
    return (
      <>
        {header}
        <p className="bad">
          The serving layer did not answer, so the list of models is unavailable right now. Nothing is wrong with the
          models themselves, and nothing here has changed them.{" "}
          <button type="button" className="link" onClick={() => setReload((n) => n + 1)}>
            Try again
          </button>
        </p>
      </>
    );
  }

  return (
    <>
      {header}
      <div className="table-card">
        <table>
          <thead>
            <tr>
              <th>Model</th>
              <th>Served by</th>
              <th>Who can use it</th>
            </tr>
          </thead>
          <tbody>
            {models.map((model) => (
              <tr key={model.id}>
                <td>
                  <span className="named">
                    <span className="mark">
                      <ModelIcon model={model.id} />
                    </span>
                    <span className="mono">{model.id}</span>
                  </span>
                </td>
                <td>{model.owned_by ?? "—"}</td>
                <td>
                  {model.group_path ? (
                    <GroupPill
                      groupPath={model.group_path}
                      size={groupSizes.get(model.group_path)}
                      onOpen={() => setOpenGroup(model.group_path)}
                    />
                  ) : (
                    <span className="pill">Everyone who can sign in</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {openGroup && (
        <GroupDrawer
          groupPath={openGroup}
          gates={models.filter((model) => model.group_path === openGroup).map((model) => model.id)}
          csrfToken={csrfToken}
          manageable={manageable}
          onClose={() => setOpenGroup(null)}
        />
      )}
    </>
  );
}

interface Member {
  id: string;
  username: string | null;
  email: string | null;
}

function people(count: number): string {
  return `${count} ${count === 1 ? "person" : "people"}`;
}

/** The group that gates a model, with its size once the directory has answered. */
function GroupPill({ groupPath, size, onOpen }: { groupPath: string; size?: number; onOpen: () => void }) {
  return (
    <button type="button" className="pill brand" onClick={onOpen}>
      {groupPath} group{size === undefined ? "" : ` · ${people(size)}`}
    </button>
  );
}

function GroupDrawer({
  groupPath,
  gates,
  csrfToken,
  manageable,
  onClose,
}: {
  groupPath: string;
  gates: string[];
  csrfToken: string;
  manageable: boolean;
  onClose: () => void;
}) {
  const [reload, setReload] = useState(0);
  const resource = useResource<{ group_path: string; members: Member[] }>(
    groupMembersPath(groupPath),
    reload,
  );
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [chosen, setChosen] = useState<UserRow[]>([]);
  const searched = useDebounced(query.trim(), 300);
  const candidates = useResource<{ users: UserRow[] }>(
    searched.length >= 2 ? `api/users?query=${encodeURIComponent(searched)}` : "",
  );

  const members = resource?.state === "ok" ? resource.data.members : [];

  async function change(action: "grant" | "revoke", person: { id: string; email: string | null }) {
    const result = await postJson(
      "api/model-access",
      { user_id: person.id, group_path: groupPath, action, user_label: person.email },
      csrfToken,
    );
    return result.state === "ok";
  }

  async function revoke(member: Member) {
    setBusy(true);
    setProblem(null);
    if (!(await change("revoke", member))) setProblem("That change did not go through. Nothing was altered.");
    setBusy(false);
    setReload((n) => n + 1);
  }

  // One request per person, in order, stopping at the first refusal so the
  // message can say exactly who was and was not added.
  async function addChosen() {
    setBusy(true);
    setProblem(null);
    const added: string[] = [];
    for (const person of chosen) {
      if (!(await change("grant", person))) {
        const done = added.length ? ` ${added.join(", ")} ${added.length === 1 ? "was" : "were"} added.` : "";
        setProblem(`Adding ${person.email ?? person.username} did not go through.${done}`);
        break;
      }
      added.push(person.email ?? person.username);
    }
    setChosen((current) => current.filter((person) => !added.includes(person.email ?? person.username)));
    setQuery("");
    setBusy(false);
    setReload((n) => n + 1);
  }

  const options =
    candidates?.state === "ok"
      ? candidates.data.users.filter(
          (person) =>
            !members.some((member) => member.id === person.id) && !chosen.some((picked) => picked.id === person.id),
        )
      : [];

  return (
    <>
      <div className="scrim" onClick={onClose} />
      <aside className="drawer" aria-label={`The ${groupPath} group`}>
        <div className="drawer-head">
          <h3>
            <span className="mono">{groupPath}</span> group
          </h3>
          {resource?.state === "ok" && <span className="status">{people(members.length)}</span>}
          <button type="button" className="icon-button" onClick={onClose} title="Close" aria-label="Close">
            <X size={16} strokeWidth={1.75} aria-hidden="true" />
          </button>
        </div>
        <p>
          A group in your identity provider (Keycloak). Anyone in it may use the models it gates. Adding or removing
          somebody here changes that group directly, and you can only add people who already have a hub account.
        </p>

        <div className="drawer-section">
          <span className="drawer-label">Gates</span>
          <div className="chips">
            {gates.map((model) => (
              <span key={model} className="pill">
                {model}
              </span>
            ))}
          </div>
        </div>

        {manageable && (
          <div className="drawer-section">
            <label className="drawer-label" htmlFor="grant-search">
              Add people
            </label>
            <div className="combo">
              {chosen.length === 0 && <UserRound size={16} strokeWidth={1.75} aria-hidden="true" />}
              {chosen.map((person) => (
                <span key={person.id} className="chosen">
                  {person.email ?? person.username}
                  <button
                    type="button"
                    aria-label={`Do not add ${person.email ?? person.username}`}
                    onClick={() => setChosen((current) => current.filter((picked) => picked.id !== person.id))}
                  >
                    <X size={12} strokeWidth={2} aria-hidden="true" />
                  </button>
                </span>
              ))}
              <input
                id="grant-search"
                type="search"
                autoComplete="off"
                placeholder={chosen.length ? "" : "Add people by name or address"}
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                disabled={busy}
              />
            </div>
            {query.trim().length >= 2 && options.length > 0 && (
              <ul className="options">
                {options.map((person) => (
                  <li key={person.id}>
                    <button
                      type="button"
                      onClick={() => {
                        setChosen((current) => [...current, person]);
                        setQuery("");
                      }}
                    >
                      <Person email={person.email ?? person.username} />
                    </button>
                  </li>
                ))}
              </ul>
            )}
            {candidates?.state === "unavailable" && (
              <p className="empty">This hub has no user directory configured, so there is nobody to search.</p>
            )}
            {chosen.length > 0 && (
              <div className="drawer-actions">
                <button type="button" className="primary" disabled={busy} onClick={() => void addChosen()}>
                  Add {people(chosen.length)}
                </button>
              </div>
            )}
          </div>
        )}

        {problem && <p className="bad">{problem}</p>}

        <div className="drawer-section">
          <span className="drawer-label">Members</span>
          {resource === null && <Loading subject={`members of ${groupPath}`} />}
          {resource !== null && resource.state !== "ok" && (
            <Failure resource={resource} subject={`members of ${groupPath}`} />
          )}
          {resource?.state === "ok" && members.length === 0 && <p className="empty">Nobody is in this group yet.</p>}
          {members.length > 0 && (
            <ul className="member-list">
              {members.map((member) => (
                <li key={member.id}>
                  <Person email={member.email ?? member.username ?? member.id} />
                  <span />
                  {manageable && (
                    <button type="button" className="danger" disabled={busy} onClick={() => void revoke(member)}>
                      Remove
                    </button>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>

        <p className="banner">
          The model serving gateway enforces this group, so a change takes effect wherever people make requests.
        </p>
      </aside>
    </>
  );
}

// Revokes the server refuses because nobody could administer the hub after.
const ROLE_REFUSALS: Record<string, string> = {
  self_revoke: "You cannot remove your own administrator role. Ask another administrator.",
  last_operator: "This is the last administrator. Grant the role to someone else first.",
  not_operator: "That person no longer holds the administrator role, so there was nothing to remove.",
};

type RoleFilter = "all" | "admins" | "members";

export function Users({ csrfToken }: { csrfToken: string }) {
  const [reload, setReload] = useState(0);
  const pages = usePages();
  const [query, setQuery] = useState("");
  const searched = useDebounced(query.trim(), 300);
  const [filter, setFilter] = useState<RoleFilter>("all");
  const params = [
    pages.cursor === null ? "" : `first=${pages.cursor}`,
    searched ? `query=${encodeURIComponent(searched)}` : "",
  ]
    .filter(Boolean)
    .join("&");
  const resource = useResource<{ users: UserRow[]; manageable: boolean; next_first: number | null }>(
    params ? `api/users?${params}` : "api/users",
    reload,
  );
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  async function changeRole(user: UserRow, action: "grant" | "revoke") {
    setBusy(true);
    setProblem(null);
    const result = await postJson(
      `api/users/${encodeURIComponent(user.id)}/role`,
      { action, user_label: user.email },
      csrfToken,
    );
    setBusy(false);
    if (result.state === "refused" && result.reason in ROLE_REFUSALS) {
      setProblem(ROLE_REFUSALS[result.reason]);
    } else if (result.state !== "ok") {
      setProblem("That change did not go through. Nothing was altered.");
    }
    setReload((n) => n + 1);
  }

  const users = resource?.state === "ok" ? resource.data.users : [];
  const complete = resource?.state === "ok" && resource.data.next_first === null && !pages.hasNewer;
  const admins = users.filter((user) => user.role !== null);
  const shown = filter === "admins" ? admins : filter === "members" ? users.filter((user) => user.role === null) : users;

  return (
    <>
      <PageHeader title="Users">
        Everyone this hub knows about, and whether they hold the administrator role. Roles normally come from your
        identity provider group; one granted here stays until someone removes it. Removing a role that came from your
        identity provider takes effect immediately. That person holds it again at their next sign-in while the group
        still lists them.
      </PageHeader>
      <div className="toolbar">
        <label className="search">
          <Search size={16} strokeWidth={1.75} aria-hidden="true" />
          <span className="visually-hidden">Search people</span>
          <input
            className="field"
            type="search"
            autoComplete="off"
            placeholder="Search people"
            value={query}
            onChange={(event) => {
              setQuery(event.target.value);
              pages.reset();
            }}
          />
        </label>
        <Tabs
          label="Show"
          value={filter}
          onChange={setFilter}
          options={[
            { value: "all", label: tabLabel("All", users.length, complete) },
            { value: "admins", label: tabLabel("Administrators", admins.length, complete) },
            { value: "members", label: tabLabel("Members", users.length - admins.length, complete) },
          ]}
        />
      </div>
      {problem && <p className="bad">{problem}</p>}
      {resource === null && <Loading subject="users" />}
      {resource !== null && resource.state !== "ok" && <Failure resource={resource} subject="users" />}
      {resource?.state === "ok" && (
        <div className="table-card">
          <table>
            <thead>
              <tr>
                <th>Person</th>
                <th>Role</th>
                <th>Admin access</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((user) => (
                <tr key={user.id}>
                  <td>
                    <Person email={user.email ?? user.id} sub={user.username} />
                  </td>
                  <td>
                    {resource.data.manageable ? (
                      <label className="rolepick">
                        <span className="visually-hidden">Role for {user.email ?? user.id}</span>
                        <select
                          value={user.role ? "operator" : "member"}
                          disabled={busy}
                          onChange={(event) => changeRole(user, event.target.value === "operator" ? "grant" : "revoke")}
                        >
                          <option value="member">Member</option>
                          <option value="operator">Administrator</option>
                        </select>
                      </label>
                    ) : (
                      <span>{user.role ? "Administrator" : "Member"}</span>
                    )}
                  </td>
                  <td>
                    {accessLabel(user.role === null ? null : user.role_source) ? (
                      <span className="status">{accessLabel(user.role_source)}</span>
                    ) : (
                      <span className="empty">—</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {shown.length === 0 && <p className="empty row">Nobody matches.</p>}
        </div>
      )}
      {resource?.state === "ok" && <Pager pages={pages} next={resource.data.next_first} />}
    </>
  );
}

function connectorLine(connector: ConnectorRow): string {
  if (!connector.configured) return "Not set up in this deployment's configuration";
  if (connector.credential === "broker") return "Signs in as each person, individually";
  if (connector.credential === "static") return "Signs in as the hub itself";
  return "Set up in this deployment's configuration";
}

export function Connectors({ csrfToken }: { csrfToken: string }) {
  const [reload, setReload] = useState(0);
  const resource = useResource<{ connectors: ConnectorRow[]; switchable: boolean }>("api/connectors", reload);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  async function setEnabled(connector: ConnectorRow, enabled: boolean) {
    setBusy(true);
    setProblem(null);
    const result = await postJson(`api/connectors/${encodeURIComponent(connector.key)}`, { enabled }, csrfToken);
    setBusy(false);
    if (result.state !== "ok") setProblem("That change did not go through. Nothing was altered.");
    setReload((n) => n + 1);
  }

  return (
    <>
      <PageHeader title="Connectors">
        Which outside services this hub can talk to. Most sign in as each person individually, so whether one works is
        a question per person.
      </PageHeader>
      <p className="banner">Turning a connector off takes it away from everyone immediately.</p>
      {problem && <p className="bad">{problem}</p>}
      {resource === null && <Loading subject="connectors" />}
      {resource !== null && resource.state !== "ok" && <Failure resource={resource} subject="connectors" />}
      {resource?.state === "ok" && (
        <div className="rows">
          {resource.data.connectors.map((connector) => (
            <div key={connector.key} className="row">
              <span className="mark large">
                <ConnectorIcon connector={connector.key} />
              </span>
              <span className="row-text">
                <strong>{connector.label}</strong>
                <span>{connectorLine(connector)}</span>
              </span>
              <span className="row-end">
                {resource.data.switchable && connector.configured && (
                  <button
                    type="button"
                    role="switch"
                    aria-checked={connector.enabled}
                    aria-label={`${connector.label} available`}
                    className="switch"
                    disabled={busy}
                    onClick={() => setEnabled(connector, !connector.enabled)}
                  />
                )}
                {!connector.configured ? (
                  <span className="status">Not set up</span>
                ) : connector.enabled ? (
                  <span className="status live">On</span>
                ) : (
                  <span className="status">Off</span>
                )}
              </span>
            </div>
          ))}
        </div>
      )}
    </>
  );
}

const RANGES: { value: UsageRange; label: string }[] = [
  { value: "all", label: "All time" },
  { value: "30d", label: "Last 30 days" },
  { value: "7d", label: "Last 7 days" },
];

export function Usage() {
  const [range, setRange] = useState<UsageRange>("all");
  // Fixed when the range is picked, so a re-render does not ask a new question.
  const [since, setSince] = useState<string | null>(null);
  const resource = useResource<HubUsage>(since ? `api/usage?since=${encodeURIComponent(since)}` : "api/usage");

  function pick(next: UsageRange) {
    setRange(next);
    setSince(sinceFor(next, new Date()));
  }

  const header = (
    <PageHeader title="Hub usage" end={<Tabs label="Period" options={RANGES} value={range} onChange={pick} />}>
      Activity across every organization on this hub.
    </PageHeader>
  );

  if (resource === null) return <>{header}<Loading subject="usage" /></>;
  if (resource.state !== "ok") return <>{header}<Failure resource={resource} subject="usage" /></>;

  const { users_total: users, events_total: events } = resource.data;
  const organizations = usageRows(resource.data.organizations, range);
  const percent = shares(organizations);

  return (
    <>
      {header}
      <div className="cards">
        <Figure Icon={UsersIcon} value={users} label="people have used this hub" />
        <Figure Icon={Activity} value={events} label="recorded actions" />
        <Figure Icon={Building2} value={organizations.length} label="organizations" />
      </div>
      <p className="empty">
        Someone is counted once they have used the hub from the desktop app or the API as a member of an organization,
        and stays counted after their account is removed. The Users screen lists the accounts the identity provider
        holds now, including people who have never used the hub, so either count can be the larger one. The period
        bounds the actions and the organizations listed; the people counts are all-time.
      </p>
      <div className="table-card">
        <table>
          <thead>
            <tr>
              <th>Organization</th>
              <th>People, all-time</th>
              <th>Actions</th>
              <th>Share of actions</th>
            </tr>
          </thead>
          <tbody>
            {organizations.map((org, index) => (
              <tr key={org.org_id}>
                <td className={org.name ? undefined : "mono"}>{org.name ?? org.org_id}</td>
                <td className="num">{org.users}</td>
                <td className="num">{org.events}</td>
                <td>
                  <span className="share">
                    <span className="share-track">
                      <span className="share-fill" style={{ width: `${percent[index]}%` }} />
                    </span>
                    <span>{percent[index]}%</span>
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {organizations.length === 0 && <p className="empty row">Nothing recorded in this period.</p>}
      </div>
    </>
  );
}

/** One total. The icon is decorative; the label beneath it is the real name. */
function Figure({ Icon, value, label }: { Icon: typeof UsersIcon; value: number; label: string }) {
  return (
    <div className="card figure-card">
      <span className="tile">
        <Icon size={16} strokeWidth={1.75} aria-hidden="true" />
      </span>
      <span className="big">{value}</span>
      <span className="card-detail">{label}</span>
    </div>
  );
}

interface OrganizationRow {
  id: string;
  name: string | null;
  members: number;
  created_at: string | null;
}

/** The picker's value while no organization has been chosen yet. */
const NOT_CHOSEN = "";
/** The picker's value that opens the create-an-organization row. */
const CREATE_ORGANIZATION = "__create__";

type StatusFilter = "all" | "pending" | "revoked";

export function Invitations({ csrfToken }: { csrfToken: string }) {
  const [reload, setReload] = useState(0);
  const [organizationsReload, setOrganizationsReload] = useState(0);
  const [shownOrg, setShownOrg] = useState("");
  const [status, setStatus] = useState<StatusFilter>("all");
  const pages = usePages();
  const query = [
    pages.cursor === null ? "" : `offset=${pages.cursor}`,
    shownOrg === "" ? "" : `org_id=${encodeURIComponent(shownOrg)}`,
  ]
    .filter(Boolean)
    .join("&");
  const resource = useResource<{ invitations: InvitationRow[]; next_offset: number | null }>(
    query ? `api/invitations?${query}` : "api/invitations",
    reload,
  );
  const organizations = useLoaded(() => loadAllOrganizations<OrganizationRow>(), String(organizationsReload));
  const [email, setEmail] = useState("");
  const [target, setTarget] = useState(NOT_CHOSEN);
  const [role, setRole] = useState<"member" | "owner">("member");
  const [newName, setNewName] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ text: string; bad: boolean } | null>(null);

  const address = email.trim();
  const known = organizations?.state === "ok" ? organizations.data.organizations : [];
  // Every invitation names an organization. With none to choose from yet the
  // picker opens on creating one, which is the only thing to do.
  const picked =
    target === NOT_CHOSEN && organizations?.state === "ok" && known.length === 0 ? CREATE_ORGANIZATION : target;
  const creating = picked === CREATE_ORGANIZATION;
  const intoExisting = picked !== NOT_CHOSEN && !creating;

  async function createOrganization() {
    const name = newName.trim();
    if (!name) return;

    setBusy(true);
    setNotice(null);
    const result = await postJson("api/organizations", { name }, csrfToken);
    setBusy(false);

    if (result.state === "ok") {
      const created = (result.data as { organization: { id: string; name: string } }).organization;
      setTarget(created.id);
      setNewName("");
      setOrganizationsReload((n) => n + 1);
      setNotice({ text: `${created.name} created. Now invite its first owner.`, bad: false });
    } else {
      setNotice(organizationNotice(result));
    }
  }

  async function send(event: React.FormEvent) {
    event.preventDefault();
    if (!address || !intoExisting) return;

    setBusy(true);
    setNotice(null);
    const result = await postJson("api/invitations", { email: address, org_id: picked, role }, csrfToken);
    setBusy(false);

    if (result.state === "ok") setEmail("");
    setNotice(invitationNotice(result));
    setReload((n) => n + 1);
  }

  async function revoke(invitation: InvitationRow) {
    setBusy(true);
    setNotice(null);
    const result = await postJson(`api/invitations/${encodeURIComponent(invitation.id)}/revoke`, {}, csrfToken);
    setBusy(false);
    if (result.state !== "ok") {
      setNotice({ text: "That invitation was not revoked.", bad: true });
    }
    setReload((n) => n + 1);
  }

  const invitations = resource?.state === "ok" ? resource.data.invitations : [];
  const complete = resource?.state === "ok" && resource.data.next_offset === null && !pages.hasNewer;
  const count = (wanted: string) => invitations.filter((invitation) => invitation.status === wanted).length;
  const shown = status === "all" ? invitations : invitations.filter((invitation) => invitation.status === status);

  return (
    <>
      <PageHeader title="Invitations">
        Invite someone into an organization: one that exists, or one you create here first. The first person you
        invite as an owner runs it from then on.
      </PageHeader>

      <form onSubmit={send}>
        <label className="form-label" htmlFor="invite-email">
          Invite someone
        </label>
        <div className="form-row">
          <span className="rolepick field-like">
            <select
              aria-label="Organization"
              value={picked}
              onChange={(event) => setTarget(event.target.value)}
              disabled={busy}
            >
              <option value={NOT_CHOSEN} disabled>
                Choose an organization
              </option>
              {known.map((org) => (
                <option key={org.id} value={org.id}>
                  {organizationLabel(org)}
                </option>
              ))}
              <option value={CREATE_ORGANIZATION}>Create an organization…</option>
            </select>
          </span>
          <input
            id="invite-email"
            className="field grow"
            type="email"
            autoComplete="off"
            placeholder="Enter email address"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            disabled={busy}
          />
          <span className="rolepick field-like">
            <select
              aria-label="Role in the organization"
              value={role}
              onChange={(event) => setRole(event.target.value as "member" | "owner")}
              disabled={busy || !intoExisting}
            >
              <option value="member">Member</option>
              <option value="owner">Owner</option>
            </select>
          </span>
          <button type="submit" className="primary" disabled={busy || !intoExisting || !address}>
            Invite
          </button>
        </div>
        {organizations?.state === "ok" || organizations === null ? null : (
          <Failure resource={organizations} subject="the organizations" />
        )}
      </form>

      {creating && (
        <div>
          <label className="form-label" htmlFor="invite-organization-name">
            New organization
          </label>
          <div className="form-row">
            <input
              id="invite-organization-name"
              className="field grow"
              type="text"
              autoComplete="off"
              placeholder="Organization name"
              value={newName}
              onChange={(event) => setNewName(event.target.value)}
              disabled={busy}
            />
            <button
              type="button"
              className="primary"
              disabled={busy || !newName.trim()}
              onClick={() => void createOrganization()}
            >
              Create organization
            </button>
          </div>
        </div>
      )}

      {notice && <p className={notice.bad ? "bad" : "good"}>{notice.text}</p>}

      <div className="toolbar">
        <h3>Issued invitations</h3>
        <div className="toolbar-end">
          {known.length > 1 && (
            <span className="rolepick">
              <select
                aria-label="Show invitations for"
                value={shownOrg}
                onChange={(event) => {
                  setShownOrg(event.target.value);
                  pages.reset();
                }}
              >
                <option value="">All organizations</option>
                {known.map((org) => (
                  <option key={org.id} value={org.id}>
                    {org.name ?? "Unnamed organization"}
                  </option>
                ))}
              </select>
            </span>
          )}
          <Tabs
            label="Show"
            value={status}
            onChange={setStatus}
            options={[
              { value: "all", label: tabLabel("All", invitations.length, complete) },
              { value: "pending", label: tabLabel("Pending", count("pending"), complete) },
              { value: "revoked", label: tabLabel("Revoked", count("revoked"), complete) },
            ]}
          />
        </div>
      </div>
      <InvitationList resource={resource} shown={shown} busy={busy} onRevoke={revoke} />
      {resource?.state === "ok" && <Pager pages={pages} next={resource.data.next_offset} />}
    </>
  );
}

const STATUS_LABELS: Record<string, string> = {
  pending: "Pending",
  accepted: "Accepted",
  revoked: "Revoked",
  expired: "Expired",
};

function InvitationList({
  resource,
  shown,
  busy,
  onRevoke,
}: {
  resource: Resource<{ invitations: InvitationRow[]; next_offset: number | null }> | null;
  shown: InvitationRow[];
  busy: boolean;
  onRevoke: (invitation: InvitationRow) => void;
}) {
  if (resource === null) return <Loading subject="invitations" />;
  if (resource.state !== "ok") return <Failure resource={resource} subject="invitations" />;
  if (resource.data.invitations.length === 0) {
    return <p className="empty">No invitations have been issued on this deployment yet.</p>;
  }

  return (
    <div className="table-card">
      <table>
        <thead>
          <tr>
            <th>Invited</th>
            <th>Organization</th>
            <th>Role</th>
            <th>State</th>
            <th>Expires</th>
            <th className="action">
              <span className="visually-hidden">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {shown.map((invitation) => (
            <tr key={invitation.id}>
              <td>
                <Person email={invitation.email} />
              </td>
              <td>
                {invitation.org_id === null
                  ? "New, created when they accept"
                  : (invitation.organization ?? "Unnamed organization")}
              </td>
              <td>{invitation.role === "owner" ? "Owner" : "Member"}</td>
              <td>
                <span className={invitation.status === "pending" ? "status live" : "status"}>
                  {STATUS_LABELS[invitation.status] ?? invitation.status}
                </span>
              </td>
              <td>{shortDate(invitation.expires_at)}</td>
              <td className="action">
                {invitation.status === "pending" && (
                  <button type="button" className="danger" disabled={busy} onClick={() => onRevoke(invitation)}>
                    <X size={14} strokeWidth={2} aria-hidden="true" />
                    Revoke
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {shown.length === 0 && <p className="empty row">None in this view.</p>}
    </div>
  );
}

export function Audit() {
  const pages = usePages();
  const resource = useResource<{ entries: AuditRow[]; next_before_id: number | null }>(
    pages.cursor === null ? "api/audit" : `api/audit?before_id=${pages.cursor}`,
  );

  const header = (
    <PageHeader title="Audit log">
      Every change an administrator has made, newest first. Entries are permanent once written.
    </PageHeader>
  );

  if (resource === null) return <>{header}<Loading subject="the audit log" /></>;
  if (resource.state !== "ok") return <>{header}<Failure resource={resource} subject="the audit log" /></>;

  const { entries, next_before_id } = resource.data;

  return (
    <>
      {header}
      {entries.length === 0 ? (
        <p className="empty">Nothing has been recorded yet.</p>
      ) : (
        <div className="table-card">
          <table>
            <thead>
              <tr>
                <th>When</th>
                <th>Who</th>
                <th>Did what</th>
                <th>To whom</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((entry) => {
                const whom = entry.target_label ?? entry.target_id;
                return (
                  <tr key={entry.id}>
                    <td className="mono">{entry.at.replace("T", " ").slice(0, 19)}</td>
                    <td>
                      <Person email={entry.actor_label ?? entry.actor} />
                    </td>
                    <td className="mono">{entry.action}</td>
                    {/* A person gets their initials; a connector or a group is named plainly. */}
                    <td>{whom === null ? "—" : whom.includes("@") ? <Person email={whom} /> : whom}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      <Pager pages={pages} next={next_before_id} />
    </>
  );
}
