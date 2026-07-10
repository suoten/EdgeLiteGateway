"""add ai_models and ai_inference_logs tables

Revision ID: 002_ai_models
Revises:
Create Date: 2026-05-20

This migration is idempotent - it uses try/except blocks to handle
databases that were previously initialized via Base.metadata.create_all.
"""

import sqlalchemy as sa

from alembic import op

revision = "002_ai_models"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _create_table_if_not_exists(
        "ai_models",
        sa.Column("model_id", sa.String(36), primary_key=True),
        sa.Column("model_name", sa.String(128), nullable=False),
        sa.Column("model_version", sa.String(16), nullable=False),
        sa.Column("model_type", sa.String(16), nullable=False),
        sa.Column("model_file_path", sa.String(256), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="inactive"),
        sa.Column("is_preset", sa.Boolean, nullable=False, server_default=sa.text("0")),
        sa.Column("input_schema", sa.Text, nullable=False, server_default="{}"),
        sa.Column("output_schema", sa.Text, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
    )

    _create_table_if_not_exists(
        "ai_inference_logs",
        sa.Column("log_id", sa.String(36), primary_key=True),
        sa.Column("model_id", sa.String(36), nullable=False),
        sa.Column("model_name", sa.String(128), nullable=False),
        sa.Column("device_id", sa.String(64), nullable=True),
        sa.Column("point_name", sa.String(64), nullable=True),
        sa.Column("input_summary", sa.String(256), nullable=False, server_default=""),
        sa.Column("output_summary", sa.String(256), nullable=False, server_default=""),
        sa.Column("latency_ms", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("status", sa.String(16), nullable=False, server_default="success"),
        sa.Column("error_message", sa.String(512), nullable=True),
        sa.Column("timestamp", sa.DateTime, server_default=sa.func.now()),
    )
    _create_index_idempotent("idx_ai_logs_model", "ai_inference_logs", ["model_id"])
    _create_index_idempotent("idx_ai_logs_timestamp", "ai_inference_logs", ["timestamp"])


def downgrade() -> None:
    _drop_index_if_exists("idx_ai_logs_timestamp", table_name="ai_inference_logs")
    _drop_index_if_exists("idx_ai_logs_model", table_name="ai_inference_logs")
    op.drop_table("ai_inference_logs")
    op.drop_table("ai_models")


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
