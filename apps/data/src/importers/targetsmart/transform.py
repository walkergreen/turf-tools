"""TargetSmart raw → canonical `Person` transformation SQL.

Maps a BigQuery-exported Parquet extract of TargetSmart's national voter file
(one row per person, `vb_*` columns) onto the `Person` required core (see
`src/models.py`) plus the dataset's filterable fields. Every source column is
CAST to VARCHAR before use — a BigQuery export types some codes (district
numbers, ZIPs, flags) as INTEGER or BOOLEAN depending on the query that
produced it — and empty strings fold to NULL so `concat_ws` never emits stray
separators.

The vote columns (`vb_vf_g<year>` generals, `vb_vf_p<year>` primaries) are
assembled into the canonical `voting_history` STRUCT[] directly in SQL: one
entry per non-empty column, so a row with no votes gets an empty list.
"""

from __future__ import annotations

import re

# `vb_vf_<g|p><year>`: one column per election, holding a single-letter vote
# code or NULL (did not vote).
_VOTE_COLUMN_RE = re.compile(r"^vb_vf_([gp])(\d{4})$")
_VOTE_COLUMN_TYPES = {"g": "general", "p": "primary"}

# Canonical type of the `voting_history` column every importer produces.
VOTING_HISTORY_TYPE = "STRUCT(year INTEGER, type VARCHAR, date VARCHAR, method VARCHAR)[]"

# Raw columns the transform reads besides the vote columns. The importer checks
# these are present so a mis-exported extract fails with the column names
# rather than a binder error.
SOURCE_COLUMNS: frozenset[str] = frozenset(
    {
        "vb_voterbase_id",
        "vb_tsmart_first_name",
        "vb_tsmart_middle_name",
        "vb_tsmart_last_name",
        "vb_tsmart_name_suffix",
        "vb_vf_reg_cass_street_num",
        "vb_vf_reg_cass_pre_directional",
        "vb_vf_reg_cass_street_name",
        "vb_vf_reg_cass_street_suffix",
        "vb_vf_reg_cass_post_directional",
        "vb_vf_reg_cass_unit_designator",
        "vb_vf_reg_cass_apt_num",
        "vb_vf_reg_address_1",
        "vb_vf_reg_address_2",
        "vb_vf_reg_cass_city",
        "vb_vf_reg_city",
        "vb_vf_reg_state",
        "vb_vf_reg_cass_zip",
        "vb_vf_reg_zip",
        "vb_vf_reg_zip4",
        "vb_vf_county_code",
        "vb_vf_precinct_id",
        "vb_vf_cd",
        "vb_vf_sd",
        "vb_vf_hd",
        "vb_voterbase_gender",
        "vb_voterbase_dob",
        "vb_vf_party",
        "vb_vf_voter_status",
        "vb_voterbase_registration_status",
        "vb_vf_registration_date",
        "vb_voterbase_deceased_flag",
        "legal_commercial_model_usage_ok",
    }
)

# TargetSmart raw → canonical mappings used by the SQL CASE expressions below.
# Keys are TargetSmart's labels; values are cross-state canonical labels.

TS_REGISTRATION_STATUS_LABELS: dict[str, str] = {
    "Active": "active",
    "Inactive": "inactive",
}

TS_PARTY_LABELS: dict[str, str] = {
    "Democrat": "democratic",
    "Republican": "republican",
    "Unaffiliated": "unaffiliated",
    "No Party": "unaffiliated",
    "Working Fam": "working_families",
    "Green": "green",
    "Libertarian": "libertarian",
    "Conservative": "conservative",
}

# Single-letter vote codes → canonical voting method. Codes not listed (Y, F,
# B, Z, R, S — a recorded vote with no usable method) map to `other`.
TS_VOTE_METHOD_LABELS: dict[str, str] = {
    "P": "poll_site",
    "E": "early_voting",
    "A": "absentee",
    "M": "absentee",
    "Q": "affidavit",
}

# `vb_voterbase_deceased_flag` values marking a person deceased, compared
# case-insensitively against the column's VARCHAR cast so a BOOLEAN `true`
# matches too. NULL is kept: an absent flag is not evidence of death.
TS_DECEASED_VALUES: tuple[str, ...] = ("Y", "1", "TRUE")


def _text(col: str) -> str:
    """SQL reading raw column `col` as a trimmed VARCHAR, empty → NULL."""
    return f"nullif(trim(CAST(raw.{col} AS VARCHAR)), '')"


def county_fips_sql(columns: list[str]) -> str:
    """SQL deriving the optional 3-digit `county_fips` from TargetSmart's county
    code: `vb_vf_county_code` (the Census county FIPS code within
    `vb_vf_reg_state`, exported without zero-padding — 61 for New York County,
    5 for the Bronx), falling back to `vb_tsmart_county_code` when the extract
    carries it. A 1–3 digit numeric code is zero-padded to three digits;
    anything else (a name, blank, NULL) yields NULL so scope resolution falls
    back to the whole state."""
    sources = [_text("vb_vf_county_code")]
    if "vb_tsmart_county_code" in columns:
        sources.append(_text("vb_tsmart_county_code"))
    code = f"coalesce({', '.join(sources)})" if len(sources) > 1 else sources[0]
    return f"CASE WHEN {code} ~ '^[0-9]{{1,3}}$' THEN lpad({code}, 3, '0') END"


def _case_from_map(expr: str, mapping: dict[str, str], default: str, null_passthrough: bool) -> str:
    """SQL CASE mapping `expr`'s values via `mapping`; unmatched values take
    `default`. With `null_passthrough`, a NULL input stays NULL instead of
    falling through to `default`."""
    branches = "\n        ".join(f"WHEN {expr} = '{raw}' THEN '{canonical}'" for raw, canonical in mapping.items())
    null_branch = f"WHEN {expr} IS NULL THEN NULL\n        " if null_passthrough else ""
    return f"CASE\n        {null_branch}{branches}\n        ELSE '{default}'\n    END"


def iso_date_sql(col: str) -> str:
    """SQL normalizing raw date column `col` to a YYYY-MM-DD VARCHAR, NULL when
    unrecognized. Accepts compact `YYYYMMDD` strings and anything whose VARCHAR
    cast starts with an ISO date — a DATE/TIMESTAMP-typed column or an ISO
    string — so the export's date typing doesn't matter. The ISO branch uses
    `regexp_matches` (a partial match) because `~` is a full match in DuckDB."""
    s = _text(col)
    return (
        "CASE\n"
        f"        WHEN {s} ~ '^[0-9]{{8}}$'"
        f" THEN substr({s}, 1, 4) || '-' || substr({s}, 5, 2) || '-' || substr({s}, 7, 2)\n"
        f"        WHEN regexp_matches({s}, '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}') THEN substr({s}, 1, 10)\n"
        "        ELSE NULL\n"
        "    END"
    )


def general_election_date_sql(year: int) -> str:
    """SQL for the U.S. general election date of `year` as a YYYY-MM-DD VARCHAR:
    the first Tuesday after the first Monday in November. `dayofweek` is
    0 = Sunday, so `(8 - dayofweek) % 7` days after November 1 is the first
    Monday (November 1 itself when it falls on a Monday) and one more day is
    the Tuesday."""
    nov1 = f"make_date({year}, 11, 1)"
    return f"strftime({nov1} + CAST(((8 - dayofweek({nov1})) % 7) + 1 AS INTEGER), '%Y-%m-%d')"


def vote_columns(columns: list[str]) -> list[tuple[str, str, int]]:
    """The `(column, type, year)` triple for every vote column in `columns`, in
    election order (year, then primary before general) so the assembled
    history reads chronologically."""
    found: list[tuple[str, str, int]] = []
    for col in columns:
        m = _VOTE_COLUMN_RE.match(col)
        if m:
            found.append((col, _VOTE_COLUMN_TYPES[m.group(1)], int(m.group(2))))
    found.sort(key=lambda t: (t[2], t[1] == "general"))
    return found


def _voting_history_sql(columns: list[str]) -> str:
    """SQL building the `voting_history` STRUCT[] from the vote columns present
    in `columns`. Each non-empty code becomes one entry; generals carry the
    election date, primaries none (the source records only the year)."""
    entries: list[str] = []
    for col, election_type, year in vote_columns(columns):
        code = f"upper({_text(col)})"
        date = general_election_date_sql(year) if election_type == "general" else "CAST(NULL AS VARCHAR)"
        method = _case_from_map(code, TS_VOTE_METHOD_LABELS, default="other", null_passthrough=True)
        entries.append(
            f"CASE WHEN {code} IS NOT NULL THEN "
            f"{{'year': {year}, 'type': '{election_type}', 'date': {date}, 'method': {method}}} END"
        )
    if not entries:
        return f"CAST([] AS {VOTING_HISTORY_TYPE})"
    joined = ",\n        ".join(entries)
    return f"CAST(list_filter([\n        {joined}\n    ], e -> e IS NOT NULL) AS {VOTING_HISTORY_TYPE})"


def targetsmart_transformation_query(source_table: str, columns: list[str]) -> str:
    """SQL transformation from a TargetSmart Parquet extract → Person schema.

    Args:
        source_table: fully-qualified `persons_raw` table the query reads from,
            aliased to ``raw`` so the column expressions below resolve.
        columns: the raw table's column names. The vote columns among them
            drive the `voting_history` assembly, so an extract carrying more or
            fewer election years than the reference schema still loads.
    """
    street_name = _text("vb_vf_reg_cass_street_name")
    deceased_values = ", ".join(f"'{v}'" for v in TS_DECEASED_VALUES)
    enrollment = _case_from_map(_text("vb_vf_party"), TS_PARTY_LABELS, default="other", null_passthrough=True)
    registration_status = _case_from_map(
        _text("vb_vf_voter_status"), TS_REGISTRATION_STATUS_LABELS, default="unknown", null_passthrough=False
    )

    return f"""
SELECT
    {_text("vb_voterbase_id")} AS external_id,
    'ts_voterbase' AS external_id_type,
    {_text("vb_tsmart_first_name")} AS first_name,
    {_text("vb_tsmart_last_name")} AS last_name,
    {_text("vb_tsmart_middle_name")} AS middle_name,
    {_text("vb_tsmart_name_suffix")} AS name_suffix,
    -- The CASS-standardized street parts when the standardizer produced a
    -- street name; the registration address as entered otherwise. Every part
    -- is empty-folded to NULL so concat_ws leaves no double or trailing spaces.
    CASE
        WHEN {street_name} IS NOT NULL THEN concat_ws(
            ' ',
            {_text("vb_vf_reg_cass_street_num")},
            {_text("vb_vf_reg_cass_pre_directional")},
            {street_name},
            {_text("vb_vf_reg_cass_street_suffix")},
            {_text("vb_vf_reg_cass_post_directional")}
        )
        ELSE {_text("vb_vf_reg_address_1")}
    END AS address_line_1,
    coalesce(
        nullif(concat_ws(' ', {_text("vb_vf_reg_cass_unit_designator")}, {_text("vb_vf_reg_cass_apt_num")}), ''),
        {_text("vb_vf_reg_address_2")}
    ) AS address_line_2,
    -- TargetSmart's CASS parts carry no fractional house number.
    CAST(NULL AS VARCHAR) AS half_code,
    coalesce({_text("vb_vf_reg_cass_city")}, {_text("vb_vf_reg_city")}) AS city,
    {_text("vb_vf_reg_state")} AS state,
    -- The ZIP may arrive as ZIP+4 without a separator; the leading five digits
    -- are the ZIP5 either way.
    substr(coalesce({_text("vb_vf_reg_cass_zip")}, {_text("vb_vf_reg_zip")}), 1, 5) AS zip5,
    {_text("vb_vf_reg_zip4")} AS zip4,
    -- Canonical voter-file scalars. Top-level columns so filters on them hit
    -- Parquet column pruning + Bloom filters.
    {enrollment} AS enrollment,
    {_text("vb_voterbase_gender")} AS gender,
    {iso_date_sql("vb_voterbase_dob")} AS date_of_birth,
    {iso_date_sql("vb_vf_registration_date")} AS registration_date,
    {registration_status} AS registration_status,
    {_text("vb_vf_county_code")} AS county_code,
    {county_fips_sql(columns)} AS county_fips,
    -- Named `precinct_id`, not `precinct`: the web's text-multi editor rewrites
    -- values of a field keyed `precinct` into the NYC `AA-EEE` form.
    {_text("vb_vf_precinct_id")} AS precinct_id,
    {_text("vb_vf_hd")} AS assembly_district,
    {_text("vb_vf_sd")} AS senate_district,
    {_text("vb_vf_cd")} AS congressional_district,
    {_voting_history_sql(columns)} AS voting_history,
    raw.legal_commercial_model_usage_ok AS commercial_model_ok
FROM {source_table} AS raw
WHERE coalesce({_text("vb_voterbase_registration_status")}, '') <> 'Unregistered'
  AND NOT coalesce(upper({_text("vb_voterbase_deceased_flag")}) IN ({deceased_values}), false)
"""
