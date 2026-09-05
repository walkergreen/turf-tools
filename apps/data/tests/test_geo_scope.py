"""Geographic scope resolution (`src/geo/scope.py`, `src/geo/tiger_scope.py`).

Behaviors locked in:

- `TIGER_SCOPE` parsing: state:county lists, `*`/bare-state statewide,
  postal states, padding and whitespace tolerance, malformed fragments and
  unassigned state FIPS codes raise.
- The legacy `TIGER_STATE_FIPS` + `TIGER_COUNTY_FIPS` pair folds into the
  same spec form (both halves required, at `Settings` construction too), and
  the five-borough pin resolves to exactly the five NYC pairs (the NYC
  no-change guard).
- Settings precedence: TIGER_SCOPE > legacy pair > derive.
- `resolve_scope`: a spec wins over the data, and its explicit county codes
  are checked against the county table (a typo raises); statewide entries
  expand; derived scopes narrow to `county_fips` only when a state's codes
  are complete and real, else widen with a warning through `warn`; an empty
  result raises.
- `resolve_tiger_scope` reads `state` / `county_fips` from a DuckDB table
  with an injected expander (no network): per-state row counts go through
  `log`, unknown state values are skipped with a warning, a missing
  `county_fips` column warns, and a pinned scope warns about persons in
  states it does not cover.
- `scope_sql` / `format_scope` / `scope_metadata` string forms.
"""

from types import SimpleNamespace

import pytest

import duckdb
from src.geo.scope import (
    CountyScope,
    format_scope,
    group_by_state,
    legacy_scope_spec,
    parse_scope_spec,
    resolve_scope,
    scope_metadata,
    scope_spec_from_settings,
    scope_sql,
)
from src.geo.tiger_scope import resolve_tiger_scope
from src.models import TableRef

NYC = ["005", "047", "061", "081", "085"]
NYC_SCOPE = [CountyScope("36", c) for c in NYC]

# A stand-in for the TIGER national county file.
COUNTIES = {"36": ["005", "047", "061", "081", "085", "119"], "34": ["003", "013", "017"], "09": ["001", "009"]}


def _expander(calls=None):
    def expand(states):
        if calls is not None:
            calls.append(list(states))
        return {s: COUNTIES.get(s, []) for s in states}

    return expand


# ---------------------------------------------------------------------------
# parse_scope_spec
# ---------------------------------------------------------------------------


class TestParseScopeSpec:
    def test_state_county_pairs(self):
        assert parse_scope_spec("36:005,047,061,081,085;34:017") == {"36": NYC, "34": ["017"]}

    @pytest.mark.parametrize("spec", ["36:*", "36", " 36 : * "])
    def test_wildcard_is_statewide(self, spec):
        assert parse_scope_spec(spec) == {"36": None}

    def test_postal_state_and_padding(self):
        assert parse_scope_spec("NY:61") == {"36": ["061"]}
        assert parse_scope_spec("36: 5, 47") == {"36": ["005", "047"]}
        assert parse_scope_spec("6:*") == {"06": None}

    def test_newlines_and_repeated_states_merge(self):
        assert parse_scope_spec("36:061\n36:005;36:061") == {"36": ["005", "061"]}
        assert parse_scope_spec("36:061;36:*") == {"36": None}

    @pytest.mark.parametrize(
        "spec", ["36:", ":005", "36:abc", "", "  ;  ", "ZZ:001", "36:0001", "99:001", "03:*", "0:001"]
    )
    def test_malformed_spec_raises(self, spec):
        with pytest.raises(ValueError):
            parse_scope_spec(spec)

    def test_error_names_the_bad_fragment(self):
        with pytest.raises(ValueError, match="36:abc"):
            parse_scope_spec("34:017;36:abc")
        with pytest.raises(ValueError, match="unknown state in '99:001'"):
            parse_scope_spec("99:001")


# ---------------------------------------------------------------------------
# Legacy settings + precedence
# ---------------------------------------------------------------------------


class TestSettingsSpec:
    def test_legacy_pair_folds_into_spec_form(self):
        assert legacy_scope_spec("36", ["061", "005", "047", "081", "085"]) == "36:061,005,047,081,085"
        assert legacy_scope_spec(None, None) is None
        assert legacy_scope_spec("", None) is None

    def test_counties_without_a_state_raise(self):
        with pytest.raises(ValueError, match="TIGER_STATE_FIPS"):
            legacy_scope_spec(None, ["061"])

    @pytest.mark.parametrize("counties", [None, []])
    def test_state_without_counties_raises_rather_than_going_statewide(self, counties):
        with pytest.raises(ValueError, match=r"TIGER_SCOPE=<fips>:\*.*or remove TIGER_STATE_FIPS"):
            legacy_scope_spec("36", counties)

    def test_legacy_defaults_resolve_to_the_five_nyc_pairs(self):
        settings = SimpleNamespace(tiger_scope=None, tiger_state_fips="36", tiger_county_fips=NYC)
        spec, source = scope_spec_from_settings(settings)
        assert source == "legacy"
        scope = resolve_scope(spec=spec, states_present=[], counties_present=None, expand_states=_expander())
        assert scope == NYC_SCOPE

    def test_tiger_scope_overrides_legacy_fields(self):
        settings = SimpleNamespace(tiger_scope="34:017", tiger_state_fips="36", tiger_county_fips=NYC)
        assert scope_spec_from_settings(settings) == ("34:017", "settings")

    def test_nothing_set_means_derive(self):
        settings = SimpleNamespace(tiger_scope=None, tiger_state_fips=None, tiger_county_fips=None)
        assert scope_spec_from_settings(settings) == (None, "derived")

    def test_settings_object_validates_tiger_scope(self):
        from src.settings import Settings

        with pytest.raises(ValueError):
            Settings(_env_file=None, tiger_scope="36:abc")
        with pytest.raises(ValueError, match="unknown state"):
            Settings(_env_file=None, tiger_scope="99:001")
        assert Settings(_env_file=None, tiger_scope="").tiger_scope is None
        assert Settings(_env_file=None, tiger_scope="NY:61").tiger_scope == "NY:61"

    def test_settings_object_rejects_half_a_legacy_pin(self):
        """A stale TIGER_STATE_FIPS without its county list fails at startup
        instead of sending an import statewide."""
        from src.settings import Settings

        with pytest.raises(ValueError, match="TIGER_COUNTY_FIPS"):
            Settings(_env_file=None, tiger_state_fips="36")
        with pytest.raises(ValueError, match="TIGER_STATE_FIPS"):
            Settings(_env_file=None, tiger_county_fips=["061"])
        both = Settings(_env_file=None, tiger_state_fips="36", tiger_county_fips=NYC)
        assert scope_spec_from_settings(both) == ("36:005,047,061,081,085", "legacy")
        assert scope_spec_from_settings(Settings(_env_file=None)) == (None, "derived")

    def test_settings_object_ignores_a_stale_half_pin_under_tiger_scope(self):
        """`TIGER_SCOPE` outranks the legacy pair, so half of the pair left in
        the env beside it is never read — the validator lets it through and
        the resolved spec is the `TIGER_SCOPE` one."""
        from src.settings import Settings

        stale_state = Settings(_env_file=None, tiger_scope="36:*", tiger_state_fips="36")
        assert scope_spec_from_settings(stale_state) == ("36:*", "settings")
        stale_counties = Settings(_env_file=None, tiger_scope="36:*;34:017", tiger_county_fips=["061"])
        assert scope_spec_from_settings(stale_counties) == ("36:*;34:017", "settings")
        # A blank TIGER_SCOPE is unset, so the pair check applies again.
        with pytest.raises(ValueError, match="TIGER_COUNTY_FIPS"):
            Settings(_env_file=None, tiger_scope="", tiger_state_fips="36")

    def test_settings_object_splits_osm_urls(self):
        from src.settings import Settings

        assert Settings(_env_file=None, osm_urls="https://a/x.osm.pbf, https://b/y.osm.pbf").osm_urls == [
            "https://a/x.osm.pbf",
            "https://b/y.osm.pbf",
        ]
        assert Settings(_env_file=None, osm_urls=["u"]).osm_urls == ["u"]
        assert Settings(_env_file=None, osm_urls="").osm_urls is None


# ---------------------------------------------------------------------------
# resolve_scope (pure)
# ---------------------------------------------------------------------------


class TestResolveScope:
    def test_spec_wins_over_data(self):
        calls = []
        scope = resolve_scope(
            spec="36:061",
            states_present=["34"],
            counties_present={"34": {"017"}},
            expand_states=_expander(calls),
        )
        assert scope == [CountyScope("36", "061")]
        assert calls == [["36"]]  # explicit counties are checked against the county table

    def test_spec_county_that_is_not_a_real_county_raises(self):
        """A typo in TIGER_SCOPE fails here, naming the code, rather than as a
        Census 404 mid-DAG — and never widens the pin to the whole state."""
        with pytest.raises(ValueError, match=r"\['086'\].*state 36"):
            resolve_scope(spec="36:061,086", states_present=[], counties_present=None, expand_states=_expander())

    def test_statewide_spec_expands_through_the_county_table(self):
        calls = []
        scope = resolve_scope(spec="36:*", states_present=[], counties_present=None, expand_states=_expander(calls))
        assert scope == [CountyScope("36", c) for c in COUNTIES["36"]]
        assert calls == [["36"]]

    def test_derived_from_states_only(self):
        scope = resolve_scope(spec=None, states_present=["36", "34"], counties_present=None, expand_states=_expander())
        assert group_by_state(scope) == {"34": COUNTIES["34"], "36": COUNTIES["36"]}

    def test_derived_from_county_fips_when_present(self):
        scope = resolve_scope(
            spec=None,
            states_present=["36", "34"],
            counties_present={"36": {"061"}, "34": {"017"}},
            expand_states=_expander(),
        )
        assert scope == [CountyScope("34", "017"), CountyScope("36", "061")]

    def test_state_missing_from_counties_present_expands_while_the_other_narrows(self):
        scope = resolve_scope(
            spec=None,
            states_present=["36", "34"],
            counties_present={"36": {"061"}},
            expand_states=_expander(),
        )
        assert group_by_state(scope) == {"34": COUNTIES["34"], "36": ["061"]}

    def test_county_code_that_is_not_a_real_county_widens_its_state(self, capsys):
        scope = resolve_scope(
            spec=None,
            states_present=["36", "34"],
            counties_present={"36": {"061", "999"}, "34": {"017"}},
            expand_states=_expander(),
        )
        assert group_by_state(scope) == {"34": ["017"], "36": COUNTIES["36"]}
        assert "999" in capsys.readouterr().out

    def test_widening_is_reported_through_the_warn_callable(self, capsys):
        notes = []
        scope = resolve_scope(
            spec=None,
            states_present=["36"],
            counties_present={"36": {"061", "999"}},
            expand_states=_expander(),
            warn=notes.append,
        )
        assert group_by_state(scope) == {"36": COUNTIES["36"]}
        assert len(notes) == 1
        assert "999" in notes[0] and "every county" in notes[0]
        assert capsys.readouterr().out == ""

    def test_empty_scope_raises(self):
        with pytest.raises(ValueError):
            resolve_scope(spec=None, states_present=[], counties_present=None, expand_states=_expander())
        with pytest.raises(ValueError):
            resolve_scope(spec="72:*", states_present=[], counties_present=None, expand_states=_expander())

    def test_result_is_sorted_and_deduplicated(self):
        scope = resolve_scope(
            spec="36:081,005;34:017;36:005", states_present=[], counties_present=None, expand_states=_expander()
        )
        assert scope == [CountyScope("34", "017"), CountyScope("36", "005"), CountyScope("36", "081")]


# ---------------------------------------------------------------------------
# resolve_tiger_scope against DuckDB
# ---------------------------------------------------------------------------


def _persons(rows, with_county: bool) -> tuple[duckdb.DuckDBPyConnection, TableRef]:
    conn = duckdb.connect()
    cols = "state VARCHAR, county_fips VARCHAR" if with_county else "state VARCHAR"
    conn.execute(f"CREATE TABLE persons ({cols})")
    placeholders = "?, ?" if with_county else "?"
    conn.executemany(f"INSERT INTO persons VALUES ({placeholders})", rows)
    return conn, TableRef(catalog="memory", schema="main", table="persons", version=0)


def _resolve(conn, ref, spec=None, expand_states=None, **callbacks):
    return resolve_tiger_scope(
        conn,
        ref,
        spec=spec,
        tiger_year="2024",
        tiger_data_dir="unused",
        expand_states=expand_states or _expander(),
        **callbacks,
    )


class TestResolveTigerScope:
    def test_scope_derived_from_persons_state_column(self):
        conn, ref = _persons([("NY",), ("ny ",), ("NJ",)], with_county=False)
        calls = []
        scope = _resolve(conn, ref, expand_states=_expander(calls))
        assert group_by_state(scope) == {"34": COUNTIES["34"], "36": COUNTIES["36"]}
        assert calls == [["34", "36"]]

    def test_scope_derived_from_county_fips_when_present(self):
        conn, ref = _persons([("NY", "061"), ("NY", "061"), ("NJ", "017")], with_county=True)
        scope = _resolve(conn, ref)
        assert scope == [CountyScope("34", "017"), CountyScope("36", "061")]

    def test_null_county_fips_falls_back_to_whole_state(self, capsys):
        conn, ref = _persons([("NY", "061"), ("NY", None), ("NJ", "017")], with_county=True)
        scope = _resolve(conn, ref)
        assert group_by_state(scope) == {"34": ["017"], "36": COUNTIES["36"]}
        assert "lack a county_fips" in capsys.readouterr().out

    def test_only_unknown_state_values_raises(self):
        conn, ref = _persons([("ZZ",), ("ON",)], with_county=False)
        with pytest.raises(ValueError, match="ZZ"):
            _resolve(conn, ref)

    def test_unknown_state_values_are_skipped_with_a_warning(self):
        """One stray non-postal value among real states does not fail the
        import after the load; those rows are reported and left out."""
        conn, ref = _persons([("NY",), ("NY",), ("ZZ",)], with_county=False)
        warnings, logs = [], []
        scope = _resolve(conn, ref, warn=warnings.append, log=logs.append)
        assert group_by_state(scope) == {"36": COUNTIES["36"]}
        (skipped,) = [w for w in warnings if "skipped" in w]
        assert "'ZZ' 1 row" in skipped and "not US postal codes" in skipped
        assert any("NY (36) 2 rows" in line for line in logs)

    def test_per_state_row_counts_are_logged_before_any_download(self):
        """A single stray row provisions its whole state, so the counts are
        visible in the log where an operator can spot it and pin TIGER_SCOPE."""
        conn, ref = _persons([("NY", "061"), ("NY", "061"), ("NJ", "003")], with_county=True)
        logs, calls = [], []
        scope = _resolve(conn, ref, expand_states=_expander(calls), log=logs.append)
        assert logs == ["Deriving scope from the state column: NJ (34) 1 row; NY (36) 2 rows"]
        assert calls == [["34", "36"]]
        assert group_by_state(scope) == {"34": ["003"], "36": ["061"]}

    def test_no_county_fips_column_warns_and_goes_statewide(self):
        conn, ref = _persons([("NY",), ("NY",)], with_county=False)
        warnings = []
        scope = _resolve(conn, ref, warn=warnings.append)
        assert group_by_state(scope) == {"36": COUNTIES["36"]}
        (warning,) = warnings
        assert "has no county_fips column" in warning
        assert "NY" in warning and "TIGER_SCOPE" in warning

    def test_pinned_scope_warns_about_persons_in_other_states(self):
        """A deployment-wide pin still reads `state`: persons the pin does not
        cover are reported (they will not geocode) but the pin stands."""
        conn, ref = _persons([("NJ",), ("NJ",), ("NY",)], with_county=False)
        warnings = []
        scope = _resolve(conn, ref, spec="36:061", warn=warnings.append)
        assert scope == [CountyScope("36", "061")]
        (warning,) = warnings
        assert "NJ (34) 2 rows" in warning
        assert "covers 36 only" in warning
        assert "TIGER_SCOPE" in warning

    def test_pinned_scope_covering_every_state_present_is_quiet(self, capsys):
        conn, ref = _persons([("NY",), ("ny",)], with_county=False)
        warnings = []
        scope = _resolve(conn, ref, spec="36:061", warn=warnings.append)
        assert scope == [CountyScope("36", "061")]
        assert warnings == []
        assert capsys.readouterr().out == ""

    def test_pinned_scope_resolves_a_table_without_a_state_column(self, capsys):
        """The pin is the documented workaround for a table with no `state`."""
        conn = duckdb.connect()
        conn.execute("CREATE TABLE persons (external_id VARCHAR)")
        conn.execute("INSERT INTO persons VALUES ('a')")
        ref = TableRef(catalog="memory", schema="main", table="persons", version=0)
        warnings = []
        scope = _resolve(conn, ref, spec="36:061", warn=warnings.append)
        assert scope == [CountyScope("36", "061")]
        assert warnings == []

    def test_derived_scope_requires_a_state_column(self):
        conn = duckdb.connect()
        conn.execute("CREATE TABLE persons (external_id VARCHAR)")
        ref = TableRef(catalog="memory", schema="main", table="persons", version=0)
        with pytest.raises(ValueError, match="no `state` column"):
            _resolve(conn, ref)


# ---------------------------------------------------------------------------
# SQL / string forms
# ---------------------------------------------------------------------------


class TestStringForms:
    def test_scope_sql_single_state(self):
        assert scope_sql(NYC_SCOPE) == "((state_fips = '36' AND county_fips IN ('005', '047', '061', '081', '085')))"

    def test_scope_sql_multi_state_or_form(self):
        sql = scope_sql([CountyScope("36", "061"), CountyScope("34", "017")], state_col="s", county_col="c")
        assert sql == "((s = '34' AND c IN ('017')) OR (s = '36' AND c IN ('061')))"

    def test_scope_sql_empty_is_false(self):
        assert scope_sql([]) == "FALSE"

    def test_format_scope_round_trips(self):
        text = format_scope([CountyScope("36", "061"), CountyScope("34", "017"), CountyScope("36", "005")])
        assert text == "34:017;36:005,061"
        assert parse_scope_spec(text) == {"34": ["017"], "36": ["005", "061"]}

    def test_scope_metadata_shape(self):
        meta = scope_metadata(
            [CountyScope("36", "061"), CountyScope("34", "017")],
            source="derived",
            tiger_year="2024",
            osm_extracts=["new-jersey-latest", "new-york-260501"],
            utm_epsg=32618,
        )
        assert meta == {
            "source": "derived",
            "tigerYear": "2024",
            "states": [
                {"fips": "34", "postal": "NJ", "counties": ["017"]},
                {"fips": "36", "postal": "NY", "counties": ["061"]},
            ],
            "osmExtracts": ["new-jersey-latest", "new-york-260501"],
            "utmEpsg": 32618,
            "notes": [],
        }

    def test_scope_metadata_keeps_the_resolvers_warnings_as_notes(self):
        meta = scope_metadata(
            NYC_SCOPE,
            source="legacy",
            tiger_year="2024",
            osm_extracts=["new-york-260501"],
            utm_epsg=None,
            notes=["WARNING: persons are in state(s) NJ (34) 1 row but the pinned scope covers 36 only"],
        )
        assert meta["source"] == "legacy"
        assert meta["utmEpsg"] is None
        assert meta["notes"] == ["WARNING: persons are in state(s) NJ (34) 1 row but the pinned scope covers 36 only"]
