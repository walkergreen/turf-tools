"""Resolve a dataset version's TIGER scope against DuckDB.

`resolve_tiger_scope` reads the `state` / `county_fips` columns of a persons
table and hands them to the pure `src/geo/scope.resolve_scope`, supplying
the TIGER national county file as the statewide expander (and as the check
that county codes — derived or configured — are real counties of their
state). Everything it has to say about how the scope came out (a state
widened, a stray state skipped, persons outside a pinned scope) goes through
the `warn` / `log` callables so a caller can route it to the job log.

`county_match_rate_warnings` is the post-geocode sanity check: a county whose
voters almost never matched a TIGER blockface usually means the importer's
county coding was not FIPS and the wrong county's blockfaces were loaded.
"""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from src.geo import tiger_files
from src.geo.scope import resolve_scope, scope_states
from src.geo.states import STATE_FIPS_BY_POSTAL, state_postal

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    import duckdb
    from src.geo.scope import CountyScope, ScopeSource
    from src.models import TableRef

COUNTY_TABLE_FQN = "ducklake_geo.tiger.county"

# `position_source` values that mean the voter matched a TIGER blockface
# (`osm_only` is the TIGER-miss rescue and does not count).
TIGER_MATCHED_SOURCES = ("osm_matched", "osm_complex", "osm_off_segment", "tiger_only")
# A county with at least this many persons and a TIGER match rate below this
# fraction is reported by `county_match_rate_warnings`.
LOW_MATCH_MIN_ROWS = 100
LOW_MATCH_RATE = 0.05


def ensure_county_lookup(conn: duckdb.DuckDBPyConnection, tiger_year: str, tiger_data_dir: str) -> None:
    """Populate `COUNTY_TABLE_FQN` for `tiger_year` from the TIGER national
    county file (one ~80 MB download, cached under ``{tiger_data_dir}/county/``).
    Rows are keyed by vintage so a `TIGER_YEAR` bump re-downloads rather than
    reusing stale counties."""
    conn.execute("CREATE SCHEMA IF NOT EXISTS ducklake_geo.tiger")
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {COUNTY_TABLE_FQN} (
            tiger_year   VARCHAR,
            state_fips   VARCHAR,
            county_fips  VARCHAR,
            name         VARCHAR
        )
    """)
    existing = conn.execute(f"SELECT count(*) FROM {COUNTY_TABLE_FQN} WHERE tiger_year = ?", [tiger_year]).fetchone()[0]
    if existing > 0:
        return

    url = tiger_files.tiger_zip_url("COUNTY", tiger_year, tiger_files.NATIONAL)
    data_dir = Path(tiger_data_dir) / "county"
    zip_path = data_dir / url.rsplit("/", 1)[-1]
    extract_dir = data_dir / tiger_files.NATIONAL
    print(f"Downloading TIGER national county file: {url}")
    tiger_files.download_and_extract(url, zip_path, extract_dir)
    for shp in tiger_files.shp_files(extract_dir):
        conn.execute(f"""
            INSERT INTO {COUNTY_TABLE_FQN}
            SELECT '{tiger_year}' AS tiger_year, STATEFP AS state_fips, COUNTYFP AS county_fips, NAME AS name
            FROM ST_Read('{shp}')
        """)
    n = conn.execute(f"SELECT count(*) FROM {COUNTY_TABLE_FQN} WHERE tiger_year = ?", [tiger_year]).fetchone()[0]
    print(f"  {n:,} counties loaded")


def national_counties(
    conn: duckdb.DuckDBPyConnection,
    tiger_year: str,
    tiger_data_dir: str,
    states: Sequence[str],
) -> dict[str, list[str]]:
    """``{state_fips: [county_fips, …]}`` for `states`, from the county lookup."""
    if not states:
        return {}
    ensure_county_lookup(conn, tiger_year, tiger_data_dir)
    placeholders = ", ".join("?" for _ in states)
    rows = conn.execute(
        f"SELECT state_fips, county_fips FROM {COUNTY_TABLE_FQN} "
        f"WHERE tiger_year = ? AND state_fips IN ({placeholders}) ORDER BY 1, 2",
        [tiger_year, *states],
    ).fetchall()
    result: dict[str, list[str]] = {s: [] for s in states}
    for state, county in rows:
        result.setdefault(state, []).append(county)
    return result


def _table_columns(conn: duckdb.DuckDBPyConnection, fqn: str) -> set[str]:
    return {row[0] for row in conn.execute(f"DESCRIBE {fqn}").fetchall()}


def _state_counts(conn: duckdb.DuckDBPyConnection, fqn: str) -> dict[str, int]:
    """Row count per distinct non-blank `state` value (trimmed, upper-cased)."""
    rows = conn.execute(
        f"SELECT upper(trim(state)), count(*) FROM {fqn} "
        "WHERE state IS NOT NULL AND trim(state) <> '' GROUP BY 1 ORDER BY 1"
    ).fetchall()
    return {value: n for value, n in rows}


def _split_known_states(counts: Mapping[str, int]) -> tuple[dict[str, int], dict[str, int]]:
    """Partition `_state_counts` output into US postal codes (re-keyed by
    state FIPS) and everything else (keyed by the raw value)."""
    known = {STATE_FIPS_BY_POSTAL[v]: n for v, n in counts.items() if v in STATE_FIPS_BY_POSTAL}
    unknown = {v: n for v, n in counts.items() if v not in STATE_FIPS_BY_POSTAL}
    return known, unknown


def _describe_counts(counts: Mapping[str, int], *, postal: bool) -> str:
    """``NY 2,000,000 rows; PA 1 row`` — `postal` keys are FIPS codes to
    render as postal abbreviations, otherwise raw values are quoted."""
    parts = []
    for key, n in sorted(counts.items()):
        label = f"{state_postal(key)} ({key})" if postal else repr(key)
        parts.append(f"{label} {n:,} row{'s' if n != 1 else ''}")
    return "; ".join(parts)


def _states_present(
    conn: duckdb.DuckDBPyConnection,
    fqn: str,
    *,
    warn: Callable[[str], None],
    log: Callable[[str], None],
) -> list[str]:
    """Distinct state FIPS codes in `fqn`'s `state` column (postal → FIPS).

    Every distinct value counts — one stray row provisions its whole state —
    so the per-state row counts go through `log` before anything is
    downloaded, where a stray state is visible and `TIGER_SCOPE` can pin it
    away. Values that are not US postal codes are reported through `warn`
    and skipped (those rows will not geocode); only when no known state
    remains is that an error.
    """
    known, unknown = _split_known_states(_state_counts(conn, fqn))
    if unknown:
        listed = _describe_counts(dict(list(unknown.items())[:10]), postal=False)
        if not known:
            raise ValueError(f"{fqn}.state holds values that are not US postal codes: {listed}")
        warn(
            f"WARNING: {fqn}.state holds values that are not US postal codes; skipped from the scope "
            f"(these rows will not geocode): {listed}"
        )
    if known:
        log(f"Deriving scope from the state column: {_describe_counts(known, postal=True)}")
    return sorted(known)


def _counties_present(conn: duckdb.DuckDBPyConnection, fqn: str, *, warn: Callable[[str], None]) -> dict[str, set[str]]:
    """Per state, the distinct 3-digit `county_fips` values — only for states
    where every row carries a valid code. A state with any NULL, blank, or
    malformed value is omitted (reported through `warn`) so it expands to the
    whole state."""
    rows = conn.execute(f"""
        SELECT
            upper(trim(state)) AS st,
            count(*) FILTER (
                WHERE county_fips IS NULL OR NOT regexp_matches(trim(county_fips), '^[0-9]{{3}}$')
            ) AS bad,
            list(DISTINCT trim(county_fips)) FILTER (WHERE regexp_matches(trim(county_fips), '^[0-9]{{3}}$')) AS codes
        FROM {fqn}
        WHERE state IS NOT NULL AND trim(state) <> ''
        GROUP BY 1
    """).fetchall()
    result: dict[str, set[str]] = {}
    for postal, bad, codes in rows:
        if postal not in STATE_FIPS_BY_POSTAL:
            continue
        if bad:
            warn(f"WARNING: {bad:,} rows in state {postal} lack a county_fips; the whole state is in scope")
            continue
        result[STATE_FIPS_BY_POSTAL[postal]] = set(codes or [])
    return result


def _warn_outside_pinned_scope(
    conn: duckdb.DuckDBPyConnection,
    fqn: str,
    scope: Sequence[CountyScope],
    warn: Callable[[str], None],
) -> None:
    """Report persons whose `state` is not covered by a pinned `scope`. A
    table without a `state` column is left alone: the pin is the documented
    workaround for that case."""
    if "state" not in _table_columns(conn, fqn):
        return
    known, unknown = _split_known_states(_state_counts(conn, fqn))
    if unknown:
        warn(
            f"WARNING: {fqn}.state holds values that are not US postal codes; these rows will not geocode: "
            f"{_describe_counts(dict(list(unknown.items())[:10]), postal=False)}"
        )
    in_scope = scope_states(scope)
    missing = {fips: n for fips, n in known.items() if fips not in in_scope}
    if missing:
        warn(
            f"WARNING: persons are in state(s) {_describe_counts(missing, postal=True)} but the pinned scope "
            f"covers {', '.join(in_scope)} only; those rows will not geocode — unset "
            "TIGER_STATE_FIPS/TIGER_COUNTY_FIPS to derive the scope from the data, or extend TIGER_SCOPE"
        )


def resolve_tiger_scope(
    conn: duckdb.DuckDBPyConnection,
    persons: TableRef,
    *,
    spec: str | None,
    tiger_year: str,
    tiger_data_dir: str,
    expand_states: Callable[[Sequence[str]], Mapping[str, Sequence[str]]] | None = None,
    warn: Callable[[str], None] = print,
    log: Callable[[str], None] = print,
) -> list[CountyScope]:
    """The (state, county) pairs a dataset version needs.

    `spec` (a `TIGER_SCOPE` string) pins the scope. The pin is checked
    against the data it can see: when `persons` has a `state` column, states
    present in it but absent from the pin are reported through `warn` — those
    rows will not geocode — so a deployment-wide pin never swallows a dataset
    from another state in silence. A table without a `state` column resolves
    a pin without looking (the pin is the documented workaround there).

    When `spec` is `None` the scope is read from `persons` — the `state`
    column always, `county_fips` when present — and statewide entries expand
    through the TIGER national county file (`expand_states` defaults to
    `national_counties`; tests inject a stub). Every scope-widening reason
    (rows lacking `county_fips`, no `county_fips` column at all, a county code
    that is not a county of its state, a state value skipped) goes through
    `warn`; the per-state row counts the derivation saw go through `log`.
    """
    if expand_states is None:
        expand_states = partial(national_counties, conn, tiger_year, tiger_data_dir)
    fqn = persons.fqn
    if spec and spec.strip():
        scope = resolve_scope(
            spec=spec, states_present=[], counties_present=None, expand_states=expand_states, warn=warn
        )
        _warn_outside_pinned_scope(conn, fqn, scope, warn)
        return scope
    columns = _table_columns(conn, fqn)
    if "state" not in columns:
        raise ValueError(f"{fqn} has no `state` column; set TIGER_SCOPE to pin the geographic scope")
    states = _states_present(conn, fqn, warn=warn, log=log)
    counties: dict[str, set[str]] | None = None
    if "county_fips" in columns:
        counties = _counties_present(conn, fqn, warn=warn)
    elif states:
        warn(
            f"WARNING: {fqn} has no county_fips column; every county of state(s) "
            f"{', '.join(state_postal(s) for s in states)} is in scope — set TIGER_SCOPE (or --tiger-scope) to pin it"
        )
    return resolve_scope(
        spec=None, states_present=states, counties_present=counties, expand_states=expand_states, warn=warn
    )


def scope_source(spec: str | None, configured_source: ScopeSource) -> ScopeSource:
    """Where a resolved scope came from: the settings source when a spec was
    configured, otherwise `derived`."""
    return configured_source if spec else "derived"


def county_match_rate_warnings(
    conn: duckdb.DuckDBPyConnection,
    geocoded_fqn: str,
    *,
    min_rows: int = LOW_MATCH_MIN_ROWS,
    max_rate: float = LOW_MATCH_RATE,
) -> list[str]:
    """One warning per county in `geocoded_fqn` (a `persons_geocoded` table
    carrying `state`, `county_fips`, and `position_source`) whose TIGER match
    rate is below `max_rate` across at least `min_rows` persons.

    A county whose voters almost never match any blockface loaded for it is
    the signature of a county code that is not a Census FIPS code — the
    scope derivation loaded the wrong county — rather than of bad addresses,
    which fail in the single digits of percent. Empty when the table lacks
    the columns or every county matches normally.
    """
    if not {"state", "county_fips", "position_source"} <= _table_columns(conn, geocoded_fqn):
        return []
    matched = ", ".join(f"'{s}'" for s in TIGER_MATCHED_SOURCES)
    rows = conn.execute(f"""
        SELECT upper(trim(state)), county_fips, count(*), count(*) FILTER (WHERE position_source IN ({matched}))
        FROM {geocoded_fqn}
        WHERE county_fips IS NOT NULL AND state IS NOT NULL
        GROUP BY 1, 2
        ORDER BY 1, 2
    """).fetchall()
    return [
        f"WARNING: county {postal}:{county} — {hits:,} of {n:,} persons ({hits / n:.1%}) matched a TIGER blockface; "
        "check that the importer's county_fips is a Census county FIPS code (the wrong county's blockfaces may "
        "have been loaded), or pin TIGER_SCOPE"
        for postal, county, n, hits in rows
        if n >= min_rows and hits < max_rate * n
    ]
