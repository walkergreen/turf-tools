"""CLI entrypoints for the data package."""

from __future__ import annotations

import argparse
import glob
import os
from pathlib import Path
from typing import TYPE_CHECKING

from hamilton import driver

import duckdb
from src.dags import (
    aggregate,
    assembly,
    boundaries,
    geocode,
    matching,
    osm,
    quickwit,
    tiger,
)
from src.derived import compute_derived_metadata
from src.duckdb import OPERATIONAL_PG_ALIAS, attach_operational_postgres, get_connection
from src.geo.scope import format_scope, scope_metadata, scope_spec_from_settings
from src.geo.tiger_scope import county_match_rate_warnings, resolve_tiger_scope, scope_source
from src.import_progress import NullProgress
from src.importers.nys_voter_file import NysVoterFileImporter
from src.importers.registry import IMPORTERS, get_importer
from src.models import TableRef, quote_ident
from src.perf import TimingHook
from src.quickwit import ensure_index, persons_index_id
from src.settings import get_settings
from src.tables import (
    PERSON_CATALOG,
    dataset_version_schema,
    ensure_schema,
    finalize_version,
    schema_fqn,
)
from src.tables import (
    drop_schema as _drop_schema_helper,
)

if TYPE_CHECKING:
    from src.geo.scope import CountyScope
    from src.importers.base import Importer
    from src.settings import Settings


def _render(dr: driver.Driver, filename: str) -> None:
    docs = Path(__file__).resolve().parent.parent / "docs"
    docs.mkdir(exist_ok=True)
    path = str(docs / filename)
    dr.display_all_functions(path)
    print(f"Wrote {path}")


def update_visualizations() -> None:
    """Render all Hamilton graph visualizations into docs/.

    The importer front (source → persons_validated) is no longer a Hamilton
    module — it's `importers/nys_voter_file` — so the graphs below start from
    `persons_validated`, which the shared pipeline takes as an input.
    """
    _render(driver.Builder().with_modules(tiger).build(), "tiger_graph.png")
    _render(driver.Builder().with_modules(matching).build(), "matching_graph.png")
    _render(driver.Builder().with_modules(osm).build(), "osm_graph.png")
    _render(driver.Builder().with_modules(geocode).build(), "geocode_graph.png")
    _render(driver.Builder().with_modules(assembly).build(), "assembly_graph.png")
    _render(driver.Builder().with_modules(aggregate).build(), "aggregate_graph.png")
    _render(driver.Builder().with_modules(quickwit).build(), "quickwit_graph.png")
    _render(driver.Builder().with_modules(boundaries).build(), "boundaries_graph.png")
    _render(
        driver.Builder().with_modules(tiger, osm, matching, geocode, assembly, aggregate, quickwit).build(),
        "pipeline_graph.png",
    )


# ---------------------------------------------------------------------------
# Seed commands — one-shot ingests for reference data that's static enough to
# treat as "set up at deploy time, no UI yet". When admin upload flows land,
# these get a proper job runner; for now they're CLI invocations.
# ---------------------------------------------------------------------------


# Dataset-version schema the dev seed writes into by default (slug
# `nys_voter_file`, v1). `seed-persons` creates the matching dataset row and
# activates it for the seeded orgs; `resolve_version` resolves an org to it.
_DEFAULT_SCHEMA = "nys_voter_file_v1"

# Display name of the dataset row the dev seed creates for each importer.
_SEED_DATASET_NAMES = {
    "nys_voter_file": "NY State Voter File",
    "targetsmart": "TargetSmart Voter File",
}


def seed_boundaries() -> None:
    """Derive every key group's polygons from the persons data + TIGER blocks.

    For each configured key group, downloads (if missing) the TIGER
    census-block polygons for the configured counties and unions the blocks
    where persons tagged with each distinct key live. Output goes to the
    dataset-version schema, alongside `persons_geocoded`, `buildings_geocoded`.

    Requires ``seed-persons`` to have run first. Pair `--importer` and
    `--schema` here with whatever you passed to `seed-persons`:

        uv run seed-boundaries --importer targetsmart --schema targetsmart_v1
    """
    parser = argparse.ArgumentParser(prog="seed-boundaries", description=seed_boundaries.__doc__)
    parser.add_argument(
        "--importer",
        choices=sorted(IMPORTERS),
        default=NysVoterFileImporter.name,
        help=f"Importer whose manifest names the key groups to derive (default: {NysVoterFileImporter.name!r}).",
    )
    parser.add_argument(
        "--schema",
        default=None,
        help="Dataset-version schema to read persons from (default: '<importer>_v1').",
    )
    _add_tiger_scope_arg(parser)
    args = parser.parse_args()
    schema = args.schema or dataset_version_schema(args.importer, 1)

    # One boundary table per key group the importer's manifest declares; the
    # field's `column` is the key expression.
    key_group_sources = [
        {"key_group": fd.key_group, "key_expression": fd.column}
        for section in get_importer(args.importer)().manifest().fields
        for fd in section
        if fd.key_group is not None and fd.column is not None
    ]

    settings = get_settings()
    conn = get_connection(settings)

    # Single driver — Hamilton resolves the tiger_tabblock_raw →
    # boundary_from_blocks edge itself. tiger_tabblock_raw is idempotent so
    # re-running it once per key-group iteration is cheap after the first pass.
    dr = driver.Builder().with_modules(tiger, boundaries).build()

    # Persons table referenced by FQN — must already exist (seed_persons output).
    persons_ref = TableRef(
        catalog=PERSON_CATALOG,
        schema=schema,
        table="persons_geocoded",
        version=0,
    )

    # `persons_geocoded` carries the importer's `state` / `county_fips` columns
    # through, so the scope resolves from it exactly as the import did.
    geo_scope = _resolve_scope(conn, settings, persons_ref, args.tiger_scope)
    base_inputs = {
        "tiger_year": settings.tiger_year,
        "tiger_data_dir": settings.tiger_data_dir,
        "geo_scope": geo_scope,
        "persons_geocoded": persons_ref,
        "schema": schema,
        "conn": conn,
    }

    for source in key_group_sources:
        print(f"Deriving {source['key_group']}…")
        result = dr.execute(
            final_vars=["boundary_from_blocks"],
            inputs={
                **base_inputs,
                "key_group": source["key_group"],
                "key_expression": source["key_expression"],
            },
        )
        ref = result["boundary_from_blocks"]
        count = conn.sql(f"SELECT count(*) FROM {ref.fqn}").fetchone()[0]
        print(f"  → {count:,} polygons in {ref.fqn}")

    conn.close()
    print("Boundaries seeded.")


def seed_search() -> None:
    """Index a dataset version's persons into Quickwit for the Lookup search.

    Creates the per-version index (`persons_<schema>`) if absent, then
    local-ingests `persons_geocoded`. This is the standalone/backfill analogue of
    the import's indexing step — use it to make an existing version searchable
    without a re-import, or after `seed-persons` in dev. Requires the Quickwit
    searcher to be running (`pnpm dev:search`).

        uv run seed-search --schema nys_voter_file_v1
    """
    parser = argparse.ArgumentParser(prog="seed-search", description=seed_search.__doc__)
    parser.add_argument(
        "--schema",
        default=_DEFAULT_SCHEMA,
        help=f"Dataset-version schema to index persons from (default: {_DEFAULT_SCHEMA!r}).",
    )
    args = parser.parse_args()

    settings = get_settings()
    conn = get_connection(settings)
    index_id = persons_index_id(args.schema)
    ensure_index(settings, index_id)
    persons_ref = TableRef(catalog=PERSON_CATALOG, schema=args.schema, table="persons_geocoded", version=0)
    result = (
        driver.Builder()
        .with_modules(quickwit)
        .build()
        .execute(
            final_vars=["quickwit_build_manifest_stub"],
            inputs={
                "persons_table_ref": persons_ref,
                "quickwit_binary_path": settings.quickwit_binary,
                "quickwit_config_path": settings.quickwit_config_path,
                "quickwit_index_id": index_id,
                "quickwit_batch_size": settings.quickwit_batch_size,
                "conn": conn,
            },
        )["quickwit_build_manifest_stub"]
    )
    conn.close()
    print(f"Indexed {result.indexed_doc_count:,} persons into Quickwit index {index_id}.")


def reset_ducklake(include_geo: bool = False) -> None:
    """Drop the DuckLake metadata catalog(s) in Postgres so a data reset stays
    consistent. The catalog lives in Postgres (not a local file), so deleting
    `local_data/` alone leaves it referencing removed Parquet — the next write
    then fails on a missing file. Dropping the metadata schema makes the next
    attach recreate it empty. Person catalog by default; `include_geo` also drops
    the TIGER/OSM reference catalog. No-op when a catalog isn't Postgres-backed."""
    settings = get_settings()
    targets = [(settings.ducklake_metadata_postgres_url, settings.ducklake_meta_schema)]
    if include_geo:
        targets.append((settings.ducklake_geo_metadata_postgres_url, settings.ducklake_geo_meta_schema))

    conn = duckdb.connect()
    conn.install_extension("postgres")
    conn.load_extension("postgres")
    try:
        for i, (url, schema) in enumerate(targets):
            if not url or not schema:
                continue
            alias = f"_reset_meta_{i}"
            conn.execute(f"ATTACH '{url}' AS {alias} (TYPE postgres)")
            conn.execute(f"CALL postgres_execute('{alias}', $ft$DROP SCHEMA IF EXISTS {schema} CASCADE$ft$)")
            conn.execute(f"DETACH {alias}")
            print(f"  → Dropped DuckLake metadata schema {schema!r}.")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Seed persons: load a voter file → transform to Person schema
# → geocode against TIGER blockfaces → aggregate into buildings/doors.
# ---------------------------------------------------------------------------


def _ensure_seed_dataset(conn: duckdb.DuckDBPyConnection, schema: str, importer_name: str) -> str:
    """Dev seed: create the dataset + v1 row this DuckLake schema backs and grant
    it to every seeded org, returning the version id. This is what an import would
    write to Postgres, minus the UI — `db:mock` no longer creates it, so a clean
    boot without `data:mock` has no active dataset (the empty state). Idempotent.
    The version starts `importing`; `finalize_version` marks it `ready` once the
    pipeline finishes and only then does the caller activate it, so a half-run
    seed leaves orgs with no active version rather than a pointer to empty data."""
    slug, _, version_number = schema.rpartition("_v")
    pg = OPERATIONAL_PG_ALIAS
    conn.execute(
        f"CALL postgres_execute('{pg}', $ft$"
        f"INSERT INTO app.datasets (slug, name, importer) "
        f"VALUES ('{slug}', '{_SEED_DATASET_NAMES.get(importer_name, importer_name)}', '{importer_name}') "
        f"ON CONFLICT (slug) DO NOTHING$ft$)"
    )
    dataset_id = conn.execute(f"SELECT dataset_id FROM {pg}.app.datasets WHERE slug = ?", [slug]).fetchone()[0]
    conn.execute(
        f"CALL postgres_execute('{pg}', $ft$"
        f"INSERT INTO app.dataset_versions (dataset_id, version_number, status) "
        f"SELECT '{dataset_id}', {int(version_number)}, 'importing' "
        f"WHERE NOT EXISTS (SELECT 1 FROM app.dataset_versions "
        f"WHERE dataset_id = '{dataset_id}' AND version_number = {int(version_number)})$ft$)"
    )
    version_id = conn.execute(
        f"SELECT dataset_version_id FROM {pg}.app.dataset_versions WHERE dataset_id = ? AND version_number = ?",
        [dataset_id, int(version_number)],
    ).fetchone()[0]
    conn.execute(
        f"CALL postgres_execute('{pg}', $ft$"
        f"INSERT INTO app.dataset_organizations (dataset_id, organization_id) "
        f"SELECT '{dataset_id}', organization_id FROM app.organizations "
        f"ON CONFLICT (dataset_id, organization_id) DO NOTHING$ft$)"
    )
    return str(version_id)


def _activate_for_all_orgs(conn: duckdb.DuckDBPyConnection, version_id: str) -> None:
    """Dev seed: point every org at the freshly-seeded version. Real orgs choose
    theirs via "Make active"; the seed just wires it up so the UI has data."""
    conn.execute(
        f"CALL postgres_execute('{OPERATIONAL_PG_ALIAS}', $ft$"
        f"UPDATE app.organizations SET active_dataset_version_id = '{version_id}'$ft$)"
    )


def _add_tiger_scope_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--tiger-scope",
        default=None,
        help=(
            "Pin the geographic scope for this run, overriding TIGER_SCOPE "
            "(e.g. '36:005,047,061,081,085' or '36:*'). Default: settings, else derived from the data."
        ),
    )


def _resolve_scope(
    conn: duckdb.DuckDBPyConnection,
    settings: Settings,
    persons: TableRef,
    override: str | None,
    notes: list[str] | None = None,
) -> list[CountyScope]:
    """Resolve and print the (state, county) scope for `persons`, honoring a
    `--tiger-scope` override ahead of settings. Warnings are printed and, when
    `notes` is given, collected there for the version's geoScope metadata."""
    spec, _ = (override, "settings") if override else scope_spec_from_settings(settings)

    def warn(message: str) -> None:
        print(message)
        if notes is not None:
            notes.append(message)

    geo_scope = resolve_tiger_scope(
        conn, persons, spec=spec, tiger_year=settings.tiger_year, tiger_data_dir=settings.tiger_data_dir, warn=warn
    )
    print(f"  TIGER scope: {format_scope(geo_scope)} (cache: {settings.tiger_data_dir})")
    osm_warning = osm.osm_url_scope_warning(geo_scope, settings.osm_url, settings.osm_urls)
    if osm_warning:
        warn(osm_warning)
    return geo_scope


def seed_persons() -> None:
    """Import a voter file → geocode → aggregate into buildings/doors.

    Writes into the dataset-version schema (default `<importer>_v1`, e.g.
    `nys_voter_file_v1` — the dataset the mock seeds and the seeded orgs resolve
    to). `--importer` picks the importer from the registry; `--source` takes a
    path, glob, or URL; override the target with `--schema`:

        uv run seed-persons --source fixtures/ny-voters-10k-sample.parquet
        uv run seed-persons --source https://example.com/voters.parquet --schema nys_voter_file_v1
        uv run seed-persons --importer targetsmart --source ~/exports/targetsmart/part-*.parquet

    Pass `--reset` to drop the schema first so the next run rebuilds every
    table from scratch (intermediate DAG nodes are incremental, so a pipeline
    schema change won't otherwise auto-apply).

    Final outputs (under the dataset-version schema):
    - ``persons_geocoded`` — canonical person record (canonicalized address,
      lat/lng, blockface metadata, derived `building_id` / `door_id`).
    - ``buildings_geocoded`` — one row per distinct building.
    - ``doors_geocoded`` — one row per distinct door.

    First run downloads the TIGER shapefiles for each county (a few minutes);
    subsequent runs reuse the on-disk cache.
    """
    parser = argparse.ArgumentParser(prog="seed-persons", description=seed_persons.__doc__)
    parser.add_argument(
        "--source",
        required=True,
        help="Path or URL of the voter file to seed from (parquet, CSV, or the raw fixed-width .txt).",
    )
    parser.add_argument(
        "--importer",
        choices=sorted(IMPORTERS),
        default=NysVoterFileImporter.name,
        help=f"Importer to load the source with (default: {NysVoterFileImporter.name!r}).",
    )
    parser.add_argument(
        "--schema",
        default=None,
        help="Target dataset-version schema (default: '<importer>_v1', e.g. 'nys_voter_file_v1').",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Drop the schema before running. Use after a pipeline schema change.",
    )
    parser.add_argument(
        "--timing",
        action="store_true",
        help="Print per-node wall time as the pipeline runs, plus a sorted summary at the end.",
    )
    _add_tiger_scope_arg(parser)
    args = parser.parse_args()
    schema = args.schema or dataset_version_schema(args.importer, 1)

    settings = get_settings()
    conn = get_connection(settings)

    # Fail fast: the manifest write at the end needs operational Postgres, so
    # attach up front rather than crashing after the minute-long geocode.
    attach_operational_postgres(conn, settings)

    # Create the dataset/version rows the pipeline fills (grant them to the seeded
    # orgs). Activation waits until the pipeline finishes — see below.
    version_id = _ensure_seed_dataset(conn, schema, args.importer)

    if args.reset:
        print(f"Dropping schema {schema_fqn(schema)}…")
        _drop_schema_helper(conn, schema)
        ensure_schema(conn, schema)

    # `glob` rather than a plain existence test: a Parquet export may be
    # addressed as `part-*.parquet`, which the importer hands to DuckDB as-is.
    source = os.path.expanduser(args.source)
    if "://" not in source and not glob.glob(source, recursive=True):
        print(f"Voter file not found at {source}.")
        return

    print(f"Seeding persons from {source} (schema={schema})…")
    if args.importer == NysVoterFileImporter.name and settings.voter_zip5_filter:
        print(f"  Voter ZIP5 filter (dev scope): {settings.voter_zip5_filter}")

    # Import the voter file (source → persons_validated) outside Hamilton, then
    # run the shared pipeline from that seam. The NYS importer takes the dev
    # ZIP5 slice as instance config (the fixture is already NYC-only, so no
    # county filter); every other importer is constructed without arguments.
    importer: Importer
    if args.importer == NysVoterFileImporter.name:
        importer = NysVoterFileImporter(zip5_filter=settings.voter_zip5_filter)
    else:
        importer = get_importer(args.importer)()
    persons_validated = importer.load(source, schema, conn, NullProgress())
    scope_notes: list[str] = []
    geo_scope = _resolve_scope(conn, settings, persons_validated, args.tiger_scope, scope_notes)

    timing = TimingHook() if args.timing else None
    builder = driver.Builder().with_modules(
        tiger,
        osm,
        matching,
        geocode,
        assembly,
        aggregate,
    )
    if timing is not None:
        builder = builder.with_adapters(timing)
    dr = builder.build()
    result = dr.execute(
        final_vars=[
            "persons_geocoded",
            "geocoding_summary",
            "buildings_geocoded",
            "doors_geocoded",
            "utm_epsg",
            "osm_pbfs",
        ],
        inputs={
            "persons_validated": persons_validated,
            "geo_scope": geo_scope,
            "schema": schema,
            "tiger_year": settings.tiger_year,
            "tiger_data_dir": settings.tiger_data_dir,
            "osm_url_template": settings.osm_url_template,
            "osm_url_pins": settings.osm_url_pins,
            "osm_urls": settings.osm_urls,
            "osm_url": settings.osm_url,
            "osm_data_dir": settings.osm_data_dir,
            "conn": conn,
        },
    )
    print(f"  UTM zone: EPSG:{result['utm_epsg']}; OSM extracts: {[osm.extract_id(p) for p in result['osm_pbfs']]}")

    geocoded_ref = result["persons_geocoded"]
    for message in county_match_rate_warnings(conn, geocoded_ref.fqn):
        print(f"  {message}")
        scope_notes.append(message)
    summary_ref = result["geocoding_summary"]
    buildings_ref = result["buildings_geocoded"]
    doors_ref = result["doors_geocoded"]
    (total, matched, unmatched, pct, m_road, m_complex, m_off_seg, m_tiger_only, m_osm_only) = conn.sql(f"""
        SELECT total_persons, matched, unmatched, match_pct,
               matched_osm_road_projected, matched_osm_complex,
               matched_osm_off_segment,
               matched_tiger_only, matched_osm_only
        FROM {summary_ref.fqn}
    """).fetchone()
    building_count = conn.sql(f"SELECT count(*) FROM {buildings_ref.fqn}").fetchone()[0]
    door_count = conn.sql(f"SELECT count(*) FROM {doors_ref.fqn}").fetchone()[0]
    print(
        f"  → {matched:,}/{total:,} matched ({pct}%); {unmatched:,} unmatched.\n"
        f"      OSM road-projected : {m_road:>8,d}\n"
        f"      OSM complex        : {m_complex:>8,d}\n"
        f"      OSM off-segment    : {m_off_seg:>8,d}\n"
        f"      TIGER rank fallback: {m_tiger_only:>8,d}\n"
        f"      OSM-only (TIGER-miss rescue): {m_osm_only:>8,d}\n"
        f"  → {building_count:,} buildings, {door_count:,} doors.\n"
        f"  → Outputs: {geocoded_ref.fqn}, {buildings_ref.fqn}, {doors_ref.fqn}"
    )

    # Land the manifest + row count on the version and mark it `ready`, then
    # activate it for the seeded orgs — only now that the data exists, so a
    # crash mid-pipeline never leaves an org pointing at an empty dataset.
    manifest = importer.manifest()
    configured_spec, configured_source = scope_spec_from_settings(settings)
    derived = compute_derived_metadata(
        conn,
        geocoded_ref.fqn,
        manifest,
        geo_scope=scope_metadata(
            geo_scope,
            source="settings" if args.tiger_scope else scope_source(configured_spec, configured_source),
            tiger_year=settings.tiger_year,
            osm_extracts=[osm.extract_id(p) for p in result["osm_pbfs"]],
            utm_epsg=result["utm_epsg"],
            notes=scope_notes,
        ),
    )
    finalize_version(conn, settings, version_id, manifest, derived)
    _activate_for_all_orgs(conn, version_id)
    person_count = derived["rowCount"]
    print(f"  → Wrote manifest + row_count={person_count:,}; activated version {version_id} for all orgs.")

    if timing is not None:
        timing.print_summary()
    conn.close()
    print("Persons seeded.")


def rename_schema() -> None:
    """Rename a DuckLake schema (``ducklake.<from>`` → ``ducklake.<to>``).

    Used when a dataset's slug changes so its version schemas stay addressable.
    No-op if the source schema doesn't exist.

        uv run rename-schema --from old-slug_v1 --to new-slug_v1
    """
    parser = argparse.ArgumentParser(prog="rename-schema", description=rename_schema.__doc__)
    parser.add_argument("--from", dest="src", required=True, help="Current schema name.")
    parser.add_argument("--to", dest="dst", required=True, help="New schema name.")
    args = parser.parse_args()

    if args.src == args.dst:
        print("--from and --to must differ.")
        return

    settings = get_settings()
    conn = get_connection(settings)

    src_exists = (
        conn.execute(
            "SELECT 1 FROM information_schema.schemata WHERE catalog_name = ? AND schema_name = ?",
            [PERSON_CATALOG, args.src],
        ).fetchone()
        is not None
    )
    if not src_exists:
        print(f"No schema {schema_fqn(args.src)} — nothing to rename.")
        conn.close()
        return

    conn.execute(f"ALTER SCHEMA {schema_fqn(args.src)} RENAME TO {quote_ident(args.dst)}")
    conn.close()
    print(f"Renamed schema: {args.src} → {args.dst}.")


def drop_schema() -> None:
    """Drop ``ducklake.<schema>`` and all tables in it (CASCADE). Idempotent.

    uv run drop-schema --schema nys_voter_file_v1
    """
    parser = argparse.ArgumentParser(prog="drop-schema", description=drop_schema.__doc__)
    parser.add_argument("--schema", required=True, help="Schema to drop.")
    args = parser.parse_args()

    settings = get_settings()
    conn = get_connection(settings)
    _drop_schema_helper(conn, args.schema)
    conn.close()
    print(f"Dropped schema {schema_fqn(args.schema)}.")
