"""The TargetSmart field manifest.

The filterable fields a TargetSmart extract produces, owned by the importer that
produces them. Field kinds and labels match the NYS manifest wherever the
semantics match, so segments read the same way across datasets; values are the
canonical labels `transform.py` emits. System filters (`all`, `canvass-outcome`,
`canvass-response`, `segment`) are dataset-independent and live outside the
manifest.
"""

from __future__ import annotations

from src.importers.base import EnumValue, FieldDef, Manifest


def _enum(value: str, label: str) -> EnumValue:
    return EnumValue(value=value, label=label)


TARGETSMART_MANIFEST = Manifest(
    fields=[
        # Identity + demographics
        [
            FieldDef(column="first_name", label="First Name", filter_kind="text"),
            FieldDef(column="last_name", label="Last Name", filter_kind="text"),
            # Composite: reads several columns directly, so no single `column`.
            FieldDef(key="address", label="Address", filter_kind="address"),
            # zip5 doubles as the `nyc_zips` boundary key (derivable from the address,
            # so any geocoded dataset can zone by zip).
            FieldDef(
                column="zip5",
                label="Zip Code",
                filter_kind="text-multi",
                key_group="nyc_zips",
                key_group_label="ZIP codes",
            ),
            # Open-valued: `vb_vf_county_code` follows TargetSmart's own county
            # coding, so no curated catalog — the user types the codes.
            FieldDef(column="county_code", label="County", filter_kind="text-multi"),
            FieldDef(
                column="gender",
                label="Gender",
                filter_kind="enum",
                values=[_enum("M", "Male"), _enum("F", "Female"), _enum("U", "Unknown")],
            ),
            # Age range derived from the birthdate column.
            FieldDef(column="date_of_birth", label="Age", filter_kind="age-range"),
        ],
        # Geographic divisions — the precinct id is the `precincts` boundary key.
        [
            FieldDef(
                column="precinct_id",
                label="Precinct",
                filter_kind="text-multi",
                key_group="precincts",
                key_group_label="Precincts",
            ),
            FieldDef(column="assembly_district", label="Assembly District", filter_kind="text-multi"),
            FieldDef(column="senate_district", label="Senate District", filter_kind="text-multi"),
            FieldDef(column="congressional_district", label="Congressional District", filter_kind="text-multi"),
        ],
        # Voter behavior
        [
            FieldDef(
                column="enrollment",
                label="Party",
                filter_kind="enum",
                values=[
                    _enum("democratic", "Democratic"),
                    _enum("republican", "Republican"),
                    _enum("conservative", "Conservative"),
                    _enum("working_families", "Working Families"),
                    _enum("unaffiliated", "Unaffiliated"),
                    _enum("green", "Green"),
                    _enum("libertarian", "Libertarian"),
                    _enum("other", "Other"),
                ],
            ),
            FieldDef(column="registration_date", label="Registration Date", filter_kind="date-range"),
            FieldDef(
                column="registration_status",
                label="Registration Status",
                filter_kind="enum",
                values=[
                    _enum("active", "Active"),
                    _enum("inactive", "Inactive"),
                    _enum("unknown", "Unknown"),
                ],
            ),
            # Two filters over the one `voting_history` STRUCT[] column, so both
            # carry an explicit `key` to disambiguate. Detail's picker values are
            # precomputed per version at import (see `compute_derived_metadata`)
            # and read from `dataset_versions.derived_metadata`, so no static
            # `values` here.
            FieldDef(
                column="voting_history",
                key="voting_history_count",
                label="Voting History Count",
                filter_kind="voting-history-count",
            ),
            FieldDef(
                column="voting_history",
                key="voting_history_detail",
                label="Voting History Detail",
                filter_kind="voting-history-detail",
            ),
        ],
    ]
)
