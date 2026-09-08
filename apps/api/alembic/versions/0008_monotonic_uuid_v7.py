"""Make uuid_generate_v7() monotonic within a millisecond

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-08

The original ``uuid_generate_v7()`` (migration 0001) overlays only the
48-bit millisecond timestamp and leaves the following 12 ``rand_a`` bits
random. Two ids generated in the same wall-clock millisecond therefore sort
in *random* order relative to insertion order.

Every keyset-paginated list in the codebase sorts by ``(created_at, id)``,
and ``created_at`` defaults to ``now()`` (the transaction timestamp), so
rows inserted in one transaction share a ``created_at`` and the tie-break
falls entirely on ``id``. With a non-monotonic generator that tie-break
does not match insertion order, which surfaced as an intermittently
failing pagination test.

This replaces the 12 ``rand_a`` bits with sub-millisecond clock precision
(RFC 9562 method 3 - "increased clock precision"): the fractional part of
the millisecond, scaled to 12 bits. Sequential inserts are microseconds
apart, so their ids are now strictly increasing. If two ids somehow land in
the same ~244 ns window they fall back to the still-random ``rand_b`` bits,
so pagination stays a stable total order regardless. The version nibble is
now set explicitly to ``0111`` rather than derived from a timestamp byte.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MONOTONIC = """
    CREATE OR REPLACE FUNCTION uuid_generate_v7()
    RETURNS uuid
    LANGUAGE sql
    VOLATILE
    AS $$
        WITH gen AS (
            SELECT
                uuid_send(gen_random_uuid())                  AS rnd,
                extract(epoch FROM clock_timestamp()) * 1000  AS ms_frac
        )
        SELECT encode(
            set_byte(
                set_byte(
                    set_byte(
                        overlay(
                            rnd
                            PLACING substring(
                                int8send(floor(ms_frac)::bigint) FROM 3 FOR 6
                            )
                            FROM 1 FOR 6
                        ),
                        -- byte 6: version 0111 in the high nibble, high 4 bits
                        -- of the 12-bit sub-millisecond value in the low nibble
                        6, 112 | (floor((ms_frac - floor(ms_frac)) * 4096)::int >> 8)
                    ),
                    -- byte 7: low 8 bits of the sub-millisecond value
                    7, floor((ms_frac - floor(ms_frac)) * 4096)::int & 255
                ),
                -- byte 8: RFC variant 10xxxxxx, keeping the low 6 random bits
                8, (get_byte(rnd, 8) & 63) | 128
            ),
            'hex'
        )::uuid
        FROM gen;
    $$;
"""

_ORIGINAL = """
    CREATE OR REPLACE FUNCTION uuid_generate_v7()
    RETURNS uuid
    LANGUAGE sql
    VOLATILE
    AS $$
        SELECT encode(
            set_bit(
                set_bit(
                    overlay(
                        uuid_send(gen_random_uuid())
                        PLACING substring(
                            int8send(
                                floor(
                                    extract(epoch FROM clock_timestamp()) * 1000
                                )::bigint
                            )
                            FROM 3
                        )
                        FROM 1 FOR 6
                    ),
                    52, 1
                ),
                53, 1
            ),
            'hex'
        )::uuid;
    $$;
"""


def upgrade() -> None:
    op.execute(_MONOTONIC)


def downgrade() -> None:
    op.execute(_ORIGINAL)
