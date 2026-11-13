/**
 * One way of asking the admin API a question.
 *
 * The states are kept apart because the panel says something different for
 * each, and collapsing them is how a screen ends up showing "no models" when
 * the truth is "the serving layer is down". That distinction is the whole
 * reason the endpoints answer 503 and 502 rather than an empty list.
 */

export type Resource<T> =
  | { state: "ok"; data: T }
  | { state: "forbidden" }
  /** The session ran out while the panel was open; signing in again fixes it. */
  | { state: "signed-out" }
  /** The request was understood and declined, and the server said why. */
  | { state: "refused"; reason: string }
  | { state: "unavailable"; reason: string }
  | { state: "upstream-error"; reason: string }
  | { state: "error" };

/**
 * The server's own word for what happened.
 *
 * Reads `outcome` before `error`: endpoints that can decline for several
 * distinct reasons name them in `outcome`, and flattening those to a generic
 * failure would lose the difference between "that address already has a live
 * invitation" and "the hub is broken".
 */
/** Raised on `window` when any call finds the session gone, so the panel as a
 * whole says so once, and no screen reports it as a failure. */
export const SIGNED_OUT_EVENT = "collab-admin:signed-out";

function signedOut(): { state: "signed-out" } {
  if (typeof window !== "undefined") window.dispatchEvent(new Event(SIGNED_OUT_EVENT));
  return { state: "signed-out" };
}

async function reason(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { outcome?: unknown; error?: unknown };
    if (typeof body.outcome === "string") return body.outcome;
    if (typeof body.error === "string") return body.error;
    // The hub's standard error body, `{"error": {"code": ..., "message": ...}}`,
    // which is what a refusal raised below an endpoint answers with.
    const code = (body.error as { code?: unknown } | null | undefined)?.code;
    return typeof code === "string" ? code : "unknown";
  } catch {
    return "unknown";
  }
}

export async function getJson<T>(path: string, fetchImpl: typeof fetch = fetch): Promise<Resource<T>> {
  let response: Response;
  try {
    response = await fetchImpl(path, {
      headers: { accept: "application/json" },
      credentials: "same-origin",
    });
  } catch {
    return { state: "error" };
  }

  if (response.status === 401) return signedOut();
  if (response.status === 403) return { state: "forbidden" };
  // 503 is "this deployment cannot answer" (no database, no credential); 502 is
  // "the thing behind us did not". An operator needs to tell those apart: one
  // is configuration, the other is an outage somewhere else.
  if (response.status === 503) return { state: "unavailable", reason: await reason(response) };
  if (response.status === 502) return { state: "upstream-error", reason: await reason(response) };
  if (response.status === 400 || response.status === 409) {
    return { state: "refused", reason: await reason(response) };
  }
  if (!response.ok) return { state: "error" };

  try {
    return { state: "ok", data: (await response.json()) as T };
  } catch {
    return { state: "error" };
  }
}

export async function postJson(
  path: string,
  body: unknown,
  csrfToken: string,
  fetchImpl: typeof fetch = fetch,
): Promise<Resource<unknown>> {
  let response: Response;
  try {
    response = await fetchImpl(path, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "content-type": "application/json",
        accept: "application/json",
        // The session cookie authenticates; this proves the request came from
        // the panel rather than from a page that merely knows the cookie is
        // there. The token itself is read from the session endpoint, because
        // the cookie holding it is HttpOnly.
        "X-CSRF-Token": csrfToken,
      },
      body: JSON.stringify(body),
    });
  } catch {
    return { state: "error" };
  }

  if (response.status === 401) return signedOut();
  if (response.status === 403) return { state: "forbidden" };
  if (response.status === 503) return { state: "unavailable", reason: await reason(response) };
  if (response.status === 502) return { state: "upstream-error", reason: await reason(response) };
  if (response.status === 400 || response.status === 409) {
    return { state: "refused", reason: await reason(response) };
  }
  if (!response.ok) return { state: "error" };
  // A success can still carry news -- an invitation created whose email did
  // not go out is a 201 -- so the body is handed back rather than dropped.
  try {
    const text = await response.text();
    return { state: "ok", data: text ? (JSON.parse(text) as unknown) : null };
  } catch {
    return { state: "ok", data: null };
  }
}
