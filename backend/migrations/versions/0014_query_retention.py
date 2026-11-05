"""提供默认关闭的租户查询原文短期保留。"""

from alembic import op

revision = "0014_query_retention"
down_revision = "0013_audit_risk_index"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE tenant_query_retention (
            tenant_id UUID PRIMARY KEY REFERENCES tenants(id),
            retention_hours INTEGER NOT NULL CHECK (retention_hours BETWEEN 0 AND 24),
            version INTEGER NOT NULL CHECK (version >= 1),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT statement_timestamp()
        )
    """)
    op.execute("""
        CREATE TABLE retained_queries (
            usage_id UUID PRIMARY KEY REFERENCES usage_events(id) ON DELETE CASCADE,
            tenant_id UUID NOT NULL REFERENCES tenant_query_retention(tenant_id),
            policy_version INTEGER NOT NULL CHECK (policy_version >= 1),
            query TEXT NOT NULL CHECK (char_length(query) BETWEEN 1 AND 1024),
            expires_at TIMESTAMPTZ NOT NULL
        )
    """)
    op.create_index("ix_retained_queries_expires", "retained_queries", ["expires_at", "usage_id"])
    op.create_index(
        "ix_retained_queries_tenant_version",
        "retained_queries",
        ["tenant_id", "policy_version", "usage_id"],
    )
    op.execute("""
        CREATE INDEX ix_audit_query_retention_history
        ON audit_events (tenant_id, id DESC)
        WHERE action = 'settings.query_retention.update'
    """)


def downgrade():
    op.drop_index("ix_audit_query_retention_history", table_name="audit_events")
    op.drop_table("retained_queries")
    op.drop_table("tenant_query_retention")
