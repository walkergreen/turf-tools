"""Static state reference tables.

Postal abbreviation ↔ Census FIPS code for the 50 states, DC, and the
territories, plus the Geofabrik OSM extract slug for every state that has
one under `north-america/us/`. Importers emit `state` as a 2-letter postal
code; TIGER and the county lookup are keyed by FIPS; Geofabrik names its
extracts by slug — these tables bridge the three.
"""

from __future__ import annotations


class UnknownStateError(ValueError):
    """A `state` value that is not a US postal abbreviation or FIPS code."""

    def __init__(self, value: str) -> None:
        super().__init__(f"unknown state code {value!r}")
        self.value = value


STATE_FIPS_BY_POSTAL: dict[str, str] = {
    "AL": "01",
    "AK": "02",
    "AZ": "04",
    "AR": "05",
    "CA": "06",
    "CO": "08",
    "CT": "09",
    "DE": "10",
    "DC": "11",
    "FL": "12",
    "GA": "13",
    "HI": "15",
    "ID": "16",
    "IL": "17",
    "IN": "18",
    "IA": "19",
    "KS": "20",
    "KY": "21",
    "LA": "22",
    "ME": "23",
    "MD": "24",
    "MA": "25",
    "MI": "26",
    "MN": "27",
    "MS": "28",
    "MO": "29",
    "MT": "30",
    "NE": "31",
    "NV": "32",
    "NH": "33",
    "NJ": "34",
    "NM": "35",
    "NY": "36",
    "NC": "37",
    "ND": "38",
    "OH": "39",
    "OK": "40",
    "OR": "41",
    "PA": "42",
    "RI": "44",
    "SC": "45",
    "SD": "46",
    "TN": "47",
    "TX": "48",
    "UT": "49",
    "VT": "50",
    "VA": "51",
    "WA": "53",
    "WV": "54",
    "WI": "55",
    "WY": "56",
    "AS": "60",
    "GU": "66",
    "MP": "69",
    "PR": "72",
    "VI": "78",
}

POSTAL_BY_STATE_FIPS: dict[str, str] = {fips: postal for postal, fips in STATE_FIPS_BY_POSTAL.items()}

# Geofabrik sub-region slug under https://download.geofabrik.de/north-america/us/,
# keyed by state FIPS. Guam, American Samoa, and the Northern Mariana Islands
# are published under Geofabrik's Oceania tree, not `us/`, so they have no
# entry here — a dataset covering them needs `OSM_URLS` set explicitly.
GEOFABRIK_SLUG_BY_STATE_FIPS: dict[str, str] = {
    "01": "alabama",
    "02": "alaska",
    "04": "arizona",
    "05": "arkansas",
    "06": "california",
    "08": "colorado",
    "09": "connecticut",
    "10": "delaware",
    "11": "district-of-columbia",
    "12": "florida",
    "13": "georgia",
    "15": "hawaii",
    "16": "idaho",
    "17": "illinois",
    "18": "indiana",
    "19": "iowa",
    "20": "kansas",
    "21": "kentucky",
    "22": "louisiana",
    "23": "maine",
    "24": "maryland",
    "25": "massachusetts",
    "26": "michigan",
    "27": "minnesota",
    "28": "mississippi",
    "29": "missouri",
    "30": "montana",
    "31": "nebraska",
    "32": "nevada",
    "33": "new-hampshire",
    "34": "new-jersey",
    "35": "new-mexico",
    "36": "new-york",
    "37": "north-carolina",
    "38": "north-dakota",
    "39": "ohio",
    "40": "oklahoma",
    "41": "oregon",
    "42": "pennsylvania",
    "44": "rhode-island",
    "45": "south-carolina",
    "46": "south-dakota",
    "47": "tennessee",
    "48": "texas",
    "49": "utah",
    "50": "vermont",
    "51": "virginia",
    "53": "washington",
    "54": "west-virginia",
    "55": "wisconsin",
    "56": "wyoming",
    "72": "puerto-rico",
    "78": "us-virgin-islands",
}


def state_fips(postal: str) -> str:
    """The 2-digit FIPS code for a postal abbreviation (case/whitespace
    insensitive). Raises `UnknownStateError` for anything else."""
    key = postal.strip().upper()
    try:
        return STATE_FIPS_BY_POSTAL[key]
    except KeyError:
        raise UnknownStateError(postal) from None


def state_postal(fips: str) -> str:
    """The postal abbreviation for a 2-digit FIPS code. Raises `UnknownStateError`."""
    try:
        return POSTAL_BY_STATE_FIPS[fips]
    except KeyError:
        raise UnknownStateError(fips) from None
