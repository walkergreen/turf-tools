import { text, timestamp, uniqueIndex, uuid } from "drizzle-orm/pg-core";
import { app } from "./app";
import { users } from "./auth/users";
import { datasets } from "./datasets";
import { organizations } from "./organizations";

// Access grant: which orgs can reference a dataset. Two rows on one dataset =
// shared. Gates visibility only — the active version is per-org
// (`organizations.activeDatasetVersionId`), so shared orgs activate independently.
// Lives in its own file (like `memberships`) so `datasets` needn't import
// `organizations`, keeping the two entity modules free of an import cycle.
export const datasetOrganizations = app.table(
  "dataset_organizations",
  {
    datasetOrganizationId: uuid().defaultRandom().primaryKey(),
    datasetId: uuid()
      .notNull()
      .references(() => datasets.datasetId),
    organizationId: uuid()
      .notNull()
      .references(() => organizations.organizationId),
    createdAt: timestamp({ withTimezone: true }).defaultNow().notNull(),
    // Compliance approval provenance, recorded by the service API when a
    // grant fulfils an approved Zendesk request: the ticket, when Compliance
    // approved, when the chapter's contribution was reported, any note, and
    // the actor user the service token acts as. All null on grants made
    // in-app or by the dev seed.
    approvalTicketId: text(),
    approvedAt: timestamp({ withTimezone: true }),
    contributionReportedAt: timestamp({ withTimezone: true }),
    approvalNote: text(),
    grantedByUserId: uuid().references(() => users.id),
  },
  (t) => [uniqueIndex("dataset_organizations_dataset_org").on(t.datasetId, t.organizationId)],
);
