import { Box, ChartNoAxesColumn, FileText, Mail, Plug, Users as UsersIcon } from "lucide-react";

import type { SectionKey } from "./App";
import type { Resource } from "./api";
import {
  type Summary,
  auditSummary,
  connectorSummary,
  invitationSummary,
  modelSummary,
  usageSummary,
  userSummary,
} from "./summaries";
import type { AuditRow, ConnectorRow, HubUsage, InvitationRow, ModelRow, UserRow } from "./types";
import { PageHeader } from "./ui";
import { useResource } from "./useResource";

/**
 * The landing screen: one card per section, each saying the one thing worth
 * knowing about it, and opening it.
 *
 * Each card reads its own endpoint, so a section this hub cannot answer for
 * (no usage store, say) costs only its own card.
 */
export function Overview({ onOpen }: { onOpen: (section: SectionKey) => void }) {
  const models = useResource<{ models: ModelRow[]; catalog_error: string | null }>("api/models");
  const connectors = useResource<{ connectors: ConnectorRow[] }>("api/connectors");
  const users = useResource<{ users: UserRow[]; next_first: number | null }>("api/users");
  const invitations = useResource<{ invitations: InvitationRow[]; next_offset: number | null }>("api/invitations");
  const usage = useResource<HubUsage>("api/usage");
  const audit = useResource<{ entries: AuditRow[] }>("api/audit?limit=1");

  return (
    <>
      <PageHeader title="Overview">
        Hub admin manages the hub itself. Your administrator access comes from your identity provider group, checked
        again every time you sign in. Removing you from that group removes this panel at your next sign-in.
      </PageHeader>
      <div className="cards">
        <Card
          Icon={Box}
          title="Models and access"
          onOpen={() => onOpen("models")}
          summary={read(models, (data) => (data.catalog_error ? null : modelSummary(data.models)))}
        />
        <Card
          Icon={Plug}
          title="Connectors"
          onOpen={() => onOpen("connectors")}
          summary={read(connectors, (data) => connectorSummary(data.connectors))}
        />
        <Card
          Icon={UsersIcon}
          title="Users"
          onOpen={() => onOpen("users")}
          summary={read(users, (data) => userSummary(data.users, data.next_first === null))}
        />
        <Card
          Icon={Mail}
          title="Invitations"
          onOpen={() => onOpen("invitations")}
          summary={read(invitations, (data) => invitationSummary(data.invitations, data.next_offset === null))}
        />
        <Card
          Icon={ChartNoAxesColumn}
          title="Hub usage"
          onOpen={() => onOpen("usage")}
          summary={read(usage, (data) => usageSummary(data.events_total, data.organizations.length))}
        />
        <Card
          Icon={FileText}
          title="Audit log"
          onOpen={() => onOpen("audit")}
          summary={read(audit, (data) => auditSummary(data.entries[0]))}
        />
      </div>
      <p className="banner">
        Everything an administrator changes in Hub admin is recorded in the audit log, including who made the change
        and when.
      </p>
    </>
  );
}

const LOADING: Summary = { figure: "…", detail: "Loading" };
const UNAVAILABLE: Summary = { figure: "Unavailable", detail: "Open the section to see why" };

/** A card's summary once its endpoint has answered; a placeholder until then or if it could not. */
function read<T>(resource: Resource<T> | null, summarise: (data: T) => Summary | null): Summary {
  if (resource === null) return LOADING;
  if (resource.state !== "ok") return UNAVAILABLE;
  return summarise(resource.data) ?? UNAVAILABLE;
}

function Card({
  Icon,
  title,
  summary,
  onOpen,
}: {
  Icon: typeof Box;
  title: string;
  summary: Summary;
  onOpen: () => void;
}) {
  return (
    <button type="button" className="card" onClick={onOpen}>
      <span className="card-head">
        <span className="tile">
          <Icon size={16} strokeWidth={1.75} aria-hidden="true" />
        </span>
        {title}
      </span>
      <span className="card-figure">{summary.figure}</span>
      <span className="card-detail">{summary.detail}</span>
    </button>
  );
}
