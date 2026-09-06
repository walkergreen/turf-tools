import { eq } from "@turf-tools/db";
import { serviceTokens } from "@turf-tools/db/schema";
import { afterAll, beforeAll, describe, expect, test } from "vite-plus/test";
import {
  authenticateServiceToken,
  generateServiceToken,
  hashServiceToken,
} from "../src/lib/service-auth";
import { createServiceTestDb, TEST_RAW_TOKEN, type ServiceTestDb } from "./service.setup";

let t: ServiceTestDb;
beforeAll(async () => {
  t = await createServiceTestDb();
});
afterAll(async () => {
  await t.stop();
});

const bearer = (raw: string) => new Headers({ authorization: `Bearer ${raw}` });

describe("token primitives", () => {
  test("generated tokens carry the marker and are unique", () => {
    const a = generateServiceToken();
    const b = generateServiceToken();
    expect(a.startsWith("tt_")).toBe(true);
    expect(a).not.toBe(b);
    expect(a.length).toBeGreaterThan(40);
  });

  test("hash is a stable sha256 hex digest", () => {
    expect(hashServiceToken("tt_x")).toBe(hashServiceToken("tt_x"));
    expect(hashServiceToken("tt_x")).toMatch(/^[0-9a-f]{64}$/);
    expect(hashServiceToken("tt_x")).not.toBe(hashServiceToken("tt_y"));
  });
});

describe("authenticateServiceToken", () => {
  test("valid token resolves the actor and token name", async () => {
    const ctx = await authenticateServiceToken(t.db, bearer(TEST_RAW_TOKEN));
    expect(ctx).not.toBeNull();
    expect(ctx!.actor.id).toBe(t.actor.id);
    expect(ctx!.token.name).toBe("test-token");
    expect(ctx!.db).toBe(t.db);
  });

  test("stamps lastUsedAt on success", async () => {
    const before = new Date();
    await authenticateServiceToken(t.db, bearer(TEST_RAW_TOKEN));
    const [row] = await t.db
      .select({ lastUsedAt: serviceTokens.lastUsedAt })
      .from(serviceTokens)
      .where(eq(serviceTokens.serviceTokenId, t.context.token.serviceTokenId));
    expect(row!.lastUsedAt).not.toBeNull();
    expect(row!.lastUsedAt!.getTime()).toBeGreaterThanOrEqual(before.getTime() - 1000);
  });

  test("header scheme is case-insensitive", async () => {
    const ctx = await authenticateServiceToken(
      t.db,
      new Headers({ authorization: `bearer ${TEST_RAW_TOKEN}` }),
    );
    expect(ctx?.token.name).toBe("test-token");
  });

  test("unknown token is rejected", async () => {
    expect(await authenticateServiceToken(t.db, bearer(generateServiceToken()))).toBeNull();
  });

  test("missing or malformed header is rejected", async () => {
    expect(await authenticateServiceToken(t.db, new Headers())).toBeNull();
    expect(
      await authenticateServiceToken(t.db, new Headers({ authorization: TEST_RAW_TOKEN })),
    ).toBeNull();
    expect(
      await authenticateServiceToken(t.db, new Headers({ authorization: "Basic abc" })),
    ).toBeNull();
    expect(
      await authenticateServiceToken(t.db, new Headers({ authorization: "Bearer " })),
    ).toBeNull();
  });

  test("revoked token is rejected", async () => {
    const raw = generateServiceToken();
    await t.db.insert(serviceTokens).values({
      name: "revoked",
      tokenHash: hashServiceToken(raw),
      tokenPrefix: raw.slice(0, 12),
      actorUserId: t.actor.id,
      revokedAt: new Date(),
    });
    expect(await authenticateServiceToken(t.db, bearer(raw))).toBeNull();
  });
});
