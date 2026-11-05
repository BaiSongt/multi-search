"""持久化固定的上游来源诊断代码。"""

from alembic import op

revision = "0009_provider_reasons"
down_revision = "0008_trace_and_cost"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE provider_runs ADD COLUMN reason_codes TEXT[] NOT NULL DEFAULT '{}'::TEXT[]"
    )


def downgrade():
    op.drop_column("provider_runs", "reason_codes")
