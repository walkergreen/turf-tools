# turf-tools

Repo conventions live in `AGENTS.md` at this root. **Read it** — it covers pnpm usage, comment style, and the rule about verifying architectural claims before asserting them.

Claude Code does not load `AGENTS.md` on its own. Its memory discovery is `CLAUDE.md` / `CLAUDE.local.md` only (verified against 2.1.246; the `AGENTS.md` references in that build belong to `/init` and the Codex migration importer, not to runtime loading). This file exists so the rule below loads unconditionally, and so the rest of `AGENTS.md` gets read rather than silently skipped.

## Voter data

Row-level voter data never enters the conversation, and that means tool results as much as the terminal. This repo builds canonical Person / Building / Door records; a `SELECT *` against any of them is a PII dump with no command to inspect and no redirect to hide behind.

- Query for counts, distributions, and schema. `DESCRIBE` and `COUNT(*)` over `SELECT *`.
- When row-level output is genuinely needed, write it to a file, report the row count, and stop. Do not read the file back.
- No identity column in a projection, and no cells below k=5 — a count of 1 re-identifies.
- Fixtures under `apps/data/fixtures/` hold real voter samples. Gitignored is not the same as safe to read; don't `cat` them.

See also `~/.claude/CLAUDE.md` for the account-wide version of this rule.

> This section is duplicated verbatim in `AGENTS.md`, which is what Codex and human readers get. Keep the two in sync. The duplication is deliberate: a safety rule should not depend on an indirection resolving.
