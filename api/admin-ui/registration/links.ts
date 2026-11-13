/**
 * Where the page sends people, relative to the document at `/invite/accept`.
 *
 * Relative so the app works under a root-path prefix without knowing it.
 * `next` is the exception: it is app-relative, the server prefixes the
 * deployment's root path, and it checks the value against its own allowlist.
 *
 * None of these carries the invitation code. The code stays in this tab's
 * storage while the person signs in, and the sign-in flow returns them here.
 */

const SIGN_IN = `../web/signin?next=${encodeURIComponent("/invite/accept")}`;

/** Start on the identity provider's sign-in form. */
export const SIGN_IN_URL = SIGN_IN;

/**
 * Start on its registration form instead. An invitee has no account yet, so
 * creating one is the main path and signing in is the exception.
 */
export const CREATE_ACCOUNT_URL = `${SIGN_IN}&register=1`;

/**
 * Run the sign-in flow even though a session exists, which is the only way to
 * obtain a current assertion of the verified email address.
 */
export const RENEW_SIGN_IN_URL = `${SIGN_IN}&renew=1`;

export const DATA_STATEMENT_URL = "../web/data-statement";
