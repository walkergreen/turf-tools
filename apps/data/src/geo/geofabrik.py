"""OSM extract URL resolution for the states in scope."""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.geo.states import GEOFABRIK_SLUG_BY_STATE_FIPS

if TYPE_CHECKING:
    from pathlib import Path


def extract_id(pbf: Path) -> str:
    """An extract's identity in the `ducklake_geo.osm.*` tables: the PBF
    filename without its `.osm.pbf` suffix (`new-york-260501`,
    `new-york-latest`, `NewYork`)."""
    return pbf.name.removesuffix(".osm.pbf")


def geofabrik_slug(state_fips: str) -> str:
    """Geofabrik's `north-america/us/` sub-region slug for a state FIPS code."""
    try:
        return GEOFABRIK_SLUG_BY_STATE_FIPS[state_fips]
    except KeyError:
        raise ValueError(f"no Geofabrik extract known for state FIPS {state_fips!r}; set OSM_URLS explicitly") from None


def slug_for_url(url: str) -> str | None:
    """The Geofabrik state slug an extract URL is named for — ``new-york`` for
    ``…/new-york-260501.osm.pbf`` — or `None` when the filename does not
    start with a known slug (a BBBike city extract, a custom file). The
    longest matching slug wins so a filename is never read as a shorter slug
    it happens to contain."""
    filename = url.rsplit("/", 1)[-1]
    matches = [slug for slug in GEOFABRIK_SLUG_BY_STATE_FIPS.values() if filename.startswith(slug + "-")]
    return max(matches, key=len) if matches else None


def osm_url_for_state(state_fips: str, template: str, pins: dict[str, str] | None = None) -> str:
    """The extract URL for one state: the pinned URL for its slug when `pins`
    has one, else `template` with ``{state}`` filled by the slug."""
    slug = geofabrik_slug(state_fips)
    if pins and slug in pins:
        return pins[slug]
    return template.format(state=slug)
