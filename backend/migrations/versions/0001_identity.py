"""建立租户、成员、机器密钥和审计数据。"""

from alembic import op

revision = "0001_identity"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE tenants (
            id UUID PRIMARY KEY,
            slug VARCHAR(63) NOT NULL UNIQUE,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE users (
            id UUID PRIMARY KEY,
            email VARCHAR(254) NOT NULL UNIQUE,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE memberships (
            tenant_id UUID NOT NULL REFERENCES tenants (id),
            user_id UUID NOT NULL REFERENCES users (id),
            role VARCHAR(24) NOT NULL CHECK (role IN (
                'developer', 'tenant_admin', 'operations_admin', 'platform_admin', 'auditor'
            )),
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            PRIMARY KEY (tenant_id, user_id)
        )
    """)
    op.execute("""
        CREATE TABLE api_keys (
            id UUID PRIMARY KEY,
            tenant_id UUID NOT NULL,
            owner_id UUID NOT NULL,
            secret_hash BYTEA NOT NULL UNIQUE CHECK (octet_length(secret_hash) = 32),
            prefix VARCHAR(16) NOT NULL,
            suffix CHAR(4) NOT NULL,
            scopes TEXT[] NOT NULL CHECK (
                cardinality(scopes) > 0 AND scopes <@ ARRAY['search:text', 'search:image']::TEXT[]
            ),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ NOT NULL,
            revoked_at TIMESTAMPTZ,
            FOREIGN KEY (tenant_id, owner_id) REFERENCES memberships (tenant_id, user_id)
        )
    """)
    op.create_index("ix_api_keys_tenant_owner", "api_keys", ["tenant_id", "owner_id"])
    op.execute("""
        CREATE TABLE audit_events (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            tenant_id UUID NOT NULL REFERENCES tenants (id),
            actor_type VARCHAR(24) NOT NULL,
            action VARCHAR(48) NOT NULL,
            target_id UUID NOT NULL,
            details JSONB NOT NULL DEFAULT '{}',
            occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.create_index("ix_audit_tenant_time", "audit_events", ["tenant_id", "occurred_at"])


def downgrade():
    for table in ("audit_events", "api_keys", "memberships", "users", "tenants"):
        op.drop_table(table)
