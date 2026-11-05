"""在请求追踪中保存实际来源尝试次数。"""

from alembic import op

revision = "0015_provider_attempts"
down_revision = "0014_query_retention"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE provider_runs ADD COLUMN attempts INTEGER CHECK (attempts >= 0)")


def downgrade():
    op.drop_column("provider_runs", "attempts")
