"""extensions and helper functions

Revision ID: 0001
Revises:
Create Date: 2026-09-02

Enables the Postgres extensions the schema depends on and installs the base
helper functions used by every later migration:

  * ``uuid_generate_v7()``  — time-ordered UUIDv7 primary keys (RFC 9562).
  * ``set_updated_at()``    — BEFORE UPDATE trigger that stamps ``updated_at``.
  * ``reject_mutation()``   — trigger guard for append-only tables.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")

    # Time-ordered UUIDv7. Postgres 16 has no native generator, so define one.
    # Takes a random v4 UUID (correct variant bits), overlays the first 48 bits
    # with the current Unix time in milliseconds, then sets the version nibble
    # to 0b0111. Bit indices 52/53 target the high nibble of byte 6 because
    # Postgres numbers bits least-significant-first within each byte.
    op.execute(
        """
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
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION set_updated_at()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            NEW.updated_at := now();
            RETURN NEW;
        END;
        $$;
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'table % is append-only; % is not permitted',
                TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS reject_mutation();")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at();")
    op.execute("DROP FUNCTION IF EXISTS uuid_generate_v7();")
    # Extensions are intentionally left installed: they are cluster-level and
    # may be shared with other databases or managed by the platform.
