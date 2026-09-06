import { ORPCError } from "@orpc/server";
import { and, asc, desc, eq, isNull, type Db } from "@turf-tools/db";
import {
  datasetOrganizations,
  datasets,
  datasetVersions,
  organizations,
} from "@turf-tools/db/schema";
import { z } from "zod";
import { serviceMut } from "../context";
import { resolveOrg } from "./resolve-org";

// The newest `ready`, unarchived version — what a fresh grant activates.
async function latestReadyVersionId(db: Db, datasetId: string): Promise<string | null> {
  const [row] = await db
    .select({ datasetVersionId: datasetVersions.datasetVersionId })
    .from(datasetVersions)
    .where(
      and(
        eq(datasetVersions.datasetId, datasetId),
        eq(datasetVersions.status, "ready"),
        isNull(datasetVersions.archivedAt),
      ),
    )
    .orderBy(desc(datasetVersions.versionNumber))
    .limit(1);
  return row?.datasetVersionId ?? null;
}

// Every dataset on the deployment, with the version a grant would activate.
// Datasets are deployment-level, so this is not scoped to any org.
export const list = serviceMut
  .route({ path: "/datasets/list" })
  .input(z.object({}).optional())
  .handler(async ({ context }) => {
    const rows = await context.db
      .select({
        datasetId: datasets.datasetId,
        slug: datasets.slug,
        name: datasets.name,
        importer: datasets.importer,
      })
      .from(datasets)
      .orderBy(asc(datasets.name));
    // Newest first, so the first version seen per dataset is its latest ready one.
    const ready = await context.db
      .select({
        datasetId: datasetVersions.datasetId,
        datasetVersionId: datasetVersions.datasetVersionId,
      })
      .from(datasetVersions)
      .where(and(eq(datasetVersions.status, "ready"), isNull(datasetVersions.archivedAt)))
      .orderBy(desc(datasetVersions.versionNumber));
    const latestReady = new Map<string, string>();
    for (const v of ready) {
      if (!latestReady.has(v.datasetId)) latestReady.set(v.datasetId, v.datasetVersionId);
    }
    return {
      datasets: rows.map((row) => ({
        ...row,
        latestReadyVersionId: latestReady.get(row.datasetId) ?? null,
      })),
    };
  });

// A provenance timestamp on the wire: an ISO date string, or `null` for one
// the caller does not know (organizations/status hands nulls back, and a
// caller may echo them). Numbers and booleans are rejected rather than
// coerced, since `new Date(0)` would record a 1970 approval for good.
const provenanceDate = z
  .string()
  .pipe(z.coerce.date())
  .nullish()
  .transform((value) => value ?? undefined);

// Grant a dataset to an org on Compliance's approval, recording where that
// approval came from. Idempotent: a grant that already exists is kept and
// only its still-unknown provenance columns are filled — the first recorded
// approval stands. With `activate`, an org that has nothing active yet is
// pointed at the dataset's latest ready version so it is usable at once.
export const grant = serviceMut
  .route({ path: "/datasets/grant" })
  .input(
    z.object({
      orgSlug: z.string().min(1),
      datasetSlug: z.string().min(1),
      // Zendesk ticket ids are decimal, and the value is shown verbatim to
      // the chapter's admins on the Data page.
      approvalTicketId: z
        .string()
        .trim()
        .regex(/^\d{1,20}$/, "approvalTicketId must be a Zendesk ticket number"),
      approvedAt: provenanceDate,
      contributionReportedAt: provenanceDate,
      note: z.string().max(2000).optional(),
      activate: z.boolean().default(true),
    }),
  )
  .handler(async ({ context, input }) => {
    const org = await resolveOrg(context.db, input.orgSlug);
    const [dataset] = await context.db
      .select({ datasetId: datasets.datasetId })
      .from(datasets)
      .where(eq(datasets.slug, input.datasetSlug));
    if (!dataset) throw new ORPCError("NOT_FOUND", { message: "Dataset not found" });

    const provenance = {
      approvalTicketId: input.approvalTicketId,
      approvedAt: input.approvedAt ?? new Date(),
      contributionReportedAt: input.contributionReportedAt ?? null,
      approvalNote: input.note ?? null,
      grantedByUserId: context.actor.id,
    };
    const readyVersionId = input.activate
      ? await latestReadyVersionId(context.db, dataset.datasetId)
      : null;

    return context.db.transaction(async (tx) => {
      // The unique (dataset, org) index arbitrates concurrent grants: only
      // one insert lands, and everyone else takes the fill-nulls path.
      const [inserted] = await tx
        .insert(datasetOrganizations)
        .values({ datasetId: dataset.datasetId, organizationId: org.organizationId, ...provenance })
        .onConflictDoNothing({
          target: [datasetOrganizations.datasetId, datasetOrganizations.organizationId],
        })
        .returning({ datasetOrganizationId: datasetOrganizations.datasetOrganizationId });

      let datasetOrganizationId: string;
      if (inserted) {
        datasetOrganizationId = inserted.datasetOrganizationId;
      } else {
        const [existing] = await tx
          .select()
          .from(datasetOrganizations)
          .where(
            and(
              eq(datasetOrganizations.datasetId, dataset.datasetId),
              eq(datasetOrganizations.organizationId, org.organizationId),
            ),
          );
        datasetOrganizationId = existing!.datasetOrganizationId;
        await tx
          .update(datasetOrganizations)
          .set({
            approvalTicketId: existing!.approvalTicketId ?? provenance.approvalTicketId,
            approvedAt: existing!.approvedAt ?? provenance.approvedAt,
            contributionReportedAt:
              existing!.contributionReportedAt ?? provenance.contributionReportedAt,
            approvalNote: existing!.approvalNote ?? provenance.approvalNote,
            grantedByUserId: existing!.grantedByUserId ?? provenance.grantedByUserId,
          })
          .where(eq(datasetOrganizations.datasetOrganizationId, datasetOrganizationId));
      }

      let activated = false;
      if (readyVersionId) {
        // Guarded on the null pointer so an activation made meanwhile — in
        // the app or by another call — is never overwritten.
        const updated = await tx
          .update(organizations)
          .set({ activeDatasetVersionId: readyVersionId })
          .where(
            and(
              eq(organizations.organizationId, org.organizationId),
              isNull(organizations.activeDatasetVersionId),
            ),
          )
          .returning({ organizationId: organizations.organizationId });
        activated = updated.length > 0;
      }

      return { created: inserted != null, datasetOrganizationId, activated };
    });
  });
