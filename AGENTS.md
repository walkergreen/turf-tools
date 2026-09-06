# Repo guidelines

Use pnpm for everything: workspace scripts and dependency management (`pnpm dev`, `pnpm check`, `pnpm test`, `pnpm add`). The Vite+ `vp` CLI appears inside a few scripts but the pnpm scripts wrap it correctly.

- Never commit files under `apps/data/fixtures/`, we sometimes stash voter data sample data here but it's gitignored.
- After editing Python in `apps/data` or Javascript in `apps/web`, run `pnpm check` from the repo root.

# Comment style

Comments describe the current code as it stands. They never reference what the code used to do, what was replaced, what was renamed, what previous approach was rejected, or how the current shape compares to a prior one. Diffs and PRs are where the historical record lives. A comment that says "matches the prior X" or "now does Y instead of Z" is a comment that goes stale the moment a future contributor reads it without that history.

Write the comment as if the reader has never seen the previous version (because most readers haven't).

# Verify architectural claims before asserting them

When making claims about how the codebase works ("we use X for Y", "we'd lose X by doing Y", "this is handled in Z"), name the specific file or function involved. If you can't cite one, you don't actually know — grep the code and find out before asserting.

Tactical claims about code you're editing get verified naturally because you have to read the code to write the code. Architectural claims have no such built-in check, so it's easy to drift into confident-sounding wrongness based on stored memory or pattern-matching from similar codebases. Add the verification step explicitly.

Treat saved architecture notes (memories, doc files, prior conversation) as starting points for investigation, not as facts to relay. The code is the truth.

# Voter data

Row-level voter data never enters the conversation, and that means tool results as much as the terminal. This repo builds canonical Person / Building / Door records; a `SELECT *` against any of them is a PII dump with no command to inspect and no redirect to hide behind.

- Query for counts, distributions, and schema. `DESCRIBE` and `COUNT(*)` over `SELECT *`.
- When row-level output is genuinely needed, write it to a file, report the row count, and stop. Do not read the file back.
- No identity column in a projection, and no cells below k=5 — a count of 1 re-identifies.
- Fixtures under `apps/data/fixtures/` hold real voter samples. Gitignored is not the same as safe to read; don't `cat` them.
