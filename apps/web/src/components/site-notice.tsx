// A one-line notice pinned above every page, login included, for deployments
// that need a standing disclaimer (an evaluation instance, a data-use
// restriction). The text comes from VITE_SITE_NOTICE at build time; when it is
// unset nothing renders.
const NOTICE = (import.meta.env.VITE_SITE_NOTICE ?? "").trim();

export function SiteNotice() {
  if (NOTICE.length === 0) return null;
  return (
    <div
      role="status"
      className="border-b border-amber-300 bg-amber-100 px-4 py-2 text-center text-sm font-medium text-amber-950 dark:border-amber-700 dark:bg-amber-950 dark:text-amber-100"
    >
      {NOTICE}
    </div>
  );
}
