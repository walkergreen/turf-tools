"""UTM zone selection (`src/geo/projection.py`) — pure, no DuckDB.

Behaviors locked in:

- Zone N spans [-180 + 6(N-1), -180 + 6N): -78.0 is still zone 18, -78.0001
  is zone 17, -72.0 is zone 19; longitudes wrap and clamp to 1..60.
- The EPSG code is 32600 + zone (northern hemisphere).
- `utm_epsg_for_longitudes` picks from the median, warns past one zone,
  raises past a continental span or with no median at all.
"""

import math

import pytest

from src.geo.projection import (
    utm_central_meridian,
    utm_epsg_for_longitude,
    utm_epsg_for_longitudes,
    utm_zone_for_longitude,
)


@pytest.mark.parametrize(
    ("lon", "epsg"),
    [
        (-73.99, 32618),
        (-75.0, 32618),
        (-78.0, 32618),
        (-78.0001, 32617),
        (-78.1, 32617),
        (-72.0, 32619),
        (-87.6, 32616),
        (-118.24, 32611),
        (-118.4, 32611),
        (0.0, 32631),
        (-180.0, 32601),
        (179.999, 32660),
        (180.0, 32601),  # wraps to -180
        (200.0, 32604),  # normalizes to -160
    ],
)
def test_utm_epsg_from_longitude(lon, epsg):
    assert utm_epsg_for_longitude(lon) == epsg
    assert utm_zone_for_longitude(lon) == epsg - 32600


def test_central_meridian():
    assert utm_central_meridian(18) == -75.0
    assert utm_central_meridian(11) == -117.0


@pytest.mark.parametrize("median", [None, math.nan])
def test_missing_median_raises(median):
    with pytest.raises(ValueError, match="no longitudes"):
        utm_epsg_for_longitudes(median, None, None)


def test_nyc_span_is_quiet(capsys):
    assert utm_epsg_for_longitudes(-73.9, -74.2, -73.6, label="nyc") == 32618
    assert capsys.readouterr().out == ""


def test_span_past_one_zone_warns_but_returns_the_median_zone(capsys):
    assert utm_epsg_for_longitudes(-75.0, -82.0, -70.0, label="statewide") == 32618
    out = capsys.readouterr().out
    assert "WARNING" in out and "statewide" in out and "7.0°" in out


def test_continental_span_raises():
    with pytest.raises(ValueError, match="one UTM zone cannot serve"):
        utm_epsg_for_longitudes(-95.0, -122.0, -72.0)


def test_percentiles_may_be_missing():
    assert utm_epsg_for_longitudes(-73.9, None, None) == 32618
