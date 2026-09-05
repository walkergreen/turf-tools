"""NYS BOE county codes → county FIPS (`src/importers/nys_voter_file/counties.py`).

Behaviors locked in:

- All 62 counties are mapped, one-to-one, to three-digit FIPS codes.
- The five NYC boroughs map to the five counties the seed pins.
- The SQL CASE normalizes a numerically typed code and yields NULL for
  anything outside the table.
"""

import duckdb
from src.importers.nys_voter_file.counties import BOE_TO_FIPS, county_fips_sql

NYC_BOE_TO_FIPS = {"03": "005", "24": "047", "31": "061", "41": "081", "43": "085"}


def test_all_62_counties_map_to_distinct_three_digit_fips():
    assert len(BOE_TO_FIPS) == 62
    assert set(BOE_TO_FIPS) == {f"{n:02d}" for n in range(1, 63)}
    fips = list(BOE_TO_FIPS.values())
    assert len(set(fips)) == 62
    assert all(len(f) == 3 and f.isdigit() and int(f) % 2 == 1 for f in fips)


def test_nyc_boroughs():
    assert {boe: BOE_TO_FIPS[boe] for boe in NYC_BOE_TO_FIPS} == NYC_BOE_TO_FIPS


def test_sql_case_maps_normalizes_and_nulls():
    conn = duckdb.connect()
    sql = county_fips_sql("code")
    rows = conn.execute(
        f"SELECT code, {sql} FROM (VALUES ('31'), ('03'), ('3'), ('62'), ('99'), (NULL)) AS t(code)"
    ).fetchall()
    assert rows == [("31", "061"), ("03", "005"), ("3", "005"), ("62", "123"), ("99", None), (None, None)]
    assert conn.execute(f"SELECT {county_fips_sql('code')} FROM (VALUES (31)) AS t(code)").fetchone() == ("061",)
