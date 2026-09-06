"""Geographic reference tables and scope/projection helpers shared by the DAGs.

- `states` — postal ↔ FIPS ↔ Geofabrik slug tables.
- `scope` — the (state, county) pairs a dataset version needs TIGER data for.
- `projection` — UTM zone selection for metric geometry.
- `geofabrik` — OSM extract URL resolution per state in scope.
"""
