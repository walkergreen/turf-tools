"""The geographic scope of a dataset version.

A scope is the set of `(state_fips, county_fips)` pairs whose TIGER
reference data (address ranges, edges, census blocks) the pipeline must
have loaded, and whose states' OSM extracts it must ingest. It is resolved
once per import, in this order:

1. `TIGER_SCOPE` (`Settings.tiger_scope`) — an explicit spec string such as
   ``"36:005,047,061,081,085;34:017"`` or ``"36:*"`` (statewide).
2. The legacy pair `TIGER_STATE_FIPS` / `TIGER_COUNTY_FIPS`, folded into
   the same spec form.
3. Derived from the data: the distinct `state` values in `persons_validated`
   (postal → FIPS), narrowed to the distinct `county_fips` values when the
   importer supplied that column for every row of a state, otherwise every
   county of the state from the TIGER national county file.

Everything here is pure Python so it is unit-testable without a network or
a database; `src/geo/tiger_scope.py` supplies the DuckDB reads
(`resolve_tiger_scope`) and the TIGER national county lookup
(`national_counties`) that `resolve_scope` takes as callables.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from src.geo.states import state_fips, state_postal

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from src.settings import Settings

# Spec token meaning "every county in the state".
ALL_COUNTIES = "*"

ScopeSource = Literal["settings", "legacy", "derived"]

_COUNTY_RE = re.compile(r"^[0-9]{1,3}$")
_STATE_FIPS_RE = re.compile(r"^[0-9]{1,2}$")


@dataclass(frozen=True, order=True)
class CountyScope:
    """One county in scope: 2-digit state FIPS + 3-digit county FIPS."""

    state_fips: str
    county_fips: str


def _normalize_state(token: str) -> str:
    """A spec state token (FIPS digits or postal abbreviation) → 2-digit FIPS.
    A FIPS code with no state behind it (``03``, ``99``) raises
    `UnknownStateError`, so a typo fails at settings validation rather than
    at the first Geofabrik or Census lookup."""
    t = token.strip().upper()
    if _STATE_FIPS_RE.match(t):
        fips = t.zfill(2)
        state_postal(fips)
        return fips
    return state_fips(t)


def parse_scope_spec(spec: str) -> dict[str, list[str] | None]:
    """Parse a `TIGER_SCOPE` string into ``{state_fips: [county_fips, …] | None}``.

    Entries are separated by ``;`` or newlines; each is ``STATE`` or
    ``STATE:COUNTIES`` where STATE is a FIPS code or postal abbreviation and
    COUNTIES is a comma-separated list of 1–3 digit codes, or ``*``. A bare
    state or ``*`` means statewide (`None`). Codes are zero-padded, whitespace
    is ignored, and repeated states merge. Raises `ValueError` naming the bad
    fragment.
    """
    result: dict[str, list[str] | None] = {}
    entries = [e.strip() for e in re.split(r"[;\n]", spec) if e.strip()]
    if not entries:
        raise ValueError("TIGER_SCOPE is empty")
    for entry in entries:
        state_part, sep, county_part = entry.partition(":")
        try:
            state = _normalize_state(state_part)
        except ValueError:
            raise ValueError(f"TIGER_SCOPE: unknown state in {entry!r}") from None
        if not sep or county_part.strip() == ALL_COUNTIES:
            counties: list[str] | None = None
        else:
            raw = [c.strip() for c in county_part.split(",")]
            if not raw or any(not c for c in raw):
                raise ValueError(f"TIGER_SCOPE: empty county code in {entry!r}")
            bad = [c for c in raw if not _COUNTY_RE.match(c)]
            if bad:
                raise ValueError(f"TIGER_SCOPE: county codes must be 1-3 digits, got {bad!r} in {entry!r}")
            counties = sorted({c.zfill(3) for c in raw})
        if state in result:
            existing = result[state]
            if existing is None or counties is None:
                result[state] = None
            else:
                result[state] = sorted(set(existing) | set(counties))
        else:
            result[state] = counties
    return result


def legacy_scope_spec(tiger_state_fips: str | None, tiger_county_fips: Sequence[str] | None) -> str | None:
    """Fold the `TIGER_STATE_FIPS` / `TIGER_COUNTY_FIPS` pair into a spec string.

    `None` when neither is set. The two are required together: a state
    without counties raises `ValueError` (a statewide scope is the explicit
    ``TIGER_SCOPE=36:*``, never an accident of a missing county list), and
    counties without a state cannot be placed.
    """
    if tiger_state_fips is None or tiger_state_fips.strip() == "":
        if tiger_county_fips:
            raise ValueError("TIGER_COUNTY_FIPS requires TIGER_STATE_FIPS")
        return None
    if not tiger_county_fips:
        raise ValueError(
            "TIGER_STATE_FIPS requires TIGER_COUNTY_FIPS; for the whole state set TIGER_SCOPE=<fips>:* instead, "
            "or remove TIGER_STATE_FIPS to derive the scope from the imported data"
        )
    return f"{tiger_state_fips.strip()}:{','.join(tiger_county_fips)}"


def scope_spec_from_settings(settings: Settings) -> tuple[str | None, ScopeSource]:
    """The configured scope spec and where it came from: `TIGER_SCOPE`, else the
    legacy state/county pair, else `None` (derive from the data)."""
    if settings.tiger_scope:
        return settings.tiger_scope, "settings"
    legacy = legacy_scope_spec(settings.tiger_state_fips, settings.tiger_county_fips)
    if legacy is not None:
        return legacy, "legacy"
    return None, "derived"


def resolve_scope(
    *,
    spec: str | None,
    states_present: Sequence[str],
    counties_present: Mapping[str, set[str]] | None,
    expand_states: Callable[[Sequence[str]], Mapping[str, Sequence[str]]],
    warn: Callable[[str], None] = print,
) -> list[CountyScope]:
    """Resolve the county list a dataset version needs.

    `spec` (non-empty) wins outright. Its statewide entries go through
    `expand_states`, and its explicit county codes are checked against the
    same table: a code that is not a county of its state raises `ValueError`
    naming it, so a typo fails here rather than as a Census 404 mid-DAG.
    Otherwise the scope is derived: every state in `states_present` (2-digit
    FIPS), narrowed to `counties_present[state]` when that state has a
    complete, valid county set, else expanded to every county of the state.
    Derived county codes are checked too — a code that is not a county of
    its state means the importer's coding is not FIPS, and the whole state
    is used instead, reported through `warn`, rather than a wrong county.

    The result is sorted and deduplicated. Raises `ValueError` when nothing
    is in scope.
    """
    pinned = bool(spec and spec.strip())
    wanted: dict[str, list[str] | None]
    if pinned:
        wanted = parse_scope_spec(spec or "")
    else:
        wanted = {}
        for state in states_present:
            counties = counties_present.get(state) if counties_present else None
            wanted[state] = sorted(counties) if counties else None

    statewide = sorted(s for s, c in wanted.items() if c is None)
    to_check = sorted(s for s, c in wanted.items() if c is not None)
    known = expand_states(statewide + to_check) if statewide or to_check else {}

    pairs: set[CountyScope] = set()
    for state, counties in wanted.items():
        if counties is not None:
            valid = set(known.get(state, ()))
            unknown = sorted(set(counties) - valid)
            if unknown and pinned:
                raise ValueError(f"TIGER_SCOPE: county codes {unknown} are not counties of state {state}")
            if unknown:
                warn(
                    f"WARNING: county codes {unknown} are not counties of state {state}; "
                    f"using every county of the state instead"
                )
                counties = None
        if counties is None:
            counties = list(known.get(state, ()))
            if not counties:
                raise ValueError(f"no counties known for state FIPS {state!r}")
        pairs.update(CountyScope(state, c) for c in counties)

    if not pairs:
        raise ValueError("resolved geographic scope is empty")
    return sorted(pairs)


def scope_states(scope: Iterable[CountyScope]) -> list[str]:
    """Distinct state FIPS codes in `scope`, sorted."""
    return sorted({c.state_fips for c in scope})


def group_by_state(scope: Iterable[CountyScope]) -> dict[str, list[str]]:
    """``{state_fips: [county_fips, …]}`` with both levels sorted."""
    grouped: dict[str, set[str]] = {}
    for c in scope:
        grouped.setdefault(c.state_fips, set()).add(c.county_fips)
    return {s: sorted(cs) for s, cs in sorted(grouped.items())}


def scope_sql(
    scope: Sequence[CountyScope],
    state_col: str = "state_fips",
    county_col: str = "county_fips",
) -> str:
    """A SQL predicate selecting rows in `scope`, one ``(state = … AND county
    IN (…))`` disjunct per state. ``FALSE`` for an empty scope so a caller
    that interpolates it never selects everything by accident."""
    grouped = group_by_state(scope)
    if not grouped:
        return "FALSE"
    parts = []
    for state, counties in grouped.items():
        in_list = ", ".join(f"'{c}'" for c in counties)
        parts.append(f"({state_col} = '{state}' AND {county_col} IN ({in_list}))")
    return "(" + " OR ".join(parts) + ")"


def format_scope(scope: Iterable[CountyScope]) -> str:
    """Round-trip a scope to spec form (``36:005,047;34:017``) for logs."""
    return ";".join(f"{s}:{','.join(cs)}" for s, cs in group_by_state(scope).items())


def scope_metadata(
    scope: Sequence[CountyScope],
    *,
    source: ScopeSource,
    tiger_year: str,
    osm_extracts: Sequence[str],
    utm_epsg: int | None,
    notes: Sequence[str] = (),
) -> dict[str, Any]:
    """The `geoScope` block of a version's derived metadata: what reference
    data this version was built against. `notes` are the warnings raised
    while resolving and checking the scope (a state widened to every county,
    persons outside a pinned scope, a county that barely matched TIGER), so
    the record explains itself without the job log."""
    return {
        "source": source,
        "tigerYear": tiger_year,
        "states": [{"fips": s, "postal": state_postal(s), "counties": cs} for s, cs in group_by_state(scope).items()],
        "osmExtracts": list(osm_extracts),
        "utmEpsg": utm_epsg,
        "notes": list(notes),
    }
