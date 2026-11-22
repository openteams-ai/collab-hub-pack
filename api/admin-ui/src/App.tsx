import {
  Boxes,
  ChartColumn,
  LayoutDashboard,
  Mail,
  MailPen,
  Plug,
  ScrollText,
  Users as UsersIcon,
} from "lucide-react";
import { useState } from "react";

import { SIGNED_OUT_EVENT } from "./api";
import { Header } from "./Header";
import { Audit, Connectors, InvitationEmails, Invitations, Models, Usage, Users } from "./Sections";
import { type SessionResult, loadSession } from "./session";
import { type Theme, applyTheme, nextTheme, readTheme, storeTheme, systemPrefersDark } from "./theme";
import { useEffect } from "react";

/**
 * The panel shell: a nav, one section at a time, and the four ways this can
 * refuse.
 *
 * Navigation is the URL fragment rather than a routing library. The panel is
 * one screen with tabs, and a fragment gives what actually matters here -- a
 * reload keeps you where you were, and a link can point at a section -- for
 * three lines instead of a dependency.
 */

type SectionKey =
  | "overview"
  | "models"
  | "users"
  | "connectors"
  | "invitations"
  | "invitation-emails"
  | "usage"
  | "audit";

// Icons are decorative: every entry is labelled in words beside it, so they
// carry `aria-hidden` and the label remains the accessible name.
const SECTIONS: { id: SectionKey; label: string; Icon: typeof LayoutDashboard }[] = [
  { id: "overview", label: "Overview", Icon: LayoutDashboard },
  { id: "models", label: "Models and access", Icon: Boxes },
  { id: "users", label: "Users", Icon: UsersIcon },
  { id: "connectors", label: "Connectors", Icon: Plug },
  { id: "invitations", label: "Invitations", Icon: Mail },
  { id: "invitation-emails", label: "Invitation emails", Icon: MailPen },
  { id: "usage", label: "Hub usage", Icon: ChartColumn },
  { id: "audit", label: "Audit log", Icon: ScrollText },
];

const TITLES: Record<SectionKey, string> = {
  overview: "Administration",
  models: "Models and access",
  users: "Users",
  connectors: "Connectors",
  invitations: "Invitations",
  "invitation-emails": "Invitation emails",
  usage: "Hub usage",
  audit: "Audit log",
};

export function App() {
  const [result, setResult] = useState<SessionResult | null>(null);
  const [section, setSection] = useState<SectionKey>(sectionFromHash());
  const [theme, setTheme] = useState<Theme>(() => readTheme(() => window.localStorage, systemPrefersDark()));

  // Applied as an effect rather than during render: touching the document is a
  // side effect, and React may render more than once before it commits.
  useEffect(() => {
    applyTheme(document.documentElement, theme);
  }, [theme]);

  // A call that finds the session gone switches the whole panel to the
  // signed-out notice, whichever screen it came from.
  useEffect(() => {
    const ended = () => setResult({ state: "signed-out" });
    window.addEventListener(SIGNED_OUT_EVENT, ended);
    return () => window.removeEventListener(SIGNED_OUT_EVENT, ended);
  }, []);

  // The back and forward buttons change only the fragment; follow them.
  useEffect(() => {
    const follow = () => setSection(sectionFromHash());
    window.addEventListener("hashchange", follow);
    return () => window.removeEventListener("hashchange", follow);
  }, []);

  function toggleTheme() {
    const chosen = nextTheme(theme);
    storeTheme(() => window.localStorage, chosen);
    setTheme(chosen);
  }

  useEffect(() => {
    let live = true;
    loadSession().then((answer) => {
      if (live) setResult(answer);
    });
    return () => {
      live = false;
    };
  }, []);

  if (result === null) return <Notice title="Loading" body="Checking your access." />;

  switch (result.state) {
    case "signed-out":
      // The document itself is session-gated, so arriving here means the
      // session expired between loading the page and this call.
      return <Notice title="Your session ended" body="Reload the page to sign in again." />;
    case "forbidden":
      return (
        <Notice
          bad
          title="This account is not an administrator"
          body="Administrator access comes from your identity provider group, and is re-checked each time you sign in."
        />
      );
    case "unavailable":
      return (
        <Notice
          bad
          title="Access cannot be checked right now"
          body="The hub could not determine what this account is allowed to do. This is a problem with the deployment, not with your account."
        />
      );
    case "error":
      return <Notice bad title="Something went wrong" body="The hub did not answer. Try reloading." />;
    case "ok":
      break;
  }

  const { session } = result;
  return (
    <div className="app">
      <Header session={session} theme={theme} onToggleTheme={toggleTheme} />
      <div className="shell">
        <nav className="nav">
          <ul>
            {SECTIONS.map((entry) => (
              <li key={entry.id}>
                <button
                  type="button"
                  className={entry.id === section ? "navlink current" : "navlink"}
                  onClick={() => {
                    window.location.hash = entry.id;
                    setSection(entry.id);
                  }}
                >
                  <entry.Icon size={16} strokeWidth={1.75} aria-hidden="true" />
                  {entry.label}
                </button>
              </li>
            ))}
          </ul>
          <footer>
            <span className="mono">v{session.version}</span>
          </footer>
        </nav>
        <main className="main">
          <h2>{TITLES[section]}</h2>
          {section === "overview" && <Overview />}
          {section === "models" && <Models csrfToken={session.csrf_token} />}
          {section === "users" && <Users csrfToken={session.csrf_token} />}
          {section === "connectors" && <Connectors csrfToken={session.csrf_token} />}
        {section === "invitations" && <Invitations csrfToken={session.csrf_token} />}
          {section === "invitation-emails" && <InvitationEmails csrfToken={session.csrf_token} />}
          {section === "usage" && <Usage />}
          {section === "audit" && <Audit />}
        </main>
      </div>
    </div>
  );
}

function sectionFromHash(): SectionKey {
  const wanted = window.location.hash.replace(/^#/, "");
  // An unknown fragment falls back rather than rendering nothing: a stale link
  // should land somewhere useful, not on a blank panel.
  return SECTIONS.some((entry) => entry.id === wanted) ? (wanted as SectionKey) : "overview";
}

function Overview() {
  return (
    <>
      <p>
        This panel manages the hub itself: which models it offers and who may use them, who its
        users are, and which connectors are switched on.
      </p>
      <p>
        Your administrator access comes from your identity provider group, checked again every time
        you sign in. Removing you from that group removes this panel at your next sign-in.
      </p>
      <p>
        Everything an administrator changes here is recorded in the audit log, including who made
        the change and when.
      </p>
    </>
  );
}

function Notice({ title, body, bad }: { title: string; body: string; bad?: boolean }) {
  return (
    <main className="main">
      <div className={bad ? "notice bad" : "notice"}>
        <h2>{title}</h2>
        <p>{body}</p>
      </div>
    </main>
  );
}
