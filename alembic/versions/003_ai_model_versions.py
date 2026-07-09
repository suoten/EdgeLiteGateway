"""add ai_model_versions table

Revision ID: 003_ai_model_versions
Revises: 002_ai_models
Create Date: 2026-05-31

This migration is idempotent - it uses try/except blocks to handle
databases that were previously initialized via Base.metadata.create_all.
"""

from alembic import op
import sqlalchemy as sa

revision = "003_ai_model_versions"
down_revision = "002_ai_models"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _create_table_if_not_exists(
        "ai_model_versions",
        sa.Column("version_id", sa.String(36), primary_key=True),
        sa.Column("model_id", sa.String(36), nullable=False),
        sa.Column("version", sa.String(16), nullable=False),
        sa.Column("model_path", sa.String(256), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="inactive"),
        sa.Column("success_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("error_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("avg_latency_ms", sa.Float, nullable=False, server_default=sa.text("0.0")),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    _create_index_idempotent("idx_ai_versions_model", "ai_model_versions", ["model_id"])


def downgrade() -> None:
    _drop_index_if_exists("idx_ai_versions_model", table_name="ai_model_versions")
    op.drop_table("ai_model_versions")


# ── Idempotent helper functions ──────────────────────────────────────────────────


def _create_table_if_not_exists(table_name: str, *columns, **kw) -> None:
    """Create a table only if it doesn't already exist."""
    try:
        op.create_table(table_name, *columns, **kw)
    except Exception:
        pass


def _create_index_idempotent(index_name: str, table_name: str, columns: list, **kw) -> None:
    """Create an index only if it doesn't already exist."""
    try:
        op.create_index(index_name, table_name, columns, if_not_exists=True, **kw)
    except Exception:
        pass


def _drop_index_if_exists(index_name: str, table_name: str = None) -> None:
    """Drop an index only if it exists."""
    try:
        op.drop_index(index_name, table_name=table_name)
    except Exception:
        pass