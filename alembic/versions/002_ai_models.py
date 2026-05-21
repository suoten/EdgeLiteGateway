"""add ai_models and ai_inference_logs tables

Revision ID: 002_ai_models
Revises:
Create Date: 2026-05-20
"""

from alembic import op
import sqlalchemy as sa

revision = "002_ai_models"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
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

    op.create_table(
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
    op.create_index("idx_ai_logs_model", "ai_inference_logs", ["model_id"])
    op.create_index("idx_ai_logs_timestamp", "ai_inference_logs", ["timestamp"])


def downgrade() -> None:
    op.drop_index("idx_ai_logs_timestamp", table_name="ai_inference_logs")
    op.drop_index("idx_ai_logs_model", table_name="ai_inference_logs")
    op.drop_table("ai_inference_logs")
    op.drop_table("ai_models")
