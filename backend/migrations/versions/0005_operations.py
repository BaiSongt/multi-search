"""记录租户邀请、路由发布历史和范围缓存代次。"""

from alembic import op

revision = "0005_operations"
down_revision = "0004_usage_replay_retention"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE tenant_invitations (
            id UUID PRIMARY KEY,
            tenant_id UUID NOT NULL REFERENCES tenants(id),
            issuer_tenant_id UUID NOT NULL,
            issuer_id UUID NOT NULL,
            email VARCHAR(254) NOT NULL,
            role VARCHAR(24) NOT NULL CHECK (
                role IN ('developer', 'tenant_admin', 'operations_admin', 'platform_admin', 'auditor')
            ),
            token_hash BYTEA NOT NULL CHECK (octet_length(token_hash) = 32),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ NOT NULL,
            revoked_at TIMESTAMPTZ,
            accepted_at TIMESTAMPTZ,
            FOREIGN KEY (issuer_tenant_id, issuer_id) REFERENCES memberships(tenant_id, user_id),
            CHECK (expires_at > created_at),
            CHECK (revoked_at IS NULL OR accepted_at IS NULL)
        )
    """)
    op.create_index(
        "ix_invitation_tenant_created", "tenant_invitations", ["tenant_id", "created_at", "id"]
    )
    op.execute("""
        CREATE INDEX ix_invitation_tenant_pending ON tenant_invitations (tenant_id, email)
        WHERE accepted_at IS NULL AND revoked_at IS NULL
    """)
    op.execute("""
        CREATE TABLE routing_versions (
            version INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            policy JSONB NOT NULL CHECK (jsonb_typeof(policy) = 'object'),
            actor_id UUID NOT NULL REFERENCES users(id),
            reason VARCHAR(500) NOT NULL,
            rollback_of INTEGER CHECK (rollback_of >= 0 AND rollback_of < version),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE search_cache_epochs (
            tenant_id UUID NOT NULL REFERENCES tenants(id),
            scope VARCHAR(16) NOT NULL CHECK (
                scope IN ('all', 'web', 'news', 'image', 'academic', 'code', 'local')
            ),
            epoch BIGINT NOT NULL CHECK (epoch > 0),
            PRIMARY KEY (tenant_id, scope)
        )
    """)


def downgrade():
    for table in ("search_cache_epochs", "routing_versions", "tenant_invitations"):
        op.drop_table(table)
