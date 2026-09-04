"""The TargetSmart voter-file importer.

`source → (persons_validated, manifest)` for a Parquet extract of TargetSmart's
national voter file (exported from BigQuery). Reads the Parquet as-is,
transforms to the canonical `Person` schema with the voting history assembled in
SQL, and validates. Everything below the returned `persons_validated`
(geocode → assembly → aggregate) is the shared, source-agnostic pipeline.
"""

from __future__ import annotations

import glob
import os
from typing import TYPE_CHECKING

import duckdb
from src.importers.base import SourceUnreadableError, redact_source, validate_persons_table
from src.importers.targetsmart.manifest import TARGETSMART_MANIFEST
from src.importers.targetsmart.transform import SOURCE_COLUMNS, targetsmart_transformation_query
from src.models import TableRef
from src.tables import PERSON_CATALOG, ensure_schema, table_fqn

if TYPE_CHECKING:
    from src.importers.base import Manifest, Progress


def _current_version(conn: duckdb.DuckDBPyConnection) -> int:
    return conn.sql(f"FROM {PERSON_CATALOG}.current_snapshot()").fetchone()[0]


class TargetSmartImporter:
    """Curated importer for a TargetSmart Parquet extract. Implements `Importer`."""

    name = "targetsmart"
    PROGRESS_STEPS = 3  # read, transform, validate

    def manifest(self) -> Manifest:
        return TARGETSMART_MANIFEST

    def load(
        self,
        source: str,
        schema: str,
        conn: duckdb.DuckDBPyConnection,
        progress: Progress,
    ) -> TableRef:
        """Read `source` → transform → validate. Returns the `persons_validated`
        TableRef the shared pipeline reads from. Reports one progress step per
        stage (`PROGRESS_STEPS`)."""
        # Expand `~` and verify a local source exists up front so a typo fails
        # before any catalog work. `source` may be a glob (`part-*.parquet` —
        # DuckDB expands it), so the check goes through `glob` rather than a
        # plain existence test. (`://` = object-storage key.)
        source = os.path.expanduser(source)
        if "://" not in source and not glob.glob(source, recursive=True):
            raise SourceUnreadableError(f"Import source not found: {redact_source(source)!r}")
        ensure_schema(conn, schema)
        raw_fqn = table_fqn(schema, "persons_raw")
        transformed_fqn = table_fqn(schema, "persons_transformed")
        validated_fqn = table_fqn(schema, "persons_validated")

        # 1. Source → persons_raw. A remote source can't be checked up front the
        #    way a local path can, so this is where a bad URL surfaces.
        try:
            conn.execute(f"DROP TABLE IF EXISTS {raw_fqn}")
            conn.read_parquet(source).create(raw_fqn)
        except (duckdb.Error, OSError) as exc:
            raise SourceUnreadableError(
                f"Could not read the import source {redact_source(source)!r}. Check that it exists and is reachable."
            ) from exc
        progress.advance()

        # 2. Transform to the canonical Person schema (TargetSmart labels →
        #    canonical labels, address assembly, voting history from the
        #    per-election columns present in this extract).
        columns = conn.table(raw_fqn).columns
        missing = SOURCE_COLUMNS - set(columns)
        if missing:
            raise ValueError(f"TargetSmart source is missing columns: {sorted(missing)}")
        query = targetsmart_transformation_query(source_table=raw_fqn, columns=columns)
        conn.execute(f"CREATE OR REPLACE TABLE {transformed_fqn} AS {query}")
        progress.advance()

        # 3. Materialize persons_validated and check it carries the
        #    Person-required columns + a sample of rows through the model.
        conn.execute(f"CREATE OR REPLACE TABLE {validated_fqn} AS SELECT * FROM {transformed_fqn}")
        validate_persons_table(validated_fqn, conn)
        progress.advance()

        return TableRef(
            catalog=PERSON_CATALOG,
            schema=schema,
            table="persons_validated",
            version=_current_version(conn),
        )
