import { createRouterClient } from "@orpc/server";
import { PGlite } from "@electric-sql/pglite";
import { drizzle } from "drizzle-orm/pglite";
import { hashServiceToken, serviceTokenPrefix, type Db } from "@turf-tools/db";
import {
  datasetOrganizations,
  datasets,
  datasetVersions,
  memberships,
  organizations,
  serviceTokens,
  users,
} from "@turf-tools/db/schema";
import { serviceRouter } from "../src/rpc";
import type { ServiceContext } from "../src/rpc/context";

// The subset of the Drizzle schema the service API touches, as the DDL
// `pnpm db:push` would produce (snake_case columns, `app` schema), plus the
// verifications table the sign-in email path writes its OTP to. Kept in
// step with packages/db/src/schema by hand — PGlite has no push step.
export const SERVICE_SCHEMA_SQL = `
CREATE SCHEMA app;

CREATE TABLE app.users (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email text NOT NULL,
  display_email text NOT NULL,
  email_verified boolean NOT NULL DEFAULT false,
  name text NOT NULL,
  image text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  last_login_at timestamptz,
  display_timezone text
);
CREATE UNIQUE INDEX users_email ON app.users (email);

CREATE TABLE app.datasets (
  dataset_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  slug text NOT NULL,
  name text NOT NULL,
  importer text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX datasets_slug ON app.datasets (slug);

CREATE TABLE app.dataset_versions (
  dataset_version_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  dataset_id uuid NOT NULL REFERENCES app.datasets(dataset_id),
  version_number integer NOT NULL,
  manifest jsonb,
  derived_metadata jsonb,
  source_uri text,
  import_step integer,
  import_total_steps integer,
  status text NOT NULL DEFAULT 'importing',
  error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  created_by uuid REFERENCES app.users(id),
  archived_at timestamptz
);
CREATE UNIQUE INDEX dataset_versions_number ON app.dataset_versions (dataset_id, version_number);

CREATE TABLE app.organizations (
  organization_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  slug text NOT NULL UNIQUE,
  name text NOT NULL,
  active_dataset_version_id uuid REFERENCES app.dataset_versions(dataset_version_id),
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT slug_format CHECK (slug ~ '^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$')
);

CREATE TABLE app.memberships (
  membership_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES app.users(id),
  organization_id uuid NOT NULL REFERENCES app.organizations(organization_id),
  role text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  archived_at timestamptz,
  last_accessed_at timestamptz
);
CREATE UNIQUE INDEX memberships_user_org ON app.memberships (user_id, organization_id);

CREATE TABLE app.dataset_organizations (
  dataset_organization_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  dataset_id uuid NOT NULL REFERENCES app.datasets(dataset_id),
  organization_id uuid NOT NULL REFERENCES app.organizations(organization_id),
  created_at timestamptz NOT NULL DEFAULT now(),
  approval_ticket_id text,
  approved_at timestamptz,
  contribution_reported_at timestamptz,
  approval_note text,
  granted_by_user_id uuid REFERENCES app.users(id)
);
CREATE UNIQUE INDEX dataset_organizations_dataset_org
  ON app.dataset_organizations (dataset_id, organization_id);

CREATE TABLE app.questions (
  question_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL REFERENCES app.organizations(organization_id),
  name text NOT NULL,
  response_type text NOT NULL,
  text text NOT NULL,
  created_by uuid NOT NULL REFERENCES app.users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  archived_at timestamptz
);

CREATE TABLE app.response_options (
  response_option_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  question_id uuid NOT NULL REFERENCES app.questions(question_id),
  text text NOT NULL,
  "order" integer NOT NULL,
  created_by uuid NOT NULL REFERENCES app.users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  archived_at timestamptz
);

CREATE TABLE app.verifications (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  identifier text NOT NULL,
  value text NOT NULL,
  expires_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE app.service_tokens (
  service_token_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL,
  token_hash text NOT NULL,
  token_prefix text NOT NULL,
  actor_user_id uuid NOT NULL REFERENCES app.users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  last_used_at timestamptz,
  revoked_at timestamptz
);
CREATE UNIQUE INDEX service_tokens_hash ON app.service_tokens (token_hash);
`;

// A raw token with the real shape; tests mint it into the test db.
export const TEST_RAW_TOKEN = "tt_dGVzdC10b2tlbi1ub3Qtc2VjcmV0LWp1c3QtZm9yLXRlc3Rz";

export async function createServiceTestDb() {
  const pglite = new PGlite();
  await pglite.exec(SERVICE_SCHEMA_SQL);
  const db = drizzle({ client: pglite, casing: "snake_case" }) as unknown as Db;

  const [actor] = await db
    .insert(users)
    .values({
      email: "automation@example.org",
      displayEmail: "automation@example.org",
      emailVerified: true,
      name: "Org Tools automation",
    })
    .returning();
  const [token] = await db
    .insert(serviceTokens)
    .values({
      name: "test-token",
      tokenHash: hashServiceToken(TEST_RAW_TOKEN),
      tokenPrefix: serviceTokenPrefix(TEST_RAW_TOKEN),
      actorUserId: actor!.id,
    })
    .returning({ serviceTokenId: serviceTokens.serviceTokenId, name: serviceTokens.name });

  const context: ServiceContext = { db, actor: actor!, token: token! };
  const caller = createRouterClient(serviceRouter, { context });

  return {
    db,
    actor: actor!,
    context,
    caller,
    stop: () => pglite.close(),
  };
}

export type ServiceTestDb = Awaited<ReturnType<typeof createServiceTestDb>>;

// --- Seed helpers: each returns the inserted row ---

export async function seedOrg(db: Db, slug: string, name = slug) {
  const [row] = await db.insert(organizations).values({ slug, name }).returning();
  return row!;
}

export async function seedDataset(db: Db, slug: string, name = slug, importer = "targetsmart") {
  const [row] = await db.insert(datasets).values({ slug, name, importer }).returning();
  return row!;
}

export async function seedVersion(
  db: Db,
  datasetId: string,
  versionNumber: number,
  status: "importing" | "ready" | "failed" = "ready",
  archived = false,
) {
  const [row] = await db
    .insert(datasetVersions)
    .values({ datasetId, versionNumber, status, archivedAt: archived ? new Date() : null })
    .returning();
  return row!;
}

export async function seedUser(db: Db, email: string, name = "Test User") {
  const [row] = await db.insert(users).values({ email, displayEmail: email, name }).returning();
  return row!;
}

export async function seedMembership(
  db: Db,
  userId: string,
  organizationId: string,
  role: string,
  archivedAt: Date | null = null,
) {
  const [row] = await db
    .insert(memberships)
    .values({ userId, organizationId, role, archivedAt })
    .returning();
  return row!;
}

export async function seedGrant(
  db: Db,
  datasetId: string,
  organizationId: string,
  provenance: Partial<typeof datasetOrganizations.$inferInsert> = {},
) {
  const [row] = await db
    .insert(datasetOrganizations)
    .values({ datasetId, organizationId, ...provenance })
    .returning();
  return row!;
}
