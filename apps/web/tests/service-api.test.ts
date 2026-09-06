import { inspect } from "node:util";
import { ORPCError } from "@orpc/server";
import { and, eq, type Db } from "@turf-tools/db";
import {
  datasetOrganizations,
  memberships,
  organizations,
  questions,
  responseOptions,
  users,
} from "@turf-tools/db/schema";
import { afterAll, beforeAll, describe, expect, test, vi } from "vite-plus/test";
import { handleServiceRequest } from "../src/rpc/service/http";
import * as signInEmail from "../src/lib/sign-in-email";
import {
  createServiceTestDb,
  seedDataset,
  seedGrant,
  seedMembership,
  seedOrg,
  seedUser,
  seedVersion,
  TEST_RAW_TOKEN,
  type ServiceTestDb,
} from "./service.setup";

// The SMTP transport behind the real sign-in email path, so a test can make
// the send itself fail rather than the seam in front of it.
const mail = vi.hoisted(() => ({ sendMail: vi.fn<(message: unknown) => Promise<unknown>>() }));
vi.mock("nodemailer", () => ({
  default: { createTransport: () => ({ sendMail: mail.sendMail }) },
}));

// Better Auth's hooks and adapter query the package-level `db`; route them
// to this file's PGlite database once it exists (the package's own instance
// answers until then, and only for schema metadata).
const dbRef = vi.hoisted(() => ({ current: null as Db | null }));
vi.mock("@turf-tools/db", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@turf-tools/db")>();
  const db = new Proxy(actual.db, {
    get(fallback, prop) {
      const source = dbRef.current ?? fallback;
      const value = Reflect.get(source, prop);
      return typeof value === "function" ? value.bind(source) : value;
    },
  });
  return { ...actual, db };
});

let t: ServiceTestDb;
beforeAll(async () => {
  t = await createServiceTestDb();
  dbRef.current = t.db;
});
afterAll(async () => {
  await t.stop();
});

async function expectORPCError(promise: Promise<unknown>, code: string) {
  try {
    await promise;
  } catch (err) {
    expect(err).toBeInstanceOf(ORPCError);
    expect((err as ORPCError<string, unknown>).code).toBe(code);
    return;
  }
  throw new Error(`expected ORPCError ${code}`);
}

const url = (path: string) => `http://turftools.test/api/service${path}`;
const post = (path: string, body: unknown, token: string | null = TEST_RAW_TOKEN) =>
  handleServiceRequest(
    new Request(url(path), {
      method: "POST",
      headers: {
        "content-type": "application/json",
        ...(token ? { authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify(body),
    }),
    t.db,
  );

// What `console.error` would print for these calls: Error messages and own
// enumerable fields included, which is where an address would leak.
const printed = (calls: unknown[][]) => inspect(calls, { depth: 8 });

describe("healthcheck", () => {
  test("reports the calling token", async () => {
    expect(await t.caller.healthcheck()).toEqual({
      status: "ok",
      db: "connected",
      token: { name: "test-token" },
    });
  });
});

describe("organizations/ensure", () => {
  test("creates, then returns the existing org without renaming", async () => {
    const first = await t.caller.organizations.ensure({ slug: "ensure-a", name: "Chapter A" });
    expect(first.created).toBe(true);
    expect(first.organization).toMatchObject({ slug: "ensure-a", name: "Chapter A" });

    const second = await t.caller.organizations.ensure({ slug: "ensure-a", name: "Renamed" });
    expect(second.created).toBe(false);
    expect(second.organization).toEqual(first.organization);

    const rows = await t.db.select().from(organizations).where(eq(organizations.slug, "ensure-a"));
    expect(rows).toHaveLength(1);
    expect(rows[0]!.name).toBe("Chapter A");
  });

  test("rejects a slug the database CHECK would reject", async () => {
    for (const slug of ["Upper", "has space", "-leading", "trailing-", "under_score", ""]) {
      await expectORPCError(t.caller.organizations.ensure({ slug, name: "x" }), "BAD_REQUEST");
    }
  });
});

describe("organizations/status", () => {
  test("unknown slug is not found and not provisioned", async () => {
    expect(await t.caller.organizations.status({ slug: "nope" })).toEqual({
      found: false,
      provisioned: false,
      organization: null,
      grants: [],
    });
  });

  test("an org without grants is found but not provisioned", async () => {
    const org = await seedOrg(t.db, "status-empty", "Status Empty");
    const res = await t.caller.organizations.status({ slug: "status-empty" });
    expect(res.found).toBe(true);
    expect(res.provisioned).toBe(false);
    expect(res.organization).toEqual({
      organizationId: org.organizationId,
      slug: "status-empty",
      name: "Status Empty",
      activeDatasetVersionId: null,
    });
    expect(res.grants).toEqual([]);
  });

  test("a granted org is provisioned and echoes provenance", async () => {
    const org = await seedOrg(t.db, "status-granted");
    const ds = await seedDataset(t.db, "targetsmart_ny", "TargetSmart NY");
    const approvedAt = new Date("2026-09-04T12:00:00Z");
    await seedGrant(t.db, ds.datasetId, org.organizationId, {
      approvalTicketId: "12345",
      approvedAt,
      contributionReportedAt: null,
    });
    const res = await t.caller.organizations.status({ slug: "status-granted" });
    expect(res.provisioned).toBe(true);
    expect(res.grants).toEqual([
      {
        datasetId: ds.datasetId,
        datasetSlug: "targetsmart_ny",
        datasetName: "TargetSmart NY",
        approvalTicketId: "12345",
        approvedAt,
        contributionReportedAt: null,
      },
    ]);
  });
});

describe("datasets/list", () => {
  test("lists every dataset with its newest ready, unarchived version", async () => {
    const ds = await seedDataset(t.db, "list_ds", "List DS");
    await seedVersion(t.db, ds.datasetId, 1, "ready");
    const v2 = await seedVersion(t.db, ds.datasetId, 2, "ready");
    await seedVersion(t.db, ds.datasetId, 3, "ready", true);
    await seedVersion(t.db, ds.datasetId, 4, "importing");
    const bare = await seedDataset(t.db, "list_bare", "List Bare");

    const { datasets } = await t.caller.datasets.list({});
    expect(datasets).toContainEqual({
      datasetId: ds.datasetId,
      slug: "list_ds",
      name: "List DS",
      importer: "targetsmart",
      latestReadyVersionId: v2.datasetVersionId,
    });
    expect(datasets).toContainEqual({
      datasetId: bare.datasetId,
      slug: "list_bare",
      name: "List Bare",
      importer: "targetsmart",
      latestReadyVersionId: null,
    });
  });
});

describe("datasets/grant", () => {
  const grantInput = (orgSlug: string, datasetSlug: string) => ({
    orgSlug,
    datasetSlug,
    approvalTicketId: "777",
  });

  test("404s on a missing org or dataset", async () => {
    await seedDataset(t.db, "grant_exists");
    await seedOrg(t.db, "grant-org-exists");
    await expectORPCError(
      t.caller.datasets.grant(grantInput("grant-missing-org", "grant_exists")),
      "NOT_FOUND",
    );
    await expectORPCError(
      t.caller.datasets.grant(grantInput("grant-org-exists", "grant_missing_ds")),
      "NOT_FOUND",
    );
  });

  test("creates a grant with provenance attributed to the actor", async () => {
    const org = await seedOrg(t.db, "grant-create");
    const ds = await seedDataset(t.db, "grant_create_ds");
    const approvedAt = new Date("2026-09-04T10:00:00Z");
    const contributionReportedAt = new Date("2026-09-05T10:00:00Z");
    const res = await t.caller.datasets.grant({
      orgSlug: "grant-create",
      datasetSlug: "grant_create_ds",
      approvalTicketId: "555",
      approvedAt: approvedAt.toISOString(),
      contributionReportedAt: contributionReportedAt.toISOString(),
      note: "Approved in Zendesk; contribution reported",
    });
    expect(res.created).toBe(true);
    expect(res.activated).toBe(false);

    const [row] = await t.db
      .select()
      .from(datasetOrganizations)
      .where(eq(datasetOrganizations.datasetOrganizationId, res.datasetOrganizationId));
    expect(row).toMatchObject({
      datasetId: ds.datasetId,
      organizationId: org.organizationId,
      approvalTicketId: "555",
      approvedAt,
      contributionReportedAt,
      approvalNote: "Approved in Zendesk; contribution reported",
      grantedByUserId: t.actor.id,
    });
  });

  test("approvedAt defaults to now", async () => {
    await seedOrg(t.db, "grant-default-time");
    await seedDataset(t.db, "grant_default_time");
    const before = Date.now();
    const res = await t.caller.datasets.grant(
      grantInput("grant-default-time", "grant_default_time"),
    );
    const [row] = await t.db
      .select({ approvedAt: datasetOrganizations.approvedAt })
      .from(datasetOrganizations)
      .where(eq(datasetOrganizations.datasetOrganizationId, res.datasetOrganizationId));
    expect(row!.approvedAt!.getTime()).toBeGreaterThanOrEqual(before - 1000);
  });

  test("a repeat keeps the first approval and fills only null columns", async () => {
    await seedOrg(t.db, "grant-repeat");
    await seedDataset(t.db, "grant_repeat");
    const firstApproved = new Date("2026-09-01T00:00:00Z");
    const first = await t.caller.datasets.grant({
      orgSlug: "grant-repeat",
      datasetSlug: "grant_repeat",
      approvalTicketId: "100",
      approvedAt: firstApproved.toISOString(),
    });
    const reported = new Date("2026-09-06T00:00:00Z");
    const second = await t.caller.datasets.grant({
      orgSlug: "grant-repeat",
      datasetSlug: "grant_repeat",
      approvalTicketId: "999",
      approvedAt: "2026-09-07T00:00:00Z",
      contributionReportedAt: reported.toISOString(),
      note: "late note",
    });
    expect(second.created).toBe(false);
    expect(second.datasetOrganizationId).toBe(first.datasetOrganizationId);

    const rows = await t.db
      .select()
      .from(datasetOrganizations)
      .where(eq(datasetOrganizations.datasetOrganizationId, first.datasetOrganizationId));
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({
      approvalTicketId: "100",
      approvedAt: firstApproved,
      contributionReportedAt: reported,
      approvalNote: "late note",
    });
  });

  test("activates the latest ready version only when the org has none", async () => {
    const org = await seedOrg(t.db, "grant-activate");
    const ds = await seedDataset(t.db, "grant_activate");
    await seedVersion(t.db, ds.datasetId, 1, "ready");
    const v2 = await seedVersion(t.db, ds.datasetId, 2, "ready");
    await seedVersion(t.db, ds.datasetId, 3, "ready", true);
    await seedVersion(t.db, ds.datasetId, 4, "importing");

    const res = await t.caller.datasets.grant(grantInput("grant-activate", "grant_activate"));
    expect(res.activated).toBe(true);
    const [after] = await t.db
      .select({ active: organizations.activeDatasetVersionId })
      .from(organizations)
      .where(eq(organizations.organizationId, org.organizationId));
    expect(after!.active).toBe(v2.datasetVersionId);

    // Already active: a second dataset's grant leaves the pointer alone.
    const other = await seedDataset(t.db, "grant_activate_other");
    await seedVersion(t.db, other.datasetId, 1, "ready");
    const again = await t.caller.datasets.grant(
      grantInput("grant-activate", "grant_activate_other"),
    );
    expect(again.created).toBe(true);
    expect(again.activated).toBe(false);
    const [still] = await t.db
      .select({ active: organizations.activeDatasetVersionId })
      .from(organizations)
      .where(eq(organizations.organizationId, org.organizationId));
    expect(still!.active).toBe(v2.datasetVersionId);
  });

  test("rejects a ticket id that is not a Zendesk ticket number", async () => {
    const org = await seedOrg(t.db, "grant-ticket-format");
    await seedDataset(t.db, "grant_ticket_format");
    for (const approvalTicketId of ["{{ticket.id}}", "12345678901234567890123", "ZD-1", ""]) {
      const res = await post("/datasets/grant", {
        orgSlug: "grant-ticket-format",
        datasetSlug: "grant_ticket_format",
        approvalTicketId,
      });
      expect(res.status).toBe(400);
      expect(await res.json()).toMatchObject({ code: "BAD_REQUEST" });
    }
    const rows = await t.db
      .select()
      .from(datasetOrganizations)
      .where(eq(datasetOrganizations.organizationId, org.organizationId));
    expect(rows).toHaveLength(0);
  });

  test("provenance dates must be date strings when present", async () => {
    await seedOrg(t.db, "grant-date-types");
    await seedDataset(t.db, "grant_date_types");
    const base = {
      orgSlug: "grant-date-types",
      datasetSlug: "grant_date_types",
      approvalTicketId: "1",
    };
    for (const extra of [
      { approvedAt: 0 },
      { approvedAt: true },
      { approvedAt: "yesterday-ish" },
      { contributionReportedAt: 0 },
    ]) {
      const res = await post("/datasets/grant", { ...base, ...extra });
      expect(res.status).toBe(400);
      expect(await res.json()).toMatchObject({ code: "BAD_REQUEST" });
    }
  });

  test("null provenance dates mean absent, and a null column is still fillable later", async () => {
    await seedOrg(t.db, "grant-null-dates");
    await seedDataset(t.db, "grant_null_dates");
    const before = Date.now();
    const first = await post("/datasets/grant", {
      orgSlug: "grant-null-dates",
      datasetSlug: "grant_null_dates",
      approvalTicketId: "2",
      approvedAt: null,
      contributionReportedAt: null,
    });
    expect(first.status).toBe(200);
    const { datasetOrganizationId } = (await first.json()) as { datasetOrganizationId: string };
    const [row] = await t.db
      .select()
      .from(datasetOrganizations)
      .where(eq(datasetOrganizations.datasetOrganizationId, datasetOrganizationId));
    expect(row!.approvedAt!.getTime()).toBeGreaterThanOrEqual(before - 1000);
    expect(row!.contributionReportedAt).toBeNull();

    const reported = new Date("2026-09-08T00:00:00Z");
    const second = await post("/datasets/grant", {
      orgSlug: "grant-null-dates",
      datasetSlug: "grant_null_dates",
      approvalTicketId: "3",
      approvedAt: "2026-09-09T00:00:00Z",
      contributionReportedAt: reported.toISOString(),
    });
    expect(await second.json()).toMatchObject({ created: false, datasetOrganizationId });
    const [after] = await t.db
      .select()
      .from(datasetOrganizations)
      .where(eq(datasetOrganizations.datasetOrganizationId, datasetOrganizationId));
    expect(after!.approvedAt).toEqual(row!.approvedAt);
    expect(after!.contributionReportedAt).toEqual(reported);
  });

  test("does not activate without a ready version or when activate is false", async () => {
    await seedOrg(t.db, "grant-noready");
    const ds = await seedDataset(t.db, "grant_noready");
    await seedVersion(t.db, ds.datasetId, 1, "importing");
    const res = await t.caller.datasets.grant(grantInput("grant-noready", "grant_noready"));
    expect(res.created).toBe(true);
    expect(res.activated).toBe(false);

    await seedOrg(t.db, "grant-optout");
    const ready = await seedDataset(t.db, "grant_optout");
    await seedVersion(t.db, ready.datasetId, 1, "ready");
    const optOut = await t.caller.datasets.grant({
      ...grantInput("grant-optout", "grant_optout"),
      activate: false,
    });
    expect(optOut.activated).toBe(false);
    const [row] = await t.db
      .select({ active: organizations.activeDatasetVersionId })
      .from(organizations)
      .where(eq(organizations.slug, "grant-optout"));
    expect(row!.active).toBeNull();
  });
});

describe("users/invite", () => {
  const invite = (orgSlug: string, email: string, role: "owner" | "admin" | "lead" = "admin") =>
    t.caller.users.invite({ orgSlug, email, role, sendEmail: false });

  test("404s on a missing org", async () => {
    await expectORPCError(invite("invite-missing", "a@example.org"), "NOT_FOUND");
  });

  test("rejects a role outside the known set", async () => {
    await seedOrg(t.db, "invite-role");
    await expectORPCError(
      t.caller.users.invite({
        orgSlug: "invite-role",
        email: "role@example.org",
        // @ts-expect-error — exercising runtime validation
        role: "member",
        sendEmail: false,
      }),
      "BAD_REQUEST",
    );
  });

  test("creates the user and membership; email is canonicalised", async () => {
    const org = await seedOrg(t.db, "invite-new");
    const res = await invite("invite-new", " First.Last+tt@Gmail.com ", "lead");
    expect(res.membership).toBe("created");
    expect(res.emailSent).toBe(false);

    const [user] = await t.db.select().from(users).where(eq(users.id, res.userId));
    expect(user).toMatchObject({
      email: "firstlast+tt@gmail.com",
      displayEmail: "first.last+tt@gmail.com",
      name: "first.last+tt",
      emailVerified: false,
    });
    const [m] = await t.db
      .select()
      .from(memberships)
      .where(
        and(eq(memberships.userId, res.userId), eq(memberships.organizationId, org.organizationId)),
      );
    expect(m).toMatchObject({ role: "lead", archivedAt: null });
  });

  test("a user already in another org gets a membership, not a second user row", async () => {
    const orgA = await seedOrg(t.db, "invite-shared-a");
    await seedOrg(t.db, "invite-shared-b");
    const existing = await seedUser(t.db, "shared@example.org", "Shared Person");
    await seedMembership(t.db, existing.id, orgA.organizationId, "owner");

    const res = await invite("invite-shared-b", "Shared@example.org", "admin");
    expect(res.membership).toBe("created");
    expect(res.userId).toBe(existing.id);
    const rows = await t.db.select().from(users).where(eq(users.email, "shared@example.org"));
    expect(rows).toHaveLength(1);
    expect(rows[0]!.name).toBe("Shared Person");
  });

  test("an archived membership is reactivated with the requested role", async () => {
    const org = await seedOrg(t.db, "invite-archived");
    const user = await seedUser(t.db, "archived@example.org");
    await seedMembership(t.db, user.id, org.organizationId, "lead", new Date());

    const res = await invite("invite-archived", "archived@example.org", "admin");
    expect(res.membership).toBe("reactivated");
    const [m] = await t.db
      .select()
      .from(memberships)
      .where(
        and(eq(memberships.userId, user.id), eq(memberships.organizationId, org.organizationId)),
      );
    expect(m).toMatchObject({ role: "admin", archivedAt: null });
  });

  test("an active membership is left alone, role included", async () => {
    const org = await seedOrg(t.db, "invite-active");
    const user = await seedUser(t.db, "active@example.org");
    await seedMembership(t.db, user.id, org.organizationId, "owner");

    const res = await invite("invite-active", "active@example.org", "lead");
    expect(res.membership).toBe("existing");
    const [m] = await t.db
      .select({ role: memberships.role })
      .from(memberships)
      .where(
        and(eq(memberships.userId, user.id), eq(memberships.organizationId, org.organizationId)),
      );
    expect(m!.role).toBe("owner");
  });

  test("sendEmail hands the display address to the sign-in email seam", async () => {
    const spy = vi.spyOn(signInEmail, "sendSignInEmail").mockResolvedValue(undefined);
    try {
      await seedOrg(t.db, "invite-email");
      const res = await t.caller.users.invite({
        orgSlug: "invite-email",
        email: "Mail.Me@example.org",
        role: "admin",
      });
      expect(res.emailSent).toBe(true);
      expect(spy).toHaveBeenCalledWith("mail.me@example.org");
    } finally {
      spy.mockRestore();
    }
  });

  test("two overlapping invites for a known user both succeed", async () => {
    // A known user keeps both handlers in lockstep on PGlite's single
    // connection: both membership lookups run before either insert.
    const org = await seedOrg(t.db, "invite-race");
    const user = await seedUser(t.db, "race@example.org");
    const [a, b] = await Promise.all([
      invite("invite-race", "race@example.org"),
      invite("invite-race", "race@example.org"),
    ]);
    expect(a.userId).toBe(user.id);
    expect(b.userId).toBe(user.id);
    expect([a.membership, b.membership].sort()).toEqual(["created", "existing"]);
    const rows = await t.db
      .select()
      .from(memberships)
      .where(
        and(eq(memberships.userId, a.userId), eq(memberships.organizationId, org.organizationId)),
      );
    expect(rows).toHaveLength(1);
  });

  test("a failed sign-in email keeps the membership and reports emailSent:false", async () => {
    // Shaped like the error a failed membership lookup produces: the
    // message and params both carry the address being looked up.
    const dbFailure = Object.assign(
      new Error(
        'Failed query: select "id" from "app"."users" where "email" = $1\nparams: flaky@example.org',
      ),
      {
        name: "DrizzleQueryError",
        query: 'select "id" from "app"."users" where "email" = $1',
        params: ["flaky@example.org"],
        cause: Object.assign(new Error("connect ECONNREFUSED"), { code: "ECONNREFUSED" }),
      },
    );
    const send = vi
      .spyOn(signInEmail, "sendSignInEmail")
      .mockRejectedValueOnce(dbFailure)
      .mockResolvedValueOnce(undefined);
    const logged = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      const org = await seedOrg(t.db, "invite-mail-fails");
      const body = {
        orgSlug: "invite-mail-fails",
        email: "flaky@example.org",
        role: "admin" as const,
      };

      const first = await t.caller.users.invite(body);
      expect(first).toMatchObject({ membership: "created", emailSent: false });
      expect(logged).toHaveBeenCalledWith("[service-api] users/invite sign-in email failed", {
        name: "DrizzleQueryError",
        code: undefined,
        status: undefined,
        responseCode: undefined,
        causeName: "Error",
        causeCode: "ECONNREFUSED",
        orgSlug: "invite-mail-fails",
      });
      expect(printed(logged.mock.calls)).not.toContain("flaky@example.org");
      const [m] = await t.db
        .select({ role: memberships.role, archivedAt: memberships.archivedAt })
        .from(memberships)
        .where(
          and(
            eq(memberships.userId, first.userId),
            eq(memberships.organizationId, org.organizationId),
          ),
        );
      expect(m).toMatchObject({ role: "admin", archivedAt: null });

      // The same call again is the resend: nothing else changes.
      const second = await t.caller.users.invite(body);
      expect(second).toMatchObject({
        userId: first.userId,
        membership: "existing",
        emailSent: true,
      });
      expect(send).toHaveBeenCalledTimes(2);
    } finally {
      send.mockRestore();
      logged.mockRestore();
    }
  });

  test("a rejected SMTP send reaches the response as emailSent:false", async () => {
    // The real path — Better Auth's hooks, the OTP row, and the send —
    // with only the transport stubbed. Better Auth swallows the rejection
    // itself, so the flag has to come from the send callback's own record.
    const env = { SMTP_HOST: process.env.SMTP_HOST, EMAIL_FROM: process.env.EMAIL_FROM };
    process.env.SMTP_HOST = "smtp.example.org";
    process.env.EMAIL_FROM = "turf@example.org";
    const smtpRejection = Object.assign(
      new Error("Can't send mail - all recipients were rejected: 550 5.1.1 <bounce@example.org>"),
      {
        code: "EENVELOPE",
        responseCode: 550,
        command: "RCPT TO",
        rejected: ["bounce@example.org"],
      },
    );
    mail.sendMail
      .mockRejectedValueOnce(smtpRejection)
      .mockResolvedValueOnce({ accepted: ["bounce@example.org"] });
    const logged = vi.spyOn(console, "error").mockImplementation(() => {});
    const quiet = vi.spyOn(console, "log").mockImplementation(() => {});
    try {
      await seedOrg(t.db, "invite-smtp");
      const body = { orgSlug: "invite-smtp", email: "Bounce@example.org", role: "admin" as const };

      const first = await t.caller.users.invite(body);
      expect(first).toMatchObject({ membership: "created", emailSent: false });
      expect(mail.sendMail).toHaveBeenCalledTimes(1);
      expect(mail.sendMail.mock.calls[0]![0]).toMatchObject({
        from: "turf@example.org",
        to: "bounce@example.org",
      });
      expect(logged).toHaveBeenCalledWith("[service-api] users/invite sign-in email failed", {
        name: "Error",
        code: "EENVELOPE",
        status: undefined,
        responseCode: 550,
        causeName: undefined,
        causeCode: undefined,
        orgSlug: "invite-smtp",
      });
      expect(printed(logged.mock.calls)).not.toContain("bounce@example.org");

      const second = await t.caller.users.invite(body);
      expect(second).toMatchObject({
        userId: first.userId,
        membership: "existing",
        emailSent: true,
      });
      expect(mail.sendMail).toHaveBeenCalledTimes(2);
    } finally {
      logged.mockRestore();
      quiet.mockRestore();
      mail.sendMail.mockReset();
      process.env.SMTP_HOST = env.SMTP_HOST;
      process.env.EMAIL_FROM = env.EMAIL_FROM;
    }
  });
});

describe("questions/create", () => {
  test("404s on a missing org", async () => {
    await expectORPCError(
      t.caller.questions.create({ orgSlug: "q-missing", name: "Q", text: "", options: [] }),
      "NOT_FOUND",
    );
  });

  test("creates the question and ordered options as the actor", async () => {
    const org = await seedOrg(t.db, "q-create");
    const res = await t.caller.questions.create({
      orgSlug: "q-create",
      name: "Support the strike?",
      text: "Do you support the strike?",
      options: ["Yes", "No", "Undecided"],
    });
    expect(res.created).toBe(true);
    expect(res.optionIds).toHaveLength(3);

    const [q] = await t.db.select().from(questions).where(eq(questions.questionId, res.questionId));
    expect(q).toMatchObject({
      organizationId: org.organizationId,
      name: "Support the strike?",
      responseType: "single_select",
      text: "Do you support the strike?",
      createdBy: t.actor.id,
    });
    const opts = await t.db
      .select()
      .from(responseOptions)
      .where(eq(responseOptions.questionId, res.questionId))
      .orderBy(responseOptions.order);
    expect(opts.map((o) => o.text)).toEqual(["Yes", "No", "Undecided"]);
    expect(opts.map((o) => o.order)).toEqual([0, 1, 2]);
    expect(opts.map((o) => o.responseOptionId)).toEqual(res.optionIds);
    expect(opts.every((o) => o.createdBy === t.actor.id)).toBe(true);
  });

  test("a repeat with different case and whitespace returns the existing question", async () => {
    await seedOrg(t.db, "q-repeat");
    const first = await t.caller.questions.create({
      orgSlug: "q-repeat",
      name: "Yard sign?",
      text: "Want a yard sign?",
      options: ["Yes", "No"],
    });
    const second = await t.caller.questions.create({
      orgSlug: "q-repeat",
      name: "  YARD SIGN?  ",
      text: "different text",
      options: ["Maybe"],
    });
    expect(second).toEqual({
      created: false,
      questionId: first.questionId,
      optionIds: first.optionIds,
    });

    const rows = await t.db
      .select()
      .from(questions)
      .where(eq(questions.organizationId, (await orgId("q-repeat"))!));
    expect(rows).toHaveLength(1);
    expect(rows[0]!.text).toBe("Want a yard sign?");
  });

  test("two overlapping creates of the same name yield one question", async () => {
    await seedOrg(t.db, "q-race");
    const input = { orgSlug: "q-race", name: "Race?", text: "", options: ["A", "B"] };
    const [a, b] = await Promise.all([
      t.caller.questions.create(input),
      t.caller.questions.create(input),
    ]);
    expect([a.created, b.created].filter(Boolean)).toHaveLength(1);
    expect(a.questionId).toBe(b.questionId);
    expect(a.optionIds).toEqual(b.optionIds);
    const rows = await t.db
      .select()
      .from(questions)
      .where(eq(questions.organizationId, (await orgId("q-race"))!));
    expect(rows).toHaveLength(1);
  });

  test("an archived question with the same name does not block a new one", async () => {
    const org = await seedOrg(t.db, "q-archived");
    await t.db.insert(questions).values({
      organizationId: org.organizationId,
      name: "Old",
      responseType: "single_select",
      text: "",
      createdBy: t.actor.id,
      archivedAt: new Date(),
    });
    const res = await t.caller.questions.create({
      orgSlug: "q-archived",
      name: "old",
      text: "",
      options: [],
    });
    expect(res.created).toBe(true);
  });

  test("open_ended ignores options", async () => {
    await seedOrg(t.db, "q-open");
    const res = await t.caller.questions.create({
      orgSlug: "q-open",
      name: "Anything else?",
      text: "",
      responseType: "open_ended",
      options: ["Ignored"],
    });
    expect(res.created).toBe(true);
    expect(res.optionIds).toEqual([]);
    const opts = await t.db
      .select()
      .from(responseOptions)
      .where(eq(responseOptions.questionId, res.questionId));
    expect(opts).toHaveLength(0);
  });

  async function orgId(slug: string) {
    const [row] = await t.db
      .select({ organizationId: organizations.organizationId })
      .from(organizations)
      .where(eq(organizations.slug, slug));
    return row?.organizationId;
  }
});

describe("HTTP dispatch", () => {
  test("a missing or bad bearer is a JSON 401", async () => {
    const missing = await handleServiceRequest(new Request(url("/healthcheck")), t.db);
    expect(missing.status).toBe(401);
    expect(await missing.json()).toEqual({ error: "unauthorized" });

    const bad = await post("/organizations/status", { slug: "x" }, "tt_wrong");
    expect(bad.status).toBe(401);
    expect(await bad.json()).toEqual({ error: "unauthorized" });
  });

  test("GET /healthcheck answers with the token name", async () => {
    const res = await handleServiceRequest(
      new Request(url("/healthcheck"), { headers: { authorization: `Bearer ${TEST_RAW_TOKEN}` } }),
      t.db,
    );
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({
      status: "ok",
      db: "connected",
      token: { name: "test-token" },
    });
  });

  test("POST bodies map onto procedures and dates serialise as ISO strings", async () => {
    const ensure = await post("/organizations/ensure", { slug: "http-org", name: "HTTP Org" });
    expect(ensure.status).toBe(200);
    expect(await ensure.json()).toMatchObject({
      created: true,
      organization: { slug: "http-org", name: "HTTP Org" },
    });

    await seedDataset(t.db, "http_ds");
    const grant = await post("/datasets/grant", {
      orgSlug: "http-org",
      datasetSlug: "http_ds",
      approvalTicketId: "42",
      approvedAt: "2026-09-04T12:00:00Z",
    });
    expect(grant.status).toBe(200);
    expect(await grant.json()).toMatchObject({ created: true, activated: false });

    const status = await post("/organizations/status", { slug: "http-org" });
    const body = (await status.json()) as {
      provisioned: boolean;
      grants: { approvedAt: string }[];
    };
    expect(body.provisioned).toBe(true);
    expect(body.grants[0]!.approvedAt).toBe("2026-09-04T12:00:00.000Z");
  });

  test("handler errors come back as oRPC JSON with code, status and message", async () => {
    const res = await post("/datasets/grant", {
      orgSlug: "http-none",
      datasetSlug: "http_ds",
      approvalTicketId: "1",
    });
    expect(res.status).toBe(404);
    expect(await res.json()).toMatchObject({
      code: "NOT_FOUND",
      status: 404,
      message: "Organization not found",
    });

    const invalid = await post("/organizations/ensure", { slug: "Bad Slug", name: "x" });
    expect(invalid.status).toBe(400);
    expect(await invalid.json()).toMatchObject({ code: "BAD_REQUEST", status: 400 });
  });

  test("an unknown path is a JSON 404", async () => {
    const res = await post("/nope", {});
    expect(res.status).toBe(404);
    expect(await res.json()).toEqual({ error: "not_found" });
  });
});
