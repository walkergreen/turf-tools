import os
from typing import Annotated, Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode

from src.geo.scope import parse_scope_spec, scope_spec_from_settings


def _default_database_url() -> str:
    """Dev Postgres fallback — mirrors packages/db/src/index.ts."""
    if os.environ.get("NODE_ENV") == "production":
        raise ValueError("DATABASE_URL is required in production.")
    return "postgres://postgres:postgres@127.0.0.1:5432/postgres"


def _default_ducklake_metadata_url() -> str | None:
    """Dev defaults DuckLake to a Postgres catalog (concurrency-safe, so import
    jobs don't fight the serving connection over a single-writer local file).
    Prod sets it explicitly; unset in prod → local-file fallback."""
    if os.environ.get("NODE_ENV") == "production":
        return None
    return _default_database_url()


class StorageConfig(BaseSettings):
    """S3-compatible object storage for the deployment.

    One bucket per deployment; the lakes and the search index are separated by
    prefix inside it, so a single credential covers everything.
    """

    model_config = {"env_prefix": "STORAGE_"}

    endpoint_url: str = ""
    access_key_id: str = ""
    secret_access_key: str = ""
    bucket: str = ""
    region: str = "auto"
    # DuckDB addressing style. Latitude accepts path; DO Spaces needs vhost.
    url_style: str = "path"


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = {"env_file": ".env"}

    ducklake_metadata_postgres_url: str | None = Field(
        default_factory=_default_ducklake_metadata_url,
        description="PostgreSQL connection URL for the DuckLake metadata catalog. If not set, uses local DuckDB file.",
    )
    # Postgres schema holding this catalog's metadata tables. Same names in
    # every environment: dev shares one Postgres DB across catalogs via these
    # schemas; prod gives each catalog its own DB but keeps the schema name.
    ducklake_meta_schema: str = "catalog"

    ducklake_geo_metadata_postgres_url: str | None = Field(
        default_factory=_default_ducklake_metadata_url,
        description=(
            "PostgreSQL connection URL for the geo DuckLake metadata catalog. If not set, uses local DuckDB file."
        ),
    )
    ducklake_geo_meta_schema: str = "catalog_geo"

    database_url: str = Field(
        default_factory=_default_database_url,
        description="PostgreSQL connection URL for operational data shared with the web app.",
    )

    storage: StorageConfig = Field(default_factory=StorageConfig)
    ducklake_prefix: str = Field(default="ducklake", description="Key prefix for the person lake.")
    ducklake_geo_prefix: str = Field(default="ducklake-geo", description="Key prefix for the geo lake.")

    # TIGER download settings. The geographic scope (which states/counties to
    # load) is resolved per dataset version by `src/geo/scope.py`: an explicit
    # `TIGER_SCOPE` pins it for the deployment; otherwise the legacy
    # `TIGER_STATE_FIPS` + `TIGER_COUNTY_FIPS` pair does (both required, and
    # not read at all once `TIGER_SCOPE` is set); otherwise it is derived from
    # the `state` / `county_fips` columns of the imported data. A pin is
    # deployment-wide — every dataset imported here geocodes against it — so
    # it must be removed or extended before a dataset from another state is
    # imported; the import job warns when persons fall outside it.
    tiger_year: str = Field(
        default="2024",
        description="TIGER/Line vintage year to download.",
    )
    tiger_scope: str | None = Field(
        default=None,
        description=(
            "Pinned geographic scope: ';'-separated `STATE:COUNTIES` entries, e.g. "
            "`36:005,047,061,081,085;34:017`, with `36:*` (or a bare `36`) for statewide. "
            "States accept FIPS or postal codes. Unset → derive the scope from the imported data."
        ),
    )
    tiger_state_fips: str | None = Field(
        default=None,
        description=(
            "Single-state pin used when TIGER_SCOPE is unset: the state FIPS code (e.g. 36). "
            "Requires TIGER_COUNTY_FIPS; for the whole state set TIGER_SCOPE=36:* instead. "
            "Not read when TIGER_SCOPE is set."
        ),
    )
    tiger_county_fips: list[str] | None = Field(
        default=None,
        description=(
            'County FIPS codes (JSON array, e.g. ["061","005"]) within TIGER_STATE_FIPS. '
            "Only read alongside TIGER_STATE_FIPS when TIGER_SCOPE is unset; the two are set together."
        ),
    )
    tiger_data_dir: str = Field(
        default="./tiger_cache",
        description="Local directory to cache downloaded TIGER shapefiles.",
    )

    @field_validator("tiger_scope")
    @classmethod
    def _validate_tiger_scope(cls, value: str | None) -> str | None:
        """Fail at startup on a malformed TIGER_SCOPE; blank means unset."""
        if value is None or value.strip() == "":
            return None
        parse_scope_spec(value)
        return value

    @model_validator(mode="after")
    def _validate_legacy_scope_pair(self) -> Self:
        """Fail at startup when only one half of the legacy pin is set and no
        `TIGER_SCOPE` outranks it, so a stale TIGER_STATE_FIPS without its
        county list cannot send an import statewide. `scope_spec_from_settings`
        applies the precedence the import job uses, so a half pin left in the
        env beside a `TIGER_SCOPE` is inert here exactly as it is there."""
        scope_spec_from_settings(self)
        return self

    voter_zip5_filter: list[str] | None = Field(
        default=None,
        description=(
            "When set, restrict seed-persons to voters whose residential ZIP5 is in this list. "
            "Used to scope dev runs to a small set of test areas for fast iteration."
        ),
    )

    # OSM refinement layer. One PBF extract per state in scope, resolved by
    # `osm.osm_extract_urls`: an explicit `OSM_URLS` list wins; otherwise each
    # state takes the pinned URL for its Geofabrik slug in `OSM_URL_PINS`, else
    # `OSM_URL_TEMPLATE`. A single `OSM_URL` acts as the pin for the
    # state its filename names (`new-york-260501.osm.pbf` pins `new-york`), so
    # other states in scope still resolve; a URL not named for a Geofabrik
    # state (a BBBike city extract) is ingested verbatim as the only extract.
    # Extracts are ingested once each (keyed by PBF filename), so a template
    # URL ending in `-latest` is pinned by the copy in `osm_data_dir` — delete
    # that file to refresh it, and every table reloads the extract; a
    # date-stamped pin (YYMMDD in the filename) makes the snapshot explicit.
    osm_url_template: str = Field(
        default="https://download.geofabrik.de/north-america/us/{state}-latest.osm.pbf",
        description="Per-state extract URL; `{state}` is the Geofabrik slug (new-york, new-jersey, …).",
    )
    osm_url_pins: dict[str, str] = Field(
        default={"new-york": "https://download.geofabrik.de/north-america/us/new-york-260501.osm.pbf"},
        description=(
            "Geofabrik slug → exact extract URL, used instead of OSM_URL_TEMPLATE for that state (JSON object)."
        ),
    )
    osm_urls: Annotated[list[str] | None, NoDecode] = Field(
        default=None,
        description="Comma-separated extract URLs to ingest verbatim, overriding per-state resolution.",
    )
    osm_url: str | None = Field(
        default=None,
        description=(
            "A single extract URL. Named for a Geofabrik state (…/new-york-260501.osm.pbf) it pins that state and "
            "other states in scope still resolve via OSM_URL_PINS / OSM_URL_TEMPLATE; any other URL is ingested "
            "verbatim as the only extract."
        ),
    )
    osm_data_dir: str = Field(
        default="./osm_cache",
        description="Local directory to cache the downloaded OSM PBFs.",
    )

    @field_validator("osm_urls", mode="before")
    @classmethod
    def _split_osm_urls(cls, value: object) -> object:
        """`OSM_URLS` arrives from the environment as one comma-separated string."""
        if isinstance(value, str):
            items = [u.strip() for u in value.split(",") if u.strip()]
            return items or None
        return value

    quickwit_batch_size: int = Field(
        default=1_000_000,
        description=(
            "Number of voter records to stream per Quickwit local-ingest batch. "
            "1,000,000 is the conservative default; larger batch sizes may be faster "
            "if the builder machine has enough memory."
        ),
    )
    quickwit_url: str = Field(
        default="http://127.0.0.1:7280",
        description="Base URL of the Quickwit searcher's REST API (index create + search).",
    )
    quickwit_binary: str = Field(
        default="quickwit",
        description="Path to the Quickwit CLI binary used for `tool local-ingest`.",
    )
    quickwit_config_path: str = Field(
        default="quickwit/node.yaml",
        description="Quickwit node config passed to `local-ingest` (carries the metastore URI).",
    )


def get_settings() -> Settings:
    """Create and return application settings from environment variables."""
    return Settings()
