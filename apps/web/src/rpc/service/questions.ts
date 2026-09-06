import { and, asc, eq, isNull, sql } from "@turf-tools/db";
import { questions, responseOptions } from "@turf-tools/db/schema";
import { z } from "zod";
import { serviceMut } from "../context";
import { resolveOrg } from "./resolve-org";

const RESPONSE_TYPES = ["single_select", "multi_select", "open_ended"] as const;

// Create a survey question with its options in one call. Idempotent on name:
// an active question in the org with the same name (case-insensitive,
// trimmed) is returned untouched with its current active option ids, so a
// re-run of the approving automation never duplicates a question.
export const create = serviceMut
  .route({ path: "/questions/create" })
  .input(
    z.object({
      orgSlug: z.string().min(1),
      name: z.string().trim().min(1).max(200),
      text: z.string().trim().max(2000),
      responseType: z.enum(RESPONSE_TYPES).default("single_select"),
      options: z.array(z.string().trim().min(1).max(200)).max(50).default([]),
    }),
  )
  .handler(async ({ context, input }) => {
    const org = await resolveOrg(context.db, input.orgSlug);

    // Open-ended questions have no options to choose from.
    const optionTexts = input.responseType === "open_ended" ? [] : input.options;

    return context.db.transaction(async (tx) => {
      // Nothing in the schema makes an active name unique per org, so two
      // overlapping creates would both miss the lookup. Holding a per-org
      // lock for the transaction makes the second see the first's row.
      await tx.execute(sql`select pg_advisory_xact_lock(hashtext(${org.organizationId}))`);

      const [existing] = await tx
        .select({ questionId: questions.questionId })
        .from(questions)
        .where(
          and(
            eq(questions.organizationId, org.organizationId),
            isNull(questions.archivedAt),
            sql`lower(trim(${questions.name})) = ${input.name.toLowerCase()}`,
          ),
        )
        .orderBy(asc(questions.createdAt))
        .limit(1);
      if (existing) {
        const options = await tx
          .select({ responseOptionId: responseOptions.responseOptionId })
          .from(responseOptions)
          .where(
            and(
              eq(responseOptions.questionId, existing.questionId),
              isNull(responseOptions.archivedAt),
            ),
          )
          .orderBy(asc(responseOptions.order));
        return {
          created: false,
          questionId: existing.questionId,
          optionIds: options.map((o) => o.responseOptionId),
        };
      }

      const [question] = await tx
        .insert(questions)
        .values({
          organizationId: org.organizationId,
          name: input.name,
          responseType: input.responseType,
          text: input.text,
          createdBy: context.actor.id,
        })
        .returning({ questionId: questions.questionId });
      const questionId = question!.questionId;

      if (optionTexts.length === 0) return { created: true, questionId, optionIds: [] };

      const inserted = await tx
        .insert(responseOptions)
        .values(
          optionTexts.map((text, order) => ({
            questionId,
            text,
            order,
            createdBy: context.actor.id,
          })),
        )
        .returning({
          responseOptionId: responseOptions.responseOptionId,
          order: responseOptions.order,
        });
      inserted.sort((a, b) => a.order - b.order);
      return { created: true, questionId, optionIds: inserted.map((o) => o.responseOptionId) };
    });
  });
