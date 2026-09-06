"""Per-state OSM extracts — URL resolution, download, and per-extract
ingestion, no network and no osmium.

Behaviors locked in:

- `osm_extract_urls`: one URL per state in scope from the template (sorted,
  deduped by state), a pinned URL wins for its slug, an explicit `osm_urls`
  list wins verbatim, a single `osm_url` named for a Geofabrik state pins
  that state only (other states still resolve), a single `osm_url` not named
  for any state is ingested verbatim (warning when the scope spans several
  states), and a state without a Geofabrik slug raises.
- `_download_pbf` streams into a `.part` file renamed into place on
  completion; an interrupted transfer leaves nothing under the cached name.
- `osm_pbfs` drops a freshly downloaded extract's rows and osmium caches
  before the loaders run again (a re-downloaded `-latest` is a new snapshot);
  an extract downloaded for the first time has nothing to drop, so no DELETE
  runs and the log calls it a first download.
- `extract_id` strips the full `.osm.pbf` suffix.
- `_extract_loaded` is per extract, so a second extract appends instead of
  short-circuiting and re-running the same extract adds nothing — for the
  addresses, landuse, and building-polygon loaders alike.
- `_drop_unless_columns` rebuilds a raw table that lacks a required column
  or carries a forbidden one.
"""

import json
import urllib.request
from pathlib import Path

import pytest

import duckdb
from src.dags import osm
from src.geo.geofabrik import extract_id, geofabrik_slug, slug_for_url
from src.geo.scope import CountyScope
from src.models import TableRef

TEMPLATE = "https://download.geofabrik.de/north-america/us/{state}-latest.osm.pbf"
SCOPE = [CountyScope("36", "061"), CountyScope("34", "017"), CountyScope("36", "005")]
NJ_LATEST = "https://download.geofabrik.de/north-america/us/new-jersey-latest.osm.pbf"
CT_LATEST = "https://download.geofabrik.de/north-america/us/connecticut-latest.osm.pbf"
NY_PIN = "https://download.geofabrik.de/north-america/us/new-york-260501.osm.pbf"
BBBIKE = "https://download.bbbike.org/osm/bbbike/NewYork/NewYork.osm.pbf"


def _boom(*_args, **_kwargs):
    raise AssertionError("network or osmium call attempted")


class TestExtractUrls:
    def test_osm_urls_from_template_per_state(self):
        assert osm.osm_extract_urls(SCOPE, TEMPLATE, {}) == [
            "https://download.geofabrik.de/north-america/us/new-jersey-latest.osm.pbf",
            "https://download.geofabrik.de/north-america/us/new-york-latest.osm.pbf",
        ]

    def test_pinned_url_replaces_the_template_for_its_state(self):
        pins = {"new-york": "https://download.geofabrik.de/north-america/us/new-york-260501.osm.pbf"}
        assert osm.osm_extract_urls(SCOPE, TEMPLATE, pins) == [
            "https://download.geofabrik.de/north-america/us/new-jersey-latest.osm.pbf",
            "https://download.geofabrik.de/north-america/us/new-york-260501.osm.pbf",
        ]

    def test_explicit_osm_urls_override_the_template(self):
        urls = ["https://x/a.osm.pbf", "https://x/b.osm.pbf", "https://x/a.osm.pbf"]
        assert osm.osm_extract_urls(SCOPE, TEMPLATE, {}, osm_urls=urls) == [
            "https://x/a.osm.pbf",
            "https://x/b.osm.pbf",
        ]

    def test_non_geofabrik_osm_url_is_ingested_verbatim(self, capsys):
        """A BBBike city extract has no state slug: it is the only extract,
        quietly for a one-state scope, with a warning when the scope spans two."""
        assert osm.osm_extract_urls([CountyScope("36", "061")], TEMPLATE, {}, osm_url=BBBIKE) == [BBBIKE]
        assert "WARNING" not in capsys.readouterr().out
        assert osm.osm_extract_urls(SCOPE, TEMPLATE, {}, osm_url=BBBIKE) == [BBBIKE]
        out = capsys.readouterr().out
        assert "spans 2 states (34, 36)" in out and "not named for a Geofabrik state" in out

    def test_geofabrik_named_osm_url_pins_its_own_state_only(self, capsys):
        """The deployment carried `OSM_URL=…/new-york-260501.osm.pbf`: a New
        York scope still gets exactly that file, a New Jersey scope gets New
        Jersey's extract rather than New York's, and both states get both."""
        assert osm.osm_extract_urls([CountyScope("36", "061")], TEMPLATE, {}, osm_url=NY_PIN) == [NY_PIN]
        assert osm.osm_extract_urls([CountyScope("34", "017")], TEMPLATE, {}, osm_url=NY_PIN) == [NJ_LATEST]
        assert osm.osm_extract_urls(SCOPE, TEMPLATE, {}, osm_url=NY_PIN) == [NJ_LATEST, NY_PIN]
        assert "WARNING" not in capsys.readouterr().out

    def test_osm_url_outranks_osm_url_pins_for_its_slug(self):
        pins = {"new-york": "https://download.geofabrik.de/north-america/us/new-york-250101.osm.pbf"}
        assert osm.osm_extract_urls([CountyScope("36", "061")], TEMPLATE, pins, osm_url=NY_PIN) == [NY_PIN]

    def test_explicit_osm_urls_outrank_osm_url(self):
        assert osm.osm_extract_urls(SCOPE, TEMPLATE, {}, osm_urls=[BBBIKE], osm_url=NY_PIN) == [BBBIKE]

    def test_unknown_state_raises(self):
        with pytest.raises(ValueError, match="OSM_URLS"):
            osm.osm_extract_urls([CountyScope("66", "010")], TEMPLATE, {})
        with pytest.raises(ValueError):
            geofabrik_slug("99")


@pytest.mark.parametrize(
    ("url", "slug"),
    [
        (NY_PIN, "new-york"),
        ("https://download.geofabrik.de/north-america/us/new-york-latest.osm.pbf", "new-york"),
        ("https://download.geofabrik.de/north-america/us/west-virginia-latest.osm.pbf", "west-virginia"),
        ("https://download.geofabrik.de/north-america/us/virginia-latest.osm.pbf", "virginia"),
        ("https://download.geofabrik.de/north-america/us/us-virgin-islands-latest.osm.pbf", "us-virgin-islands"),
        (BBBIKE, None),
        ("https://mirror/new-yorkshire-latest.osm.pbf", None),
        ("https://mirror/new-york.osm.pbf", None),
    ],
)
def test_slug_for_url_reads_the_state_off_the_filename(url, slug):
    assert slug_for_url(url) == slug


class TestUrlScopeWarning:
    def test_quiet_when_the_url_names_a_state_or_covers_one_state(self):
        assert osm.osm_url_scope_warning(SCOPE, NY_PIN, None) is None
        assert osm.osm_url_scope_warning([CountyScope("36", "061")], BBBIKE, None) is None
        assert osm.osm_url_scope_warning(SCOPE, None, None) is None

    def test_quiet_when_explicit_osm_urls_take_over(self):
        assert osm.osm_url_scope_warning(SCOPE, BBBIKE, [BBBIKE, NJ_LATEST]) is None

    def test_warns_for_a_city_extract_over_a_multi_state_scope(self):
        message = osm.osm_url_scope_warning(SCOPE, BBBIKE, None)
        assert message is not None
        assert message.startswith("WARNING: OSM_URL (NewYork.osm.pbf)")
        assert "spans 2 states (34, 36)" in message and "OSM_URLS" in message


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("new-york-260501.osm.pbf", "new-york-260501"),
        ("new-york-latest.osm.pbf", "new-york-latest"),
        ("NewYork.osm.pbf", "NewYork"),
    ],
)
def test_extract_name_is_the_pbf_stem(filename, expected):
    assert extract_id(Path("/cache") / filename) == expected


# ---------------------------------------------------------------------------
# Download and snapshot invalidation
# ---------------------------------------------------------------------------


class TestDownloadPbf:
    def test_interrupted_download_leaves_nothing_under_the_cached_name(self, tmp_path, monkeypatch):
        def broken(url, filename):
            Path(filename).write_bytes(b"partial")
            raise ConnectionResetError("connection reset")

        monkeypatch.setattr(urllib.request, "urlretrieve", broken)
        with pytest.raises(ConnectionResetError):
            osm._download_pbf(NJ_LATEST, tmp_path)
        assert list(tmp_path.iterdir()) == []

    def test_completed_download_is_renamed_into_place(self, tmp_path, monkeypatch):
        def complete(url, filename):
            assert Path(filename).name == "new-jersey-latest.osm.pbf.part"
            Path(filename).write_bytes(b"pbf bytes")

        monkeypatch.setattr(urllib.request, "urlretrieve", complete)
        path, fresh = osm._download_pbf(NJ_LATEST, tmp_path)
        assert (path, fresh) == (tmp_path / "new-jersey-latest.osm.pbf", True)
        assert path.read_bytes() == b"pbf bytes"
        assert list(tmp_path.iterdir()) == [path]

    def test_cached_pbf_is_reused_without_downloading(self, tmp_path, monkeypatch):
        monkeypatch.setattr(urllib.request, "urlretrieve", _boom)
        cached = tmp_path / "new-jersey-latest.osm.pbf"
        cached.write_bytes(b"pbf bytes")
        assert osm._download_pbf(NJ_LATEST, tmp_path) == (cached, False)


def _seed_raw_tables(conn):
    """Two of the three raw tables holding rows for two extracts; the third
    (`landuse_residential`) does not exist yet, as on a partially loaded lake."""
    conn.execute("CREATE SCHEMA IF NOT EXISTS ducklake_geo.osm")
    conn.execute(
        "CREATE TABLE ducklake_geo.osm.buildings_polygons "
        "(osm_id BIGINT, centroid_lat DOUBLE, centroid_lon DOUBLE, extract VARCHAR)"
    )
    conn.execute(
        "INSERT INTO ducklake_geo.osm.buildings_polygons VALUES "
        "(1, 40.74, -74.03, 'new-jersey-latest'), (2, 40.75, -73.99, 'new-york-260501')"
    )
    conn.execute("CREATE TABLE ducklake_geo.osm.addresses (osm_id BIGINT, extract VARCHAR)")
    conn.execute("INSERT INTO ducklake_geo.osm.addresses VALUES (1, 'new-jersey-latest'), (2, 'new-york-260501')")


def _extracts_in(conn, table):
    return {r[0] for r in conn.execute(f"SELECT DISTINCT extract FROM ducklake_geo.osm.{table}").fetchall()}


class TestFreshDownloadInvalidatesTheExtract:
    def test_fresh_download_drops_the_extracts_rows_and_osmium_caches(self, dual_conn, tmp_path, monkeypatch, capsys):
        """Run 1 loaded `new-jersey-latest` (snapshot A) into two tables and
        died; run 2 downloads the file again (snapshot B). Snapshot A's rows go
        before any loader runs, so no table keeps A while another loads B."""
        _seed_raw_tables(dual_conn)
        pbf = tmp_path / "new-jersey-latest.osm.pbf"
        filtered, geojson = osm._osmium_cache_paths(pbf)
        filtered.write_bytes(b"")
        geojson.write_bytes(b"")
        monkeypatch.setattr(osm, "_download_pbf", lambda url, cache: (pbf, True))
        capsys.readouterr()

        assert osm.osm_pbfs([NJ_LATEST], str(tmp_path), dual_conn) == [pbf]
        assert _extracts_in(dual_conn, "buildings_polygons") == {"new-york-260501"}
        assert _extracts_in(dual_conn, "addresses") == {"new-york-260501"}
        assert not filtered.exists() and not geojson.exists()
        out = capsys.readouterr().out
        assert "downloaded new-jersey-latest again: dropped 2 rows and 2 osmium cache file(s)" in out
        assert "first download" not in out

    def test_first_download_of_an_extract_drops_nothing(self, dual_conn, tmp_path, monkeypatch, capsys):
        """An extract no table has ever held arrives fresh too, but there is
        nothing to invalidate: the other extracts' rows stay, no DELETE runs
        (the geo catalog's snapshot is unchanged), and the log calls it a
        first download rather than a reload."""
        _seed_raw_tables(dual_conn)
        before = dual_conn.sql("FROM ducklake_geo.current_snapshot()").fetchone()[0]
        pbf = tmp_path / "connecticut-latest.osm.pbf"
        monkeypatch.setattr(osm, "_download_pbf", lambda url, cache: (pbf, True))
        capsys.readouterr()

        assert osm.osm_pbfs([CT_LATEST], str(tmp_path), dual_conn) == [pbf]
        assert _extracts_in(dual_conn, "buildings_polygons") == {"new-jersey-latest", "new-york-260501"}
        assert _extracts_in(dual_conn, "addresses") == {"new-jersey-latest", "new-york-260501"}
        assert dual_conn.sql("FROM ducklake_geo.current_snapshot()").fetchone()[0] == before
        out = capsys.readouterr().out
        assert "first download of connecticut-latest" in out
        assert "again" not in out and "dropped" not in out

    def test_invalidation_reports_the_rows_it_dropped(self, dual_conn, tmp_path):
        _seed_raw_tables(dual_conn)
        assert osm._invalidate_extract(dual_conn, tmp_path / "new-york-260501.osm.pbf") == 2
        assert osm._invalidate_extract(dual_conn, tmp_path / "new-york-260501.osm.pbf") == 0

    def test_cached_pbf_keeps_the_extracts_rows(self, dual_conn, tmp_path, monkeypatch):
        _seed_raw_tables(dual_conn)
        pbf = tmp_path / "new-jersey-latest.osm.pbf"
        filtered, geojson = osm._osmium_cache_paths(pbf)
        filtered.write_bytes(b"")
        geojson.write_bytes(b"")
        monkeypatch.setattr(osm, "_download_pbf", lambda url, cache: (pbf, False))

        assert osm.osm_pbfs([NJ_LATEST], str(tmp_path), dual_conn) == [pbf]
        assert _extracts_in(dual_conn, "buildings_polygons") == {"new-jersey-latest", "new-york-260501"}
        assert _extracts_in(dual_conn, "addresses") == {"new-jersey-latest", "new-york-260501"}
        assert filtered.exists() and geojson.exists()

    def test_invalidation_tolerates_missing_tables(self, dual_conn, tmp_path):
        """Nothing loaded yet — no raw table exists — is not an error."""
        osm._invalidate_extract(dual_conn, tmp_path / "new-jersey-latest.osm.pbf")


# ---------------------------------------------------------------------------
# Per-extract ingestion
# ---------------------------------------------------------------------------

# Synthetic "parsed PBF" contents keyed by extract id: (id, kind, lat, lon,
# housenumber, street, zip).
STAGED = {
    "new-york-260501": [
        (1, "node", 40.75, -73.99, "100", "Broadway", "10001"),
        (2, "way", None, None, "200", "Broadway", "10001"),
    ],
    "new-jersey-latest": [
        (3, "node", 40.74, -74.03, "5", "Washington St", "07030"),
        (2, "way", None, None, "200", "Broadway", "10001"),  # border way present in both extracts
    ],
}


def _fake_stage(conn, pbf):
    conn.execute("""
        CREATE OR REPLACE TEMP TABLE _addressed (
            id BIGINT, kind VARCHAR, lat DOUBLE, lon DOUBLE, housenumber VARCHAR, street VARCHAR,
            unit VARCHAR, zip_code VARCHAR, city VARCHAR, state VARCHAR, building VARCHAR
        )
    """)
    for osm_id, kind, lat, lon, hn, street, zip_code in STAGED[extract_id(pbf)]:
        conn.execute(
            "INSERT INTO _addressed VALUES (?, ?, ?, ?, ?, ?, NULL, ?, NULL, NULL, 'yes')",
            [osm_id, kind, lat, lon, hn, street, zip_code],
        )


def _polygons_table(conn) -> TableRef:
    conn.execute("CREATE SCHEMA IF NOT EXISTS ducklake_geo.osm")
    conn.execute("""
        CREATE TABLE ducklake_geo.osm.buildings_polygons (
            osm_id BIGINT, centroid_lat DOUBLE, centroid_lon DOUBLE, extract VARCHAR
        )
    """)
    conn.executemany(
        "INSERT INTO ducklake_geo.osm.buildings_polygons VALUES (?, ?, ?, ?)",
        [(2, 40.751, -73.991, "new-york-260501"), (2, 40.752, -73.992, "new-jersey-latest")],
    )
    return TableRef(catalog="ducklake_geo", schema="osm", table="buildings_polygons", version=0)


def _counts_by_extract(conn):
    return dict(conn.execute("SELECT extract, count(*) FROM ducklake_geo.osm.addresses GROUP BY 1").fetchall())


class TestPerExtractIngest:
    def test_ingest_is_incremental_per_extract(self, dual_conn):
        dual_conn.execute("CREATE SCHEMA IF NOT EXISTS ducklake_geo.osm")
        dual_conn.execute("CREATE TABLE ducklake_geo.osm.addresses (osm_id BIGINT, extract VARCHAR)")
        dual_conn.execute("INSERT INTO ducklake_geo.osm.addresses VALUES (1, 'new-york-260501')")
        assert osm._extract_loaded(dual_conn, "ducklake_geo.osm.addresses", "new-york-260501")
        assert not osm._extract_loaded(dual_conn, "ducklake_geo.osm.addresses", "new-jersey-latest")

    def test_second_extract_appends_instead_of_short_circuiting(self, dual_conn, monkeypatch):
        monkeypatch.setattr(osm, "_stage_addressed", _fake_stage)
        polys = _polygons_table(dual_conn)
        ny = Path("/cache/new-york-260501.osm.pbf")
        nj = Path("/cache/new-jersey-latest.osm.pbf")

        osm.osm_addresses([ny], polys, dual_conn)
        assert _counts_by_extract(dual_conn) == {"new-york-260501": 2}

        osm.osm_addresses([ny, nj], polys, dual_conn)
        assert _counts_by_extract(dual_conn) == {"new-york-260501": 2, "new-jersey-latest": 2}

        # Re-running the same extracts adds nothing.
        osm.osm_addresses([ny, nj], polys, dual_conn)
        assert _counts_by_extract(dual_conn) == {"new-york-260501": 2, "new-jersey-latest": 2}

    def test_way_centroids_come_from_the_same_extract(self, dual_conn, monkeypatch):
        """The border way (osm_id 2) appears in both extracts with different
        polygon centroids; each address row takes its own extract's centroid
        rather than fanning out across the join."""
        monkeypatch.setattr(osm, "_stage_addressed", _fake_stage)
        polys = _polygons_table(dual_conn)
        osm.osm_addresses([Path("/c/new-york-260501.osm.pbf"), Path("/c/new-jersey-latest.osm.pbf")], polys, dual_conn)
        rows = dual_conn.execute(
            "SELECT extract, lat, lon FROM ducklake_geo.osm.addresses WHERE osm_id = 2 ORDER BY extract"
        ).fetchall()
        assert rows == [("new-jersey-latest", 40.752, -73.992), ("new-york-260501", 40.751, -73.991)]

    def test_table_without_extract_column_is_rebuilt(self, dual_conn, monkeypatch):
        monkeypatch.setattr(osm, "_stage_addressed", _fake_stage)
        polys = _polygons_table(dual_conn)
        dual_conn.execute("CREATE TABLE ducklake_geo.osm.addresses (osm_id BIGINT, kind VARCHAR)")
        dual_conn.execute("INSERT INTO ducklake_geo.osm.addresses VALUES (99, 'node')")
        osm.osm_addresses([Path("/c/new-york-260501.osm.pbf")], polys, dual_conn)
        assert _counts_by_extract(dual_conn) == {"new-york-260501": 2}


# ---------------------------------------------------------------------------
# Landuse and building-polygon loaders, per extract
# ---------------------------------------------------------------------------

# Synthetic `landuse=residential` ways keyed by extract id: (way id, node refs,
# node positions). New York's ring is closed (first ref repeated); New Jersey's
# is open, which the loader closes itself.
RINGS = {
    "new-york-260501": (
        100,
        [1, 2, 3, 4, 1],
        {1: (40.75, -73.99), 2: (40.75, -73.98), 3: (40.76, -73.98), 4: (40.76, -73.99)},
    ),
    "new-jersey-latest": (
        200,
        [11, 12, 13, 14],
        {11: (40.74, -74.04), 12: (40.74, -74.03), 13: (40.75, -74.03), 14: (40.75, -74.04)},
    ),
}


def _fake_stage_landuse(conn, pbf):
    way_id, refs, nodes = RINGS[extract_id(pbf)]
    conn.execute("CREATE OR REPLACE TEMP TABLE _landuse_res (id BIGINT, refs BIGINT[], name VARCHAR)")
    conn.execute("INSERT INTO _landuse_res VALUES (?, ?, 'Complex')", [way_id, refs])
    conn.execute("CREATE OR REPLACE TEMP TABLE _landuse_node_pos (id BIGINT, lat DOUBLE, lon DOUBLE)")
    conn.executemany(
        "INSERT INTO _landuse_node_pos VALUES (?, ?, ?)", [(i, lat, lon) for i, (lat, lon) in nodes.items()]
    )


class TestLanduseIngest:
    def test_polygons_load_per_extract_and_only_once(self, dual_conn, monkeypatch):
        monkeypatch.setattr(osm, "_stage_landuse", _fake_stage_landuse)
        ny = Path("/cache/new-york-260501.osm.pbf")
        nj = Path("/cache/new-jersey-latest.osm.pbf")

        ref = osm.osm_landuse_residential([ny], dual_conn)
        rows = dual_conn.execute(f"SELECT landuse_id, extract, ST_NPoints(geom) FROM {ref.fqn}").fetchall()
        assert rows == [(100, "new-york-260501", 5)]

        osm.osm_landuse_residential([ny, nj], dual_conn)
        rows = dual_conn.execute(
            f"SELECT landuse_id, extract, ST_NPoints(geom), ST_IsValid(geom) FROM {ref.fqn} ORDER BY 1"
        ).fetchall()
        assert rows == [(100, "new-york-260501", 5, True), (200, "new-jersey-latest", 5, True)]

        osm.osm_landuse_residential([ny, nj], dual_conn)
        assert dual_conn.execute(f"SELECT count(*) FROM {ref.fqn}").fetchone()[0] == 2

    def test_table_without_extract_column_is_rebuilt(self, dual_conn, monkeypatch):
        monkeypatch.setattr(osm, "_stage_landuse", _fake_stage_landuse)
        dual_conn.execute("CREATE SCHEMA IF NOT EXISTS ducklake_geo.osm")
        dual_conn.execute("CREATE TABLE ducklake_geo.osm.landuse_residential (landuse_id BIGINT, geom GEOMETRY)")
        dual_conn.execute("INSERT INTO ducklake_geo.osm.landuse_residential VALUES (9, NULL)")
        ref = osm.osm_landuse_residential([Path("/cache/new-york-260501.osm.pbf")], dual_conn)
        assert dual_conn.execute(f"SELECT landuse_id FROM {ref.fqn}").fetchall() == [(100,)]


def _feature(osm_id, lon, lat, size=0.01):
    ring = [[lon, lat], [lon + size, lat], [lon + size, lat + size], [lon, lat + size], [lon, lat]]
    return {
        "type": "Feature",
        "properties": {"@id": osm_id, "@type": "way", "building": "yes"},
        "geometry": {"type": "Polygon", "coordinates": [ring]},
    }


class TestBuildingsPolygonsIngest:
    def test_polygons_load_per_extract_from_the_osmium_cache(self, dual_conn, tmp_path, monkeypatch):
        """With both osmium products cached beside the PBF, the loader reads
        the GeoJSONSeq straight into the table, tagged with the extract, and
        a second run over the same extract adds nothing."""
        monkeypatch.setattr(osm, "_require_osmium", lambda: "osmium")
        monkeypatch.setattr(osm.subprocess, "run", _boom)
        pbf = tmp_path / "x.osm.pbf"
        pbf.write_bytes(b"")
        filtered, geojson = osm._osmium_cache_paths(pbf)
        filtered.write_bytes(b"")
        # osmium's geojsonseq: RFC 8142 record separators before each feature.
        features = [_feature(1, -73.99, 40.75), _feature(2, -74.04, 40.74)]
        geojson.write_text("".join("\x1e" + json.dumps(f) + "\n" for f in features))

        ref = osm.osm_buildings_polygons([pbf], dual_conn)
        rows = dual_conn.execute(
            f"SELECT osm_id, extract, centroid_lat, centroid_lon FROM {ref.fqn} ORDER BY osm_id"
        ).fetchall()
        assert [(r[0], r[1]) for r in rows] == [(1, "x"), (2, "x")]
        assert rows[0][2] == pytest.approx(40.755, abs=1e-6)
        assert rows[0][3] == pytest.approx(-73.985, abs=1e-6)

        osm.osm_buildings_polygons([pbf], dual_conn)
        assert dual_conn.execute(f"SELECT count(*) FROM {ref.fqn}").fetchone()[0] == 2


class TestDropUnlessColumns:
    FQN = "ducklake_geo.osm.buildings_polygons"

    def test_forbidden_column_drops_the_table(self, dual_conn):
        dual_conn.execute("CREATE SCHEMA IF NOT EXISTS ducklake_geo.osm")
        dual_conn.execute(f"CREATE TABLE {self.FQN} (osm_id BIGINT, geom GEOMETRY, extract VARCHAR)")
        osm._drop_unless_columns(dual_conn, self.FQN, required={"extract"}, forbidden={"geom"})
        with pytest.raises(duckdb.CatalogException):
            dual_conn.execute(f"DESCRIBE {self.FQN}")

    def test_missing_required_column_drops_the_table(self, dual_conn):
        dual_conn.execute("CREATE SCHEMA IF NOT EXISTS ducklake_geo.osm")
        dual_conn.execute(f"CREATE TABLE {self.FQN} (osm_id BIGINT)")
        osm._drop_unless_columns(dual_conn, self.FQN, required={"extract"}, forbidden=set())
        with pytest.raises(duckdb.CatalogException):
            dual_conn.execute(f"DESCRIBE {self.FQN}")

    def test_current_shape_and_missing_table_are_left_alone(self, dual_conn):
        dual_conn.execute("CREATE SCHEMA IF NOT EXISTS ducklake_geo.osm")
        osm._drop_unless_columns(dual_conn, self.FQN, required={"extract"}, forbidden={"geom"})
        dual_conn.execute(f"CREATE TABLE {self.FQN} (osm_id BIGINT, extract VARCHAR)")
        dual_conn.execute(f"INSERT INTO {self.FQN} VALUES (1, 'x')")
        osm._drop_unless_columns(dual_conn, self.FQN, required={"extract"}, forbidden={"geom"})
        assert dual_conn.execute(f"SELECT count(*) FROM {self.FQN}").fetchone()[0] == 1
