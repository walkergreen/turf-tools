"""Derive walkable relationships between blockfaces.

    blockface_unpivoted ─┐
                         ├─► blockface_relationships
    tiger_edges_raw     ─┘

Output is one row per (blockface pair, meeting point): how a canvasser
gets from one blockface to the other and what they must cross to do it
(see ``src/blockface_topology.py`` for the taxonomy and the wedge model
that classifies node meetings).

The SQL half of the work — scoping blockfaces, finding the nodes they
touch, and computing each incident edge's departure bearing in a metric
projection — runs vectorized in DuckDB. The per-node radial sort and
pair classification is circular-index bookkeeping, which is miserable in
SQL and trivial in Python, so rows come out to Python grouped by node
and the result is bulk-inserted back.

Scoping: ``relationship_zip_codes`` limits the *blockfaces* (relationship
endpoints) to those zips, but the wedge structure at each node always
uses every physical edge incident to the node regardless of zip — a
boundary node still knows about the arterial just outside the zip. Rows
to out-of-scope blockfaces are simply not emitted. A scoped run rebuilds
the output table with only scoped rows (non-incremental, like
``blockface_final``); production runs pass ``None``.
"""

import pandas as pd

import duckdb
from src.blockface_topology import (
    Blockface,
    EdgeEnd,
    across_relationships,
    classify_node,
)
from src.dags.tiger import GEO_CATALOG, TIGER_SCHEMA
from src.geo.projection import NORTHERN_UTM_EPSG_BASE, utm_zone_sql
from src.models import TableRef

# How far along the edge (meters) to sample when computing the departure
# bearing at a node. Sampling in from the endpoint — rather than using
# the immediately adjacent vertex — keeps curb-cut wiggles in the TIGER
# linestring from scrambling the radial order. Edges shorter than twice
# this fall back to their midpoint.
_BEARING_SAMPLE_M = 10.0

# Physical edge classes that separate corners at a node: streets (S*),
# rail (R*), hydrography (H*). Nonvisible legal/statistical boundaries
# (P*) and miscellaneous features (L*) run through intersections without
# blocking a pedestrian, so they must not split wedges.
_PHYSICAL_MTFCC_PREFIXES = ("S", "R", "H")

_RESULT_COLUMNS = [
    "blockface_id_a",
    "blockface_id_b",
    "kind",
    "node_id",
    "crossed_line_ids",
    "crossed_classes",
    "penalty_class",
    "crossing_cost_m",
]


def _fqn(table: str) -> str:
    return f"{GEO_CATALOG}.{TIGER_SCHEMA}.{table}"


def _current_version(conn: duckdb.DuckDBPyConnection) -> int:
    return conn.sql(f"FROM {GEO_CATALOG}.current_snapshot()").fetchone()[0]


def blockface_relationships(
    blockface_unpivoted: TableRef,
    tiger_edges_raw: TableRef,
    conn: duckdb.DuckDBPyConnection,
    relationship_zip_codes: list[str] | None = None,
    # Not named `utm_epsg`: Hamilton binds parameters to graph nodes by name,
    # and `geocode.utm_epsg` would pull the voter graph (`persons_best_match`)
    # into this TIGER-only build whenever the two modules share a driver.
    bearing_epsg: int | None = None,
) -> TableRef:
    """Build ``geo_ducklake.tiger.blockface_relationships``.

    One row per (blockface pair, meeting point). ``node_id`` is NULL for
    mid-block ``across`` rows; a pair can meet at more than one node, so
    consumers take the min-cost row per pair.

    ``bearing_epsg`` is the metric projection for the departure-bearing
    sample; ``None`` projects each node in the UTM zone of its own
    longitude, so the shared catalog may span several zones (or both
    coasts) and every node still gets an honest 10 m sample. The output is
    insensitive to the zone — bearings are locally conformal and crossing
    costs come from the MTFCC table — only the sample length is measured.
    """
    table = "blockface_relationships"
    fqn = _fqn(table)
    unpivoted_fqn = blockface_unpivoted.fqn
    edges_fqn = tiger_edges_raw.fqn

    # -- 1. Scoped blockfaces (dedup alias/address-range rows per id) -----
    zip_clause = ""
    params: list[object] = []
    if relationship_zip_codes is not None:
        zip_clause = "AND list_contains(?, zip_code)"
        params.append(relationship_zip_codes)
    conn.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE _bfrel_blockfaces AS
        SELECT
            blockface_id,
            ANY_VALUE(tiger_line_id) AS tiger_line_id,
            ANY_VALUE(side)          AS side,
            ANY_VALUE(from_node_id)  AS from_node_id,
            ANY_VALUE(to_node_id)    AS to_node_id
        FROM {unpivoted_fqn}
        WHERE from_node_id IS NOT NULL
          AND to_node_id IS NOT NULL
          {zip_clause}
        GROUP BY blockface_id
        """,
        params,
    )

    # -- 2. Nodes those blockfaces touch ----------------------------------
    conn.execute("""
        CREATE OR REPLACE TEMP TABLE _bfrel_nodes AS
        SELECT DISTINCT node_id FROM (
            SELECT from_node_id AS node_id FROM _bfrel_blockfaces
            UNION ALL
            SELECT to_node_id FROM _bfrel_blockfaces
        )
    """)

    # -- 3. Physical edges incident to those nodes ------------------------
    # GROUP BY tiger_line_id guards against the same TLID loaded from two
    # county files (edges on a county border appear in both).
    mtfcc_filter = " OR ".join(f"feature_class_code LIKE '{p}%'" for p in _PHYSICAL_MTFCC_PREFIXES)
    conn.execute(f"""
        CREATE OR REPLACE TEMP TABLE _bfrel_edges AS
        SELECT
            tiger_line_id,
            ANY_VALUE(feature_class_code) AS feature_class_code,
            ANY_VALUE(from_node_id)       AS from_node_id,
            ANY_VALUE(to_node_id)         AS to_node_id,
            ANY_VALUE(geom)               AS geom
        FROM {edges_fqn}
        WHERE ({mtfcc_filter})
          AND (from_node_id IN (SELECT node_id FROM _bfrel_nodes)
               OR to_node_id IN (SELECT node_id FROM _bfrel_nodes))
        GROUP BY tiger_line_id
    """)

    # -- 4. Departure bearings, vectorized in the metric projection -------
    # One row per (node, incident edge end) in geographic coordinates, with
    # the node's longitude and UTM zone. Every end at a node shares the
    # node's longitude, so a node's radial order is always computed in one
    # zone even when the catalog spans several.
    conn.execute(f"""
        CREATE OR REPLACE TEMP TABLE _bfrel_ends AS
        WITH ends AS (
            SELECT
                from_node_id AS node_id, tiger_line_id, 'from' AS which_end, feature_class_code, geom,
                ST_X(ST_StartPoint(geom)) AS node_lon
            FROM _bfrel_edges
            WHERE geom IS NOT NULL AND from_node_id IN (SELECT node_id FROM _bfrel_nodes)
            UNION ALL
            SELECT
                to_node_id, tiger_line_id, 'to', feature_class_code, geom,
                ST_X(ST_EndPoint(geom))
            FROM _bfrel_edges
            WHERE geom IS NOT NULL AND to_node_id IN (SELECT node_id FROM _bfrel_nodes)
        )
        SELECT *, {utm_zone_sql("node_lon")} AS zone FROM ends
    """)
    if bearing_epsg is None:
        zones = [z for (z,) in conn.execute("SELECT DISTINCT zone FROM _bfrel_ends ORDER BY 1").fetchall()]
        passes = [(NORTHERN_UTM_EPSG_BASE + z, f"zone = {z}") for z in zones]
    else:
        passes = [(bearing_epsg, "TRUE")]

    # A degenerate sample (zero-length edge, or sample == endpoint) can't
    # yield a bearing; those ends are dropped and the node classifies
    # from the remaining edges.
    edge_end_rows: list[tuple] = []
    for epsg, zone_filter in passes:
        edge_end_rows.extend(
            conn.execute(f"""
                WITH prepared AS (
                    SELECT
                        node_id, tiger_line_id, which_end, feature_class_code,
                        ST_Transform(geom, 'OGC:CRS84', 'EPSG:{epsg}') AS geom_m
                    FROM _bfrel_ends
                    WHERE {zone_filter}
                ),
                measured AS (
                    SELECT *, ST_Length(geom_m) AS len_m FROM prepared
                ),
                ends AS (
                    SELECT
                        node_id, tiger_line_id, which_end, feature_class_code,
                        CASE WHEN which_end = 'from' THEN ST_StartPoint(geom_m) ELSE ST_EndPoint(geom_m) END AS origin,
                        ST_LineInterpolatePoint(
                            geom_m,
                            CASE WHEN which_end = 'from'
                                 THEN LEAST({_BEARING_SAMPLE_M} / len_m, 0.5)
                                 ELSE GREATEST(1.0 - {_BEARING_SAMPLE_M} / len_m, 0.5)
                            END
                        ) AS sample
                    FROM measured
                    WHERE len_m > 0
                )
                SELECT
                    node_id, tiger_line_id, which_end, feature_class_code,
                    degrees(atan2(ST_Y(sample) - ST_Y(origin), ST_X(sample) - ST_X(origin))) AS bearing_deg
                FROM ends
                WHERE ST_X(sample) != ST_X(origin) OR ST_Y(sample) != ST_Y(origin)
            """).fetchall()
        )
    # The streaming loop below relies on nodes arriving as contiguous groups.
    edge_end_rows.sort(key=lambda r: r[0])

    # -- 5. Per-node classification in Python -----------------------------
    blockfaces_by_line_side: dict[tuple[str, str], Blockface] = {}
    all_blockfaces: list[Blockface] = []
    for bf_id, line_id, side in conn.execute(
        "SELECT blockface_id, tiger_line_id, side FROM _bfrel_blockfaces"
    ).fetchall():
        bf = Blockface(blockface_id=bf_id, tiger_line_id=line_id, side=side)
        blockfaces_by_line_side[(line_id, side)] = bf
        all_blockfaces.append(bf)

    feature_class_by_line: dict[str, str | None] = {
        line_id: mtfcc
        for line_id, mtfcc in conn.execute("SELECT tiger_line_id, feature_class_code FROM _bfrel_edges").fetchall()
    }

    relationships = across_relationships(all_blockfaces, feature_class_by_line)

    # edge_end_rows is ordered by node_id, so nodes stream as groups.
    current_node: str | None = None
    current_ends: list[EdgeEnd] = []
    for node_id, line_id, which_end, mtfcc, bearing_deg in edge_end_rows:
        if node_id != current_node:
            if current_node is not None:
                relationships.extend(classify_node(current_node, current_ends, blockfaces_by_line_side))
            current_node = node_id
            current_ends = []
        current_ends.append(
            EdgeEnd(
                tiger_line_id=line_id,
                end=which_end,
                bearing_deg=bearing_deg % 360.0,
                feature_class_code=mtfcc,
            )
        )
    if current_node is not None:
        relationships.extend(classify_node(current_node, current_ends, blockfaces_by_line_side))

    # -- 6. Bulk write-back ------------------------------------------------
    conn.execute(f"CREATE SCHEMA IF NOT EXISTS {GEO_CATALOG}.{TIGER_SCHEMA}")
    conn.execute(f"DROP TABLE IF EXISTS {fqn}")
    conn.execute(f"""
        CREATE TABLE {fqn} (
            blockface_id_a    VARCHAR,
            blockface_id_b    VARCHAR,
            kind              VARCHAR,
            node_id           VARCHAR,
            crossed_line_ids  VARCHAR[],
            crossed_classes   VARCHAR[],
            penalty_class     VARCHAR,
            crossing_cost_m   DOUBLE
        )
    """)
    if relationships:
        result_df = pd.DataFrame(
            [
                (
                    r.blockface_id_a,
                    r.blockface_id_b,
                    r.kind,
                    r.node_id,
                    list(r.crossed_line_ids),
                    list(r.crossed_classes),
                    r.penalty_class,
                    r.crossing_cost_m,
                )
                for r in relationships
            ],
            columns=_RESULT_COLUMNS,
        )
        conn.register("_bfrel_result_df", result_df)
        conn.execute(f"""
            INSERT INTO {fqn}
            SELECT
                blockface_id_a, blockface_id_b, kind, node_id,
                CAST(crossed_line_ids AS VARCHAR[]),
                CAST(crossed_classes AS VARCHAR[]),
                penalty_class, crossing_cost_m
            FROM _bfrel_result_df
        """)
        conn.unregister("_bfrel_result_df")

    version = _current_version(conn)
    return TableRef(catalog=GEO_CATALOG, schema=TIGER_SCHEMA, table=table, version=version)
