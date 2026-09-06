import meow from "meow";
import { and, asc, db, eq, isNull } from "@turf-tools/db";
import { serviceTokens } from "@turf-tools/db/schema";
import { createLogger } from "./_logging";

const log = createLogger("revoke-service-token");

const cli = meow(
  `
  Usage
    $ pnpm service-token:revoke --prefix <prefix>
    $ pnpm service-token:revoke --name <name>
    $ pnpm service-token:revoke --list

  Options
    --prefix  Revoke the token whose stored prefix matches (see --list)
    --name    Revoke the single active token with this name
    --list    Print every token: name, prefix, created, last used, revoked

  Examples
    $ pnpm service-token:revoke --list
    $ pnpm service-token:revoke --prefix tt_AbCdEfGhI

  Revocation sets revoked_at; the row stays for audit and the token stops
  authenticating immediately. Hashes are never printed.
`,
  {
    importMeta: import.meta,
    flags: {
      prefix: { type: "string" },
      name: { type: "string" },
      list: { type: "boolean", default: false },
    },
  },
);

const fmt = (d: Date | null) => (d ? d.toISOString() : "—");

if (cli.flags.list) {
  const rows = await db
    .select({
      name: serviceTokens.name,
      tokenPrefix: serviceTokens.tokenPrefix,
      createdAt: serviceTokens.createdAt,
      lastUsedAt: serviceTokens.lastUsedAt,
      revokedAt: serviceTokens.revokedAt,
    })
    .from(serviceTokens)
    .orderBy(asc(serviceTokens.createdAt));
  if (rows.length === 0) {
    log.info("no service tokens");
  }
  for (const r of rows) {
    log.info(
      `${r.name}  prefix=${r.tokenPrefix}  created=${fmt(r.createdAt)}  lastUsed=${fmt(r.lastUsedAt)}  revoked=${fmt(r.revokedAt)}`,
    );
  }
  process.exit(0);
}

const prefix = cli.flags.prefix?.trim();
const name = cli.flags.name?.trim();

if (!prefix && !name) {
  cli.showHelp(1);
}

const active = await db
  .select({ serviceTokenId: serviceTokens.serviceTokenId, name: serviceTokens.name })
  .from(serviceTokens)
  .where(
    and(
      prefix ? eq(serviceTokens.tokenPrefix, prefix) : eq(serviceTokens.name, name!),
      isNull(serviceTokens.revokedAt),
    ),
  );

if (active.length === 0) {
  log.skip(`no active token matches ${prefix ? `prefix "${prefix}"` : `name "${name}"`}`);
  process.exit(0);
}
if (active.length > 1) {
  log.error(`${active.length} active tokens are named "${name}"; revoke by --prefix instead`);
  process.exit(1);
}

await db
  .update(serviceTokens)
  .set({ revokedAt: new Date() })
  .where(eq(serviceTokens.serviceTokenId, active[0].serviceTokenId));

log.success(`revoked service token "${active[0].name}"`);
process.exit(0);
