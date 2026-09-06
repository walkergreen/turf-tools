import meow from "meow";
import { and, db, eq, isNull } from "@turf-tools/db";
import { memberships, organizations, users } from "@turf-tools/db/schema";
import { createLogger } from "./_logging";
import { normalizeEmail } from "./_normalize-email";

const log = createLogger("add-user");

const cli = meow(
  `
  Usage
    $ pnpm prod:add-user [<slug> <name> <email>] [--role <role>]
    $ pnpm prod:add-user --slug <slug> --name <name> --email <email> [--role <role>]

  Options
    --slug    Org slug to add the user to
    --name    User's display name
    --email   User's email
    --role    Role (default: owner)

  Examples
    $ pnpm prod:add-user myorg 'Jane Doe' jane@example.com
    $ pnpm prod:add-user myorg 'Jane Doe' jane@example.com --role member
`,
  {
    importMeta: import.meta,
    flags: {
      slug: { type: "string" },
      name: { type: "string" },
      email: { type: "string" },
      role: { type: "string", default: "owner" },
    },
  },
);

const slug = cli.flags.slug ?? cli.input[0];
const name = cli.flags.name ?? cli.input[1];
const rawEmail = cli.flags.email ?? cli.input[2];
const role = cli.flags.role;

if (!slug || !name || !rawEmail) {
  cli.showHelp(1);
}

const email = normalizeEmail(rawEmail);

const [org] = await db
  .select({ organizationId: organizations.organizationId })
  .from(organizations)
  .where(eq(organizations.slug, slug));

if (!org) {
  log.error(`no organization with slug "${slug}"`);
  process.exit(1);
}

const [existingUser] = await db.select({ id: users.id }).from(users).where(eq(users.email, email));

let userId: string;
if (existingUser) {
  userId = existingUser.id;
  log.info(`user already exists (${email}, id=${userId}); adding membership`);
} else {
  const [row] = await db
    .insert(users)
    .values({
      email,
      displayEmail: rawEmail,
      emailVerified: true,
      name,
    })
    .returning({ id: users.id });
  userId = row.id;
  log.info(`created user: ${name} <${rawEmail}> (id=${userId})`);
}

const [existingMembership] = await db
  .select({ membershipId: memberships.membershipId })
  .from(memberships)
  .where(
    and(
      eq(memberships.userId, userId),
      eq(memberships.organizationId, org.organizationId),
      isNull(memberships.archivedAt),
    ),
  );

if (existingMembership) {
  log.error(`user already has active membership in "${slug}"`);
  process.exit(1);
}

await db.insert(memberships).values({
  userId,
  organizationId: org.organizationId,
  role,
});

log.success(`added ${rawEmail} to ${slug} as ${role}`);
process.exit(0);
