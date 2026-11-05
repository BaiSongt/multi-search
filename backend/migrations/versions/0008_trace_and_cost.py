"""关联服务端追踪标识，并区分实际模型用量与预算。"""

from alembic import op

revision = "0008_trace_and_cost"
down_revision = "0007_local_index"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE usage_events
            ADD COLUMN trace_id VARCHAR(32),
            ADD COLUMN llm_usage_unknown BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN llm_tokens_reserved INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN estimated_cost NUMERIC(18,8),
            ADD COLUMN cost_currency CHAR(3)
    """)
    op.execute("""
        CREATE INDEX ix_usage_trace_id ON usage_events (trace_id) WHERE trace_id IS NOT NULL
    """)


def downgrade():
    op.drop_index("ix_usage_trace_id", table_name="usage_events")
    for name in (
        "cost_currency",
        "estimated_cost",
        "llm_tokens_reserved",
        "llm_usage_unknown",
        "trace_id",
    ):
        op.drop_column("usage_events", name)
