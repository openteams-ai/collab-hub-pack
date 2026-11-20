/**
 * Ending the session, through the route the rest of the surface already uses.
 *
 * `POST /web/signout` clears the hub's session cookie and answers with the
 * realm's logout URL, which ends the Keycloak session too (#335): clearing the
 * cookie alone left that session alive, so the next "Log in" came straight back
 * with no password. The server-rendered pages reach the route with an HTML form
 * and are forwarded by the page it answers with. The panel posts it with
 * `fetch` instead, because its Content-Security-Policy sets `form-action
 * 'none'` and should keep doing so -- a surface that submits no forms is a
 * surface a markup-injection bug cannot use to post anywhere. `fetch` cannot
 * follow anyone to the realm, so the panel asks for JSON and navigates there
 * itself.
 *
 * `require_csrf` reads the `X-CSRF-Token` header before it falls back to form
 * fields, so this needs nothing new on the server.
 */

// `next` sends "Sign in again" on the signed-out page back to the panel. It is
// app-relative (the server prefixes the deployment's root path), the server
// checks it against the same allowlist as the sign-in redirect, and it rides
// the realm round trip in `post_logout_redirect_uri`.
export const SIGNOUT_URL = "../web/signout?next=%2Fadmin%2F";

const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]"]);

/** The realm logout URL is navigated to, so only a real web URL is accepted. */
function isRealmUrl(value: unknown): value is string {
  if (typeof value !== "string") return false;
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    return false;
  }
  // Plain http only for a loopback realm, the rule the server applies to the
  // realm URL itself.
  return url.protocol === "https:" || (url.protocol === "http:" && LOOPBACK_HOSTS.has(url.hostname));
}

/**
 * Where to go once signed out, or `null` if the server did not confirm it.
 *
 * The signed-out page is static and cannot tell anyone their session
 * survived, so a sign-out that failed must not end there: the session cookie
 * may still be live, and the person would leave believing otherwise.
 */
export async function signOut(csrfToken: string, fetchImpl: typeof fetch = fetch): Promise<string | null> {
  try {
    const response = await fetchImpl(SIGNOUT_URL, {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrfToken, Accept: "application/json" },
    });
    if (!response.ok) return null;
    const body: unknown = await response.json();
    const location = typeof body === "object" && body !== null ? (body as { location?: unknown }).location : undefined;
    return isRealmUrl(location) ? location : null;
  } catch {
    return null;
  }
}
