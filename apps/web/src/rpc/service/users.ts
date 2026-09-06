import { and, eq } from "@turf-tools/db";
import { memberships, users } from "@turf-tools/db/schema";
import { z } from "zod";
import { normalizeEmail } from "~/lib/normalize-email";
import { ROLES } from "~/lib/permissions";
import { sendSignInEmail } from "~/lib/sign-in-email";
import { serviceMut } from "../context";
import { resolveOrg } from "./resolve-org";

const userSelect = { id: users.id, displayEmail: users.displayEmail };
const membershipSelect = {
  membershipId: memberships.membershipId,
  archivedAt: memberships.archivedAt,
};

// Invite a user into an org, or make sure they are in it. The user row is
// found-or-created by canonical email — the same person may already belong
// to another chapter's org — and the membership outcome says what changed:
// `created` (none existed), `reactivated` (was archived; role reset), or
// `existing` (active; role left alone). The sign-in email doubles as a resend;
// `emailSent` reports whether the mail transport accepted it.
export const invite = serviceMut
  .route({ path: "/users/invite" })
  .input(
    z.object({
      orgSlug: z.string().min(1),
      email: z.string().trim().email(),
      name: z.string().trim().min(1).max(120).optional(),
      role: z.enum(ROLES),
      sendEmail: z.boolean().default(true),
    }),
  )
  .handler(async ({ context, input }) => {
    const org = await resolveOrg(context.db, input.orgSlug);
    const email = normalizeEmail(input.email);
    const displayEmail = input.email.toLowerCase();

    let [user] = await context.db.select(userSelect).from(users).where(eq(users.email, email));
    if (!user) {
      // A concurrent invite for the same address may win the insert; the
      // unique email index makes this a no-op and the re-read picks it up.
      [user] = await context.db
        .insert(users)
        .values({ email, displayEmail, name: input.name ?? displayEmail.split("@")[0]! })
        .onConflictDoNothing({ target: users.email })
        .returning(userSelect);
      if (!user) {
        [user] = await context.db.select(userSelect).from(users).where(eq(users.email, email));
      }
    }
    const userId = user!.id;

    const loadMembership = async () =>
      (
        await context.db
          .select(membershipSelect)
          .from(memberships)
          .where(
            and(eq(memberships.userId, userId), eq(memberships.organizationId, org.organizationId)),
          )
      )[0];

    let membership = await loadMembership();
    let created = false;
    if (!membership) {
      // Two overlapping invites can both find no membership; the unique
      // (user, org) index lets one insert land and the other re-reads the
      // row, which is then classified like any pre-existing membership.
      const [inserted] = await context.db
        .insert(memberships)
        .values({ userId, organizationId: org.organizationId, role: input.role })
        .onConflictDoNothing({ target: [memberships.userId, memberships.organizationId] })
        .returning({ membershipId: memberships.membershipId });
      if (inserted) created = true;
      else membership = await loadMembership();
    }

    let outcome: "created" | "reactivated" | "existing";
    if (created) {
      outcome = "created";
    } else if (membership!.archivedAt) {
      await context.db
        .update(memberships)
        .set({ archivedAt: null, role: input.role })
        .where(eq(memberships.membershipId, membership!.membershipId));
      outcome = "reactivated";
    } else {
      outcome = "existing";
    }

    // The membership is already in place by now, so a mail-provider fault is
    // reported as `emailSent: false` rather than a 500 the caller would retry
    // (each retry re-sending). Calling again with the same body resends.
    let emailSent = false;
    if (input.sendEmail) {
      try {
        await sendSignInEmail(user!.displayEmail);
        emailSent = true;
      } catch (err) {
        // Only type-level detail is logged: a query error's message and
        // params, and an SMTP error's message and response, all carry the
        // invitee's address.
        const e = err as {
          name?: string;
          code?: string;
          status?: number;
          responseCode?: number;
          cause?: { name?: string; code?: string };
        };
        console.error("[service-api] users/invite sign-in email failed", {
          name: e?.name,
          code: e?.code,
          status: e?.status,
          responseCode: e?.responseCode,
          causeName: e?.cause?.name,
          causeCode: e?.cause?.code,
          orgSlug: input.orgSlug,
        });
      }
    }

    return { userId, membership: outcome, emailSent };
  });
