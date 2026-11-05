"""保留配置费率折算到单个 Token 后的费用精度。"""

from alembic import op

revision = "0010_model_cost_precision"
down_revision = "0009_provider_reasons"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE usage_events ALTER COLUMN estimated_cost TYPE NUMERIC(24,14)")


def downgrade():
    op.execute("ALTER TABLE usage_events ALTER COLUMN estimated_cost TYPE NUMERIC(18,8)")
