import { useEffect, useState } from "react";

/**
 * Walk a listing page by page with the cursor the server hands back.
 *
 * The cursors already walked past are kept, newest first, so "Newer" steps
 * back without the server having to page in both directions. `cursor` is
 * `null` on the first page.
 */
export function usePages() {
  const [cursors, setCursors] = useState<number[]>([]);
  return {
    cursor: cursors.length ? cursors[cursors.length - 1] : null,
    older: (next: number) => setCursors((c) => [...c, next]),
    newer: () => setCursors((c) => c.slice(0, -1)),
    hasNewer: cursors.length > 0,
    reset: () => setCursors([]),
  };
}

export function Pager({ pages, next }: { pages: ReturnType<typeof usePages>; next: number | null }) {
  if (!pages.hasNewer && next === null) return null;
  return (
    <p className="pager">
      {pages.hasNewer && (
        <button type="button" className="link" onClick={pages.newer}>
          Newer
        </button>
      )}{" "}
      {next !== null && (
        <button type="button" className="link" onClick={() => pages.older(next)}>
          Older
        </button>
      )}
    </p>
  );
}

/** *value*, once it has stopped changing for *ms*: one request per pause in typing. */
export function useDebounced<T>(value: T, ms: number): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), ms);
    return () => clearTimeout(timer);
  }, [value, ms]);
  return settled;
}
