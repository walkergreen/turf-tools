"""End-to-end tests for the blockface_relationships DAG node.

Builds a synthetic plus-sign intersection with real linestring geometry
in an isolated DuckLake and runs the full node — SQL bearing extraction,
per-node classification, and the write-back — asserting the same facts a
human reads off the drawing.

Layout (around lon -73.99, lat 40.73, so the UTM 18N transform is honest):

                  NN_END
                    │ N        MAIN ST runs E-W (lines W, E)
     NW_END ────────┼──────── NE_END      CROSS ST runs N-S (lines N, S)
              W     │ E
                    │ S        W is digitized *toward* the center node
                  NS_END       (its to-end) to exercise the left/right flip.

MAIN ST is in zip 10001, CROSS ST in zip 10002 — the zip-scoping test
cuts along that line.
"""

import statistics

import pytest

from src.dags.blockface_relationships import blockface_relationships
from src.geo.projection import utm_epsg_for_longitudes
from src.models import TableRef

CENTER = (-73.99, 40.73)  # UTM zone 18
# Los Angeles longitude at the same latitude: UTM zone 11, 44° west of CENTER.
WEST_COAST = (-118.24, CENTER[1])
STEP = 0.001  # ~85-110m; comfortably longer than the bearing sample


def _lines(center, prefix=""):
    """The plus-sign's four lines around `center`:
    line_id: (wkt from -> to, from_node, to_node, zip). `prefix` goes on
    every line and node id so two plus-signs can share one table."""
    lon, lat = center
    p = prefix
    return {
        f"{p}E": (f"LINESTRING({lon} {lat}, {lon + STEP} {lat})", f"{p}NC", f"{p}NE_END", "10001"),
        f"{p}N": (f"LINESTRING({lon} {lat}, {lon} {lat + STEP})", f"{p}NC", f"{p}NN_END", "10002"),
        # Digitized toward the center: NC is this line's TO end.
        f"{p}W": (f"LINESTRING({lon - STEP} {lat}, {lon} {lat})", f"{p}NW_END", f"{p}NC", "10001"),
        f"{p}S": (f"LINESTRING({lon} {lat}, {lon} {lat - STEP})", f"{p}NC", f"{p}NS_END", "10002"),
    }


LINES = _lines(CENTER)


@pytest.fixture()
def geo_tables(dual_conn):
    """Production-shaped blockface_unpivoted + edges with the plus-sign."""
    return _build_geo_tables(dual_conn, CENTER)


def _build_geo_tables(conn, center, prefix="", create=True):
    """Insert the plus-sign around `center` (ids prefixed with `prefix`);
    `create` builds the two tables first, otherwise the rows append."""
    conn.execute("CREATE SCHEMA IF NOT EXISTS ducklake_geo.tiger")
    if create:
        _create_geo_tables(conn)
    for line_id, (wkt, from_node, to_node, zip_code) in _lines(center, prefix).items():
        name = "MAIN ST" if line_id.removeprefix(prefix) in ("E", "W") else "CROSS ST"
        conn.execute(
            """
            INSERT INTO ducklake_geo.tiger.edges VALUES
            (?, ?, 'S1400', ?, ?, [], '36', '061', ST_GeomFromText(?))
            """,
            [line_id, name, from_node, to_node, wkt],
        )
        for side in ("left", "right"):
            conn.execute(
                """
                INSERT INTO ducklake_geo.tiger.blockface_unpivoted VALUES
                (?, ?, '1', '99', ?, ?, ?, [], ?, ?, ST_GeomFromText(?))
                """,
                [f"{line_id}:{side}", side, zip_code, name, line_id, from_node, to_node, wkt],
            )
    return {
        "unpivoted": TableRef(catalog="ducklake_geo", schema="tiger", table="blockface_unpivoted", version=0),
        "edges": TableRef(catalog="ducklake_geo", schema="tiger", table="edges", version=0),
    }


def _create_geo_tables(conn):
    conn.execute("""
        CREATE TABLE ducklake_geo.tiger.blockface_unpivoted (
            blockface_id        VARCHAR,
            side                VARCHAR,
            raw_from            VARCHAR,
            raw_to              VARCHAR,
            zip_code            VARCHAR,
            full_name           VARCHAR,
            tiger_line_id       VARCHAR,
            street_name_tokens  VARCHAR[],
            from_node_id        VARCHAR,
            to_node_id          VARCHAR,
            geom                GEOMETRY
        )
    """)
    conn.execute("""
        CREATE TABLE ducklake_geo.tiger.edges (
            tiger_line_id       VARCHAR,
            full_name           VARCHAR,
            feature_class_code  VARCHAR,
            from_node_id        VARCHAR,
            to_node_id          VARCHAR,
            street_name_tokens  VARCHAR[],
            state_fips          VARCHAR,
            county_fips         VARCHAR,
            geom                GEOMETRY
        )
    """)


def _run(conn, geo_tables, zips=None):
    ref = blockface_relationships(geo_tables["unpivoted"], geo_tables["edges"], conn, zips)
    rows = conn.execute(f"""
        SELECT blockface_id_a, blockface_id_b, kind, node_id,
               crossed_line_ids, crossed_classes, penalty_class, crossing_cost_m
        FROM {ref.fqn}
        ORDER BY blockface_id_a, blockface_id_b, COALESCE(node_id, '')
    """).fetchall()
    return rows


def _find(rows, a, b):
    key = tuple(sorted((a, b)))
    matches = [r for r in rows if (r[0], r[1]) == key]
    assert matches, f"no relationship for {key}"
    return matches


class TestPlusSign:
    def test_row_inventory(self, dual_conn, geo_tables):
        rows = _run(dual_conn, geo_tables)
        # 4 mid-block across + 24 center-node pairs + 4 dead-end hinges.
        assert len(rows) == 32
        kinds = {}
        for r in rows:
            kinds[r[2]] = kinds.get(r[2], 0) + 1
        assert kinds == {
            "across": 4,
            "hinge": 4 + 4,  # 4 center corners + 4 dead-end wraps
            "continue": 4,
            "turn": 8,
            "kitty_corner": 8,
        }

    def test_across_rows_are_node_free(self, dual_conn, geo_tables):
        rows = _run(dual_conn, geo_tables)
        across = [r for r in rows if r[2] == "across"]
        for r in across:
            assert r[3] is None
            assert r[4] == [r[0].split(":")[0]]  # crosses its own line
            assert r[6] == "minor"

    def test_flipped_digitization_lands_on_physical_sides(self, dual_conn, geo_tables):
        # W runs west->east (center is its TO end), so W's digitized
        # LEFT is the physical NORTH side. The north-sidewalk continue
        # along MAIN is therefore E:left <-> W:left, crossing N.
        rows = _run(dual_conn, geo_tables)
        (north,) = _find(rows, "E:left", "W:left")
        assert north[2] == "continue"
        assert north[4] == ["N"]
        # And the NW physical corner hinge is N:left <-> W:left.
        (nw,) = _find(rows, "N:left", "W:left")
        assert nw[2] == "hinge"
        assert nw[7] == 0.0

    def test_dead_ends_hinge(self, dual_conn, geo_tables):
        rows = _run(dual_conn, geo_tables)
        for line in ("E", "N", "W", "S"):
            pair_rows = _find(rows, f"{line}:left", f"{line}:right")
            by_kind = {r[2] for r in pair_rows}
            assert by_kind == {"across", "hinge"}  # mid-block + dead-end wrap


class TestZipScoping:
    def test_only_scoped_blockfaces_emit_rows(self, dual_conn, geo_tables):
        rows = _run(dual_conn, geo_tables, zips=["10001"])
        ids = {r[0] for r in rows} | {r[1] for r in rows}
        assert ids == {"E:left", "E:right", "W:left", "W:right"}
        # 2 across + 2 dead-end hinges + 4 center pairs (6 MAIN-only
        # pairs minus 2 same-line ones).
        assert len(rows) == 8

    def test_out_of_scope_edges_still_shape_and_price_crossings(self, dual_conn, geo_tables):
        # CROSS ST is outside the zip scope, but continuing along MAIN
        # still means crossing it — the crossed line must be reported
        # even though no relationship rows are emitted for it.
        rows = _run(dual_conn, geo_tables, zips=["10001"])
        (north,) = _find(rows, "E:left", "W:left")
        assert north[2] == "continue"
        assert north[4] == ["N"]
        assert north[7] > 0.0


class TestProjectionZone:
    """The classification does not depend on the UTM zone: bearings are
    locally conformal and crossing costs come from the MTFCC table."""

    @staticmethod
    def _signature(rows):
        # Pair, kind, node, how many lines are crossed, penalty, cost. The
        # symmetric plus-sign has two equally short kitty-corner routes, so
        # *which* lines are crossed is a tie the geometry may break either way.
        return sorted((r[0], r[1], r[2], r[3], len(r[4]), len(r[5]), r[6], r[7]) for r in rows)

    def test_derived_zone_matches_explicit_18n(self, dual_conn, geo_tables):
        derived = blockface_relationships(geo_tables["unpivoted"], geo_tables["edges"], dual_conn)
        derived_rows = dual_conn.execute(f"SELECT * FROM {derived.fqn}").fetchall()
        explicit = blockface_relationships(geo_tables["unpivoted"], geo_tables["edges"], dual_conn, bearing_epsg=32618)
        explicit_rows = dual_conn.execute(f"SELECT * FROM {explicit.fqn}").fetchall()
        assert self._signature(derived_rows) == self._signature(explicit_rows)
        assert len(derived_rows) == 32

    def test_plus_sign_in_zone_16_classifies_identically(self, dual_conn):
        """The same plus-sign shifted west into UTM zone 16 (Illinois longitude,
        same latitude) yields the same relationship set under EPSG:32616."""
        nyc = _build_geo_tables(dual_conn, CENTER)
        nyc_rows = _run(dual_conn, nyc)
        dual_conn.execute("DROP TABLE ducklake_geo.tiger.blockface_unpivoted")
        dual_conn.execute("DROP TABLE ducklake_geo.tiger.edges")
        west = _build_geo_tables(dual_conn, (-87.6, CENTER[1]))
        ref = blockface_relationships(west["unpivoted"], west["edges"], dual_conn, None, 32616)
        west_rows = dual_conn.execute(f"""
            SELECT blockface_id_a, blockface_id_b, kind, node_id,
                   crossed_line_ids, crossed_classes, penalty_class, crossing_cost_m
            FROM {ref.fqn}
            ORDER BY blockface_id_a, blockface_id_b, COALESCE(node_id, '')
        """).fetchall()
        assert self._signature(west_rows) == self._signature(nyc_rows)
        assert len(west_rows) == 32

    @staticmethod
    def _strip(rows, prefix):
        """The rows with `prefix` removed from every blockface, node, and line id."""
        strip = lambda v: v.removeprefix(prefix) if isinstance(v, str) else v  # noqa: E731
        return [(strip(r[0]), strip(r[1]), r[2], strip(r[3]), [strip(x) for x in r[4]], *r[5:]) for r in rows]

    def test_two_coast_catalog_projects_each_node_in_its_own_zone(self, dual_conn):
        """The NYC plus-sign and a Los Angeles copy share one catalog, 44° of
        longitude apart — more than one UTM zone can serve. With no
        `bearing_epsg`, each node is measured in the zone of its own longitude
        (18 for New York, 11 for Los Angeles), so the run neither refuses the
        span nor mis-scales either coast, and each plus-sign classifies
        exactly as it does alone."""
        nyc = _build_geo_tables(dual_conn, CENTER)
        alone = self._signature(_run(dual_conn, nyc))
        _build_geo_tables(dual_conn, WEST_COAST, prefix="CA_", create=False)
        rows = _run(dual_conn, nyc)

        # The per-node zone table outlives the call on the connection: two
        # zones were sampled, and the catalog's longitudes are ones a single
        # zone would refuse.
        zones = {z for (z,) in dual_conn.execute("SELECT DISTINCT zone FROM _bfrel_ends").fetchall()}
        assert zones == {11, 18}
        lons = [lon for (lon,) in dual_conn.execute("SELECT node_lon FROM _bfrel_ends").fetchall()]
        with pytest.raises(ValueError, match="one UTM zone cannot serve it"):
            utm_epsg_for_longitudes(statistics.median(lons), min(lons), max(lons), label="catalog")

        assert len(rows) == 64
        east = [r for r in rows if not r[0].startswith("CA_")]
        west = [r for r in rows if r[0].startswith("CA_")]
        assert self._signature(east) == alone
        assert self._signature(self._strip(west, "CA_")) == alone
