"""The static state tables in `src/geo/states.py`.

Behaviors locked in:

- Postal ↔ FIPS round-trips, zero-padded VARCHAR codes, case/whitespace
  insensitive lookup, unknown codes raise naming the value.
- The FIPS table covers the 50 states + DC + territories with no duplicates.
- Every state and DC has a Geofabrik slug; every slug key is a known FIPS.
"""

import pytest

from src.geo.states import (
    GEOFABRIK_SLUG_BY_STATE_FIPS,
    POSTAL_BY_STATE_FIPS,
    STATE_FIPS_BY_POSTAL,
    UnknownStateError,
    state_fips,
    state_postal,
)

TERRITORIES = {"AS", "GU", "MP", "PR", "VI"}


@pytest.mark.parametrize(("postal", "fips"), [("NY", "36"), ("NJ", "34"), ("CA", "06"), ("DC", "11")])
def test_postal_code_maps_to_fips(postal, fips):
    assert state_fips(postal) == fips
    assert state_postal(fips) == postal


def test_lookup_is_case_and_whitespace_insensitive():
    assert state_fips(" ny ") == "36"


def test_unknown_postal_code_raises_naming_the_value():
    with pytest.raises(UnknownStateError, match="ZZ"):
        state_fips("ZZ")
    with pytest.raises(UnknownStateError, match="99"):
        state_postal("99")


def test_fips_table_is_complete_and_unique():
    states = set(STATE_FIPS_BY_POSTAL) - TERRITORIES
    assert len(states) == 51  # 50 states + DC
    assert set(STATE_FIPS_BY_POSTAL) >= TERRITORIES
    fips = list(STATE_FIPS_BY_POSTAL.values())
    assert len(fips) == len(set(fips))
    assert all(len(f) == 2 and f.isdigit() for f in fips)
    assert {f: p for p, f in STATE_FIPS_BY_POSTAL.items()} == POSTAL_BY_STATE_FIPS


def test_every_state_has_a_geofabrik_slug():
    with_slug = {POSTAL_BY_STATE_FIPS[f] for f in GEOFABRIK_SLUG_BY_STATE_FIPS}
    assert set(STATE_FIPS_BY_POSTAL) - TERRITORIES <= with_slug
    assert set(GEOFABRIK_SLUG_BY_STATE_FIPS) <= set(POSTAL_BY_STATE_FIPS)
    assert GEOFABRIK_SLUG_BY_STATE_FIPS["36"] == "new-york"
    assert GEOFABRIK_SLUG_BY_STATE_FIPS["34"] == "new-jersey"
    assert GEOFABRIK_SLUG_BY_STATE_FIPS["11"] == "district-of-columbia"
