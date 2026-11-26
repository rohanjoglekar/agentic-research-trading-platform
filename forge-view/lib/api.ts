// The demo's API client. Calls go to /api/*, which Next rewrites to the public
// read-only API (see next.config.mjs).

/** SWR fetcher: JSON on success, a thrown error (with the status) otherwise. */
export const fetcher = (url: string) =>
  fetch(url, { cache: "no-store" }).then((r) => {
    if (!r.ok) throw new Error(`${url} -> ${r.status}`);
    return r.json();
  });
