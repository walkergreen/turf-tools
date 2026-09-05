"""The TIGER loaders over a (state, county) pair list — no network.

Behaviors locked in:

- URL construction per layer for multiple pairs; the national county URL.
- The tabblock filter scopes each state to its own counties (no cross-
  product leak of a county code into another state).
- `_pending` / `group_by_state` bookkeeping: loaded pairs are skipped,
  pending ones grouped per state.
- A loader with every pair already loaded never downloads; one with a new
  pair does (proved by a monkeypatched download that raises).
- `national_counties` reads a pre-populated county table without downloading.
- `boundary_from_blocks` unions only blocks inside the pair list.
- `county_match_rate_warnings` flags a county whose persons almost never
  matched a TIGER blockface (the signature of a non-FIPS county code), and
  only above the row floor.
"""

import pytest

import duckdb
from src.dags import boundaries, tiger
from src.geo import tiger_files, tiger_scope
from src.geo.scope import CountyScope, group_by_state
from src.models import TableRef
from src.tables import ensure_schema, table_fqn

PAIRS = [CountyScope("36", "061"), CountyScope("34", "017")]


def _boom(*_args, **_kwargs):
    raise AssertionError("network download attempted")


# ---------------------------------------------------------------------------
# URLs and predicates (pure)
# ---------------------------------------------------------------------------


class TestUrls:
    def test_addrfeat_urls_for_each_pair(self):
        assert [tiger_files.tiger_zip_url("ADDRFEAT", "2024", p.state_fips, p.county_fips) for p in PAIRS] == [
            "https://www2.census.gov/geo/tiger/TIGER2024/ADDRFEAT/tl_2024_36061_addrfeat.zip",
            "https://www2.census.gov/geo/tiger/TIGER2024/ADDRFEAT/tl_2024_34017_addrfeat.zip",
        ]

    def test_edges_urls_for_each_pair(self):
        assert [tiger_files.tiger_zip_url("EDGES", "2024", p.state_fips, p.county_fips) for p in PAIRS] == [
            "https://www2.census.gov/geo/tiger/TIGER2024/EDGES/tl_2024_36061_edges.zip",
            "https://www2.census.gov/geo/tiger/TIGER2024/EDGES/tl_2024_34017_edges.zip",
        ]

    def test_tabblock_urls_are_per_state_and_deduped(self):
        scope = [CountyScope("36", "061"), CountyScope("36", "005"), CountyScope("34", "017")]
        urls = [tiger_files.tiger_zip_url("TABBLOCK20", "2024", s) for s in group_by_state(scope)]
        assert urls == [
            "https://www2.census.gov/geo/tiger/TIGER2024/TABBLOCK20/tl_2024_34_tabblock20.zip",
            "https://www2.census.gov/geo/tiger/TIGER2024/TABBLOCK20/tl_2024_36_tabblock20.zip",
        ]

    def test_national_county_url(self):
        assert (
            tiger_files.tiger_zip_url("COUNTY", "2024", tiger_files.NATIONAL)
            == "https://www2.census.gov/geo/tiger/TIGER2024/COUNTY/tl_2024_us_county.zip"
        )

    def test_tabblock_filter_scopes_each_state_to_its_own_counties(self):
        """(34, 061) must not leak in just because 061 is wanted in state 36."""
        conn = duckdb.connect()
        conn.execute("CREATE TABLE blocks (STATEFP20 VARCHAR, COUNTYFP20 VARCHAR)")
        conn.executemany(
            "INSERT INTO blocks VALUES (?, ?)", [("36", "061"), ("36", "999"), ("34", "017"), ("34", "061")]
        )
        scope = group_by_state([CountyScope("36", "061"), CountyScope("34", "017")])
        selected = set()
        for state, counties in scope.items():
            rows = conn.execute(
                f"SELECT STATEFP20, COUNTYFP20 FROM blocks WHERE {tiger_files.tabblock_filter_sql(state, counties)}"
            ).fetchall()
            selected.update(rows)
        assert selected == {("36", "061"), ("34", "017")}


# ---------------------------------------------------------------------------
# Loader bookkeeping
# ---------------------------------------------------------------------------


def _insert_addrfeat_marker(conn, state, county):
    conn.execute(
        "INSERT INTO ducklake_geo.tiger.addrfeat VALUES "
        "('1', 'X', '1', '9', '2', '8', '10001', '10001', [], ?, ?, NULL)",
        [state, county],
    )


class TestPending:
    def test_pending_skips_loaded_pairs_and_groups_by_state(self, dual_conn, monkeypatch):
        monkeypatch.setattr(tiger_files, "download_and_extract", _boom)
        # An empty scope creates the table without fetching anything.
        tiger.tiger_addrfeat_raw([], "2024", "unused", dual_conn)
        _insert_addrfeat_marker(dual_conn, "36", "061")
        scope = [CountyScope("36", "061"), CountyScope("36", "005"), CountyScope("34", "017")]
        pending = tiger._pending(dual_conn, "ducklake_geo.tiger.addrfeat", scope)
        assert pending == [CountyScope("34", "017"), CountyScope("36", "005")]
        assert group_by_state(pending) == {"34": ["017"], "36": ["005"]}

    def test_loader_skips_pairs_already_loaded(self, dual_conn, monkeypatch):
        monkeypatch.setattr(tiger_files, "download_and_extract", _boom)
        tiger.tiger_addrfeat_raw([], "2024", "unused", dual_conn)
        _insert_addrfeat_marker(dual_conn, "36", "061")
        ref = tiger.tiger_addrfeat_raw([CountyScope("36", "061")], "2024", "unused", dual_conn)
        assert ref.table == "addrfeat"
        with pytest.raises(AssertionError, match="network"):
            tiger.tiger_addrfeat_raw([CountyScope("36", "061"), CountyScope("36", "005")], "2024", "unused", dual_conn)

    def test_tabblock_loader_downloads_only_states_with_pending_counties(self, dual_conn, monkeypatch):
        fetched = []

        def fake_download(url, zip_path, extract_dir):
            fetched.append(url)
            extract_dir.mkdir(parents=True, exist_ok=True)  # no shapefiles → nothing inserted

        monkeypatch.setattr(tiger_files, "download_and_extract", fake_download)
        tiger.tiger_tabblock_raw([], "2024", "unused", dual_conn)
        dual_conn.execute("INSERT INTO ducklake_geo.tiger.tabblock VALUES ('360610001001000', '36', '061', 1, NULL)")
        tiger.tiger_tabblock_raw([CountyScope("36", "061"), CountyScope("34", "017")], "2024", "unused", dual_conn)
        assert fetched == ["https://www2.census.gov/geo/tiger/TIGER2024/TABBLOCK20/tl_2024_34_tabblock20.zip"]


class TestNationalCounties:
    def test_statewide_expands_from_the_county_table(self, dual_conn, monkeypatch):
        monkeypatch.setattr(tiger_files, "download_and_extract", _boom)
        dual_conn.execute("CREATE SCHEMA IF NOT EXISTS ducklake_geo.tiger")
        dual_conn.execute(
            f"CREATE TABLE {tiger_scope.COUNTY_TABLE_FQN} "
            "(tiger_year VARCHAR, state_fips VARCHAR, county_fips VARCHAR, name VARCHAR)"
        )
        dual_conn.executemany(
            f"INSERT INTO {tiger_scope.COUNTY_TABLE_FQN} VALUES (?, ?, ?, ?)",
            [
                ("2024", "36", "061", "New York"),
                ("2024", "36", "005", "Bronx"),
                ("2024", "36", "047", "Kings"),
                ("2024", "34", "017", "Hudson"),
                ("2023", "34", "999", "Stale vintage"),
            ],
        )
        assert tiger_scope.national_counties(dual_conn, "2024", "unused", ["36", "34"]) == {
            "36": ["005", "047", "061"],
            "34": ["017"],
        }
        assert tiger_scope.national_counties(dual_conn, "2024", "unused", []) == {}

    def test_missing_vintage_triggers_a_download(self, dual_conn, monkeypatch):
        monkeypatch.setattr(tiger_files, "download_and_extract", _boom)
        with pytest.raises(AssertionError, match="network"):
            tiger_scope.national_counties(dual_conn, "2024", "unused", ["36"])


# ---------------------------------------------------------------------------
# boundary_from_blocks scoping
# ---------------------------------------------------------------------------


def _square(lon, lat, half=0.01):
    return (
        f"POLYGON(({lon - half} {lat - half}, {lon + half} {lat - half}, "
        f"{lon + half} {lat + half}, {lon - half} {lat + half}, {lon - half} {lat - half}))"
    )


def test_boundary_from_blocks_scopes_by_pair_list(dual_conn):
    conn = dual_conn
    org = "scope_test"
    ensure_schema(conn, org)
    conn.execute("CREATE SCHEMA IF NOT EXISTS ducklake_geo.tiger")
    conn.execute("""
        CREATE TABLE ducklake_geo.tiger.tabblock (
            block_geoid VARCHAR, state_fips VARCHAR, county_fips VARCHAR, land_area BIGINT, geom GEOMETRY
        )
    """)
    ny_lon, ny_lat = -73.99, 40.75
    nj_lon, nj_lat = -74.03, 40.74
    conn.execute(
        "INSERT INTO ducklake_geo.tiger.tabblock VALUES "
        f"('NY1', '36', '061', 100, ST_GeomFromText('{_square(ny_lon, ny_lat)}')), "
        f"('NJ1', '34', '017', 100, ST_GeomFromText('{_square(nj_lon, nj_lat)}'))"
    )
    persons_fqn = table_fqn(org, "persons_geocoded")
    conn.execute(f"CREATE TABLE {persons_fqn} (zip5 VARCHAR, longitude DOUBLE, latitude DOUBLE)")
    conn.executemany(
        f"INSERT INTO {persons_fqn} VALUES (?, ?, ?)",
        [("10001", ny_lon, ny_lat), ("07030", nj_lon, nj_lat)],
    )
    persons_ref = TableRef(catalog="ducklake", schema=org, table="persons_geocoded", version=0)
    tabblock_ref = TableRef(catalog="ducklake_geo", schema="tiger", table="tabblock", version=0)

    ref = boundaries.boundary_from_blocks(
        persons_geocoded=persons_ref,
        tiger_tabblock_raw=tabblock_ref,
        geo_scope=[CountyScope("36", "061")],
        key_group="nyc_zips",
        key_expression="zip5",
        schema=org,
        conn=conn,
    )
    keys = {r[0] for r in conn.execute(f"SELECT key FROM {ref.fqn}").fetchall()}
    assert keys == {"10001"}

    ref = boundaries.boundary_from_blocks(
        persons_geocoded=persons_ref,
        tiger_tabblock_raw=tabblock_ref,
        geo_scope=[CountyScope("36", "061"), CountyScope("34", "017")],
        key_group="nyc_zips",
        key_expression="zip5",
        schema=org,
        conn=conn,
    )
    keys = {r[0] for r in conn.execute(f"SELECT key FROM {ref.fqn}").fetchall()}
    assert keys == {"10001", "07030"}


# ---------------------------------------------------------------------------
# Post-geocode county sanity check
# ---------------------------------------------------------------------------


def _geocoded(rows, columns="state VARCHAR, county_fips VARCHAR, position_source VARCHAR"):
    conn = duckdb.connect()
    conn.execute(f"CREATE TABLE geocoded ({columns})")
    placeholders = ", ".join("?" for _ in columns.split(","))
    conn.executemany(f"INSERT INTO geocoded VALUES ({placeholders})", rows)
    return conn


def _county(state, county, matched, unmatched):
    return [(state, county, "osm_matched")] * matched + [(state, county, None)] * unmatched


class TestCountyMatchRateWarnings:
    def test_county_that_barely_matched_is_reported(self):
        # 2 of 150 Kings-coded persons matched; New York County matched normally.
        conn = _geocoded(_county("NY", "047", 2, 148) + _county("NY", "061", 140, 10))
        (warning,) = tiger_scope.county_match_rate_warnings(conn, "geocoded")
        assert warning.startswith("WARNING: county NY:047 — 2 of 150 persons (1.3%)")
        assert "Census county FIPS" in warning and "TIGER_SCOPE" in warning

    def test_small_counties_and_normal_counties_are_quiet(self):
        # 50 unmatched rows sit under the row floor; 150 well-matched rows pass.
        conn = _geocoded(_county("NY", "047", 0, 50) + _county("NY", "061", 140, 10))
        assert tiger_scope.county_match_rate_warnings(conn, "geocoded") == []

    def test_osm_only_rescues_do_not_count_as_tiger_matches(self):
        conn = _geocoded([("NJ", "017", "osm_only")] * 150)
        (warning,) = tiger_scope.county_match_rate_warnings(conn, "geocoded")
        assert "NJ:017 — 0 of 150" in warning

    def test_thresholds_are_parameters(self):
        conn = _geocoded(_county("NY", "047", 0, 50))
        assert tiger_scope.county_match_rate_warnings(conn, "geocoded", min_rows=10) != []
        conn = _geocoded(_county("NY", "047", 20, 130))  # 13 % matched
        assert tiger_scope.county_match_rate_warnings(conn, "geocoded") == []
        assert tiger_scope.county_match_rate_warnings(conn, "geocoded", max_rate=0.2) != []

    def test_rows_without_a_county_are_skipped_and_missing_columns_are_tolerated(self):
        conn = _geocoded([("NY", None, None)] * 200)
        assert tiger_scope.county_match_rate_warnings(conn, "geocoded") == []
        conn = _geocoded([("NY", None)] * 200, columns="state VARCHAR, position_source VARCHAR")
        assert tiger_scope.county_match_rate_warnings(conn, "geocoded") == []
