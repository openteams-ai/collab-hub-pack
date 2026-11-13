import { LogOut, Moon, Sun } from "lucide-react";
import { useState } from "react";

import { LogoMark } from "./Logo";
import { initials } from "./avatar";
import type { AdminSession } from "./session";
import { signOut } from "./signout";
import type { Theme } from "./theme";

/**
 * The product header: the mark and name, that this is the hub's admin, which
 * hub it is, and who you are.
 *
 * The hub is named by the host the panel was opened at, the one thing the
 * browser already knows for certain, so an operator with several deployments
 * open can tell the tabs apart without the server saying anything new.
 */
export function Header({
  session,
  theme,
  onToggleTheme,
}: {
  session: AdminSession;
  theme: Theme;
  onToggleTheme: () => void;
}) {
  const [signOutFailed, setSignOutFailed] = useState(false);

  async function leave() {
    const next = await signOut(session.csrf_token);
    if (next) window.location.href = next;
    else setSignOutFailed(true);
  }

  const nextLabel = theme === "dark" ? "Switch to light theme" : "Switch to dark theme";

  return (
    <header className="header">
      <div className="lockup">
        <LogoMark />
        <span className="lockup-name">Collab</span>
      </div>
      <span className="badge-brand">Hub admin</span>
      <span className="chip">{window.location.hostname}</span>
      <span className="header-spacer" />
      <button type="button" className="icon-button" onClick={onToggleTheme} title={nextLabel} aria-label={nextLabel}>
        {theme === "dark" ? (
          <Sun size={16} strokeWidth={1.75} aria-hidden="true" />
        ) : (
          <Moon size={16} strokeWidth={1.75} aria-hidden="true" />
        )}
      </button>
      <div className="header-identity">
        <span className="avatar" aria-hidden="true">
          {initials(session)}
        </span>
        <span className="who">{session.email || session.user}</span>
      </div>
      {signOutFailed && <span className="bad">Sign-out did not go through. Try again.</span>}
      <button type="button" className="icon-button" onClick={leave} title="Sign out" aria-label="Sign out">
        <LogOut size={16} strokeWidth={1.75} aria-hidden="true" />
      </button>
    </header>
  );
}
