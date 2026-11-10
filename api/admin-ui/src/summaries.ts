/**
 * The words and numbers the screens show about the rows they load.
 *
 * Kept apart from the components so each rule is checked on its own: a count
 * that would be wrong on a paged listing says so, and a share never divides by
 * zero.
 */

import type { AuditRow, ConnectorRow, ModelRow, OrganizationUsage, UserRow } from "./types";

/** A figure and the line beneath it on an overview card. */
export interface Summary {
  figure: string;
  detail: string;
}

function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`;
}

/** Each organization's whole-number percentage of all recorded actions. */
export function shares(organizations: OrganizationUsage[]): number[] {
  const total = organizations.reduce((sum, org) => sum + org.events, 0);
  return organizations.map((org) => (total === 0 ? 0 : Math.round((org.events / total) * 100)));
}

export type UsageRange = "all" | "30d" | "7d";

const RANGE_DAYS: Record<Exclude<UsageRange, "all">, number> = { "30d": 30, "7d": 7 };

/** The `since` bound the usage endpoint takes for *range*, or `null` for all time. */
export function sinceFor(range: UsageRange, now: Date): string | null {
  if (range === "all") return null;
  return new Date(now.getTime() - RANGE_DAYS[range] * 24 * 60 * 60 * 1000).toISOString();
}

/**
 * A filter tab's label. The count shows only when every row is loaded: on a
 * paged listing it would count one page and read as the total.
 */
export function tabLabel(name: string, count: number, complete: boolean): string {
  return complete ? `${name} ${count}` : name;
}

const ACCESS_LABELS: Record<string, string> = {
  manual: "Granted here",
  idp: "Granted in Keycloak",
};

/** Where an administrator's role came from, or `null` for someone who holds none. */
export function accessLabel(roleSource: string | null): string | null {
  return roleSource === null ? null : (ACCESS_LABELS[roleSource] ?? null);
}

export function modelSummary(models: ModelRow[]): Summary {
  const limited = models.filter((model) => model.group_path !== null);
  const groups = new Set(limited.map((model) => model.group_path));
  const figure = plural(models.length, "model", "models");
  if (limited.length === 0) return { figure, detail: "Everyone who can sign in can use them" };
  const where = groups.size === 1 ? `the ${[...groups][0]} group` : "a group";
  return { figure, detail: `${limited.length} limited to ${where}` };
}

export function connectorSummary(connectors: ConnectorRow[]): Summary {
  const on = connectors.filter((connector) => connector.configured && connector.enabled).length;
  const missing = connectors.find((connector) => !connector.configured);
  return {
    figure: `${on} of ${connectors.length} on`,
    detail: missing ? `${missing.label} isn't set up yet` : "All set up",
  };
}

export function userSummary(users: UserRow[], complete: boolean): Summary {
  const administrators = users.filter((user) => user.role !== null).length;
  if (complete) {
    return {
      figure: plural(users.length, "person", "people"),
      detail: plural(administrators, "administrator", "administrators"),
    };
  }
  return {
    figure: `More than ${plural(users.length, "person", "people")}`,
    detail: `${plural(administrators, "administrator", "administrators")} on the first page`,
  };
}

export function invitationSummary(invitations: { status: string }[], complete: boolean): Summary {
  const pending = invitations.filter((invitation) => invitation.status === "pending").length;
  const revoked = invitations.filter((invitation) => invitation.status === "revoked").length;
  const scope = complete ? "" : " on the first page";
  return { figure: `${pending} pending${scope}`, detail: `${revoked} revoked${scope}` };
}

export function usageSummary(events: number, organizations: number): Summary {
  return {
    figure: plural(events, "action", "actions"),
    detail: `Across ${plural(organizations, "organization", "organizations")}`,
  };
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** A timestamp as "19 Sep 2026", in UTC like every other date here; a dash for none. */
export function shortDate(iso: string | null): string {
  if (!iso) return "—";
  const at = new Date(iso);
  return `${at.getUTCDate()} ${MONTHS[at.getUTCMonth()]} ${at.getUTCFullYear()}`;
}

/** The newest audit entry, dated in UTC like the log itself. */
export function auditSummary(latest: Omit<AuditRow, "id"> | undefined): Summary {
  if (!latest) return { figure: "No changes yet", detail: "Nothing has been recorded" };
  const at = new Date(latest.at);
  const time = `${String(at.getUTCHours()).padStart(2, "0")}:${String(at.getUTCMinutes()).padStart(2, "0")}`;
  const who = latest.actor_label ?? latest.actor;
  const whom = latest.target_label ?? latest.target_id;
  return {
    figure: `Last change ${at.getUTCDate()} ${MONTHS[at.getUTCMonth()]}, ${time}`,
    detail: [who, latest.action, whom].filter(Boolean).join(" "),
  };
}
