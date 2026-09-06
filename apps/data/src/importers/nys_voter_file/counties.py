"""NYS Board of Elections county codes → Census county FIPS codes.

The SBOE numbers New York's 62 counties 01–62 in alphabetical order; the
Census assigns the same alphabetical order the odd FIPS codes 001–123. The
table is written out explicitly so a code is looked up, never computed —
a BOE code padded to three digits (`31` → `031`, Essex) is a real but wrong
county, which is exactly the mistake this map exists to prevent.
"""

from __future__ import annotations

BOE_TO_FIPS: dict[str, str] = {
    "01": "001",  # Albany
    "02": "003",  # Allegany
    "03": "005",  # Bronx
    "04": "007",  # Broome
    "05": "009",  # Cattaraugus
    "06": "011",  # Cayuga
    "07": "013",  # Chautauqua
    "08": "015",  # Chemung
    "09": "017",  # Chenango
    "10": "019",  # Clinton
    "11": "021",  # Columbia
    "12": "023",  # Cortland
    "13": "025",  # Delaware
    "14": "027",  # Dutchess
    "15": "029",  # Erie
    "16": "031",  # Essex
    "17": "033",  # Franklin
    "18": "035",  # Fulton
    "19": "037",  # Genesee
    "20": "039",  # Greene
    "21": "041",  # Hamilton
    "22": "043",  # Herkimer
    "23": "045",  # Jefferson
    "24": "047",  # Kings
    "25": "049",  # Lewis
    "26": "051",  # Livingston
    "27": "053",  # Madison
    "28": "055",  # Monroe
    "29": "057",  # Montgomery
    "30": "059",  # Nassau
    "31": "061",  # New York
    "32": "063",  # Niagara
    "33": "065",  # Oneida
    "34": "067",  # Onondaga
    "35": "069",  # Ontario
    "36": "071",  # Orange
    "37": "073",  # Orleans
    "38": "075",  # Oswego
    "39": "077",  # Otsego
    "40": "079",  # Putnam
    "41": "081",  # Queens
    "42": "083",  # Rensselaer
    "43": "085",  # Richmond
    "44": "087",  # Rockland
    "45": "089",  # St. Lawrence
    "46": "091",  # Saratoga
    "47": "093",  # Schenectady
    "48": "095",  # Schoharie
    "49": "097",  # Schuyler
    "50": "099",  # Seneca
    "51": "101",  # Steuben
    "52": "103",  # Suffolk
    "53": "105",  # Sullivan
    "54": "107",  # Tioga
    "55": "109",  # Tompkins
    "56": "111",  # Ulster
    "57": "113",  # Warren
    "58": "115",  # Washington
    "59": "117",  # Wayne
    "60": "119",  # Westchester
    "61": "121",  # Wyoming
    "62": "123",  # Yates
}


def county_fips_sql(county_code_expr: str) -> str:
    """SQL CASE mapping a raw BOE county-code expression to its 3-digit county
    FIPS, NULL for anything outside the table. The code is cast and left-padded
    to two digits first so a numerically typed export (`3`) matches `03`."""
    normalized = f"lpad(CAST({county_code_expr} AS VARCHAR), 2, '0')"
    branches = "\n        ".join(f"WHEN {normalized} = '{boe}' THEN '{fips}'" for boe, fips in BOE_TO_FIPS.items())
    return f"CASE\n        {branches}\n        ELSE NULL\n    END"
