"""为过期幂等结果增加清理索引。"""

from alembic import op

revision = "0004_usage_replay_retention"
down_revision = "0003_usage_reporting"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE INDEX ix_usage_idempotency_expiration ON usage_events (idempotency_until)
        WHERE idempotency_until IS NOT NULL
    """)


def downgrade():
    op.drop_index("ix_usage_idempotency_expiration", table_name="usage_events")
