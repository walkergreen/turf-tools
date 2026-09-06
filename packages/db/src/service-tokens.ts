import { createHash, randomBytes } from "node:crypto";

// Raw service tokens: a recognisable `tt_` marker followed by 32 random
// bytes, base64url. The marker makes a token easy to spot in a paste or a
// log scrubber. These helpers sit beside the schema because the web tier
// (authentication) and the operator scripts (minting) must hash identically.

const RAW_TOKEN_MARKER = "tt_";
const TOKEN_PREFIX_LENGTH = 12;

export function generateServiceToken(): string {
  return `${RAW_TOKEN_MARKER}${randomBytes(32).toString("base64url")}`;
}

// What `service_tokens.token_hash` stores. Plain sha256 is sufficient: the
// raw token carries 256 bits of entropy, so there is nothing to brute-force
// and no per-token salt is needed.
export function hashServiceToken(raw: string): string {
  return createHash("sha256").update(raw).digest("hex");
}

// What `service_tokens.token_prefix` stores — an identifier, not a secret.
export function serviceTokenPrefix(raw: string): string {
  return raw.slice(0, TOKEN_PREFIX_LENGTH);
}
