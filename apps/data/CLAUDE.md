# Data Package

Package documentation lives in `AGENTS.md` in this directory. **Read it** — it covers the Hamilton/DuckDB pipeline, geographic scope resolution, `TableRef` conventions, and naming.

Claude Code loads `CLAUDE.md`, not `AGENTS.md`, so this file carries the rule that must load unconditionally and points at the rest.

## Voter data in queries

See the repo root `CLAUDE.md` for the general rule. Specific to this package:

- `ducklake` holds per-organization Person data. Any `SELECT *` against `persons_validated`, the Person / Building / Door tables, or an address-matched intermediate returns row-level PII straight to the terminal.
- Inspect the DAG with schema reads and aggregates: `DESCRIBE`, `COUNT(*)`, `GROUP BY` on non-identity columns, null-rate checks. That answers almost every pipeline question.
- When a node's output must be eyeballed row by row, write it to Parquet or CSV and open it outside the session rather than printing it.
- `ducklake_geo` (TIGER blockfaces, OSM buildings, landuse, boundaries) carries no person data and is fine to query freely. The catalog is the line.

> Duplicated verbatim in `AGENTS.md`. Keep the two in sync.
