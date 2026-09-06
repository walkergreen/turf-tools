import { and, eq, hashServiceToken, isNull, type Db } from "@turf-tools/db";
import { serviceTokens, users } from "@turf-tools/db/schema";
import type { ServiceContext } from "~/rpc/context";

export { generateServiceToken, hashServiceToken } from "@turf-tools/db";

// Resolve `Authorization: Bearer <token>` to a service context. Null when the
// header is missing or malformed, the token is unknown, or it has been
// revoked — the caller answers all of those with the same 401. Only the hash
// ever reaches the database.
export async function authenticateServiceToken(
  db: Db,
  headers: Headers,
): Promise<ServiceContext | null> {
  const raw = bearerToken(headers.get("authorization"));
  if (!raw) return null;

  const [row] = await db
    .select({
      serviceTokenId: serviceTokens.serviceTokenId,
      name: serviceTokens.name,
      actor: users,
    })
    .from(serviceTokens)
    .innerJoin(users, eq(users.id, serviceTokens.actorUserId))
    .where(
      and(eq(serviceTokens.tokenHash, hashServiceToken(raw)), isNull(serviceTokens.revokedAt)),
    );
  if (!row) return null;

  // Usage stamp is best-effort: a failure here must not fail the request.
  try {
    await db
      .update(serviceTokens)
      .set({ lastUsedAt: new Date() })
      .where(eq(serviceTokens.serviceTokenId, row.serviceTokenId));
  } catch (err) {
    console.error("[service-auth] failed to stamp lastUsedAt", err);
  }

  return {
    db,
    actor: row.actor,
    token: { serviceTokenId: row.serviceTokenId, name: row.name },
  };
}

function bearerToken(header: string | null): string | null {
  if (!header) return null;
  const match = /^Bearer\s+(\S+)$/i.exec(header.trim());
  return match?.[1] ?? null;
}
