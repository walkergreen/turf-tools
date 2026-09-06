import { ORPCError } from "@orpc/server";
import { eq, type Db } from "@turf-tools/db";
import { organizations } from "@turf-tools/db/schema";

export type OrganizationRow = typeof organizations.$inferSelect;

// Service calls name their org by slug; every handler resolves it the same
// way and answers a miss with the same 404.
export async function resolveOrg(db: Db, slug: string): Promise<OrganizationRow> {
  const [org] = await db.select().from(organizations).where(eq(organizations.slug, slug));
  if (!org) throw new ORPCError("NOT_FOUND", { message: "Organization not found" });
  return org;
}
