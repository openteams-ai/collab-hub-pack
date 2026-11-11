/** The rows the admin API answers with, shared by the screens and the overview that summarises them. */

export interface ModelRow {
  id: string;
  owned_by: string | null;
  group_path: string | null;
}

export interface UserRow {
  id: string;
  username: string;
  email: string | null;
  role: string | null;
  role_source: string | null;
}

export interface ConnectorRow {
  key: string;
  label: string;
  configured: boolean;
  credential: string | null;
  probeable: boolean;
  enabled: boolean;
}

export interface OrganizationUsage {
  org_id: string;
  /** `null` when the hub has no name stored for it, or cannot read names. */
  name: string | null;
  users: number;
  events: number;
}

export interface HubUsage {
  users_total: number;
  events_total: number;
  events: { event: string; count: number }[];
  organizations: OrganizationUsage[];
}

export interface InvitationRow {
  id: string;
  email: string;
  status: string;
  created_at: string | null;
  expires_at: string | null;
  org_id: string | null;
  organization: string | null;
  role: string;
}

export interface AuditRow {
  id: number;
  at: string;
  actor: string;
  actor_label: string | null;
  action: string;
  target_label: string | null;
  target_id: string | null;
}
