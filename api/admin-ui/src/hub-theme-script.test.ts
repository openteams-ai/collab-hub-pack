import { describe, expect, it } from "vitest";

import SOURCE from "../../src/collab_hub_api/web/theme.js?raw";

// The hub's server-rendered home and account pages load this script; it is not
// part of the panel's bundle. It is tested here because this is the repo's one
// JavaScript test runner.

type Listener = (event: { persisted?: boolean }) => void;

function load() {
  const windowListeners: Record<string, Listener[]> = {};
  const reloads: number[] = [];
  const window = {
    matchMedia: () => ({ matches: false, addEventListener: () => {} }),
    addEventListener: (type: string, listener: Listener) => {
      (windowListeners[type] ??= []).push(listener);
    },
    location: { reload: () => reloads.push(1) },
  };
  const document = {
    documentElement: { dataset: {} as Record<string, string> },
    addEventListener: () => {},
    querySelectorAll: () => [],
  };
  const localStorage = { getItem: () => null, setItem: () => {} };
  new Function("window", "document", "localStorage", SOURCE)(window, document, localStorage);
  const fire = (event: { persisted?: boolean }) => {
    for (const listener of windowListeners.pageshow ?? []) listener(event);
  };
  return { fire, reloads };
}

describe("hub theme.js", () => {
  // #335: after Log out, Back showed the signed-in page Chrome had kept in its
  // back/forward cache, with no request to the server. The page must ask the
  // server again so it shows who is (not) signed in now.
  it("reloads a page the browser restored from its back/forward cache", () => {
    const { fire, reloads } = load();
    fire({ persisted: true });
    expect(reloads).toHaveLength(1);
  });

  it("leaves an ordinary page load alone", () => {
    const { fire, reloads } = load();
    fire({ persisted: false });
    expect(reloads).toHaveLength(0);
  });
});
