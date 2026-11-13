/**
 * Light or dark, chosen by the viewer, defaulting to their system setting.
 *
 * Three states rather than two, even though the control is a single toggle:
 * light, dark, and *nothing chosen yet*. Until someone picks, the panel follows
 * the operating system, which is what the server-rendered pages beside it do.
 * The moment they pick, that choice wins and is remembered.
 *
 * The choice lives in `localStorage`, which is per-browser and never reaches
 * the server. Every access is wrapped: a private window or blocked site data
 * throws on access rather than returning null, and a colour scheme is never
 * worth failing a render over. Storage is passed as a getter because reading
 * `window.localStorage` is itself the access that throws.
 */

export type Theme = "light" | "dark";

export const THEME_KEY = "collab-admin-theme";

/**
 * The cookie the hub's server-rendered pages keep the same choice in. Read
 * first and written on every change, so switching the theme on either surface
 * switches it on both. Not HttpOnly, since this code has to read it; it holds
 * one of two words and nothing else.
 */
export const THEME_COOKIE = "collab-theme";

function isTheme(value: unknown): value is Theme {
  return value === "light" || value === "dark";
}

/** The theme named in a `document.cookie` string, or null. */
export function themeFromCookie(cookies: string): Theme | null {
  const match = cookies.split(";").map((part) => part.trim()).find((part) => part.startsWith(`${THEME_COOKIE}=`));
  const value = match?.slice(THEME_COOKIE.length + 1);
  return isTheme(value) ? value : null;
}

export function readTheme(store: () => Storage, systemPrefersDark: boolean, cookies = ""): Theme {
  const shared = themeFromCookie(cookies);
  if (shared) return shared;
  let stored: string | null = null;
  try {
    stored = store().getItem(THEME_KEY);
  } catch {
    stored = null;
  }
  if (isTheme(stored)) return stored;
  return systemPrefersDark ? "dark" : "light";
}

/** The `Set-Cookie` text that records a choice for a year, for every path of this origin. */
export function themeCookie(theme: Theme): string {
  return `${THEME_COOKIE}=${theme}; Path=/; Max-Age=31536000; SameSite=Lax; Secure`;
}

export function storeTheme(store: () => Storage, theme: Theme, setCookie?: (cookie: string) => void): void {
  try {
    store().setItem(THEME_KEY, theme);
  } catch {
    // The toggle still works for this page view; it just will not be
    // remembered. Refusing to switch would be the worse failure.
  }
  setCookie?.(themeCookie(theme));
}

export function nextTheme(theme: Theme): Theme {
  return theme === "dark" ? "light" : "dark";
}

/**
 * Put the choice where CSS can see it.
 *
 * `data-theme` on the root element, plus `color-scheme` so the browser's own
 * surfaces -- scrollbars, form controls, the canvas behind the page -- match
 * the rest rather than staying stubbornly light.
 */
export function applyTheme(root: HTMLElement, theme: Theme): void {
  root.dataset.theme = theme;
  root.style.colorScheme = theme;
}

export function systemPrefersDark(): boolean {
  return typeof window !== "undefined" && window.matchMedia?.("(prefers-color-scheme: dark)").matches === true;
}
