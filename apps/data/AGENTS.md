# Data Package

This package builds the voter-data tables that the web and native apps query
against. It loads voter files, matches addresses against TIGER blockfaces,
refines positions with OpenStreetMap data, and aggregates everything into
canonical Person / Building / Door records.

## Tooling

### Use `uv`

Run everything through `uv` — even ad-hoc one-off scripts. Don't reach for `pip`
or a global Python.

### Hamilton + DuckDB

The pipeline is a [Hamilton](https://github.com/dagworks-inc/hamilton) DAG
backed by DuckDB with two DuckLake catalogs attached:

- `ducklake` — voter / Person data (per-organization schemas)
- `ducklake_geo` — TIGER blockfaces, OSM buildings, landuse polygons, boundary shapes

Both catalogs share **one DuckDB connection** (`src/duckdb.get_connection`),
so cross-catalog joins are free — no data copying.

### Geographic scope

Each dataset version has a **geographic scope**: the `(state_fips,
county_fips)` pairs whose TIGER data it needs and whose states' OSM extracts
it ingests. `src/geo/scope.py` (pure) and `src/geo/tiger_scope.py` (DuckDB
reads + the TIGER national county file) resolve it once per import, after the
importer runs and before the DAG:

1. `TIGER_SCOPE` (`Settings.tiger_scope`) — `36:005,047,061,081,085;34:017`,
   or `36:*` for statewide; states accept FIPS or postal codes.
2. The legacy `TIGER_STATE_FIPS` + `TIGER_COUNTY_FIPS` pair, folded into the
   same form (both required; statewide is `TIGER_SCOPE=36:*`, and `Settings`
   rejects one half without the other at startup — unless `TIGER_SCOPE` is
   set, which outranks the pair and leaves a stale half inert).
3. Derived from the data: the distinct `state` values of `persons_validated`,
   narrowed to the distinct `county_fips` values for a state when every row of
   that state carries a valid 3-digit code, else every county of the state
   from `ducklake_geo.tiger.county` (loaded once per `TIGER_YEAR` from
   `tl_{year}_us_county.zip`). Every distinct `state` value counts — one stray
   row provisions its whole state — so the per-state row counts are logged
   before any download; values that are not US postal codes are skipped with
   a warning (only a table with no known state at all is an error). Derived
   county codes are checked against the county table; a code that is not a
   county of its state widens the state instead of loading a wrong county.

Explicit county codes in a spec (steps 1 and 2) go through the same county
table, and a code that is not a county of its state raises at scope
resolution rather than as a Census 404 mid-DAG. A pin is deployment-wide: it
applies to every dataset imported on that deployment, so remove it (or extend
`TIGER_SCOPE`) before importing a second state. A pinned scope still reads
`persons_validated.state`, and persons in a state the pin does not cover are
reported as a warning (those rows will not geocode). Every such reason —
rows lacking `county_fips`, no `county_fips` column, a widened state, a
skipped state value, persons outside a pin — goes through the resolver's
`warn` callable (`log` carries the per-state row counts); `import_job` routes
both to `app.job_messages` and keeps the warnings as `geoScope.notes`.

Importers feed step 3 through two optional-by-contract columns on
`persons_validated`: `state` (2-letter postal; part of the `Person` core) and
`county_fips` (3-digit county FIPS within that state, or NULL — see the
`Importer` docstring in `src/importers/base.py`). The NYS importer maps BOE
county codes through `importers/nys_voter_file/counties.py`; TargetSmart
zero-pads a 1–3 digit numeric `vb_vf_county_code`.

The DAG receives the result as the `geo_scope` input (`list[CountyScope]`);
`tiger_*_raw`, `blockface_final`, `boundary_from_blocks`, and
`osm_extract_urls` all read it. `import_job` and `seed-persons` log the
resolved scope and record it (with the OSM extracts, UTM zone, and the
warnings as `notes`) under `derived_metadata.geoScope`. After geocoding,
`tiger_scope.county_match_rate_warnings` flags any county with at least 100
persons where under 5 % matched a TIGER blockface — the signature of a county
code that is not a Census FIPS code (the wrong county's blockfaces were
loaded) — into the same log and notes.

The metric projection is per version too: `geocode.utm_epsg` picks the UTM
zone of the median matched-blockface longitude (`src/geo/projection.py`), and
every `ST_Transform` to meters in `geocode` uses it. `blockface_relationships`
projects each node in the UTM zone of its own longitude unless given a
`bearing_epsg` (named so Hamilton never binds it to `geocode.utm_epsg`).

### Hamilton node return values: `TableRef`

Hamilton nodes don't return DataFrames or relations. They execute their work
against the shared DuckDB connection and return a `TableRef` dataclass
(`src/models.py`):

```python
TableRef(catalog="ducklake", schema="default", table="persons_geocoded", version=N)
```

Downstream nodes accept these as inputs and use them to locate data in
DuckLake. The `version` field is the DuckLake snapshot version at the time
the node finished — useful for time-travel queries during debugging.

### SQL style

Most nodes use plain SQL strings via `conn.execute(f"…")`. The DuckDB
relational API (`rel.filter()`, `rel.aggregate()`, etc.) is available but
rarely used in this codebase — the SQL is more readable for the kind of
multi-CTE work this pipeline does.

## Naming conventions

### DAG nodes

Every node has a **noun** name describing its data. The function name matches
the table suffix when materialized: `persons_decomposed` produces
`{slug}_persons_decomposed`. Within a family, names use a stage qualifier:
`persons_transformed`, `persons_validated`, `persons_decomposed`,
`persons_candidates`, `persons_scored`, `persons_best_match`,
`persons_geocoded`.

Some nodes (e.g. `persons_validated`) don't materialize a new table — they
pass through the `TableRef` they received after running checks. Consumers
don't need to know whether a fresh table was written.

### voter vs person

The input is a **voter file** — a parquet dump from a state BOE. The
downstream canonical schema is **Person** (`src/models.py`). We keep
"voter_file" in input-side names (`voter_file_loader.py`, `{slug}_voters_raw`,
`NysVoterFileImporter`) because that's literally what's being loaded.
Everything after validation uses "person" because rows conform to the
Person schema regardless of source.

### Per-organization namespace

`ducklake` schemas are per-organization: `ducklake.{organization_slug}.*`.
The slug is the URL/SQL-safe identifier stored alongside `organizationId`
in `organizations` (`packages/db/src/schema/organizations.ts`).

`ducklake_geo` is organization-agnostic — TIGER and OSM data is shared
across all orgs in the same state.

## The Hamilton modules

| Module              | Role                                                                                     | Output catalog            |
| ------------------- | ---------------------------------------------------------------------------------------- | ------------------------- |
| `voter_file_loader` | Parse voter parquet → Person schema                                                      | `ducklake.{org}`          |
| `tiger`             | TIGER shapefiles → `blockface_final`                                                     | `ducklake_geo.tiger`      |
| `osm`               | OSM PBF → `osm_building_lookup` + raw OSM tables                                         | `ducklake_geo.osm`        |
| `matching`          | Voter ↔ TIGER blockface (`persons_best_match`)                                           | `ducklake.{org}`          |
| `geocode`           | Lat/lon assignment (`refined_positions`, `osm_only_matches`)                             | `ducklake.{org}`          |
| `assembly`          | Canonical Person record (`canonical_addresses`, `persons_geocoded`, `geocoding_summary`) | `ducklake.{org}`          |
| `aggregate`         | `persons_geocoded` → `buildings_geocoded` + `doors_geocoded`                             | `ducklake.{org}`          |
| `boundaries`        | Derive polygons (EDs, zips) from voter data + TIGER blocks                               | `ducklake_geo.boundaries` |
| `quickwit`          | Stream Person records into a Quickwit search index                                       | external                  |

`tiger` and `osm` are symmetric — both extract geographic reference
data into `ducklake_geo`. `matching`, `geocode`, `assembly`, and
`aggregate` form the per-voter pipeline that consumes those references.

### voter_file_loader

```
voters_raw → persons_transformed → persons_validated
```

`voters_raw` ingests the parquet as-is. `persons_transformed` runs the
state-specific SQL in `src/transformations.py` to map raw fields to
the Person schema. `persons_validated` is a passthrough that runs Pydantic
checks.

### tiger

```
tiger_addrfeat_raw ─┐
                    ├─► blockface_unpivoted → blockface_normalized → blockface_final
tiger_edges_raw  ───┘
address_tokens ──────────────────────────────────────────► blockface_final
```

Downloads TIGER/Line shapefiles for every `(state, county)` pair in
`geo_scope` (per `tiger_year`), joins them into one blockface table. The
raw loaders are incremental per pair — a pair already in the table is not
fetched again — and `tiger_tabblock_raw` downloads each state's TABBLOCK20
file once, filtered to that state's counties in scope. URL construction and
the download step live in `src/geo/tiger_files.py`; a download streams into
a `.part` file renamed into place only when complete, so an interrupted
transfer never poisons the cache, and a cached file that is not a zip is
deleted and reported with its path. Each TIGER edge has both a left and
right side with independent house-number ranges; `blockface_unpivoted`
splits each edge into two rows (one per side).

`blockface_final` carries:

- house-number range (from, to) + parity (odd/even/mixed)
- side (left/right) + prefix
- `full_name` — canonical street name (alias-collapsed; see below)
- `street_tokens_match` — equivalency-expanded union of all alias rows'
  tokens (drives the matching predicate so a voter using any spelling
  matches)
- `street_tokens_lookup` — equivalency-expanded tokens of _only_ the
  canonical full_name (drives the OSM `canonical_key` lookup, so voters
  at the same building hit the same OSM record regardless of which
  alias their raw address used)
- a `GEOMETRY` representing the edge line

**Alias collapse.** When TIGER's addrfeat stores the same physical
blockface under multiple names (a street and its commemorative co-name,
abbreviation variants of the same name, …), those rows share
`(tlid, side, prefix, from, to)`. `blockface_final` collapses them into
one row, picking the canonical `full_name` by frequency across this
version's `geo_scope` (alphabetical tiebreak).

Equivalency expansion uses `EQUIVALENT_TOKEN_GROUPS` from
`src/addressing.py`. Non-incremental: `blockface_final` is rebuilt per
import from the `blockface_normalized` rows whose TIGER line belongs to a
county in `geo_scope` (`tiger_addrfeat_raw` carries each line's state and
county), while the raw / unpivoted / normalized tables stay the shared,
additive cache — so another state's counties in that cache never shift this
version's canonical names, `utm_epsg` fallback, or table size.

### osm

```
geo_scope → osm_extract_urls → osm_pbfs → osm_buildings_polygons  (osmium-derived building polygons + area centroids)
                                        → osm_addresses            (raw OSM addressed elements)
                                        → osm_landuse_residential  (assembled landuse polygons)

osm_addresses + osm_landuse_residential + address_tokens
    → osm_building_lookup            (per-building keyed for fast join)
```

Symmetric to `tiger`: extracts OSM reference data into
`ducklake_geo.osm`. `osm_extract_urls` resolves one PBF per state in scope
(`OSM_URLS` verbatim > the state's slug in `OSM_URL_PINS` >
`OSM_URL_TEMPLATE`; slugs in `src/geo/states.py`). A single `OSM_URL` named
for a Geofabrik state (`src/geo/geofabrik.slug_for_url`) acts as the pin for
that state, so other states in scope still resolve; a URL not named for any
state (a BBBike city extract) is ingested verbatim as the only extract, with
a warning (`osm_url_scope_warning`, also written to the job log) when the
scope spans more than one state. `osm_pbfs` downloads through a `.part`
file renamed into place on completion, and a freshly downloaded extract
first drops its rows from the three raw tables and its osmium caches
(`_invalidate_extract`; an extract downloaded for the first time has nothing
to drop and is logged as a first download) — a re-downloaded `-latest` file
is a new snapshot under the same extract id, so deleting the cached PBF is
how to refresh one.
The three raw tables carry an `extract` column (the PBF filename stem,
`src/geo/geofabrik.extract_id`) and load incrementally per extract, each
in one INSERT, so an interrupted extract is retried and a new state appends.
The way→polygon join in `osm_addresses` is constrained to the same extract
because Geofabrik state extracts overlap at borders. The downstream
`geocode` module consumes `osm_building_lookup` — built from this version's
extracts only (`osm_pbfs`), so another state's or a retired snapshot's rows
in the shared tables never feed it — along with `blockface_final` for
coordinate assignment.

Rows for an extract no version uses any more stay in the raw tables as
storage only. To retire one by hand:

```sql
DELETE FROM ducklake_geo.osm.buildings_polygons   WHERE extract = '<id>';
DELETE FROM ducklake_geo.osm.addresses            WHERE extract = '<id>';
DELETE FROM ducklake_geo.osm.landuse_residential  WHERE extract = '<id>';
```

`reset_ducklake --include-geo` drops the whole reference catalog instead.

`osm_building_lookup` is one row per OSM-known building, keyed on
`(zip_code, canonical_key, housenumber_norm)`, carrying:

- `osm_lat`, `osm_lon` — area-weighted centroid
- `street` — the OSM `addr:street` tag (used as the canonical street for
  `building_id` when this OSM record matches a voter)
- `housenumber` — the OSM `addr:housenumber` tag (used by
  `canonical_addresses` to normalize voter-side surface-form variants)
- `in_residential_complex` — true if the building's centroid is inside
  a `landuse=residential` polygon

When multiple OSM records share the same `(zip_code, canonical_key,
housenumber_norm)` (rare; same address tagged on multiple polygons),
the chosen record is the way (over a node) with the smallest `osm_id`,
then the first `extract` name for the same way seen in two of this
version's overlapping extracts — deterministic across runs.

### matching

```
persons_validated → persons_decomposed → persons_candidates
                                              │
blockface_final ──────────────────────────────┘
                                              │
                                       persons_scored
                                              │
                                       persons_best_match
```

The voter ↔ TIGER blockface step. Produces the highest-scoring blockface
per voter; coordinate assignment is downstream in `geocode`.

- `persons_decomposed` — parse `address_line_1` into house number,
  prefix, half_code, street tokens. Tokens have `STREET_REWRITES`
  applied before tokenization (see "Street-name handling").
- `persons_candidates` — for each voter, find every TIGER blockface
  where the zip/parity/prefix/range matches and the token overlap
  clears the "≥ 2 total + ≥ 1 distinctive" bar. Implemented as an
  inverted-index join (unnest each side to one row per distinctive
  token, equi-join on token, then DISTINCT to dedupe by pair) — the
  list-overlap predicate becomes a hash-join key. The hydration step
  re-applies the range/prefix predicate so multi-row-per-blockface_id
  cases (multiple address ranges on one TIGER edge) hydrate
  deterministically.
- `persons_scored` — score each candidate by token overlap + numeric-
  token bonus.
- `persons_best_match` — pick the highest-scoring blockface per voter
  (`ROW_NUMBER` ordered by `match_score DESC, blockface_id, full_name`
  for deterministic tiebreaks).

### geocode

```
persons_best_match + blockface_final
    → utm_epsg                       (the version's UTM zone, from the median matched longitude)

persons_best_match + persons_decomposed + blockface_final + osm_building_lookup + utm_epsg
    → refined_positions              (TIGER-matched voter lat/lon)

persons_decomposed + persons_best_match + osm_building_lookup
                       + blockface_final + address_tokens + utm_epsg
    → osm_only_matches               (TIGER-miss voter lat/lon + snapped blockface)
```

All metric work (the 7 m road offset, 4 m building spacing, snap
distances) runs in `utm_epsg`; scale error one zone from the central
meridian is ~0.3 %, and `utm_epsg_for_longitudes` refuses a dataset whose
5th–95th percentile longitudes span more than 20° from it.

The actual coordinate-assignment step. Two paths, mutually exclusive
(each voter appears in exactly one):

1. **`refined_positions`** — for TIGER-matched voters: project the OSM
   building centroid onto the matched blockface (or use the OSM centroid
   directly when the projection clamps or the building is inside a
   residential complex). The rank-based fallback partitions by
   `(tiger_line_id, side)` and orders by `(prefix, house_number,
half_code)` so voters across multiple address ranges on the same
   physical TIGER line get distinct ranks. A 1D shove keeps distinct
   buildings on the same blockface ≥ 4 m apart.
2. **`osm_only_matches`** — for TIGER-miss voters: derive a
   canonical_key from the raw voter address, look them up directly in
   OSM, and snap to the nearest blockface in their zip for downstream
   grouping.

`position_source` values:

- `osm_matched` — TIGER blockface, with the OSM-projected fraction along
  it + 7 m perpendicular offset
- `osm_complex` — OSM centroid used directly (no road projection), for
  voters inside a `landuse=residential` polygon
- `osm_off_segment` — OSM centroid used directly, for voters whose OSM
  building geometrically projects outside the matched TIGER blockface
  (TIGER's address range exceeds its physical line length). Avoids
  snapping the voter to the wrong endpoint of a too-short segment.
- `tiger_only` — DENSE_RANK rank-ramp fallback when no OSM building
  matched
- `osm_only` — TIGER-miss voter rescued via direct OSM lookup

### assembly

```
persons_best_match  + refined_positions + osm_only_matches + persons_decomposed
    → canonical_addresses

persons_validated + canonical_addresses + refined_positions + osm_only_matches
                                        + persons_best_match
    → persons_geocoded
    → geocoding_summary
```

- `canonical_addresses` — produce the canonical `address_line_1` and
  `matched_tokens` for each voter. Street: OSM `osm_street` when an OSM
  match exists, else TIGER `full_name` (see "Street-name handling").
  Housenumber: OSM `osm_housenumber` when the voter's parsed
  housenumber and the OSM record's housenumber normalize to the same
  string (so surface-form variants like `646` ↔ `6-46` unify), else
  the voter's parsed form.
- `persons_geocoded` — final canonical Person record: identity fields
  from the voter file, canonical address, lat/lon from
  `refined_positions` OR `osm_only_matches`, `building_id` + `door_id`
  derived from the canonical address.
- `geocoding_summary` — match-rate diagnostics, broken down by
  `position_source`.

### aggregate

```
persons_geocoded → buildings_geocoded
                 → doors_geocoded
```

A **building** is one physical structure: `address_line_1 + zip5`. A **door**
is one unit within a building: `address_line_1 + address_line_2 + zip5`.
lat/lng is the centroid of contained voters — they share an address so
coordinates match within float noise.

### boundaries

Loads administrative polygons (NYC Election Districts, ZIP areas, Census
tracts, etc.) into `ducklake_geo.boundaries.{key_group}`. Three loaders
write to the same shape:

- `boundary_from_blocks` (preferred) — union the TIGER census blocks where
  voters with each key live, reading only blocks inside the version's
  `geo_scope`. No external shapefile needed; polygons match the voter file
  by construction.
- `boundary_from_geojson` — external GeoJSON (NYC Open Data, custom exports).
- `boundary_from_table` — already in DuckLake (TIGER ZCTAs, tracts, etc.).

### quickwit

Streams the Person records into a pre-existing Quickwit index for full-text
search. Uses the Quickwit CLI's `tool local-ingest` command over NDJSON
stdin.

## Address handling

The pipeline pulls address strings from three sources, each with its own
spelling conventions:

| Source     | Example for FDR Drive                                          |
| ---------- | -------------------------------------------------------------- |
| Voter file | `"FRANKLIN D ROOSEVELT DRIVE"`, `"FDR DRIVE"`, `"F D R DRIVE"` |
| TIGER      | `"F D R Dr"`                                                   |
| OSM        | `"FDR Drive"`                                                  |

The strategy is straightforward: convert each source to a sorted token
set so the same physical street produces the same tokens regardless of
spelling, then use those tokens for matching and joining. For
human-readable output, pick the most authoritative source as the
display form.

Everything lives in `src/addressing.py`.

### Building blocks

SQL helpers — each is a function returning a SQL fragment that callers
embed in their own queries:

| Helper                                          | Purpose                                                                                                 |
| ----------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| `street_rewrite_sql(col)`                       | Apply phrase-level `STREET_REWRITES` rewrites before tokenizing (e.g. `fdr` → `f d r`).                 |
| `tokenize_street_sql(col)`                      | Produce a sorted, deduped, lowercase-alphanumeric token array.                                          |
| `canonical_key_sql(tokens)`                     | Sort + `\|`-join an (expanded) token array into a join-key string.                                      |
| `housenumber_norm_sql(col)`                     | Normalize a house number: strip leading zeros after `^` or `-`, then strip hyphens. Used as a join key. |
| `housenumber_display_sql(prefix, number, half)` | Assemble the human-readable house-number string.                                                        |

Lookup tables backing them:

- **`STREET_REWRITES`** — phrase-level regex rewrites applied
  uniformly to every source before tokenization (collapsing
  abbreviations that won't be caught by tokenization alone).
- **`EQUIVALENT_TOKEN_GROUPS`** — token synonyms (`[st, street, saint]`,
  `[ave, avenue, av]`, `[1st, first]`, …) materialized into the
  `address_tokens` table. Used to _expand_ a tokenized set with
  equivalent tokens so abbreviations and full forms both appear.
- **`GENERIC_STREET_TOKENS`** — directionals and street-type suffixes
  (`east`, `street`, `avenue`, …) that don't identify a street on
  their own. Used only by the matching predicate (see below).

### The token pipeline

The same three-step pipeline runs on every source — voter raw strings
(`persons_decomposed`), TIGER `full_name` (`blockface_final`), and OSM
`addr:street` (`osm_building_lookup`):

```
raw → street_rewrite_sql → tokenize_street_sql → expand via address_tokens
```

After this, the same physical street produces the same token set
everywhere.

### Where the tokens get used

**1. Token-overlap matching** (`persons_candidates`). Voter tokens
intersect TIGER blockface tokens; passes if ≥ 2 tokens overlap and at
least one is non-generic. The non-generic requirement keeps voters
from matching wrong streets that happen to share only directionals or
street-type words.

**2. OSM lookup `canonical_key`** (`refined_positions`,
`osm_only_matches`). `canonical_key_sql(tokens)` produces the join-key
string. Both sides — voter and OSM record — produce the same
canonical_key for the same street, so the OSM lookup is a
strict-equality join on `(zip5, canonical_key, housenumber_norm)`.
Every token participates: not stripping "generic" suffixes keeps
parallel streets like `60 Place` and `60 Lane` distinct.

**3. Display `address_line_1`** (`canonical_addresses`). The human-
readable canonical address that drives `building_id`. The street part
prefers OSM's `addr:street` when an OSM match exists, falling back to
TIGER's canonical `full_name` otherwise. The housenumber part prefers
OSM's `addr:housenumber` when it normalizes to the same string as the
voter's parsed form (so surface-form variants like `646` ↔ `6-46`
unify); otherwise keeps the voter's form (so OSM subunit suffixes
like `100A` don't overwrite voter-correct forms like `100`).

OSM wins when available because OSM tags one canonical street and one
canonical housenumber per physical building — every voter at that
building converges on the same `address_line_1` and `building_id`.
TIGER's canonical `full_name` is itself the most-common alias for the
matched blockface (picked in `blockface_final`), so the fallback is
also stable across runs.

## building_id and door_id

```
building_id = "{address_line_1}|{zip5}"
door_id     = "{address_line_1}|{address_line_2}|{zip5}"
```

These are derived in `persons_geocoded`. The `|` separator keeps them
unambiguous. Single-family doors get an empty middle segment
(`address_line_1||zip5`) so `building_id` and `door_id` never collide.

`aggregate.buildings_geocoded` and `aggregate.doors_geocoded` GROUP BY
these keys to produce one row per physical structure / unit.

## Incremental vs non-incremental nodes

Most nodes are **incremental** — `CREATE TABLE IF NOT EXISTS` + insert
only new rows (`WHERE external_id NOT IN (SELECT external_id FROM …)`).
Re-running the pipeline after adding voters or a new county only processes
the new data.

The exceptions (drop + recreate every run) are:

- `blockface_final` — alias collapse + canonical-name pick need to see
  all rows; non-incremental.
- `osm_building_lookup` — depends on `STREET_REWRITES` and equivalency
  groups, both of which can change between runs.
- `refined_positions` — window functions partition on
  `(tiger_line_id, side)`; new voters would change everyone else's
  rank.
- `osm_only_matches` — same reason (the snap is keyed on the full set).
- `persons_geocoded` — pure assembly, cheap to redo.
- `buildings_geocoded` / `doors_geocoded` — aggregations over
  `persons_geocoded`.
- `geocoding_summary` — cheap diagnostic aggregate, always overwrites.

Other tables (`persons_decomposed`, `persons_candidates`,
`persons_scored`, `persons_best_match`, `canonical_addresses`) are
incremental. When you change SQL upstream of these, `pnpm data:clear`
(or `data:clear:all` if geo references changed too) is required to
rebuild — skipping the clear means existing rows keep their old values.

## Determinism

Same input + same code should produce byte-identical output across
runs. To preserve that:

- `arg_max(x, key)` and similar tie-prone aggregates need a tiebreaker
  in `key` (e.g. include `osm_id` after the primary criterion). The
  pure form picks arbitrarily on ties.
- `SELECT DISTINCT ON (k) …` needs an explicit `ORDER BY` that
  uniquely determines the row to keep.
- `ROW_NUMBER() OVER (… ORDER BY a, b, …)` is only deterministic when
  the ORDER BY clause uniquely orders the partition. Include enough
  columns to break every realistic tie.

When adding any of these constructs, lean on the natural keys
(`osm_id`, `blockface_id`, `full_name`, …) for the tiebreaker.

## Graph visualization

When a Hamilton module is added or modified, regenerate the visualizations:

```
uv run update-visualizations
```

Writes one PNG per module (`voter_file_loader_graph.png`, `tiger_graph.png`,
`osm_graph.png`, `matching_graph.png`, `geocode_graph.png`,
`assembly_graph.png`, `aggregate_graph.png`, `boundaries_graph.png`,
`quickwit_graph.png`) plus a combined `pipeline_graph.png` into `docs/`.
Graphviz must be installed (`brew install graphviz`).

## Voter data in queries

See the root `AGENTS.md` for the general rule. Specific to this package:

- `ducklake` holds per-organization Person data. Any `SELECT *` against `persons_validated`, the Person / Building / Door tables, or an address-matched intermediate returns row-level PII straight to the terminal.
- Inspect the DAG with schema reads and aggregates: `DESCRIBE`, `COUNT(*)`, `GROUP BY` on non-identity columns, null-rate checks. That answers almost every pipeline question.
- When a node's output must be eyeballed row by row, write it to Parquet or CSV and open it outside the session rather than printing it.
- `ducklake_geo` (TIGER blockfaces, OSM buildings, landuse, boundaries) carries no person data and is fine to query freely. The catalog is the line.
