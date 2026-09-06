// Canonical form used for user lookup + uniqueness, mirrors
// apps/web/src/lib/normalize-email.ts. Better Auth's magic-link flow
// queries `users.email` after running the typed input through this same
// transformation, so any value a script inserts must match exactly.
export function normalizeEmail(input: string): string {
  const trimmed = input.trim().toLowerCase();
  const at = trimmed.lastIndexOf("@");
  if (at < 0) return trimmed;
  const local = trimmed.slice(0, at);
  let domain = trimmed.slice(at + 1);
  if (domain === "googlemail.com") domain = "gmail.com";
  if (domain !== "gmail.com") return `${local}@${domain}`;
  const plusIdx = local.indexOf("+");
  const beforePlus = plusIdx >= 0 ? local.slice(0, plusIdx) : local;
  const afterPlus = plusIdx >= 0 ? local.slice(plusIdx) : "";
  return `${beforePlus.replace(/\./g, "")}${afterPlus}@${domain}`;
}
