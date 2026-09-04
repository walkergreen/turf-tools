"""The TargetSmart importer: a synthetic Parquet extract → `persons_validated`.

Every row here is invented. The fixture covers the transform's branches — the
CASS-vs-registration address fallback, ZIP+4 truncation, the party / status
label maps, the vote-code → method map with the SQL-computed general election
date, the Unregistered / deceased exclusions, and an INTEGER-typed district
column — plus the manifest contract the web and compiler read.
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

import pytest

import duckdb
from src.importers.base import Manifest, SourceUnreadableError
from src.importers.targetsmart import TargetSmartImporter
from src.importers.targetsmart.manifest import TARGETSMART_MANIFEST
from src.importers.targetsmart.transform import general_election_date_sql, iso_date_sql
from src.models import Person

if TYPE_CHECKING:
    from pathlib import Path

SCHEMA = "targetsmart_v1"

# Source column → DuckDB type for the synthetic extract. `vb_vf_cd` is INTEGER
# on purpose: a BigQuery export types numeric-looking codes that way.
SCALAR_TYPES: dict[str, str] = {
    "vb_voterbase_id": "VARCHAR",
    "vb_tsmart_first_name": "VARCHAR",
    "vb_tsmart_middle_name": "VARCHAR",
    "vb_tsmart_last_name": "VARCHAR",
    "vb_tsmart_name_suffix": "VARCHAR",
    "vb_vf_reg_cass_street_num": "VARCHAR",
    "vb_vf_reg_cass_pre_directional": "VARCHAR",
    "vb_vf_reg_cass_street_name": "VARCHAR",
    "vb_vf_reg_cass_street_suffix": "VARCHAR",
    "vb_vf_reg_cass_post_directional": "VARCHAR",
    "vb_vf_reg_cass_unit_designator": "VARCHAR",
    "vb_vf_reg_cass_apt_num": "VARCHAR",
    "vb_vf_reg_address_1": "VARCHAR",
    "vb_vf_reg_address_2": "VARCHAR",
    "vb_vf_reg_cass_city": "VARCHAR",
    "vb_vf_reg_city": "VARCHAR",
    "vb_vf_reg_state": "VARCHAR",
    "vb_vf_reg_cass_zip": "VARCHAR",
    "vb_vf_reg_zip": "VARCHAR",
    "vb_vf_reg_zip4": "VARCHAR",
    "vb_vf_county_code": "VARCHAR",
    "vb_vf_county_name": "VARCHAR",
    "vb_vf_precinct_id": "VARCHAR",
    "vb_vf_precinct_name": "VARCHAR",
    "vb_vf_cd": "INTEGER",
    "vb_vf_sd": "VARCHAR",
    "vb_vf_hd": "VARCHAR",
    "vb_voterbase_gender": "VARCHAR",
    "vb_voterbase_dob": "VARCHAR",
    "vb_vf_party": "VARCHAR",
    "vb_vf_voter_status": "VARCHAR",
    "vb_voterbase_registration_status": "VARCHAR",
    "vb_vf_registration_date": "VARCHAR",
    "vb_voterbase_deceased_flag": "VARCHAR",
    "legal_commercial_model_usage_ok": "BOOLEAN",
}
VOTE_COLUMNS = [f"vb_vf_{kind}{year}" for year in range(2016, 2027) for kind in ("g", "p")]
ALL_COLUMNS = [*SCALAR_TYPES, *VOTE_COLUMNS]

# Reserved by the downstream assembly step; an importer must never emit them.
RESERVED_COLUMNS = {"latitude", "longitude", "position_source", "blockface_id", "building_id", "door_id"}


def _row(**values: object) -> dict[str, object]:
    unknown = set(values) - set(ALL_COLUMNS)
    assert not unknown, f"not source columns: {sorted(unknown)}"
    return {col: values.get(col) for col in ALL_COLUMNS}


def _registered(**values: object) -> dict[str, object]:
    return _row(vb_voterbase_registration_status="Registered", vb_vf_reg_state="NY", **values)


ROWS = [
    # Full CASS address, ZIP+4 run together, three votes across two years.
    _registered(
        vb_voterbase_id="TS-0001",
        vb_tsmart_first_name="Ada",
        vb_tsmart_middle_name="Q",
        vb_tsmart_last_name="Testperson",
        vb_tsmart_name_suffix="Jr",
        vb_vf_reg_cass_street_num="123",
        vb_vf_reg_cass_pre_directional="N",
        vb_vf_reg_cass_street_name="MAIN",
        vb_vf_reg_cass_street_suffix="ST",
        vb_vf_reg_cass_post_directional="W",
        vb_vf_reg_cass_unit_designator="APT",
        vb_vf_reg_cass_apt_num="4B",
        vb_vf_reg_address_1="123 North Main Street West",
        vb_vf_reg_address_2="Apartment 4B",
        vb_vf_reg_cass_city="NEW YORK",
        vb_vf_reg_city="New York",
        vb_vf_reg_cass_zip="100011234",
        vb_vf_reg_zip="10001",
        vb_vf_reg_zip4="1234",
        vb_vf_county_code="061",
        vb_vf_precinct_id="065012",
        vb_vf_cd=12,
        vb_vf_sd="47",
        vb_vf_hd="65",
        vb_voterbase_gender="F",
        vb_voterbase_dob="19850214",
        vb_vf_party="Democrat",
        vb_vf_voter_status="Active",
        vb_vf_registration_date="20100105",
        vb_voterbase_deceased_flag="N",
        legal_commercial_model_usage_ok=True,
        vb_vf_g2024="P",
        vb_vf_p2024="E",
        vb_vf_g2022="A",
    ),
    # No CASS parts at all: the registration address, city, and ZIP fall through.
    _registered(
        vb_voterbase_id="TS-0002",
        vb_tsmart_first_name="Bram",
        vb_tsmart_last_name="Placeholder",
        vb_vf_reg_address_1="45 Oak Avenue",
        vb_vf_reg_address_2="Unit 2",
        vb_vf_reg_city="Brooklyn",
        vb_vf_reg_zip="11201",
        vb_vf_cd=7,
        vb_vf_party="Republican",
        vb_vf_voter_status="Inactive",
        legal_commercial_model_usage_ok=False,
        vb_vf_g2024="Y",
    ),
    # Unregistered: excluded.
    _row(
        vb_voterbase_id="TS-0003",
        vb_tsmart_first_name="Cleo",
        vb_tsmart_last_name="Dropped",
        vb_voterbase_registration_status="Unregistered",
        vb_vf_reg_address_1="1 Nowhere Rd",
        vb_vf_reg_city="Albany",
        vb_vf_reg_state="NY",
        vb_vf_reg_zip="12207",
    ),
    # Deceased flag `Y`: excluded.
    _registered(
        vb_voterbase_id="TS-0004",
        vb_tsmart_first_name="Dov",
        vb_tsmart_last_name="Dropped",
        vb_vf_reg_address_1="2 Nowhere Rd",
        vb_vf_reg_city="Albany",
        vb_vf_reg_zip="12207",
        vb_voterbase_deceased_flag="Y",
    ),
    # Empty CASS street name with a stray street number: fallback fires. No
    # votes, ISO birthdate, `No Party`, status Unknown, NULL deceased flag.
    _registered(
        vb_voterbase_id="TS-0005",
        vb_tsmart_first_name="Esme",
        vb_tsmart_last_name="Fallback",
        vb_vf_reg_cass_street_num="9",
        vb_vf_reg_cass_street_name="",
        vb_vf_reg_address_1="9 Elm Ct",
        vb_vf_reg_city="Yonkers",
        vb_vf_reg_zip="10701",
        vb_voterbase_dob="1990-07-04",
        vb_vf_party="No Party",
        vb_vf_voter_status="Unknown",
    ),
    # Absentee (`M`) general + affidavit (`Q`) primary; blank vote codes skipped.
    # Empty unit parts with no registration line 2 → no address_line_2.
    _registered(
        vb_voterbase_id="TS-0006",
        vb_tsmart_first_name="Femi",
        vb_tsmart_last_name="Methods",
        vb_vf_reg_cass_street_num="77",
        vb_vf_reg_cass_street_name="RIVER",
        vb_vf_reg_cass_street_suffix="RD",
        vb_vf_reg_cass_unit_designator="",
        vb_vf_reg_cass_apt_num="",
        vb_vf_reg_cass_city="BRONX",
        vb_vf_reg_cass_zip="10451",
        vb_vf_party="Working Fam",
        vb_vf_voter_status="Active",
        vb_vf_g2024="M",
        vb_vf_p2024="Q",
        vb_vf_g2020="",
        vb_vf_p2020=" ",
    ),
    # Party outside the map → `other`; NULL status → `unknown`; flag `0` kept.
    _registered(
        vb_voterbase_id="TS-0007",
        vb_tsmart_first_name="Gil",
        vb_tsmart_last_name="Otherparty",
        vb_vf_reg_address_1="300 Lake St",
        vb_vf_reg_city="Buffalo",
        vb_vf_reg_zip="14202",
        vb_vf_party="Peace and Freedom",
        vb_voterbase_deceased_flag="0",
    ),
    # NULL party stays NULL; flag `false` kept.
    _registered(
        vb_voterbase_id="TS-0008",
        vb_tsmart_first_name="Hana",
        vb_tsmart_last_name="Noparty",
        vb_vf_reg_address_1="8 Pine Way",
        vb_vf_reg_city="Ithaca",
        vb_vf_reg_zip="14850",
        vb_voterbase_deceased_flag="false",
    ),
    # Deceased flag `1`: excluded.
    _registered(
        vb_voterbase_id="TS-0009",
        vb_tsmart_first_name="Ivo",
        vb_tsmart_last_name="Dropped",
        vb_vf_reg_address_1="3 Nowhere Rd",
        vb_vf_reg_city="Albany",
        vb_vf_reg_zip="12207",
        vb_voterbase_deceased_flag="1",
    ),
]
KEPT_IDS = {"TS-0001", "TS-0002", "TS-0005", "TS-0006", "TS-0007", "TS-0008"}


class _Progress:
    def __init__(self) -> None:
        self.steps = 0

    def advance(self, n: int = 1) -> None:
        self.steps += n


def _write_parquet(
    path: Path,
    rows: list[dict[str, object]],
    types: dict[str, str] | None = None,
    exclude: tuple[str, ...] = (),
) -> str:
    """Write `rows` to `path` with the reference column set, typed per
    `SCALAR_TYPES` (vote columns VARCHAR) with `types` overriding, minus
    `exclude`."""
    col_types = {**SCALAR_TYPES, **dict.fromkeys(VOTE_COLUMNS, "VARCHAR"), **(types or {})}
    columns = [c for c in col_types if c not in exclude]
    writer = duckdb.connect()
    writer.execute("CREATE TABLE src (" + ", ".join(f"{c} {col_types[c]}" for c in columns) + ")")
    placeholders = ", ".join("?" for _ in columns)
    writer.executemany(f"INSERT INTO src VALUES ({placeholders})", [[row[c] for c in columns] for row in rows])
    writer.execute(f"COPY src TO '{path}' (FORMAT PARQUET)")
    writer.close()
    return str(path)


def _load(conn: duckdb.DuckDBPyConnection, source: str) -> tuple[dict[str, dict[str, object]], _Progress]:
    """Run the importer on `source`; return persons_validated keyed by external_id."""
    progress = _Progress()
    ref = TargetSmartImporter().load(source, SCHEMA, conn, progress)
    rel = conn.table(ref.fqn)
    rows = [dict(zip(rel.columns, row, strict=True)) for row in rel.fetchall()]
    return {str(r["external_id"]): r for r in rows}, progress


@pytest.fixture()
def persons(conn, tmp_path) -> dict[str, dict[str, object]]:
    rows, _ = _load(conn, _write_parquet(tmp_path / "extract.parquet", ROWS))
    return rows


# ---------------------------------------------------------------------------
# Row selection + the Person contract
# ---------------------------------------------------------------------------


def test_unregistered_and_deceased_rows_are_excluded(persons) -> None:
    assert set(persons) == KEPT_IDS


def test_person_required_columns_present_and_rows_validate(persons) -> None:
    columns = set(next(iter(persons.values())))
    assert set(Person.model_fields) <= columns
    assert not (columns & RESERVED_COLUMNS)
    # The web's text-multi editor rewrites a field keyed `precinct`; the
    # TargetSmart precinct must not be bound to that key.
    assert "precinct_id" in columns and "precinct" not in columns
    for row in persons.values():
        Person.model_validate(row)
        assert row["external_id_type"] == "ts_voterbase"
        assert row["state"] == "NY"
        assert row["half_code"] is None


def test_progress_steps_match_declaration(conn, tmp_path) -> None:
    _, progress = _load(conn, _write_parquet(tmp_path / "extract.parquet", ROWS))
    assert progress.steps == TargetSmartImporter.PROGRESS_STEPS


# ---------------------------------------------------------------------------
# Address assembly
# ---------------------------------------------------------------------------


def test_cass_parts_assemble_address_and_unit(persons) -> None:
    row = persons["TS-0001"]
    assert row["address_line_1"] == "123 N MAIN ST W"
    assert row["address_line_2"] == "APT 4B"
    assert row["city"] == "NEW YORK"
    assert row["zip5"] == "10001"
    assert row["zip4"] == "1234"
    assert row["middle_name"] == "Q"
    assert row["name_suffix"] == "Jr"


def test_missing_cass_parts_fall_back_to_registration_address(persons) -> None:
    row = persons["TS-0002"]
    assert row["address_line_1"] == "45 Oak Avenue"
    assert row["address_line_2"] == "Unit 2"
    assert row["city"] == "Brooklyn"
    assert row["zip5"] == "11201"
    assert row["zip4"] is None


def test_empty_cass_street_name_falls_back_even_with_a_street_number(persons) -> None:
    assert persons["TS-0005"]["address_line_1"] == "9 Elm Ct"


def test_empty_unit_parts_yield_no_address_line_2(persons) -> None:
    row = persons["TS-0006"]
    assert row["address_line_1"] == "77 RIVER RD"
    assert row["address_line_2"] is None


# ---------------------------------------------------------------------------
# Scalar canonicalization
# ---------------------------------------------------------------------------


def test_party_and_status_labels(persons) -> None:
    expected = {
        "TS-0001": ("democratic", "active"),
        "TS-0002": ("republican", "inactive"),
        "TS-0005": ("unaffiliated", "unknown"),
        "TS-0006": ("working_families", "active"),
        "TS-0007": ("other", "unknown"),
        "TS-0008": (None, "unknown"),
    }
    assert {k: (persons[k]["enrollment"], persons[k]["registration_status"]) for k in expected} == expected


def test_dates_normalize_to_iso(persons) -> None:
    assert persons["TS-0001"]["date_of_birth"] == "1985-02-14"
    assert persons["TS-0001"]["registration_date"] == "2010-01-05"
    assert persons["TS-0005"]["date_of_birth"] == "1990-07-04"
    assert persons["TS-0005"]["registration_date"] is None


def test_districts_and_codes_are_varchar_passthroughs(persons) -> None:
    row = persons["TS-0001"]
    assert row["congressional_district"] == "12"
    assert row["assembly_district"] == "65"
    assert row["senate_district"] == "47"
    assert row["precinct_id"] == "065012"
    assert row["county_code"] == "061"
    assert row["gender"] == "F"
    assert persons["TS-0002"]["congressional_district"] == "7"


def test_commercial_model_flag_passes_through(persons) -> None:
    assert persons["TS-0001"]["commercial_model_ok"] is True
    assert persons["TS-0002"]["commercial_model_ok"] is False
    assert persons["TS-0005"]["commercial_model_ok"] is None


# ---------------------------------------------------------------------------
# Voting history
# ---------------------------------------------------------------------------


def test_voting_history_entries(persons) -> None:
    assert persons["TS-0001"]["voting_history"] == [
        {"year": 2022, "type": "general", "date": "2022-11-08", "method": "absentee"},
        {"year": 2024, "type": "primary", "date": None, "method": "early_voting"},
        {"year": 2024, "type": "general", "date": "2024-11-05", "method": "poll_site"},
    ]
    assert persons["TS-0002"]["voting_history"] == [
        {"year": 2024, "type": "general", "date": "2024-11-05", "method": "other"},
    ]
    assert persons["TS-0006"]["voting_history"] == [
        {"year": 2024, "type": "primary", "date": None, "method": "affidavit"},
        {"year": 2024, "type": "general", "date": "2024-11-05", "method": "absentee"},
    ]


def test_no_votes_is_an_empty_list_not_null(persons) -> None:
    assert persons["TS-0005"]["voting_history"] == []
    assert persons["TS-0008"]["voting_history"] == []


@pytest.mark.parametrize(
    ("year", "expected"),
    [(2016, "2016-11-08"), (2018, "2018-11-06"), (2020, "2020-11-03"), (2022, "2022-11-08"), (2024, "2024-11-05")],
)
def test_general_election_date_sql(year: int, expected: str) -> None:
    assert duckdb.connect().execute(f"SELECT {general_election_date_sql(year)}").fetchone()[0] == expected


@pytest.mark.parametrize(
    ("literal", "expected"),
    [
        ("'19850214'", "1985-02-14"),
        ("'1985-02-14'", "1985-02-14"),
        ("DATE '1985-02-14'", "1985-02-14"),
        ("TIMESTAMP '1985-02-14 10:30:00'", "1985-02-14"),
        ("'2/14/1985'", None),
        ("''", None),
        ("NULL", None),
    ],
)
def test_iso_date_sql_accepts_compact_iso_and_typed_dates(literal: str, expected: str | None) -> None:
    sql = f"SELECT {iso_date_sql('vb_voterbase_dob')} FROM (SELECT {literal} AS vb_voterbase_dob) AS raw"
    assert duckdb.connect().execute(sql).fetchone()[0] == expected


# ---------------------------------------------------------------------------
# Source typing + source handling
# ---------------------------------------------------------------------------


def test_boolean_deceased_flag_and_date_typed_columns(conn, tmp_path) -> None:
    rows = [
        _registered(
            vb_voterbase_id="TS-0101",
            vb_tsmart_first_name="Jo",
            vb_tsmart_last_name="Typed",
            vb_vf_reg_address_1="5 Typed St",
            vb_vf_reg_city="Utica",
            vb_vf_reg_zip="13501",
            vb_vf_registration_date=datetime.date(2015, 3, 9),
            vb_voterbase_deceased_flag=False,
        ),
        _registered(
            vb_voterbase_id="TS-0102",
            vb_tsmart_first_name="Kit",
            vb_tsmart_last_name="Dropped",
            vb_vf_reg_address_1="6 Typed St",
            vb_vf_reg_city="Utica",
            vb_vf_reg_zip="13501",
            vb_voterbase_deceased_flag=True,
        ),
    ]
    types = {"vb_vf_registration_date": "DATE", "vb_voterbase_deceased_flag": "BOOLEAN"}
    persons, _ = _load(conn, _write_parquet(tmp_path / "typed.parquet", rows, types=types))
    assert set(persons) == {"TS-0101"}
    assert persons["TS-0101"]["registration_date"] == "2015-03-09"


def test_glob_source_loads_every_part(conn, tmp_path) -> None:
    parts = tmp_path / "export"
    parts.mkdir()
    _write_parquet(parts / "part-00000.parquet", ROWS[:1])
    _write_parquet(parts / "part-00001.parquet", ROWS[1:2])
    persons, _ = _load(conn, str(parts / "part-*.parquet"))
    assert set(persons) == {"TS-0001", "TS-0002"}


def test_missing_source_column_names_the_column(conn, tmp_path) -> None:
    source = _write_parquet(tmp_path / "short.parquet", ROWS, exclude=("vb_vf_party",))
    with pytest.raises(ValueError, match="vb_vf_party"):
        _load(conn, source)


def test_missing_local_source_fails_before_touching_the_catalog() -> None:
    with pytest.raises(SourceUnreadableError, match="Import source not found"):
        TargetSmartImporter().load("/nope/part-*.parquet", SCHEMA, None, _Progress())


def test_unreadable_source_is_wrapped(conn, tmp_path) -> None:
    not_parquet = tmp_path / "extract.parquet"
    not_parquet.write_text("this is not a parquet file", encoding="utf-8")
    with pytest.raises(SourceUnreadableError) as excinfo:
        TargetSmartImporter().load(str(not_parquet), SCHEMA, conn, _Progress())
    assert "Check that it exists and is reachable." in str(excinfo.value)
    assert excinfo.value.__cause__ is not None


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def test_manifest_round_trips_and_declares_two_key_groups() -> None:
    payload = TARGETSMART_MANIFEST.model_dump(by_alias=True)
    assert Manifest.model_validate(payload) == TARGETSMART_MANIFEST
    fields = [fd for section in TARGETSMART_MANIFEST.fields for fd in section]
    identifiers = [fd.identifier for fd in fields]
    assert len(identifiers) == len(set(identifiers))
    assert {fd.key_group: (fd.column, fd.key_group_label) for fd in fields if fd.key_group} == {
        "nyc_zips": ("zip5", "ZIP codes"),
        "precincts": ("precinct_id", "Precincts"),
    }


def test_manifest_columns_exist_in_persons_validated(persons) -> None:
    columns = set(next(iter(persons.values())))
    declared = {fd.column for section in TARGETSMART_MANIFEST.fields for fd in section if fd.column}
    assert declared <= columns
