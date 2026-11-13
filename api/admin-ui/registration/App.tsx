import { useEffect, useState } from "react";

import logo from "../src/assets/collab-logo.png";
import { endSession } from "../src/signout";
import { pageCopy } from "./copy";
import { type Claim, type Tab, redeem, startingState } from "./flow";
import { CREATE_ACCOUNT_URL, DATA_STATEMENT_URL, RENEW_SIGN_IN_URL, SIGN_IN_URL } from "./links";
import { type InviteSession, loadSession } from "./session";

/**
 * The invitation-acceptance page.
 *
 * One page with many states. The state is a word: either one this page works
 * out for itself (no code, sign in first, ready) or the outcome the hub answers
 * a redemption with. This component turns the word into copy and adds the few
 * controls a state needs. The claim it is handed holds the invitation code,
 * and the only thing this component does with it is pass it back to the flow.
 */
export function App({ tab, claimed }: { tab: Tab; claimed: Claim }) {
  const [claim, setClaim] = useState(claimed);
  // `undefined` while the hub has not answered yet, `null` when it could not.
  const [session, setSession] = useState<InviteSession | null | undefined>(undefined);
  const [state, setState] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [signOutFailed, setSignOutFailed] = useState(false);

  async function open() {
    const answer = await loadSession();
    setSession(answer);
    setState(answer ? startingState(claim, answer) : "error");
  }

  useEffect(() => {
    void open();
    // Once, on arrival.
  }, []);

  if (state === null || session === undefined) {
    return (
      <Frame>
        <p className="quiet">Loading your invitation.</p>
      </Frame>
    );
  }

  async function accept() {
    if (!session) return;
    setBusy(true);
    setState("working");
    const result = await redeem(tab, claim, session.csrfToken);
    setClaim(result.claim);
    setState(result.state);
    setBusy(false);
  }

  async function signOut() {
    if (!session) return;
    setBusy(true);
    setSignOutFailed(false);
    if (await endSession(session.csrfToken)) await open();
    else setSignOutFailed(true);
    setBusy(false);
  }

  const copy = pageCopy(state, session?.requireVerifiedEmail ?? true);

  return (
    <Frame>
      <main aria-live="polite">
        <h1>{copy.heading}</h1>
        {copy.paragraphs.map((paragraph) => (
          <p key={paragraph}>{paragraph}</p>
        ))}

        {state === "signin" && (
          <div className="actions">
            <a className="button" href={CREATE_ACCOUNT_URL}>
              Create your account
            </a>
            <a href={SIGN_IN_URL}>Already have an account? Sign in</a>
          </div>
        )}

        {state === "reauthentication_required" && (
          <div className="actions">
            <a className="button" href={RENEW_SIGN_IN_URL}>
              Continue
            </a>
          </div>
        )}

        {state === "ready" && session && (
          <>
            <div className="statement">
              <p>{session.dataStatement}</p>
              <p>
                <a href={DATA_STATEMENT_URL}>Read the data statement</a>
              </p>
            </div>
            <div className="actions">
              <button type="button" className="button" onClick={accept} disabled={busy}>
                Accept invitation
              </button>
            </div>
          </>
        )}
      </main>

      {session?.signedIn && (
        <footer className="identity">
          <span>
            Signed in as <strong>{session.identity}</strong>
          </span>
          {session.accountUrl && <a href={session.accountUrl}>Account</a>}
          <button type="button" className="link" onClick={signOut} disabled={busy}>
            Sign out
          </button>
          {signOutFailed && <span className="bad">Sign-out did not go through. Try again.</span>}
        </footer>
      )}
    </Frame>
  );
}

/** The wordmark and the column every state is laid out in. */
function Frame({ children }: { children: React.ReactNode }) {
  return (
    <div className="page">
      <img className="wordmark" src={logo} alt="OpenTeams Collab" />
      {children}
    </div>
  );
}
