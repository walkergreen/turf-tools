"""Synthetic-data tests for blockface_final alias collapse.

`blockface_final` collapses TIGER addrfeat rows that share
`(tiger_line_id, side, house_num_prefix, from_house_num, to_house_num)`
into one row. The canonical `full_name` is the alias that appears most
often across the version's `geo_scope` (alphabetical tiebreak).

It emits two token columns:
  - `street_tokens_match`  — union of every alias row's tokens
                              (after equivalency expansion). Drives the
                              matching predicate so any voter spelling
                              of the same physical street matches.
  - `street_tokens_lookup` — only the canonical row's tokens
                              (after equivalency expansion). Drives the
                              OSM canonical_key lookup so voters at the
                              same building converge on the same OSM record.

These tests build a small synthetic blockface_normalized table by hand,
with a minimal `tiger.addrfeat` carrying each TIGER line's state and county
(the scope filter reads it), and run `tiger.blockface_final` against them.
"""

import pytest

import duckdb
from src.addressing import tokenize_street_sql
from src.dags import tiger
from src.geo.scope import CountyScope

MANHATTAN = [CountyScope("36", "061")]

# Schema mirrors `tiger.blockface_normalized`. We write rows by hand and let
# `tiger.blockface_final` consume them.
_BF_NORM_DDL = """
CREATE TABLE ducklake_geo.tiger.blockface_normalized (
    blockface_id        VARCHAR,
    side                VARCHAR,
    from_house_num      INTEGER,
    to_house_num        INTEGER,
    house_num_prefix    VARCHAR,
    number_type         VARCHAR,
    zip_code            VARCHAR,
    full_name           VARCHAR,
    tiger_line_id       VARCHAR,
    street_name_tokens  VARCHAR[],
    from_node_id        VARCHAR,
    to_node_id          VARCHAR,
    geom                GEOMETRY
)
"""

# The columns of `tiger.addrfeat` that `blockface_final` reads: which
# (state, county) file each TIGER line came from.
_ADDRFEAT_DDL = """
CREATE TABLE ducklake_geo.tiger.addrfeat (
    tiger_line_id       VARCHAR,
    full_name           VARCHAR,
    state_fips          VARCHAR,
    county_fips         VARCHAR
)
"""


def _insert_row(
    conn: duckdb.DuckDBPyConnection,
    tlid: str,
    side: str,
    prefix: str,
    hn_from: int,
    hn_to: int,
    full_name: str,
    zip_code: str = "10001",
    state_fips: str = "36",
    county_fips: str = "061",
) -> None:
    """Insert one synthetic blockface_normalized row and the addrfeat row it
    derives from (`state_fips` / `county_fips` place the line in a county)."""
    bf_id = f"{tlid}:{side}"
    number_type = (
        "odd"
        if (hn_from % 2 == 1 and hn_to % 2 == 1)
        else ("even" if (hn_from % 2 == 0 and hn_to % 2 == 0) else "mixed")
    )
    # Compute tokens via the same SQL helper the pipeline uses, so this
    # test mirrors production tokenization exactly.
    tokens = conn.execute(
        f"SELECT {tokenize_street_sql('s')} FROM (VALUES (?)) AS t(s)",
        [full_name],
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO ducklake_geo.tiger.blockface_normalized VALUES
        (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ST_GeomFromText('LINESTRING(0 0, 1 1)'))
        """,
        [bf_id, side, hn_from, hn_to, prefix, number_type, zip_code, full_name, tlid, tokens, "n1", "n2"],
    )
    conn.execute(
        "INSERT INTO ducklake_geo.tiger.addrfeat VALUES (?, ?, ?, ?)",
        [tlid, full_name, state_fips, county_fips],
    )


@pytest.fixture()
def bf_conn(dual_conn):
    """dual_conn with the tiger schema and empty blockface_normalized +
    addrfeat tables.

    blockface_final reads from tiger.blockface_normalized (scoped through
    tiger.addrfeat); we hand-build both rather than running
    tiger.tiger_addrfeat_raw / blockface_unpivoted / blockface_normalized so
    the test stays focused on alias collapse and isn't subject to TIGER's
    actual data shape.
    """
    dual_conn.execute("CREATE SCHEMA IF NOT EXISTS ducklake_geo.tiger")
    dual_conn.execute(_BF_NORM_DDL)
    dual_conn.execute(_ADDRFEAT_DDL)
    # Seed the equivalency-groups table so blockface_final can JOIN on it.
    tiger.address_tokens(conn=dual_conn)
    return dual_conn


def _run_blockface_final(conn, geo_scope=MANHATTAN):
    norm_ref = type(
        "Ref",
        (),
        {"fqn": "ducklake_geo.tiger.blockface_normalized"},
    )()
    addrfeat_ref = type(
        "Ref",
        (),
        {"fqn": "ducklake_geo.tiger.addrfeat"},
    )()
    tokens_ref = type(
        "Ref",
        (),
        {"fqn": "ducklake_geo.tiger.address_tokens"},
    )()
    return tiger.blockface_final(
        blockface_normalized=norm_ref,
        tiger_addrfeat_raw=addrfeat_ref,
        address_tokens=tokens_ref,
        geo_scope=geo_scope,
        conn=conn,
    )


class TestAliasCollapse:
    def test_two_aliases_same_blockface_collapse_to_one_row(self, bf_conn):
        """Two TIGER rows with the same (tlid, side, prefix, range) but
        different full_name should collapse into one blockface_final row."""
        # Same physical blockface, two name forms:
        _insert_row(bf_conn, tlid="T1", side="left", prefix="", hn_from=1, hn_to=99, full_name="7 Av")
        _insert_row(
            bf_conn, tlid="T1", side="left", prefix="", hn_from=1, hn_to=99, full_name="Adam Clayton Powell Jr Blvd"
        )

        ref = _run_blockface_final(bf_conn)
        count = bf_conn.execute(f"SELECT count(*) FROM {ref.fqn}").fetchone()[0]
        assert count == 1, "alias collapse should produce a single row"

    def test_canonical_full_name_is_more_frequent_alias(self, bf_conn):
        """Across the whole dataset, the canonical full_name is the more
        common one. Alphabetical breaks ties."""
        # T1 + T2 both have "7 Av" / "Adam Clayton Powell Jr Blvd" pairings.
        # Add a third T3 also using "7 Av" so it appears 3× total; "Adam
        # Clayton Powell Jr Blvd" appears 2× total → canonical = "7 Av".
        _insert_row(bf_conn, tlid="T1", side="left", prefix="", hn_from=1, hn_to=99, full_name="7 Av")
        _insert_row(
            bf_conn, tlid="T1", side="left", prefix="", hn_from=1, hn_to=99, full_name="Adam Clayton Powell Jr Blvd"
        )
        _insert_row(bf_conn, tlid="T2", side="left", prefix="", hn_from=101, hn_to=199, full_name="7 Av")
        _insert_row(
            bf_conn, tlid="T2", side="left", prefix="", hn_from=101, hn_to=199, full_name="Adam Clayton Powell Jr Blvd"
        )
        _insert_row(bf_conn, tlid="T3", side="left", prefix="", hn_from=201, hn_to=299, full_name="7 Av")

        ref = _run_blockface_final(bf_conn)
        names = {r[0] for r in bf_conn.execute(f"SELECT full_name FROM {ref.fqn}").fetchall()}
        # Every collapsed group's canonical_name should be "7 Av".
        assert names == {"7 Av"}

    def test_different_address_ranges_stay_separate(self, bf_conn):
        """Two rows with the same TIGER line + side but different address
        ranges represent legitimately separate blockfaces; they MUST NOT
        be collapsed."""
        _insert_row(bf_conn, tlid="T9", side="left", prefix="", hn_from=1, hn_to=49, full_name="Broadway")
        _insert_row(bf_conn, tlid="T9", side="left", prefix="", hn_from=51, hn_to=99, full_name="Broadway")
        ref = _run_blockface_final(bf_conn)
        count = bf_conn.execute(f"SELECT count(*) FROM {ref.fqn}").fetchone()[0]
        assert count == 2

    def test_street_tokens_match_is_union_of_alias_rows(self, bf_conn):
        """`street_tokens_match` should contain tokens from EVERY alias of
        the blockface, so a voter using any of the aliases matches."""
        _insert_row(bf_conn, tlid="T1", side="left", prefix="", hn_from=1, hn_to=99, full_name="7 Av")
        _insert_row(
            bf_conn, tlid="T1", side="left", prefix="", hn_from=1, hn_to=99, full_name="Adam Clayton Powell Jr Blvd"
        )
        ref = _run_blockface_final(bf_conn)
        toks = bf_conn.execute(f"SELECT street_tokens_match FROM {ref.fqn} LIMIT 1").fetchone()[0]
        # From "7 Av": "7", "av" (and "avenue" via equivalency expansion).
        # From "Adam Clayton Powell Jr Blvd": "adam", "clayton", "powell",
        # "jr", "blvd" (+ "boulevard" via expansion).
        for required in ("7", "adam", "clayton", "powell", "jr"):
            assert required in toks, f"merged match tokens missing {required!r}: {toks}"
        # Equivalency expansion fired for at least one of the suffix forms.
        assert ("avenue" in toks) or ("boulevard" in toks), (
            f"expected equivalency expansion in street_tokens_match: {toks}"
        )

    def test_street_tokens_lookup_is_canonical_only(self, bf_conn):
        """`street_tokens_lookup` should contain ONLY the canonical row's
        tokens (plus expansion). It's the OSM lookup key — voters at this
        building should all hit the same OSM record regardless of which
        alias they wrote on their voter registration."""
        # "7 Av" wins as canonical (more global occurrences).
        _insert_row(bf_conn, tlid="T1", side="left", prefix="", hn_from=1, hn_to=99, full_name="7 Av")
        _insert_row(
            bf_conn, tlid="T1", side="left", prefix="", hn_from=1, hn_to=99, full_name="Adam Clayton Powell Jr Blvd"
        )
        _insert_row(bf_conn, tlid="T2", side="left", prefix="", hn_from=101, hn_to=199, full_name="7 Av")
        ref = _run_blockface_final(bf_conn)
        rows = bf_conn.execute(f"SELECT full_name, street_tokens_lookup FROM {ref.fqn}").fetchall()
        for full_name, toks in rows:
            assert full_name == "7 Av"
            assert "7" in toks
            # Alias-only tokens must NOT appear in the lookup column.
            for alias_only in ("adam", "clayton", "powell"):
                assert alias_only not in toks, f"lookup tokens leaked alias token {alias_only!r}: {toks}"


class TestScope:
    """`blockface_final` is rebuilt per import from the rows whose TIGER line
    belongs to a county in `geo_scope`; the shared normalized table may hold
    other states' counties from earlier imports."""

    def _nyc_pair(self, bf_conn):
        # In scope: one Manhattan blockface under two names, "7 Av" the more
        # frequent of the two across Manhattan (T2 uses it too).
        _insert_row(bf_conn, tlid="T1", side="left", prefix="", hn_from=1, hn_to=99, full_name="7 Av")
        _insert_row(
            bf_conn, tlid="T1", side="left", prefix="", hn_from=1, hn_to=99, full_name="Adam Clayton Powell Jr Blvd"
        )
        _insert_row(bf_conn, tlid="T2", side="left", prefix="", hn_from=101, hn_to=199, full_name="7 Av")

    def test_out_of_scope_lines_are_excluded(self, bf_conn):
        self._nyc_pair(bf_conn)
        _insert_row(
            bf_conn, "J1", "left", "", 1, 99, "Washington St", zip_code="07030", state_fips="34", county_fips="017"
        )
        ref = _run_blockface_final(bf_conn)
        tlids = {r[0] for r in bf_conn.execute(f"SELECT tiger_line_id FROM {ref.fqn}").fetchall()}
        assert tlids == {"T1", "T2"}

    def test_out_of_scope_aliases_do_not_shift_the_canonical_pick(self, bf_conn):
        """Twenty New Jersey rows named "Adam Clayton Powell Jr Blvd" sit in
        the shared table; the Manhattan group still picks "7 Av", whose
        frequency is counted within the scope only."""
        self._nyc_pair(bf_conn)
        for i in range(20):
            _insert_row(
                bf_conn,
                tlid=f"J{i}",
                side="left",
                prefix="",
                hn_from=1,
                hn_to=99,
                full_name="Adam Clayton Powell Jr Blvd",
                zip_code="07030",
                state_fips="34",
                county_fips="017",
            )
        ref = _run_blockface_final(bf_conn)
        rows = bf_conn.execute(f"SELECT full_name, street_tokens_lookup FROM {ref.fqn} ORDER BY 1").fetchall()
        assert [r[0] for r in rows] == ["7 Av", "7 Av"]
        for _, toks in rows:
            assert "adam" not in toks

        # The same rows with New Jersey in scope as well: the alias outnumbers
        # "7 Av" 21 to 2, so the Manhattan group's canonical name flips.
        ref = _run_blockface_final(bf_conn, geo_scope=[*MANHATTAN, CountyScope("34", "017")])
        names = bf_conn.execute(f"SELECT DISTINCT full_name FROM {ref.fqn} WHERE tiger_line_id = 'T1'").fetchall()
        assert names == [("Adam Clayton Powell Jr Blvd",)]

    def test_county_border_line_is_in_scope_through_either_county(self, bf_conn):
        """A TIGER line on a county border appears in both counties' addrfeat
        files; either county in scope keeps the line."""
        _insert_row(bf_conn, "B1", "left", "", 1, 99, "Border Rd", state_fips="36", county_fips="061")
        _insert_row(bf_conn, "B1", "left", "", 1, 99, "Border Rd", state_fips="36", county_fips="005")
        ref = _run_blockface_final(bf_conn, geo_scope=[CountyScope("36", "005")])
        assert bf_conn.execute(f"SELECT count(*) FROM {ref.fqn}").fetchone()[0] == 1

    def test_empty_scope_yields_an_empty_table(self, bf_conn):
        self._nyc_pair(bf_conn)
        ref = _run_blockface_final(bf_conn, geo_scope=[])
        assert bf_conn.execute(f"SELECT count(*) FROM {ref.fqn}").fetchone()[0] == 0
