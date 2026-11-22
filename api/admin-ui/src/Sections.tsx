import { Activity, Building2, Users as UsersIcon, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { ConnectorIcon, ModelIcon } from "./BrandIcon";
import { postJson } from "./api";
import {
  INVITATION_TABS,
  type InvitationTab,
  emptyListText,
  invitationNotice,
  invitationsPath,
  templateProblems,
} from "./invitations";
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
            <th>Granted by</th>
            <th>Role</th>
          </tr>
        </thead>
        <tbody>
          {users.map((user) => (
            <tr key={user.id}>
              <td>{user.email ?? user.id}</td>
              <td className="mono">{user.username}</td>
              <td>{ROLE_SOURCES[user.role_source ?? ""] ?? "Not an administrator"}</td>
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
  idp: "Identity provider group",
  manual: "An administrator here",
};

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
  accepted_at: string | null;
  revoked_at: string | null;
}

interface InvitationListing {
  invitations: InvitationRow[];
  next_offset: number | null;
  counts: Record<InvitationTab, number>;
}

/** The stored and derived states, as the panel names them. "pending" is an implementation word. */
const STATE_LABELS: Record<string, string> = {
  pending: "Outstanding",
  accepted: "Accepted",
  expired: "Expired",
  revoked: "Revoked",
};

/** The date that matters for a row, which depends on where it ended up. */
function closingDate(row: InvitationRow): string | null {
  if (row.status === "accepted") return row.accepted_at;
  if (row.status === "revoked") return row.revoked_at;
  return row.expires_at;
}

const CLOSING_HEADERS: Record<InvitationTab, string> = {
  outstanding: "Expires",
  accepted: "Accepted",
  expired: "Expired",
  revoked: "Revoked",
  all: "Expires or closed",
};

function when(iso: string | null): string {
  return iso ? iso.replace("T", " ").slice(0, 16) : "—";
}

export function Invitations({ csrfToken }: { csrfToken: string }) {
  const [reload, setReload] = useState(0);
  const [tab, setTab] = useState<InvitationTab>("outstanding");
  const [query, setQuery] = useState("");
  const searched = useDebounced(query.trim(), 300);
  const pages = usePages();
  const resource = useResource<InvitationListing>(invitationsPath(tab, searched, pages.cursor), reload);
  // The tab counts outlive the request that is loading the next page, so the
  // tabs do not blank out every time the list reloads.
  const counts = useRef<Record<InvitationTab, number> | null>(null);
  if (resource?.state === "ok") counts.current = resource.data.counts;
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ text: string; bad: boolean } | null>(null);

  // A new search starts from the first page; an old cursor would skip rows.
  const { reset } = pages;
  useEffect(() => reset(), [searched, reset]);

  function choose(next: InvitationTab) {
    setTab(next);
    pages.reset();
  }

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
      <p>
        If the address already has an account that never joined an organization, they are sent the
        sign-in version of the email instead of the registration one. If it already belongs to an
        organization here, nothing is sent: they can sign in as they are.{" "}
        <a href="#invitation-emails">Edit the invitation emails</a>
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
      <div className="listing-tools">
        <div className="tabs" role="tablist" aria-label="Invitations by state">
          {INVITATION_TABS.map((entry) => (
            <button
              key={entry.status}
              type="button"
              role="tab"
              aria-selected={entry.status === tab}
              className={entry.status === tab ? "tab current" : "tab"}
              onClick={() => choose(entry.status)}
            >
              {entry.label}
              {counts.current && <span className="tab-count mono">{counts.current[entry.status]}</span>}
            </button>
          ))}
        </div>
        <label className="visually-hidden" htmlFor="invitation-search">
          Search invitations by address
        </label>
        <input
          id="invitation-search"
          className="listing-search"
          type="search"
          autoComplete="off"
          placeholder="Search by address"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
      </div>
      <InvitationList resource={resource} tab={tab} query={searched} busy={busy} onRevoke={revoke} />
      {resource?.state === "ok" && <Pager pages={pages} next={resource.data.next_offset} />}
    </>
  );
}

function InvitationList({
  resource,
  tab,
  query,
  busy,
  onRevoke,
}: {
  resource: Resource<InvitationListing> | null;
  tab: InvitationTab;
  query: string;
  busy: boolean;
  onRevoke: (invitation: InvitationRow) => void;
}) {
  if (resource === null) return <Loading subject="invitations" />;
  if (resource.state !== "ok") return <Failure resource={resource} subject="invitations" />;

  const { invitations } = resource.data;
  if (invitations.length === 0) {
    return <p className="empty">{emptyListText(tab, query)}</p>;
  }

  return (
    <table>
      <thead>
        <tr>
          <th>Invited</th>
          <th>State</th>
          <th>Sent</th>
          <th>{CLOSING_HEADERS[tab]}</th>
          <th />
        </tr>
      </thead>
      <tbody>
        {invitations.map((invitation) => (
          <tr key={invitation.id}>
            <td>{invitation.email}</td>
            <td>{STATE_LABELS[invitation.status] ?? invitation.status}</td>
            <td className="mono">{when(invitation.created_at)}</td>
            <td className="mono">{when(closingDate(invitation))}</td>
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

interface TemplateRow {
  key: string;
  label: string;
  subject: string;
  body: string;
  customized: boolean;
  updated_at: string | null;
  updated_by: string | null;
  default_subject: string;
  default_body: string;
}

interface TemplateListing {
  templates: TemplateRow[];
  required_placeholders: string[];
  optional_placeholders: string[];
  editable: boolean;
}

const TEMPLATE_PURPOSES: Record<string, string> = {
  new_account: "Sent to an address with no account. It tells them to register, then accept.",
  existing_account:
    "Sent to an address that already has an account but has not joined an organization. It tells them to sign in, not register.",
};

/** The two invitation emails, each editable, previewable and resettable (#348). */
export function InvitationEmails({ csrfToken }: { csrfToken: string }) {
  const resource = useResource<TemplateListing>("api/invitation-templates");
  // What a save or reset returned, by key. Applied over the listing instead
  // of reloading it: a reload would remount every editor and throw away
  // unsaved changes in the email that was not saved.
  const [saved, setSaved] = useState<Record<string, TemplateRow>>({});

  if (resource === null) return <Loading subject="the invitation emails" />;
  if (resource.state !== "ok") return <Failure resource={resource} subject="the invitation emails" />;

  const { required_placeholders: required, optional_placeholders: optional, editable } = resource.data;
  const templates = resource.data.templates.map((template) => saved[template.key] ?? template);

  return (
    <>
      <p>
        What invitees are sent. The panel picks the email when you send an invitation, from whether the
        address already has an account. Changes apply to the next invitation sent, and every save is in
        the audit log.
      </p>
      <p>
        Placeholders are filled in for each invitation. Every email must include{" "}
        {required.map((placeholder, i) => (
          <span key={placeholder}>
            {i > 0 && ", "}
            <code>{placeholder}</code>
          </span>
        ))}
        . It may also use{" "}
        {optional.map((placeholder, i) => (
          <span key={placeholder}>
            {i > 0 && " and "}
            <code>{placeholder}</code>
          </span>
        ))}
        . Lines between <code>[IF VERIFIED EMAIL REQUIRED]</code> and <code>[END IF]</code> are sent only
        when this hub requires a verified address.
      </p>
      {!editable && (
        <p className="bad">
          This hub has no database to keep edited emails in, so it sends the built-in text and cannot
          save changes.
        </p>
      )}
      {templates.map((template) => (
        <TemplateEditor
          // Keyed on what is stored, so a save or reset restarts only the
          // editor it changed, from the stored text.
          key={`${template.key}:${template.customized ? template.updated_at : "default"}`}
          template={template}
          editable={editable}
          csrfToken={csrfToken}
          onChanged={(changed) => setSaved((current) => ({ ...current, [changed.key]: changed }))}
        />
      ))}
    </>
  );
}

function TemplateEditor({
  template,
  editable,
  csrfToken,
  onChanged,
}: {
  template: TemplateRow;
  editable: boolean;
  csrfToken: string;
  onChanged: (template: TemplateRow) => void;
}) {
  const [subject, setSubject] = useState(template.subject);
  const [body, setBody] = useState(template.body);
  const [busy, setBusy] = useState(false);
  const [problems, setProblems] = useState<string[] | null>(null);
  const [notice, setNotice] = useState<{ text: string; bad: boolean } | null>(null);
  const [preview, setPreview] = useState<{ subject: string; body: string } | null>(null);
  const dirty = subject !== template.subject || body !== template.body;
  const path = `api/invitation-templates/${encodeURIComponent(template.key)}`;

  async function run(action: "preview" | "save" | "reset") {
    setBusy(true);
    setProblems(null);
    setNotice(null);
    const result =
      action === "reset"
        ? await postJson(`${path}/reset`, {}, csrfToken)
        : await postJson(action === "preview" ? `${path}/preview` : path, { subject, body }, csrfToken);
    setBusy(false);

    const refusal = templateProblems(result);
    if (refusal) {
      setProblems(refusal);
      return;
    }
    if (result.state !== "ok") {
      setNotice({ text: "That did not go through. Nothing was changed.", bad: true });
      return;
    }
    if (action === "preview") {
      setPreview(result.data as { subject: string; body: string });
      return;
    }
    const changed = (result.data as { template?: TemplateRow } | null)?.template;
    if (changed) onChanged(changed);
  }

  const fieldId = `template-${template.key}`;
  return (
    <section className="panel template-editor">
      <h3>{template.label}</h3>
      <p className="template-purpose">{TEMPLATE_PURPOSES[template.key]}</p>
      <p className="template-meta">
        {template.customized
          ? `Edited ${when(template.updated_at)}${template.updated_by ? ` by ${template.updated_by}` : ""}.`
          : "The built-in text."}
      </p>

      <label htmlFor={`${fieldId}-subject`}>Subject</label>
      <input
        id={`${fieldId}-subject`}
        type="text"
        value={subject}
        onChange={(event) => setSubject(event.target.value)}
        disabled={busy || !editable}
      />
      <label htmlFor={`${fieldId}-body`}>Message</label>
      <textarea
        id={`${fieldId}-body`}
        className="mono"
        rows={22}
        spellCheck
        value={body}
        onChange={(event) => setBody(event.target.value)}
        disabled={busy || !editable}
      />

      {problems && (
        <div className="bad" role="alert">
          <p className="bad">This email cannot be used as it is{problems.length ? ":" : "."}</p>
          <ul>
            {problems.map((problem) => (
              <li key={problem}>{problem}</li>
            ))}
          </ul>
        </div>
      )}
      {notice && <p className={notice.bad ? "bad" : "good"}>{notice.text}</p>}

      <div className="template-actions">
        <button type="button" className="primary" disabled={busy || !editable || !dirty} onClick={() => run("save")}>
          Save
        </button>
        <button type="button" className="link" disabled={busy} onClick={() => run("preview")}>
          Preview
        </button>
        {dirty && (
          <button
            type="button"
            className="link"
            disabled={busy}
            onClick={() => {
              setSubject(template.subject);
              setBody(template.body);
              setProblems(null);
            }}
          >
            Discard changes
          </button>
        )}
        {template.customized && (
          <button type="button" className="link" disabled={busy || !editable} onClick={() => run("reset")}>
            Reset to the built-in text
          </button>
        )}
      </div>

      {preview && (
        <div className="template-preview">
          <p>
            Preview, with sample values. The link in it cannot be used.{" "}
            <button type="button" className="link" onClick={() => setPreview(null)}>
              Close
            </button>
          </p>
          <p className="mono">Subject: {preview.subject}</p>
          <pre>{preview.body}</pre>
        </div>
      )}
    </section>
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
