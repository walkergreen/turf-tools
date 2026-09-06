import meow from "meow";
import { db, eq, generateServiceToken, hashServiceToken, serviceTokenPrefix } from "@turf-tools/db";
import { serviceTokens, users } from "@turf-tools/db/schema";
import { createLogger } from "./_logging";
import { normalizeEmail } from "./_normalize-email";

const log = createLogger("create-service-token");

const cli = meow(
  `
  Usage
    $ pnpm service-token:create --name <name> --actor-email <email> [--actor-name <name>]

  Options
    --name         Token name, shown in logs and listings (e.g. zapctl-prod)
    --actor-email  Email of the user the token acts as; created if missing
    --actor-name   Display name for a newly created actor user
                   (default: "Org Tools automation")

  Examples
    $ pnpm service-token:create --name zapctl-prod --actor-email automation@example.org

  Mints a bearer token for the service API (/api/service/*). The raw token is
  printed once; the database keeps only its sha256 hash. Rows the API creates
  are attributed to the actor user, who has no membership and so cannot log in.
`,
  {
    importMeta: import.meta,
    flags: {
      name: { type: "string" },
      actorEmail: { type: "string" },
      actorName: { type: "string", default: "Org Tools automation" },
    },
  },
);

const name = cli.flags.name?.trim() ?? "";
const rawActorEmail = cli.flags.actorEmail?.trim() ?? "";

if (!name || !rawActorEmail) {
  cli.showHelp(1);
}

const actorEmail = normalizeEmail(rawActorEmail);

const [existingActor] = await db
  .select({ id: users.id })
  .from(users)
  .where(eq(users.email, actorEmail));

let actorUserId: string;
if (existingActor) {
  actorUserId = existingActor.id;
  log.info(`actor user exists (${actorEmail}, id=${actorUserId})`);
} else {
  const [row] = await db
    .insert(users)
    .values({
      email: actorEmail,
      displayEmail: rawActorEmail.toLowerCase(),
      emailVerified: true,
      name: cli.flags.actorName,
    })
    .returning({ id: users.id });
  actorUserId = row.id;
  log.info(`created actor user: ${cli.flags.actorName} <${actorEmail}> (id=${actorUserId})`);
}

const raw = generateServiceToken();
const prefix = serviceTokenPrefix(raw);

const [token] = await db
  .insert(serviceTokens)
  .values({ name, tokenHash: hashServiceToken(raw), tokenPrefix: prefix, actorUserId })
  .returning({ serviceTokenId: serviceTokens.serviceTokenId });

log.success(`created service token "${name}" (prefix=${prefix}, id=${token.serviceTokenId})`);
console.log("");
console.log("  Store this token now. It is not recoverable and will not be shown again:");
console.log("");
console.log(`  ${raw}`);
console.log("");
process.exit(0);
