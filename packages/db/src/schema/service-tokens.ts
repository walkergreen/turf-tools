import { text, timestamp, uniqueIndex, uuid } from "drizzle-orm/pg-core";
import { app } from "./app";
import { users } from "./auth/users";

// Bearer tokens for the server-to-server service API (`/api/service/*`),
// minted by an operator script. Only the sha256 of the raw token is stored,
// so a leaked table row cannot be replayed as a credential; the raw value is
// shown once, at mint time. Every token is bound to an actor `users` row
// because the tables the API writes (questions, response options, …) carry
// a NOT NULL `createdBy` — service writes are attributed like any user's.
export const serviceTokens = app.table(
  "service_tokens",
  {
    serviceTokenId: uuid().defaultRandom().primaryKey(),
    name: text().notNull(),
    tokenHash: text().notNull(),
    // Leading characters of the raw token — enough to tell tokens apart in
    // logs and listings, far too short to authenticate with.
    tokenPrefix: text().notNull(),
    actorUserId: uuid()
      .notNull()
      .references(() => users.id),
    createdAt: timestamp({ withTimezone: true }).defaultNow().notNull(),
    lastUsedAt: timestamp({ withTimezone: true }),
    // A revoked token stops authenticating but keeps its row for audit.
    revokedAt: timestamp({ withTimezone: true }),
  },
  (t) => [uniqueIndex("service_tokens_hash").on(t.tokenHash)],
);
