"""补充浏览器会话、密钥属性和审计操作者。"""

from alembic import op

revision = "0002_identity_management"
down_revision = "0001_identity"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE users ADD COLUMN password_hash VARCHAR(160)")
    op.execute("""
        ALTER TABLE api_keys
        ADD COLUMN name VARCHAR(80) NOT NULL DEFAULT 'Legacy key',
        ADD COLUMN environment VARCHAR(4) NOT NULL DEFAULT 'dev'
            CHECK (environment IN ('dev', 'test', 'prod')),
        ADD COLUMN limits JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(limits) = 'object'),
        ADD COLUMN last_used_at TIMESTAMPTZ,
        ADD COLUMN rotation_expires_at TIMESTAMPTZ
            CHECK (rotation_expires_at <= expires_at)
    """)
    op.execute("ALTER TABLE audit_events ADD COLUMN actor_id UUID REFERENCES users (id)")
    op.execute("""
        CREATE TABLE browser_sessions (
            id UUID PRIMARY KEY,
            tenant_id UUID NOT NULL,
            user_id UUID NOT NULL,
            secret_hash BYTEA NOT NULL CHECK (octet_length(secret_hash) = 32),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ NOT NULL,
            absolute_expires_at TIMESTAMPTZ NOT NULL,
            revoked_at TIMESTAMPTZ,
            FOREIGN KEY (tenant_id, user_id) REFERENCES memberships (tenant_id, user_id),
            CHECK (expires_at <= absolute_expires_at)
        )
    """)
    op.create_index("ix_browser_sessions_expiration", "browser_sessions", ["absolute_expires_at"])
    op.create_index("ix_api_keys_tenant_cursor", "api_keys", ["tenant_id", "created_at", "id"])


def downgrade():
    op.drop_index("ix_api_keys_tenant_cursor", table_name="api_keys")
    op.drop_table("browser_sessions")
    op.execute("ALTER TABLE audit_events DROP COLUMN actor_id")
    op.execute("""
        ALTER TABLE api_keys
        DROP COLUMN name,
        DROP COLUMN environment,
        DROP COLUMN limits,
        DROP COLUMN last_used_at,
        DROP COLUMN rotation_expires_at
    """)
    op.execute("ALTER TABLE users DROP COLUMN password_hash")
