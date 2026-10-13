"""user roles and invitations

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-12

Step 1 of the training platform (``docs/training-platform-design.md``): who a person is, and
how they come to have an account. Purely additive apart from one new column on ``users``.

``users.role`` (``admin`` | ``employee``) is NOT NULL with **no default**, so no code path can
create a user without choosing a role, and nobody is an administrator by accident.

**Backfill policy - least privilege.** Every existing user becomes an ``employee``. Only users
whose address is on the operator's explicit allow-list become ``admin``. The allow-list is
``ADMIN_BOOTSTRAP_EMAILS`` (a comma-separated list, read from the environment, falling back to the
application settings / ``.env``). If it is empty nobody is promoted, and the migration says so in
its log: the operator then runs ``scripts/grant_admin.py --email ...`` deliberately. Existing
users are never deleted and never made administrators because of the data they own.

``invitations`` records who an administrator has invited and with which role. An unknown identity
that presents a *pending, unexpired* invitation for its verified address is provisioned with that
invitation's role (``services.auth``); an identity with none is refused with 403 and no row is
created. At most one pending invitation exists per address (``uq_invitations_pending_email``).
Expiry is derived from ``expires_at``, not stored as a status.

``downgrade`` drops the table and the column; the role information is not preserved.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger("alembic.runtime.migration")


def _bootstrap_emails() -> list[str]:
    """The explicit allow-list of existing administrators (lower-case, de-duplicated)."""
    raw = os.environ.get("ADMIN_BOOTSTRAP_EMAILS")
    if raw is None:
        try:
            from edutoon.core.config import get_settings

            raw = get_settings().ADMIN_BOOTSTRAP_EMAILS
        except Exception:  # noqa: BLE001 - migrations must run without the full app settings
            raw = ""
    parts = raw.replace(";", ",").replace("\n", ",").replace(" ", ",").split(",")
    return sorted({part.strip().lower() for part in parts if part.strip()})


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN role text;")
    op.execute("UPDATE users SET role = 'employee';")

    bind = op.get_bind()
    emails = _bootstrap_emails()
    if emails:
        promoted = bind.execute(
            sa.text("UPDATE users SET role = 'admin' WHERE email = ANY(:emails) RETURNING email")
            .bindparams(sa.bindparam("emails", type_=postgresql.ARRAY(sa.Text())))
            .params(emails=emails)
        ).fetchall()
    else:
        promoted = []
    total = bind.execute(sa.text("SELECT count(*) FROM users")).scalar_one()
    log.info(
        "0019: %s existing user(s); %s promoted to admin from the allow-list; the rest are "
        "employees.",
        total,
        len(promoted),
    )
    if total and not promoted:
        log.warning(
            "0019: no existing user is an administrator. Set ADMIN_BOOTSTRAP_EMAILS or run "
            "scripts/grant_admin.py --email <address> to grant one deliberately."
        )

    op.execute("ALTER TABLE users ALTER COLUMN role SET NOT NULL;")
    op.execute(
        "ALTER TABLE users ADD CONSTRAINT ck_users_role CHECK (role IN ('admin', 'employee'));"
    )

    op.execute(
        """
        CREATE TABLE invitations (
            id                  uuid        PRIMARY KEY DEFAULT uuid_generate_v7(),
            email               text        NOT NULL,
            display_name        text,
            role                text        NOT NULL,
            status              text        NOT NULL DEFAULT 'pending',
            invited_by          uuid        NOT NULL,
            expires_at          timestamptz NOT NULL,
            accepted_user_id    uuid,
            accepted_at         timestamptz,
            clerk_invitation_id text,
            created_at          timestamptz NOT NULL DEFAULT now(),
            updated_at          timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_invitations_invited_by_users
                FOREIGN KEY (invited_by) REFERENCES users (id) ON DELETE RESTRICT,
            CONSTRAINT fk_invitations_accepted_user_id_users
                FOREIGN KEY (accepted_user_id) REFERENCES users (id) ON DELETE RESTRICT,
            CONSTRAINT ck_invitations_email_lowercase CHECK (email = lower(email)),
            CONSTRAINT ck_invitations_email_shape
                CHECK (position('@' IN email) > 1 AND char_length(email) >= 3),
            CONSTRAINT ck_invitations_role CHECK (role IN ('admin', 'employee')),
            CONSTRAINT ck_invitations_status
                CHECK (status IN ('pending', 'accepted', 'revoked')),
            CONSTRAINT ck_invitations_expires_after_creation CHECK (expires_at > created_at),
            CONSTRAINT ck_invitations_accepted_consistent
                CHECK (
                    (status = 'accepted')
                    = (accepted_user_id IS NOT NULL AND accepted_at IS NOT NULL)
                )
        );
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_invitations_pending_email ON invitations (email) "
        "WHERE status = 'pending';"
    )
    op.execute("CREATE INDEX ix_invitations_created_at ON invitations (created_at, id);")
    op.execute(
        """
        CREATE TRIGGER trg_invitations_set_updated_at
            BEFORE UPDATE ON invitations
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS invitations;")
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS ck_users_role;")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS role;")
