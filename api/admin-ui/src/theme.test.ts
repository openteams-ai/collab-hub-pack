import { describe, expect, it } from "vitest";

import { THEME_COOKIE, THEME_KEY, nextTheme, readTheme, storeTheme, themeFromCookie } from "./theme";

function storage(initial: Record<string, string> = {}) {
  const data = { ...initial };
  return {
    getItem: (k: string) => (k in data ? data[k] : null),
    setItem: (k: string, v: string) => {
      data[k] = v;
    },
    data,
  } as unknown as Storage & { data: Record<string, string> };
}

const throwing = {
  getItem() {
    throw new Error("blocked");
  },
  setItem() {
    throw new Error("blocked");
  },
} as unknown as Storage;

// What `window.localStorage` does when the browser blocks site data: the
// property access itself throws, before any method is called.
function blockedStorage(): Storage {
  throw new DOMException("The operation is insecure.", "SecurityError");
}

describe("readTheme", () => {
  it("follows the system preference when nothing has been chosen", () => {
    expect(readTheme(() => storage(), true)).toBe("dark");
    expect(readTheme(() => storage(), false)).toBe("light");
  });

  it("lets an explicit choice win over the system preference", () => {
    expect(readTheme(() => storage({ [THEME_KEY]: "light" }), true)).toBe("light");
    expect(readTheme(() => storage({ [THEME_KEY]: "dark" }), false)).toBe("dark");
  });

  it("ignores a stored value that is not a theme", () => {
    expect(readTheme(() => storage({ [THEME_KEY]: "neon" }), true)).toBe("dark");
  });

  it("still answers when storage is unavailable", () => {
    // Private windows and blocked site-data both throw on access rather than
    // returning null, and a theme is never worth failing a render over.
    expect(readTheme(() => throwing, true)).toBe("dark");
  });

  it("still answers when reaching storage at all throws", () => {
    expect(readTheme(blockedStorage, false)).toBe("light");
  });
});

describe("storeTheme", () => {
  it("persists the choice", () => {
    const store = storage();
    storeTheme(() => store, "dark");
    expect(store.data[THEME_KEY]).toBe("dark");
  });

  it("does not throw when storage refuses", () => {
    expect(() => storeTheme(() => throwing, "dark")).not.toThrow();
  });

  it("does not throw when reaching storage at all throws", () => {
    expect(() => storeTheme(blockedStorage, "dark")).not.toThrow();
  });
});

describe("nextTheme", () => {
  it("flips", () => {
    expect(nextTheme("light")).toBe("dark");
    expect(nextTheme("dark")).toBe("light");
  });
});

describe("the theme shared with the hub's own pages", () => {
  it("prefers the choice the hub's pages recorded in the cookie", () => {
    expect(readTheme(() => storage({ [THEME_KEY]: "light" }), false, `${THEME_COOKIE}=dark; other=1`)).toBe("dark");
  });

  it("ignores a cookie naming a theme that does not exist", () => {
    expect(themeFromCookie(`${THEME_COOKIE}=purple`)).toBeNull();
    expect(readTheme(() => storage({ [THEME_KEY]: "light" }), true, `${THEME_COOKIE}=purple`)).toBe("light");
  });

  it("writes the choice to the cookie as well, for every path of the origin", () => {
    const written: string[] = [];
    storeTheme(() => storage(), "dark", (cookie) => written.push(cookie));
    expect(written).toEqual([`${THEME_COOKIE}=dark; Path=/; Max-Age=31536000; SameSite=Lax; Secure`]);
  });
});
