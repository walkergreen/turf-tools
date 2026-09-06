import { asc, eq } from "@turf-tools/db";
import {
  datasetOrganizations,
  datasets,
  ORG_SLUG_PATTERN,
  organizations,
} from "@turf-tools/db/schema";
import { z } from "zod";
import { serviceMut } from "../context";

const slugSchema = z
  .string()
  .regex(ORG_SLUG_PATTERN, "Slug must be lowercase letters, digits, and internal hyphens");

const organizationSelect = {
  organizationId: organizations.organizationId,
  slug: organizations.slug,
  name: organizations.name,
};

// Whether a chapter is set up in Turf Tools. `provisioned` means the org
// exists and holds at least one dataset grant — the signal automations use to
// decide whether Turf Tools fulfilment applies to a request. Grants carry
// their Compliance provenance so the caller can see what was approved.
export const status = serviceMut
  .route({ path: "/organizations/status" })
  .input(z.object({ slug: z.string().min(1) }))
  .handler(async ({ context, input }) => {
    const [org] = await context.db
      .select({
        ...organizationSelect,
        activeDatasetVersionId: organizations.activeDatasetVersionId,
      })
      .from(organizations)
      .where(eq(organizations.slug, input.slug));
    if (!org) return { found: false, provisioned: false, organization: null, grants: [] };

    const grants = await context.db
      .select({
        datasetId: datasets.datasetId,
        datasetSlug: datasets.slug,
        datasetName: datasets.name,
        approvalTicketId: datasetOrganizations.approvalTicketId,
        approvedAt: datasetOrganizations.approvedAt,
        contributionReportedAt: datasetOrganizations.contributionReportedAt,
      })
      .from(datasetOrganizations)
      .innerJoin(datasets, eq(datasets.datasetId, datasetOrganizations.datasetId))
      .where(eq(datasetOrganizations.organizationId, org.organizationId))
      .orderBy(asc(datasets.name));

    return { found: true, provisioned: grants.length > 0, organization: org, grants };
  });

// Create the org if it does not exist. An existing org is returned as is —
// its name is the chapter's to manage in-app, so a later ensure never
// renames it.
export const ensure = serviceMut
  .route({ path: "/organizations/ensure" })
  .input(z.object({ slug: slugSchema, name: z.string().trim().min(1).max(200) }))
  .handler(async ({ context, input }) => {
    const [existing] = await context.db
      .select(organizationSelect)
      .from(organizations)
      .where(eq(organizations.slug, input.slug));
    if (existing) return { created: false, organization: existing };

    // Two ensures racing on a new slug both pass the read above; the unique
    // constraint decides, and the loser reads the winner's row.
    const [inserted] = await context.db
      .insert(organizations)
      .values({ slug: input.slug, name: input.name })
      .onConflictDoNothing({ target: organizations.slug })
      .returning(organizationSelect);
    if (inserted) return { created: true, organization: inserted };

    const [row] = await context.db
      .select(organizationSelect)
      .from(organizations)
      .where(eq(organizations.slug, input.slug));
    return { created: false, organization: row! };
  });
