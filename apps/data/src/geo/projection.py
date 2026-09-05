"""UTM zone selection for metric geometry.

The geocode DAG measures offsets and spacings in meters, which needs a
projected CRS. One UTM zone serves a whole dataset version: transverse
Mercator scale error grows with distance from the zone's central meridian
(~0.04 % at the zone edge, ~0.3 % one zone out, ~1.2 % two zones out), and
the largest metric quantity in the pipeline is a 7 m road offset, so a
dataset spanning a state or two is served by the zone of its median
longitude. Past `WIDE_SPAN_ERROR_DEG` from that meridian the distortion is
no longer negligible and the helper refuses rather than mis-scale silently.
"""

from __future__ import annotations

import math

UTM_ZONE_WIDTH_DEG = 6.0
# EPSG codes for WGS 84 / UTM zone N in the northern hemisphere are 32600 + N.
NORTHERN_UTM_EPSG_BASE = 32600
# Offsets of the 5th/95th percentile longitudes from the chosen central
# meridian: warn one zone past it, refuse when the span is continental.
WIDE_SPAN_WARN_DEG = 6.0
WIDE_SPAN_ERROR_DEG = 20.0


def normalize_longitude(lon: float) -> float:
    """Wrap `lon` into [-180, 180)."""
    return ((lon + 180.0) % 360.0) - 180.0


def utm_zone_for_longitude(lon: float) -> int:
    """UTM zone (1..60) containing `lon`. Zone N spans [-180 + 6(N-1), -180 + 6N)."""
    zone = math.floor((normalize_longitude(lon) + 180.0) / UTM_ZONE_WIDTH_DEG) + 1
    return min(60, max(1, zone))


def utm_zone_sql(lon_expr: str) -> str:
    """SQL computing `utm_zone_for_longitude` for a longitude expression that
    is already in [-180, 180] (every TIGER and OSM coordinate is)."""
    return f"least(60, greatest(1, floor(({lon_expr} + 180.0) / {UTM_ZONE_WIDTH_DEG})::INTEGER + 1))"


def utm_central_meridian(zone: int) -> float:
    """Central meridian (degrees) of UTM zone `zone`."""
    return -180.0 + UTM_ZONE_WIDTH_DEG * zone - UTM_ZONE_WIDTH_DEG / 2


def utm_epsg_for_longitude(lon: float) -> int:
    """EPSG code of the northern-hemisphere UTM zone containing `lon`."""
    return NORTHERN_UTM_EPSG_BASE + utm_zone_for_longitude(lon)


def utm_epsg_for_longitudes(
    median: float | None,
    p05: float | None,
    p95: float | None,
    *,
    label: str = "dataset",
) -> int:
    """Pick one EPSG code for a longitude distribution.

    The zone comes from `median`. `p05`/`p95` are checked against the
    zone's central meridian: a warning is printed past `WIDE_SPAN_WARN_DEG`,
    and `ValueError` is raised past `WIDE_SPAN_ERROR_DEG` — a dataset that
    wide needs more than one zone. Percentiles rather than min/max so a
    handful of mis-geocoded records cannot trip either threshold.
    """
    if median is None or math.isnan(median):
        raise ValueError(f"cannot choose a UTM zone for {label}: no longitudes to measure")
    zone = utm_zone_for_longitude(median)
    meridian = utm_central_meridian(zone)
    span = max(
        abs(normalize_longitude(p) - meridian) for p in (median, p05, p95) if p is not None and not math.isnan(p)
    )
    if span > WIDE_SPAN_ERROR_DEG:
        raise ValueError(
            f"{label} spans {span:.1f}° of longitude from the UTM zone {zone} central meridian "
            f"({meridian:.0f}°); one UTM zone cannot serve it"
        )
    if span > WIDE_SPAN_WARN_DEG:
        print(
            f"WARNING: {label} reaches {span:.1f}° from the UTM zone {zone} central meridian ({meridian:.0f}°); "
            "metric offsets at the far edge carry a few tenths of a percent of scale error"
        )
    return NORTHERN_UTM_EPSG_BASE + zone
