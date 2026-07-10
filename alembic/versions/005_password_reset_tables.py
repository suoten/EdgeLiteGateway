"""add password_reset security tables

Revision ID: 005_password_reset_tables
Revises: 004_missing_tables
Create Date: 2026-06-03

This migration adds the password reset rate-limiting and tracking tables
that were previously managed by manual _migrate() code in database.py.
These tables are now under Alembic's sole management.

This migration is idempotent - it uses try/except blocks to handle
databases that were previously upgraded via manual _migrate() code.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "005_password_reset_tables"
down_revision: str | None = "004_missing_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # password_reset_attempts - rate limiting per IP
    _create_table_if_not_exists(
        "password_reset_attempts",
        sa.Column("ip", sa.String(45), primary_key=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_attempt_at", sa.Float(), nullable=False),
        sa.Column("last_attempt_at", sa.Float(), nullable=False),
    )
    _create_index_idempotent("idx_reset_attempts_last", "password_reset_attempts", ["last_attempt_at"])

    # password_reset_user_rates - rate limiting per username
    _create_table_if_not_exists(
        "password_reset_user_rates",
        sa.Column("username", sa.String(32), primary_key=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_attempt_at", sa.Float(), nullable=False),
        sa.Column("last_attempt_at", sa.Float(), nullable=False),
    )
    _create_index_idempotent("idx_reset_user_last", "password_reset_user_rates", ["last_attempt_at"])

    # used_password_reset_tokens - one-time use token tracking
    _create_table_if_not_exists(
        "used_password_reset_tokens",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("username", sa.String(32), nullable=False),
        sa.Column("used_at", sa.Float(), nullable=False),
    )
    _create_index_idempotent("idx_used_tokens_ts", "used_password_reset_tokens", ["used_at"])
    _create_index_idempotent("idx_used_tokens_user", "used_password_reset_tokens", ["username"])

    # password_reset_ip_attempts - IP-level reset usage tracking
    _create_table_if_not_exists(
        "password_reset_ip_attempts",
        sa.Column("ip", sa.String(45), primary_key=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_attempt_at", sa.Float(), nullable=False),
        sa.Column("last_attempt_at", sa.Float(), nullable=False),
    )
    _create_index_idempotent("idx_reset_ip_last", "password_reset_ip_attempts", ["last_attempt_at"])


def downgrade() -> None:
    _drop_index_if_exists("idx_reset_ip_last", table_name="password_reset_ip_attempts")
    op.drop_table("password_reset_ip_attempts")

    _drop_index_if_exists("idx_used_tokens_user", table_name="used_password_reset_tokens")
    _drop_index_if_exists("idx_used_tokens_ts", table_name="used_password_reset_tokens")
    op.drop_table("used_password_reset_tokens")

    _drop_index_if_exists("idx_reset_user_last", table_name="password_reset_user_rates")
    op.drop_table("password_reset_user_rates")

    _drop_index_if_exists("idx_reset_attempts_last", table_name="password_reset_attempts")
    op.drop_table("password_reset_attempts")


# ── Idempotent helper functions ──────────────────────────────────────────────────


def _create_index_idempotent(index_name: str, table_name: str, columns: list, **kw) -> None:
    """Create an index only if it doesn't already exist."""
    try:
        op.create_index(index_name, table_name, columns, if_not_exists=True, **kw)
    except Exception:
        pass


def _create_table_if_not_exists(table_name: str, *columns, **kw) -> None:
    """Create a table only if it doesn't already exist."""
    try:
        op.create_table(table_name, *columns, **kw)
    except Exception:
        pass


def _drop_index_if_exists(index_name: str, table_name: str = None) -> None:
    """Drop an index only if it exists."""
    try:
        op.drop_index(index_name, table_name=table_name)
    except Exception:
        pass
