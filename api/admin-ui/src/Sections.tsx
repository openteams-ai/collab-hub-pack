import { Activity, Building2, Users as UsersIcon, X } from "lucide-react";
import { useState } from "react";

import { ConnectorIcon, ModelIcon } from "./BrandIcon";
import { postJson } from "./api";
import { invitationNotice } from "./invitations";
import { Pager, useDebounced, usePages } from "./paging";
import type { Resource } from "./api";
import { useResource } from "./useResource";

/** Every failure this panel can show, in words an operator can act on. */
export function Failure({ resource, subject }: { resource: Resource<unknown>; subject: string }) {
  if (resource.state === "forbidden") {
    return <p className="bad">This account is not allowed to see {subject}.</p>;
  }
  if (resource.state === "refused") {
    return <p className="bad">That request was declined ({resource.reason}).</p>;
  }
  if (resource.state === "unavailable") {
    return (
      <p className="bad">
        This hub cannot show {subject}: it is not configured for it ({resource.reason}).
      </p>
    );
  }
  if (resource.state === "upstream-error") {
    return (
      <p className="bad">
        {subject} could not be read because another service did not answer ({resource.reason}).
      </p>
    );
  }
  return <p className="bad">Could not load {subject}.</p>;
}

function Loading({ subject }: { subject: string }) {
  return <p>Loading {subject}…</p>;
}

interface ModelRow {
  id: string;
  owned_by: string | null;
  group_path: string | null;
}

export function Models({ csrfToken }: { csrfToken: string }) {
  const [reload, setReload] = useState(0);
  const resource = useResource<{ models: ModelRow[]; catalog_error: string | null; manageable: boolean }>(
    "api/models",
    reload,
  );
  const [openGroup, setOpenGroup] = useState<string | null>(null);

  if (resource === null) return <Loading subject="models" />;
  if (resource.state !== "ok") return <Failure resource={resource} subject="models" />;

  const { models, catalog_error: catalogError, manageable } = resource.data;

  if (catalogError === "not_configured") {
    return (
      <p>
        This hub has no model catalogue endpoint configured, so there is nothing to show here yet.
      </p>
    );
  }
  if (catalogError) {
    return (
      <p className="bad">
        The serving layer did not answer, so the list of models is unavailable right now. Nothing is
        wrong with the models themselves, and nothing here has changed them.{" "}
        <button type="button" className="link" onClick={() => setReload((n) => n + 1)}>
          Try again
        </button>
      </p>
    );
  }

  return (
    <>
      <p>
        These are the models this hub currently serves, read live from the serving layer.
      </p>
      <p>
        Some models are limited to a group in your identity provider. Select a group to see who is
        in it, and to add or remove people.
      </p>
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
                  <ModelIcon model={model.id} />
                  <span className="mono">{model.id}</span>
                </span>
              </td>
              <td>{model.owned_by ?? "—"}</td>
              <td>
                {model.group_path ? (
                  <button type="button" className="link" onClick={() => setOpenGroup(model.group_path)}>
                    People in the <span className="mono">{model.group_path}</span> group
                  </button>
                ) : (
                  "Everyone who can sign in"
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {openGroup && <GroupMembers groupPath={openGroup} csrfToken={csrfToken} manageable={manageable} />}
    </>
  );
}

interface Member {
  id: string;
  username: string | null;
  email: string | null;
}

function GroupMembers({
  groupPath,
  csrfToken,
  manageable,
}: {
  groupPath: string;
  csrfToken: string;
  manageable: boolean;
}) {
  const [reload, setReload] = useState(0);
  const resource = useResource<{ group_path: string; members: Member[] }>(
    `api/model-access?group_path=${encodeURIComponent(groupPath)}`,
    reload,
  );
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const searched = useDebounced(query.trim(), 300);
  const candidates = useResource<{ users: UserRow[] }>(
    searched.length >= 2 ? `api/users?query=${encodeURIComponent(searched)}` : "",
  );

  async function change(action: "grant" | "revoke", person: { id: string; email: string | null }) {
    setBusy(true);
    setProblem(null);
    const result = await postJson(
      "api/model-access",
      { user_id: person.id, group_path: groupPath, action, user_label: person.email },
      csrfToken,
    );
    setBusy(false);
    if (result.state === "ok") {
      setQuery("");
      setReload((n) => n + 1);
    } else {
      setProblem("That change did not go through. Nothing was altered.");
    }
  }

  async function revoke(member: Member) {
    await change("revoke", member);
  }

  if (resource === null) return <Loading subject={`members of ${groupPath}`} />;
  if (resource.state !== "ok") return <Failure resource={resource} subject={`members of ${groupPath}`} />;

  return (
    <section className="panel">
      <h3>
        People in the <span className="mono">{groupPath}</span> group
      </h3>
      <p>
        <span className="mono">{groupPath}</span> is a group in your identity provider (Keycloak).
        Anyone in it may use the models it gates. Adding or removing somebody here changes that
        group directly.
      </p>
      <p>
        The model serving gateway enforces it, so a change here takes effect wherever people make
        requests.
      </p>
      {problem && <p className="bad">{problem}</p>}
      <table>
        <tbody>
          {resource.data.members.map((member) => (
            <tr key={member.id}>
              <td>{member.email ?? member.username ?? member.id}</td>
              <td>
                {manageable && (
                  <button
                    type="button"
                    className="badge-button"
                    disabled={busy}
                    onClick={() => revoke(member)}
                  >
                    <X size={13} strokeWidth={2} aria-hidden="true" />
                    Remove
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {resource.data.members.length === 0 && <p className="empty">Nobody is in this group yet.</p>}

      {manageable && (
        <div className="grant">
          <label htmlFor="grant-search">Give someone access</label>
          <input
            id="grant-search"
            type="search"
            autoComplete="off"
            placeholder="Search people by name or address"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
          {candidates?.state === "ok" && (
            <ul className="candidates">
              {candidates.data.users
                .filter((person) => !resource.data.members.some((member) => member.id === person.id))
                .map((person) => (
                  <li key={person.id}>
                    <span>{person.email ?? person.username}</span>
                    <button
                      type="button"
                      className="link"
                      disabled={busy}
                      onClick={() => change("grant", person)}
                    >
                      Add
                    </button>
                  </li>
                ))}
            </ul>
          )}
          {candidates?.state === "unavailable" && (
            <p className="empty">
              This hub has no user directory configured, so there is nobody to search.
            </p>
          )}
        </div>
      )}
    </section>
  );
}

interface UserRow {
  id: string;
  username: string;
  email: string | null;
  role: string | null;
  role_source: string | null;
}

// Revokes the server refuses because nobody could administer the hub after.
const ROLE_REFUSALS: Record<string, string> = {
  self_revoke: "You cannot remove your own administrator role. Ask another administrator.",
  last_operator: "This is the last administrator. Grant the role to someone else first.",
  not_operator: "That person no longer holds the administrator role, so there was nothing to remove.",
};

export function Users({ csrfToken }: { csrfToken: string }) {
  const [reload, setReload] = useState(0);
  const pages = usePages();
  const resource = useResource<{ users: UserRow[]; manageable: boolean; next_first: number | null }>(
    pages.cursor === null ? "api/users" : `api/users?first=${pages.cursor}`,
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

  if (resource === null) return <Loading subject="users" />;
  if (resource.state !== "ok") return <Failure resource={resource} subject="users" />;

  const { users, manageable } = resource.data;

  return (
    <>
      <p>
        Everyone this hub knows about, and whether they hold the administrator role here.
      </p>
      <p>
        Administrator access normally comes from your identity provider group, re-checked at every
        sign-in. A role granted on this screen is recorded as hand-administered, and stays until
        someone removes it here.
      </p>
      {problem && <p className="bad">{problem}</p>}
      <table>
        <thead>
          <tr>
            <th>Person</th>
            <th>Username</th>
            <th>Role</th>
            <th>Admin access</th>
          </tr>
        </thead>
        <tbody>
          {users.map((user) => (
            <tr key={user.id}>
              <td>{user.email ?? user.id}</td>
              <td className="mono">{user.username}</td>
              <td>
                {manageable ? (
                  <label className="rolepick">
                    <span className="visually-hidden">Role for {user.email ?? user.id}</span>
                    <select
                      value={user.role ? "operator" : "member"}
                      disabled={busy}
                      onChange={(event) =>
                        changeRole(user, event.target.value === "operator" ? "grant" : "revoke")
                      }
                    >
                      <option value="member">Member</option>
                      <option value="operator">Administrator</option>
                    </select>
                  </label>
                ) : (
                  <span className="empty">{user.role ? "Administrator" : "Member"}</span>
                )}
              </td>
              <td>
                <AdminAccess source={user.role_source} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="empty">
        Removing a role that came from your identity provider takes effect immediately. That person
        holds it again at their next sign-in while the group still lists them.
      </p>
      <Pager pages={pages} next={resource.data.next_first} />
    </>
  );
}

const ROLE_SOURCES: Record<string, string> = {
  idp: "Granted in Keycloak",
  manual: "Granted here",
};

function AdminAccess({ source }: { source: string | null }) {
  const label = ROLE_SOURCES[source ?? ""];
  if (label) return <span className="tag">{label}</span>;
  return (
    <>
      <span className="empty" aria-hidden="true">
        -
      </span>
      <span className="visually-hidden">No admin access</span>
    </>
  );
}

interface ConnectorRow {
  key: string;
  label: string;
  configured: boolean;
  credential: string | null;
  probeable: boolean;
  enabled: boolean;
}

export function Connectors({ csrfToken }: { csrfToken: string }) {
  const [reload, setReload] = useState(0);
  const resource = useResource<{ connectors: ConnectorRow[]; switchable: boolean }>(
    "api/connectors",
    reload,
  );
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  async function setEnabled(connector: ConnectorRow, enabled: boolean) {
    setBusy(true);
    setProblem(null);
    const result = await postJson(
      `api/connectors/${encodeURIComponent(connector.key)}`,
      { enabled },
      csrfToken,
    );
    setBusy(false);
    if (result.state !== "ok") setProblem("That change did not go through. Nothing was altered.");
    setReload((n) => n + 1);
  }

  if (resource === null) return <Loading subject="connectors" />;
  if (resource.state !== "ok") return <Failure resource={resource} subject="connectors" />;

  const { connectors, switchable } = resource.data;

  return (
    <>
      <p>
        Which outside services this hub can talk to. Most connectors sign in as each person
        individually, so whether one works is a question per person.
      </p>
      <p>
        Turning one off takes it away from everyone immediately. Credentials live in this
        deployment's configuration, so this screen switches connectors that are already set up.
      </p>
      {problem && <p className="bad">{problem}</p>}
      <table>
        <thead>
          <tr>
            <th>Connector</th>
            <th>Set up</th>
            <th>Signs in as</th>
            <th>Available</th>
          </tr>
        </thead>
        <tbody>
          {connectors.map((connector) => (
            <tr key={connector.key}>
              <td>
                <span className="named">
                  <ConnectorIcon connector={connector.key} />
                  {connector.label}
                </span>
              </td>
              <td>{connector.configured ? "Yes" : "No"}</td>
              <td>
                {connector.credential === "broker"
                  ? "Each person, individually"
                  : connector.credential === "static"
                    ? "The hub itself"
                    : "\u2014"}
              </td>
              <td>
                {switchable && connector.configured ? (
                  <button
                    type="button"
                    role="switch"
                    aria-checked={connector.enabled}
                    aria-label={`${connector.label} available`}
                    className="switch"
                    disabled={busy}
                    onClick={() => setEnabled(connector, !connector.enabled)}
                  >
                    <span className="switch-track">
                      <span className="switch-thumb" />
                    </span>
                    <span className="switch-label">{connector.enabled ? "On" : "Off"}</span>
                  </button>
                ) : (
                  <span className="empty">{connector.configured ? "On" : "Not set up"}</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

interface HubUsage {
  users_total: number;
  events_total: number;
  events: { event: string; count: number }[];
  organizations: { org_id: string; users: number; events: number }[];
}

export function Usage() {
  const resource = useResource<HubUsage>("api/usage");

  if (resource === null) return <Loading subject="usage" />;
  if (resource.state !== "ok") return <Failure resource={resource} subject="usage" />;

  const { users_total: users, events_total: events, organizations } = resource.data;

  return (
    <>
      <p>Activity across every organization on this hub.</p>
      <div className="figures">
        <Figure Icon={UsersIcon} value={users} label="people have used this hub" />
        <Figure Icon={Activity} value={events} label="recorded actions" />
        <Figure Icon={Building2} value={organizations.length} label="organizations" />
      </div>
      <table>
        <thead>
          <tr>
            <th>Organization</th>
            <th>People</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {organizations.map((org) => (
            <tr key={org.org_id}>
              <td className="mono">{org.org_id}</td>
              <td className="mono">{org.users}</td>
              <td className="mono">{org.events}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

/** One total. The icon is decorative; the label beside it is the real name. */
function Figure({
  Icon,
  value,
  label,
}: {
  Icon: typeof UsersIcon;
  value: number;
  label: string;
}) {
  return (
    <div>
      <Icon className="figure-icon" size={16} strokeWidth={1.75} aria-hidden="true" />
      <span className="figure mono">{value}</span>
      <span>{label}</span>
    </div>
  );
}

interface InvitationRow {
  id: string;
  email: string;
  status: string;
  created_at: string | null;
  expires_at: string | null;
}

export function Invitations({ csrfToken }: { csrfToken: string }) {
  const [reload, setReload] = useState(0);
  const pages = usePages();
  const resource = useResource<{ invitations: InvitationRow[]; next_offset: number | null }>(
    pages.cursor === null ? "api/invitations" : `api/invitations?offset=${pages.cursor}`,
    reload,
  );
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ text: string; bad: boolean } | null>(null);

  async function send(event: React.FormEvent) {
    event.preventDefault();
    const address = email.trim();
    if (!address) return;

    setBusy(true);
    setNotice(null);
    const result = await postJson("api/invitations", { email: address }, csrfToken);
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

  return (
    <>
      <p>
        Invite someone to this deployment. Accepting creates their organization with them as its
        owner.
      </p>

      <form className="invite" onSubmit={send}>
        <label htmlFor="invite-email">Email address</label>
        <div className="invite-row">
          <input
            id="invite-email"
            type="email"
            autoComplete="off"
            placeholder="person@example.com"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            disabled={busy}
          />
          <button type="submit" className="primary" disabled={busy || !email.trim()}>
            Send invitation
          </button>
        </div>
      </form>

      {notice && <p className={notice.bad ? "bad" : "good"}>{notice.text}</p>}

      <h3>Issued invitations</h3>
      <InvitationList resource={resource} busy={busy} onRevoke={revoke} />
      {resource?.state === "ok" && <Pager pages={pages} next={resource.data.next_offset} />}
    </>
  );
}

function InvitationList({
  resource,
  busy,
  onRevoke,
}: {
  resource: Resource<{ invitations: InvitationRow[]; next_offset: number | null }> | null;
  busy: boolean;
  onRevoke: (invitation: InvitationRow) => void;
}) {
  if (resource === null) return <Loading subject="invitations" />;
  if (resource.state !== "ok") return <Failure resource={resource} subject="invitations" />;

  const { invitations } = resource.data;
  if (invitations.length === 0) {
    return <p className="empty">No invitations have been issued on this deployment yet.</p>;
  }

  return (
    <table>
      <thead>
        <tr>
          <th>Invited</th>
          <th>State</th>
          <th>Expires</th>
          <th />
        </tr>
      </thead>
      <tbody>
        {invitations.map((invitation) => (
          <tr key={invitation.id}>
            <td>{invitation.email}</td>
            <td>{invitation.status}</td>
            <td className="mono">{invitation.expires_at?.slice(0, 10) ?? "—"}</td>
            <td>
              {invitation.status === "pending" && (
                <button
                  type="button"
                  className="badge-button"
                  disabled={busy}
                  onClick={() => onRevoke(invitation)}
                >
                  <X size={13} strokeWidth={2} aria-hidden="true" />
                  Revoke
                </button>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

interface AuditRow {
  id: number;
  at: string;
  actor: string;
  actor_label: string | null;
  action: string;
  target_label: string | null;
  target_id: string | null;
}

export function Audit() {
  const pages = usePages();
  const resource = useResource<{ entries: AuditRow[]; next_before_id: number | null }>(
    pages.cursor === null ? "api/audit" : `api/audit?before_id=${pages.cursor}`,
  );

  if (resource === null) return <Loading subject="the audit log" />;
  if (resource.state !== "ok") return <Failure resource={resource} subject="the audit log" />;

  const { entries, next_before_id } = resource.data;

  return (
    <>
      <p>
        Every change an administrator has made, newest first. Entries are permanent once written.
      </p>
      {entries.length === 0 && <p>Nothing has been recorded yet.</p>}
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
          {entries.map((entry) => (
            <tr key={entry.id}>
              <td className="mono">{entry.at.replace("T", " ").slice(0, 19)}</td>
              <td>{entry.actor_label ?? entry.actor}</td>
              <td className="mono">{entry.action}</td>
              <td>{entry.target_label ?? entry.target_id ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <Pager pages={pages} next={next_before_id} />
    </>
  );
}
