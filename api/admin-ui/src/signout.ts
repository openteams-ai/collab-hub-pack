/**
 * Ending the session, through the route the rest of the surface already uses.
 *
 * `POST /web/signout` is the existing sign-out, and it clears the session
 * cookie server-side. The server-rendered pages reach it with an HTML form; the
 * panel posts it with `fetch` instead, because its Content-Security-Policy sets
 * `form-action 'none'` and should keep doing so -- a surface that submits no
 * forms is a surface a markup-injection bug cannot use to post anywhere.
 *
 * `require_csrf` reads the `X-CSRF-Token` header before it falls back to form
 * fields, so this needs nothing new on the server.
 */

export const SIGNOUT_URL = "../web/signout";
// `next` sends "Sign in again" back to the panel. It is app-relative (the
// server prefixes the deployment's root path) and the server checks it against
// the same allowlist as the sign-in redirect.
export const SIGNED_OUT_URL = "../web/signed-out?next=%2Fadmin%2F";

/** End the session, and say whether the server confirmed it. */
export async function endSession(csrfToken: string, fetchImpl: typeof fetch = fetch): Promise<boolean> {
  try {
    const response = await fetchImpl(SIGNOUT_URL, {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrfToken },
    });
    return response.ok || (response.status >= 300 && response.status < 400);
  } catch {
    return false;
  }
}

/**
 * Where to go once signed out, or `null` if the server did not confirm it.
 *
 * The signed-out page is static and cannot tell anyone their session
 * survived, so a sign-out that failed must not end there: the session cookie
 * may still be live, and the person would leave believing otherwise.
 */
export async function signOut(csrfToken: string, fetchImpl: typeof fetch = fetch): Promise<string | null> {
  return (await endSession(csrfToken, fetchImpl)) ? SIGNED_OUT_URL : null;
}
