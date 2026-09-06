"""Prepare OSM reference data for address matching.

Parallels `tiger.py`: extracts OSM-derived data into `ducklake_geo`
for the geocoding pipeline. The actual lat/lon assignment (using
both TIGER blockfaces and OSM buildings) lives in `geocode.py`.

    geo_scope ─► osm_extract_urls ─► osm_pbfs ─┬─► osm_buildings_polygons   (building polygon centroids)
                                               ├─► osm_addresses            (addressed OSM elements)
                                               └─► osm_landuse_residential  (residential land-use polygons)

    osm_addresses + osm_landuse_residential + address_tokens
        ─► osm_building_lookup               (per-building, keyed for join)

One PBF extract per state in scope. The three raw tables carry an `extract`
column (the PBF filename stem) and are loaded incrementally per extract, so
a second state appends rather than being skipped. `osm_building_lookup` is
the consumer-facing output — one row per OSM-known building, keyed on
`(zip_code, canonical_key, housenumber_norm)` — built from this version's
extracts only (`osm_pbfs`), so rows an earlier import loaded for another
state or an older snapshot never feed this version. Those rows stay in the
raw tables as a cache until deleted (the maintenance SQL is in AGENTS.md).
"""

import json
import os
import shutil
import subprocess
import time
import urllib.request
from collections.abc import Sequence
from pathlib import Path

import duckdb
from src.addressing import (
    canonical_key_sql,
    housenumber_norm_sql,
    street_rewrite_sql,
    tokenize_street_sql,
)
from src.geo.geofabrik import extract_id, osm_url_for_state, slug_for_url
from src.geo.scope import CountyScope, scope_states
from src.models import TableRef

GEO_CATALOG = "ducklake_geo"
OSM_SCHEMA = "osm"
# The per-extract raw tables, in load order.
RAW_TABLES = ("buildings_polygons", "addresses", "landuse_residential")


def _fqn(table: str) -> str:
    return f"{GEO_CATALOG}.{OSM_SCHEMA}.{table}"


def _ensure_schema(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(f"CREATE SCHEMA IF NOT EXISTS {GEO_CATALOG}.{OSM_SCHEMA}")


def _current_version(conn: duckdb.DuckDBPyConnection) -> int:
    return conn.sql(f"FROM {GEO_CATALOG}.current_snapshot()").fetchone()[0]


def _extract_loaded(conn: duckdb.DuckDBPyConnection, fqn: str, extract: str) -> bool:
    """Whether `fqn` already holds rows for `extract`."""
    return conn.execute(f"SELECT count(*) FROM {fqn} WHERE extract = ?", [extract]).fetchone()[0] > 0


def _extract_in_sql(column: str, extracts: Sequence[str]) -> str:
    """Predicate keeping rows whose `column` is one of `extracts`; ``FALSE``
    for an empty list so a caller never selects every extract by accident."""
    if not extracts:
        return "FALSE"
    quoted = ", ".join("'" + e.replace("'", "''") + "'" for e in extracts)
    return f"{column} IN ({quoted})"


def _drop_unless_columns(conn: duckdb.DuckDBPyConnection, fqn: str, required: set[str], forbidden: set[str]) -> None:
    """Schema migration for the raw tables: drop `fqn` when its columns are not
    the current shape (missing `required`, or carrying `forbidden`), so the
    `CREATE TABLE IF NOT EXISTS` that follows rebuilds it. The PBF and osmium
    caches survive, so a rebuild re-reads local files only."""
    try:
        cols = {c[0] for c in conn.execute(f"DESCRIBE {fqn}").fetchall()}
    except duckdb.CatalogException:
        return  # table doesn't exist yet — fresh install
    if (required - cols) or (cols & forbidden):
        conn.execute(f"DROP TABLE {fqn}")


# ---------------------------------------------------------------------------
# Node 0 – which extracts, and download (or reuse) each PBF
# ---------------------------------------------------------------------------


def osm_url_scope_warning(geo_scope: list[CountyScope], osm_url: str | None, osm_urls: list[str] | None) -> str | None:
    """The warning `osm_extract_urls` prints when a single `osm_url` that is not
    named for a Geofabrik state extract has to stand in for a multi-state
    scope; `None` when the configuration is consistent. Pure, so the import
    job can put the same message in the job log."""
    if osm_urls or not osm_url or slug_for_url(osm_url) is not None:
        return None
    states = scope_states(geo_scope)
    if len(states) <= 1:
        return None
    return (
        f"WARNING: OSM_URL ({osm_url.rsplit('/', 1)[-1]}) is not named for a Geofabrik state extract, so it is "
        f"ingested verbatim as the only extract, but the scope spans {len(states)} states ({', '.join(states)}); "
        "set OSM_URLS (one extract per state) or unset OSM_URL to resolve one extract per state"
    )


def osm_extract_urls(
    geo_scope: list[CountyScope],
    osm_url_template: str,
    osm_url_pins: dict[str, str],
    osm_urls: list[str] | None = None,
    osm_url: str | None = None,
) -> list[str]:
    """The PBF extract URLs to ingest for this dataset version.

    Precedence: an explicit `osm_urls` list (verbatim, order-preserving
    dedupe); else one URL per state in `geo_scope` — the pinned URL for the
    state's Geofabrik slug in `osm_url_pins` when present, otherwise
    `osm_url_template` with ``{state}`` filled by the slug. A single
    `osm_url` joins that per-state resolution as the pin for the state its
    filename names (``new-york-260501.osm.pbf`` pins ``new-york``), so a
    scope in another state still gets that state's extract rather than New
    York's; an `osm_url` not named for any Geofabrik state (a BBBike city
    extract) is ingested verbatim as the only extract, with
    `osm_url_scope_warning` printed when the scope spans more than one state.
    """
    if osm_urls:
        return list(dict.fromkeys(osm_urls))
    states = scope_states(geo_scope)
    pins = osm_url_pins
    if osm_url:
        slug = slug_for_url(osm_url)
        if slug is None:
            warning = osm_url_scope_warning(geo_scope, osm_url, osm_urls)
            if warning:
                print(warning)
            return [osm_url]
        pins = {**osm_url_pins, slug: osm_url}
    return [osm_url_for_state(s, osm_url_template, pins) for s in states]


def _download_pbf(url: str, cache: Path) -> tuple[Path, bool]:
    """The local PBF for `url`, and whether it was downloaded just now rather
    than reused from `cache`. The download streams into a ``.part`` file that
    is renamed onto the final name only once complete, so an interrupted
    transfer never leaves a truncated PBF under the cached name."""
    filename = url.rsplit("/", 1)[-1]
    pbf_path = cache / filename
    if pbf_path.exists():
        size_mb = pbf_path.stat().st_size / (1024 * 1024)
        print(f"OSM PBF: {filename} ({size_mb:.1f} MB, cached)")
        return pbf_path, False
    print(f"Downloading OSM PBF: {url}")
    part = pbf_path.with_name(filename + ".part")
    try:
        urllib.request.urlretrieve(url, part)  # noqa: S310
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    os.replace(part, pbf_path)
    size_mb = pbf_path.stat().st_size / (1024 * 1024)
    print(f"  done ({size_mb:.1f} MB)")
    return pbf_path, True


def _osmium_cache_paths(pbf: Path) -> tuple[Path, Path]:
    """The two osmium products `osm_buildings_polygons` keeps next to `pbf`:
    the building-filtered PBF and the assembled-polygon GeoJSONSeq."""
    return pbf.with_name(f"{pbf.stem}-buildings.osm.pbf"), pbf.with_name(f"{pbf.stem}-buildings.geojsonseq")


def _invalidate_extract(conn: duckdb.DuckDBPyConnection, pbf: Path) -> int:
    """Forget everything derived from an earlier download of `pbf`'s extract:
    its rows in every raw table and the osmium caches beside it. A freshly
    downloaded ``-latest`` file is a different snapshot under the same
    extract id, and the per-extract loaders would otherwise keep one table on
    the old snapshot while filling another from the new one.

    Returns the number of rows dropped. A table that holds no rows for the
    extract is left untouched (no DELETE, so no DuckLake snapshot), and an
    extract downloaded for the first time is reported as such rather than as
    a reload."""
    extract = extract_id(pbf)
    dropped = 0
    for name in RAW_TABLES:
        fqn = _fqn(name)
        try:
            cols = {c[0] for c in conn.execute(f"DESCRIBE {fqn}").fetchall()}
        except duckdb.CatalogException:
            continue
        if "extract" not in cols:
            continue
        held = conn.execute(f"SELECT count(*) FROM {fqn} WHERE extract = ?", [extract]).fetchone()[0]
        if held == 0:
            continue
        conn.execute(f"DELETE FROM {fqn} WHERE extract = ?", [extract])
        dropped += held
    stale_caches = [cached for cached in _osmium_cache_paths(pbf) if cached.exists()]
    for cached in stale_caches:
        cached.unlink()
    if dropped or stale_caches:
        print(
            f"  downloaded {extract} again: dropped {dropped:,} rows and {len(stale_caches)} osmium cache "
            "file(s) from an earlier download; every OSM table reloads it"
        )
    else:
        print(f"  first download of {extract}: nothing to invalidate")
    return dropped


def osm_pbfs(osm_extract_urls: list[str], osm_data_dir: str, conn: duckdb.DuckDBPyConnection) -> list[Path]:
    """Download each extract into ``osm_data_dir`` if not present; return the
    local paths. A freshly downloaded extract is a new snapshot, so its rows
    from any earlier download are dropped from the raw tables first
    (`_invalidate_extract`) and every loader reloads it; deleting a cached PBF
    is therefore the supported way to refresh a ``-latest`` extract."""
    cache = Path(osm_data_dir)
    cache.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for url in osm_extract_urls:
        path, fresh = _download_pbf(url, cache)
        if fresh:
            _invalidate_extract(conn, path)
        paths.append(path)
    return paths


# ---------------------------------------------------------------------------
# Node 2 – building polygons (via osmium-tool)
# ---------------------------------------------------------------------------


def _require_osmium() -> str:
    """Locate osmium-tool on PATH or fail with an install hint."""
    osmium = shutil.which("osmium")
    if osmium is None:
        raise RuntimeError(
            "osmium-tool not found on PATH. Install:\n"
            "  macOS:  brew install osmium-tool\n"
            "  Debian: apt install osmium-tool"
        )
    return osmium


def osm_buildings_polygons(
    osm_pbfs: list[Path],
    conn: duckdb.DuckDBPyConnection,
) -> TableRef:
    """Building polygons with area-weighted centroids.

    osmium-tool assembles closed-way + multipolygon-relation building
    geometries from each PBF (in C++, streaming, off-disk) and emits a
    GeoJSONSeq. We then load it via `ST_Read` and compute
    `ST_Centroid` per polygon — area-weighted so it's robust to vertex
    density artifacts (the bias we saw with in-DB vertex-means).

    `ST_PointOnSurface` fallback when the centroid lands outside a
    concave polygon (L-buildings, courtyard apartment blocks).

    Two on-disk caches live next to each PBF:
      - `<stem>-buildings.osm.pbf`     (filtered PBF, building-tagged
                                        ways/relations + their refs)
      - `<stem>-buildings.geojsonseq`  (assembled polygon features)

    Incremental per extract: an extract with rows already present is
    skipped. To rebuild an extract, delete its cached PBF (`osm_pbfs` drops
    the extract's rows and osmium caches on the next download) or DELETE
    its rows from this table (the cached geojsonseq is then reloaded).
    """
    table = "buildings_polygons"
    fqn = _fqn(table)

    _ensure_schema(conn)

    # Nothing downstream reads the full polygon `geom` (osm_addresses only
    # consumes centroid_lat/centroid_lon), so a table carrying it — or one
    # without the `extract` column — is rebuilt in the current shape rather
    # than serializing ~1M WKB blobs per DuckLake write.
    _drop_unless_columns(conn, fqn, required={"extract"}, forbidden={"geom"})

    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {fqn} (
            osm_id        BIGINT,
            centroid_lat  DOUBLE,
            centroid_lon  DOUBLE,
            extract       VARCHAR
        )
    """)

    pending = [pbf for pbf in osm_pbfs if not _extract_loaded(conn, fqn, extract_id(pbf))]
    if not pending:
        return TableRef(catalog=GEO_CATALOG, schema=OSM_SCHEMA, table=table, version=_current_version(conn))

    osmium = _require_osmium()

    for pbf in pending:
        extract = extract_id(pbf)
        filtered_pbf, geojson_path = _osmium_cache_paths(pbf)
        config_path = pbf.with_name("osmium-export-buildings.json")

        # osmium-export config (https://docs.osmcode.org/osmium/latest/osmium-export.html):
        #   - emit @id and @type as the only attributes
        #   - linear_tags=false: skip linestrings entirely
        #   - area_tags=true:    export every area in the filtered PBF
        #   - include_tags=[building]: keep only the `building` tag as a
        #     property column. Avoids GDAL's case-insensitive collision
        #     between `fixme` / `FIXME` etc. when loading the geojsonseq.
        # Always rewrite so config edits take effect on next run.
        config_path.write_text(
            json.dumps(
                {
                    "attributes": {"id": True, "type": True},
                    "linear_tags": False,
                    "area_tags": True,
                    "include_tags": ["building"],
                }
            )
        )

        if not filtered_pbf.exists():
            print(f"Filtering buildings from {pbf.name}…")
            subprocess.run(
                [osmium, "tags-filter", str(pbf), "wr/building", "-o", str(filtered_pbf)],
                check=True,
            )
            size_mb = filtered_pbf.stat().st_size / (1024 * 1024)
            print(f"  done ({size_mb:.1f} MB)")

        if not geojson_path.exists():
            print(f"Exporting building polygons from {filtered_pbf.name} to GeoJSONSeq…")
            subprocess.run(
                [
                    osmium,
                    "export",
                    str(filtered_pbf),
                    "-c",
                    str(config_path),
                    "-f",
                    "geojsonseq",
                    "-o",
                    str(geojson_path),
                ],
                check=True,
            )
            size_mb = geojson_path.stat().st_size / (1024 * 1024)
            print(f"  done ({size_mb:.1f} MB)")
        else:
            size_mb = geojson_path.stat().st_size / (1024 * 1024)
            print(f"Building GeoJSONSeq: {geojson_path.name} ({size_mb:.1f} MB, cached)")

        print(f"Loading building polygons + computing centroids for {extract}…")
        conn.execute(f"""
            INSERT INTO {fqn}
            WITH polys AS (
                SELECT TRY_CAST("@id" AS BIGINT) AS osm_id, geom
                FROM ST_Read('{geojson_path}')
                WHERE geom IS NOT NULL
            ),
            centroided AS (
                SELECT osm_id, geom, ST_Centroid(geom) AS c FROM polys
            ),
            anchored AS (
                SELECT osm_id,
                    CASE WHEN ST_Contains(geom, c) THEN c
                         ELSE ST_PointOnSurface(geom) END AS pt
                FROM centroided
            )
            SELECT osm_id, ST_Y(pt) AS centroid_lat, ST_X(pt) AS centroid_lon, '{extract}' AS extract
            FROM anchored
            WHERE osm_id IS NOT NULL
        """)
        n = conn.execute(f"SELECT count(*) FROM {fqn} WHERE extract = ?", [extract]).fetchone()[0]
        print(f"  {n:,} building polygons loaded from {extract}")

    return TableRef(catalog=GEO_CATALOG, schema=OSM_SCHEMA, table=table, version=_current_version(conn))


# ---------------------------------------------------------------------------
# Node 3 – addressed OSM elements, raw positions only
# ---------------------------------------------------------------------------


def _stage_addressed(conn: duckdb.DuckDBPyConnection, pbf: Path) -> None:
    """One `ST_ReadOSM` pass over `pbf` into the temp table `_addressed`:
    every node/way carrying both `addr:housenumber` and `addr:street`, with
    its raw lat/lon (NULL for ways) and address tags."""
    conn.execute(f"""
        CREATE OR REPLACE TEMP TABLE _addressed AS
        SELECT
            id, kind, lat, lon,
            list_extract(map_extract(tags, 'addr:housenumber'), 1) AS housenumber,
            list_extract(map_extract(tags, 'addr:street'),       1) AS street,
            list_extract(map_extract(tags, 'addr:unit'),         1) AS unit,
            list_extract(map_extract(tags, 'addr:postcode'),     1) AS zip_code,
            list_extract(map_extract(tags, 'addr:city'),         1) AS city,
            list_extract(map_extract(tags, 'addr:state'),        1) AS state,
            list_extract(map_extract(tags, 'building'),          1) AS building
        FROM ST_ReadOSM('{pbf}')
        WHERE kind IN ('node', 'way')
          AND list_extract(map_extract(tags, 'addr:housenumber'), 1) IS NOT NULL
          AND list_extract(map_extract(tags, 'addr:street'),       1) IS NOT NULL
    """)


def osm_addresses(
    osm_pbfs: list[Path],
    osm_buildings_polygons: TableRef,
    conn: duckdb.DuckDBPyConnection,
) -> TableRef:
    """One row per address-tagged OSM element with its raw lat/lon.

    Nodes use their own (door) lat/lon. Ways are looked up in
    `osm_buildings_polygons` — within the same extract, since Geofabrik
    state extracts overlap at borders and the same way can appear in two —
    to use the area-weighted ST_Centroid of the assembled polygon
    (osmium-tool extraction). Way-tagged addresses whose polygon couldn't
    be assembled (multipolygon relations not yet supported, or otherwise)
    are dropped.

    Incremental per extract: an extract with rows already present is skipped.
    Each extract lands in a single INSERT, so an interrupted load leaves no
    partial extract behind and the next run retries it.
    """
    table = "addresses"
    fqn = _fqn(table)

    _ensure_schema(conn)
    _drop_unless_columns(conn, fqn, required={"extract"}, forbidden=set())
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {fqn} (
            osm_id        BIGINT,
            kind          VARCHAR,
            housenumber   VARCHAR,
            street        VARCHAR,
            unit          VARCHAR,
            zip_code      VARCHAR,
            city          VARCHAR,
            state         VARCHAR,
            building      VARCHAR,
            lat           DOUBLE,
            lon           DOUBLE,
            extract       VARCHAR
        )
    """)

    polys_fqn = osm_buildings_polygons.fqn

    for pbf in osm_pbfs:
        extract = extract_id(pbf)
        if _extract_loaded(conn, fqn, extract):
            continue

        print(f"Loading addressed OSM elements from {extract}…")
        _stage_addressed(conn, pbf)

        print(f"  writing to {fqn}…")
        conn.execute(f"""
            INSERT INTO {fqn}
            SELECT
                a.id   AS osm_id,
                a.kind,
                a.housenumber,
                a.street,
                a.unit,
                a.zip_code,
                a.city,
                a.state,
                a.building,
                CASE WHEN a.kind = 'way' THEN p.centroid_lat ELSE a.lat END AS lat,
                CASE WHEN a.kind = 'way' THEN p.centroid_lon ELSE a.lon END AS lon,
                '{extract}' AS extract
            FROM _addressed a
            LEFT JOIN {polys_fqn} p ON p.osm_id = a.id AND p.extract = '{extract}'
            WHERE (a.kind = 'way'  AND p.centroid_lat IS NOT NULL)
               OR (a.kind = 'node' AND a.lat IS NOT NULL)
        """)

        by_kind = conn.execute(
            f"SELECT kind, count(*) AS n FROM {fqn} WHERE extract = ? GROUP BY 1 ORDER BY n DESC", [extract]
        ).fetchall()
        print(f"  loaded {sum(n for _, n in by_kind):,} OSM addresses from {extract}:")
        for kind, n in by_kind:
            print(f"    {kind:>4}: {n:,}")

    return TableRef(catalog=GEO_CATALOG, schema=OSM_SCHEMA, table=table, version=_current_version(conn))


# ---------------------------------------------------------------------------
# Node 3 – landuse=residential polygons (for the future complex-override step)
# ---------------------------------------------------------------------------


def _stage_landuse(conn: duckdb.DuckDBPyConnection, pbf: Path) -> None:
    """Two `ST_ReadOSM` passes over `pbf` into temp tables: `_landuse_res`
    (every ``landuse=residential`` way with its node refs and name) and
    `_landuse_node_pos` (the lat/lon of every node those ways reference)."""
    conn.execute(f"""
        CREATE OR REPLACE TEMP TABLE _landuse_res AS
        SELECT id, refs,
               list_extract(map_extract(tags, 'name'), 1) AS name
        FROM ST_ReadOSM('{pbf}') o
        WHERE kind = 'way'
          AND list_extract(map_extract(tags, 'landuse'), 1) = 'residential'
    """)

    print("  reading node positions for landuse refs…")
    conn.execute("""
        CREATE OR REPLACE TEMP TABLE _needed_node_ids AS
        SELECT DISTINCT u.ref_id AS id FROM _landuse_res, UNNEST(refs) AS u(ref_id)
    """)
    conn.execute(f"""
        CREATE OR REPLACE TEMP TABLE _landuse_node_pos AS
        SELECT o.id, o.lat, o.lon
        FROM ST_ReadOSM('{pbf}') o
        JOIN _needed_node_ids n ON n.id = o.id
        WHERE o.kind = 'node'
    """)


def osm_landuse_residential(
    osm_pbfs: list[Path],
    conn: duckdb.DuckDBPyConnection,
) -> TableRef:
    """Assembled `landuse=residential` polygons. Way-only — multipolygon
    relations are rare (~14 of ~13k state-wide) and skipped in v1.

    Used downstream as the test for "is this voter inside a residential
    complex" — the complex-centroid override step that bypasses the
    blockface projection.

    Incremental per extract: an extract with rows already present is
    skipped. The temp tables (`_stage_landuse`) are rebuilt per extract, so
    peak memory is that of the largest single extract, not the sum. An
    extract with no residential ways at all leaves no rows and is re-read on
    every run.
    """
    table = "landuse_residential"
    fqn = _fqn(table)

    _ensure_schema(conn)
    _drop_unless_columns(conn, fqn, required={"extract"}, forbidden=set())
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {fqn} (
            landuse_id  BIGINT,
            name        VARCHAR,
            geom        GEOMETRY,
            extract     VARCHAR
        )
    """)

    for pbf in osm_pbfs:
        extract = extract_id(pbf)
        if _extract_loaded(conn, fqn, extract):
            continue

        print(f"Loading landuse=residential ways from {extract}…")
        _stage_landuse(conn, pbf)

        print("  assembling polygons (close-aware)…")
        conn.execute(f"""
            INSERT INTO {fqn}
            WITH ordered_pts AS (
                SELECT lr.id AS landuse_id, lr.name, u.idx, np.lat, np.lon
                FROM _landuse_res lr,
                     UNNEST(lr.refs) WITH ORDINALITY AS u(ref_id, idx)
                JOIN _landuse_node_pos np ON np.id = u.ref_id
            ),
            agg AS (
                SELECT landuse_id, name,
                       CASE WHEN list_extract(list(lat ORDER BY idx), 1)
                              = list_extract(list(lat ORDER BY idx DESC), 1)
                            AND list_extract(list(lon ORDER BY idx), 1)
                              = list_extract(list(lon ORDER BY idx DESC), 1)
                            THEN list(ST_Point(lon, lat) ORDER BY idx)
                            ELSE list_concat(
                                list(ST_Point(lon, lat) ORDER BY idx),
                                [list_extract(list(ST_Point(lon, lat) ORDER BY idx), 1)]
                            )
                       END AS closed_pts
                FROM ordered_pts GROUP BY 1, 2
            )
            SELECT landuse_id, name,
                   TRY_CAST(ST_MakePolygon(ST_MakeLine(closed_pts)) AS GEOMETRY) AS geom,
                   '{extract}' AS extract
            FROM agg
            WHERE len(closed_pts) >= 4
        """)

        total, named = conn.execute(f"SELECT count(*), count(name) FROM {fqn} WHERE extract = ?", [extract]).fetchone()
        print(f"  loaded {total:,} landuse=residential polygons ({named:,} named) from {extract}")

    return TableRef(catalog=GEO_CATALOG, schema=OSM_SCHEMA, table=table, version=_current_version(conn))


# ---------------------------------------------------------------------------
# Per-building OSM lookup keyed by (zip, canonical_key, housenumber_norm).
# Built once per run from osm_addresses + osm_landuse_residential, consumed
# by refined_positions and osm_only_matches.
# ---------------------------------------------------------------------------


def osm_building_lookup(
    osm_pbfs: list[Path],
    osm_addresses: TableRef,
    osm_landuse_residential: TableRef,
    address_tokens: TableRef,
    conn: duckdb.DuckDBPyConnection,
) -> TableRef:
    """One row per OSM-known building, keyed for fast voter lookup.

    Schema: (zip_code, canonical_key, housenumber, housenumber_norm,
              street, osm_lat, osm_lon, in_residential_complex)

    Derives canonical_key by tokenizing the OSM `street` (with
    STREET_REWRITES applied), equivalency-expanding via
    `address_tokens`, stripping generics, sorting, and joining with '|'.
    housenumber_norm strips leading zeros after hyphens then strips
    hyphens entirely (matches the voter-side normalization).
    in_residential_complex is true when the building centroid falls
    inside a landuse=residential polygon.

    Reads only the rows tagged with this version's extracts (`osm_pbfs`),
    from both the addresses and the landuse table, so other states' extracts
    and retired snapshots that share the raw tables never feed this version.
    Non-incremental: drops + recreates each run so changes to
    STREET_REWRITES or address_tokens take effect immediately.
    """
    table = "building_lookup"
    fqn = _fqn(table)
    _ensure_schema(conn)
    conn.execute(f"DROP TABLE IF EXISTS {fqn}")

    extracts = [extract_id(p) for p in osm_pbfs]
    print(f"Building OSM building lookup from {', '.join(extracts) or 'no extracts'}…")
    t0 = time.time()

    osm = osm_addresses.fqn
    tok = address_tokens.fqn
    res = osm_landuse_residential.fqn

    # STREET_REWRITES (see src/addressing.py) normalize OSM's surface
    # form toward TIGER's before tokenizing.
    conn.execute(f"""
        CREATE OR REPLACE TEMP TABLE _bl_raw_tokens AS
        SELECT
            osm_id, kind, housenumber, zip_code, lat, lon, street, extract,
            {tokenize_street_sql(street_rewrite_sql("street"))} AS raw_tokens
        FROM {osm}
        WHERE zip_code IS NOT NULL
          AND street   IS NOT NULL
          AND {_extract_in_sql("extract", extracts)}
    """)
    # Inverted-index equivalency expansion: explode raw_tokens to
    # (osm_id, token) and address_tokens groups to (group_array, token),
    # equi-join on token, dedupe by (osm_id, group_array), then flatten
    # the matched group arrays into `extra`. Replaces a cross-product
    # filtered by `len(list_intersect(...)) > 0` that doesn't scale.
    conn.execute(f"""
        CREATE OR REPLACE TEMP TABLE _bl_keyed AS
        WITH b_tok AS (
            SELECT osm_id, extract, t
            FROM _bl_raw_tokens, UNNEST(raw_tokens) AS u(t)
        ),
        g_tok AS (
            SELECT equivalent_tokens, t
            FROM {tok}, UNNEST(equivalent_tokens) AS u(t)
        ),
        matched AS (
            SELECT DISTINCT b.osm_id, b.extract, g.equivalent_tokens
            FROM b_tok b JOIN g_tok g ON g.t = b.t
        ),
        extras AS (
            SELECT osm_id, extract, flatten(list(equivalent_tokens)) AS extra
            FROM matched
            GROUP BY osm_id, extract
        ),
        combined AS (
            SELECT
                b.osm_id, b.kind, b.housenumber, b.zip_code, b.lat, b.lon, b.street, b.extract,
                list_distinct(list_concat(b.raw_tokens, COALESCE(e.extra, []))) AS expanded
            FROM _bl_raw_tokens b LEFT JOIN extras e USING (osm_id, extract)
        )
        SELECT osm_id, kind, housenumber, zip_code, lat, lon, street, extract,
               {canonical_key_sql("expanded")} AS canonical_key
        FROM combined
        WHERE expanded IS NOT NULL
    """)

    # Group by housenumber_norm (not the raw `housenumber` string) so
    # OSM tagging variants for the same physical building — `90-2` /
    # `90-02`, `1115` / `11-15` — merge into one row.
    #
    # Pick the OSM record per group via ROW_NUMBER: prefer way (polygon
    # centroid) over node (doorway point); break ties on osm_id ASC, then
    # extract, for deterministic output — a border building present in two
    # of this version's state extracts shares an osm_id, and the extract
    # name settles it (either row is a valid position for the same object).
    #
    # in_residential_complex is computed as a one-shot spatial
    # semi-join rather than per-row correlated EXISTS so DuckDB's
    # spatial bbox pre-filter applies.
    conn.execute(f"""
        CREATE TABLE {fqn} AS
        WITH keyed_norm AS (
            SELECT osm_id, zip_code, canonical_key, housenumber, street, lat, lon, kind, extract,
                   {housenumber_norm_sql("housenumber")} AS housenumber_norm
            FROM _bl_keyed
            WHERE canonical_key != ''
        ),
        -- Deterministic pick per (zip, canonical_key, housenumber_norm):
        -- prefer way over node (way = polygon centroid > doorway point);
        -- on ties (multiple ways for the same canonical address — rare but
        -- happens when OSM tags two polygons with the same addr:housenumber),
        -- break by osm_id ASC, then by extract for the same osm_id seen in
        -- two overlapping extracts. Without the tiebreakers the chosen
        -- lat/lon can shift between runs.
        ranked AS (
            SELECT *,
                ROW_NUMBER() OVER (
                    PARTITION BY zip_code, canonical_key, housenumber_norm
                    ORDER BY (kind = 'way') DESC, osm_id, extract
                ) AS rn
            FROM keyed_norm
        ),
        agg AS (
            SELECT zip_code, canonical_key, housenumber_norm,
                   housenumber, lat AS osm_lat, lon AS osm_lon, street
            FROM ranked
            WHERE rn = 1
        ),
        in_complex AS (
            SELECT DISTINCT a.zip_code, a.canonical_key, a.housenumber_norm
            FROM agg a
            JOIN {res} r
              ON ST_Contains(r.geom, ST_Point(a.osm_lon, a.osm_lat))
            WHERE {_extract_in_sql("r.extract", extracts)}
        )
        SELECT a.zip_code, a.canonical_key, a.housenumber, a.housenumber_norm,
               a.street, a.osm_lat, a.osm_lon,
               c.zip_code IS NOT NULL AS in_residential_complex
        FROM agg a
        LEFT JOIN in_complex c
          USING (zip_code, canonical_key, housenumber_norm)
    """)
    n = conn.execute(f"SELECT count(*) FROM {fqn}").fetchone()[0]
    print(f"  {n:,} buildings keyed in {time.time() - t0:.1f}s")

    return TableRef(catalog=GEO_CATALOG, schema=OSM_SCHEMA, table=table, version=_current_version(conn))
