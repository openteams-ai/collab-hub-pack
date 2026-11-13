import { Box, ChartNoAxesColumn, FileText, LayoutGrid, Mail, Plug, Users as UsersIcon } from "lucide-react";
import { useEffect, useState } from "react";

import { SIGNED_OUT_EVENT } from "./api";
import { Header } from "./Header";
import { LogoMark } from "./Logo";
import { Overview } from "./Overview";
import { Audit, Connectors, Invitations, Models, Usage, Users } from "./Sections";
import { type SessionResult, loadSession } from "./session";
import { type Theme, applyTheme, nextTheme, readTheme, storeTheme, systemPrefersDark } from "./theme";

/**
 * The panel shell: a grouped nav, one section at a time, and the four ways
 * this can refuse.
 *
 * Navigation is the URL fragment rather than a routing library. The panel is
 * one screen with tabs, and a fragment gives what actually matters here -- a
 * reload keeps you where you were, and a link can point at a section -- for
 * three lines instead of a dependency.
 */

export type SectionKey = "overview" | "models" | "connectors" | "users" | "invitations" | "usage" | "audit";

// Icons are decorative: every entry is labelled in words beside it, so they
// carry `aria-hidden` and the label remains the accessible name.
const GROUPS: { label: string; sections: { id: SectionKey; label: string; Icon: typeof LayoutGrid }[] }[] = [
  {
    label: "Hub",
    sections: [
      { id: "overview", label: "Overview", Icon: LayoutGrid },
      { id: "models", label: "Models and access", Icon: Box },
      { id: "connectors", label: "Connectors", Icon: Plug },
    ],
  },
  {
    label: "People",
    sections: [
      { id: "users", label: "Users", Icon: UsersIcon },
      { id: "invitations", label: "Invitations", Icon: Mail },
    ],
  },
  {
    label: "Activity",
    sections: [
      { id: "usage", label: "Hub usage", Icon: ChartNoAxesColumn },
      { id: "audit", label: "Audit log", Icon: FileText },
    ],
  },
];

const SECTION_IDS = GROUPS.flatMap((group) => group.sections.map((section) => section.id));

/** Where "Sign in again" goes: the sign-in flow, back to this panel. */
const SIGN_IN_URL = `../web/signin?next=${encodeURIComponent("/admin/")}`;

export function App() {
  const [result, setResult] = useState<SessionResult | null>(null);
  const [section, setSection] = useState<SectionKey>(sectionFromHash());
  const [theme, setTheme] = useState<Theme>(() =>
    readTheme(() => window.localStorage, systemPrefersDark(), document.cookie),
  );

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
    storeTheme(() => window.localStorage, chosen, (cookie) => {
      document.cookie = cookie;
    });
    setTheme(chosen);
  }

  function open(id: SectionKey) {
    window.location.hash = id;
    setSection(id);
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
      return <Notice title="Signed out" body="Your session on this browser has ended." signIn />;
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
        <nav className="nav" aria-label="Sections">
          {GROUPS.map((group) => (
            <div key={group.label}>
              <div className="nav-group">{group.label}</div>
              <ul>
                {group.sections.map((entry) => (
                  <li key={entry.id}>
                    <button
                      type="button"
                      className={entry.id === section ? "navlink current" : "navlink"}
                      aria-current={entry.id === section ? "page" : undefined}
                      onClick={() => open(entry.id)}
                    >
                      <entry.Icon size={16} strokeWidth={1.75} aria-hidden="true" />
                      {entry.label}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          ))}
          <footer>v{session.version}</footer>
        </nav>
        <main className="main">
          <div className="page">
            {section === "overview" && <Overview onOpen={open} />}
            {section === "models" && <Models csrfToken={session.csrf_token} />}
            {section === "connectors" && <Connectors csrfToken={session.csrf_token} />}
            {section === "users" && <Users csrfToken={session.csrf_token} />}
            {section === "invitations" && <Invitations csrfToken={session.csrf_token} />}
            {section === "usage" && <Usage />}
            {section === "audit" && <Audit />}
          </div>
        </main>
      </div>
    </div>
  );
}

function sectionFromHash(): SectionKey {
  const wanted = window.location.hash.replace(/^#/, "");
  // An unknown fragment falls back rather than rendering nothing: a stale link
  // should land somewhere useful, not on a blank panel.
  return SECTION_IDS.includes(wanted as SectionKey) ? (wanted as SectionKey) : "overview";
}

/** A whole-window notice: the mark on the left, what happened on the right. */
function Notice({ title, body, bad, signIn }: { title: string; body: string; bad?: boolean; signIn?: boolean }) {
  return (
    <main className="notice-page">
      <div className="notice-art" aria-hidden="true">
        <div className="notice-ring">
          <div>
            <LogoMark size={48} />
          </div>
        </div>
      </div>
      <div className="notice-body">
        <h2>{title}</h2>
        <p className={bad ? "bad" : undefined}>{body}</p>
        {signIn && (
          <a className="primary" href={SIGN_IN_URL}>
            Sign in again
          </a>
        )}
      </div>
    </main>
  );
}
