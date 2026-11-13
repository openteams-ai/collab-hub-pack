import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import { type Tab, claimInvitation } from "./flow";
import "./registration.css";

/** The real browser tab, as the flow sees it. */
const tab: Tab = {
  readFragment: () => window.location.hash || "",
  clearFragment: () => {
    try {
      window.history.replaceState(null, "", window.location.pathname);
    } catch {
      window.location.hash = "";
    }
  },
  // Reading `window.sessionStorage` is itself the access that throws where a
  // browser blocks storage; the flow catches it.
  storage: () => window.sessionStorage,
};

// Claimed once, before anything renders: the code leaves the address bar
// straight away, and the page holds this one claim for as long as it is open.
const claimed = claimInvitation(tab);

const root = document.getElementById("root");
if (!root) throw new Error("index.html must carry the #root element the page mounts into");

createRoot(root).render(
  <StrictMode>
    <App tab={tab} claimed={claimed} />
  </StrictMode>,
);
