"""TIGER/Line file addressing and fetching.

Pure URL/predicate builders for the Census TIGER/Line distribution plus
the shared download-and-unzip step the loaders in `src/dags/tiger.py` and
the county lookup in `src/geo/tiger_scope.py` both use.
"""

from __future__ import annotations

import os
import shutil
import time
import urllib.request
from typing import TYPE_CHECKING
from zipfile import BadZipFile, ZipFile

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

CENSUS_BASE_URL = "https://www2.census.gov/geo/tiger"

# TIGER/Line layer directory → the suffix its zip filenames carry.
TIGER_LAYER_SUFFIX = {
    "ADDRFEAT": "addrfeat",
    "EDGES": "edges",
    "TABBLOCK20": "tabblock20",
    "COUNTY": "county",
}

# `state` value for the national files (`tl_{year}_us_county.zip`).
NATIONAL = "us"


def tiger_zip_url(layer: str, year: str, state: str, county: str | None = None) -> str:
    """URL of one TIGER/Line zip: ``TIGER{year}/{layer}/tl_{year}_{state}{county}_{suffix}.zip``.

    `state` is a 2-digit FIPS code, or `NATIONAL` for the national files
    (COUNTY); `county` is the 3-digit FIPS code for per-county layers
    (ADDRFEAT, EDGES) and omitted for per-state (TABBLOCK20) and national ones.
    """
    suffix = TIGER_LAYER_SUFFIX[layer]
    return f"{CENSUS_BASE_URL}/TIGER{year}/{layer}/tl_{year}_{state}{county or ''}_{suffix}.zip"


def tabblock_filter_sql(state: str, counties: Sequence[str]) -> str:
    """WHERE clause selecting `counties` of `state` out of a statewide TABBLOCK20 shapefile."""
    in_list = ", ".join(f"'{c}'" for c in counties)
    return f"STATEFP20 = '{state}' AND COUNTYFP20 IN ({in_list})"


def download_and_extract(url: str, zip_path: Path, extract_dir: Path) -> None:
    """Download a zip from *url* to *zip_path* and extract into *extract_dir*.

    A zip already at *zip_path* is a prior successful download and is not
    fetched again. The download streams into ``<zip_path>.part`` and is
    renamed onto *zip_path* only once complete, so an interrupted transfer
    never leaves a truncated file under the cached name (and the archive is
    never held in memory). A cached file that is not a valid zip — an HTML
    error page served with a 200, say — is deleted and reported with its
    path, so the next run re-downloads it.
    """
    extract_dir.mkdir(parents=True, exist_ok=True)
    if not zip_path.exists():
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        # Census's Cloudflare edge rejects the default Python-urllib UA
        # and caches the HTML rejection at the edge URL-keyed; the unique
        # query param bypasses that cache. Census ignores unknown params.
        fetch_url = f"{url}?_={int(time.time() * 1000)}"
        req = urllib.request.Request(fetch_url, headers={"User-Agent": "Mozilla/5.0"})
        part = zip_path.with_name(zip_path.name + ".part")
        try:
            with urllib.request.urlopen(req) as resp, open(part, "wb") as f:  # noqa: S310
                shutil.copyfileobj(resp, f)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        os.replace(part, zip_path)

    try:
        with ZipFile(zip_path) as zf:
            zf.extractall(extract_dir)
    except BadZipFile as e:
        zip_path.unlink(missing_ok=True)
        raise RuntimeError(f"{zip_path} is not a valid zip (deleted; re-run to re-download it): {e}") from e


def shp_files(directory: Path, pattern: str = "*.shp") -> list[Path]:
    """Return all .shp files in *directory* matching *pattern*."""
    return sorted(directory.glob(pattern))
