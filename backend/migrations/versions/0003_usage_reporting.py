"""建立配额、Provider 策略和脱敏用量事件。"""

from alembic import op

revision = "0003_usage_reporting"
down_revision = "0002_identity_management"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE quota_policies (
            tenant_id UUID PRIMARY KEY REFERENCES tenants(id),
            quota JSONB NOT NULL CHECK (jsonb_typeof(quota) = 'object'),
            version INTEGER NOT NULL DEFAULT 1,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE provider_policies (
            provider_id VARCHAR(64) PRIMARY KEY,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            priority SMALLINT NOT NULL DEFAULT 0 CHECK (priority BETWEEN -100 AND 100),
            requests_per_minute SMALLINT NOT NULL DEFAULT 120 CHECK (requests_per_minute BETWEEN 1 AND 120),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE usage_events (
            id UUID PRIMARY KEY,
            tenant_id UUID NOT NULL REFERENCES tenants(id),
            principal_kind VARCHAR(12) NOT NULL CHECK (principal_kind IN ('key','session')),
            principal_id UUID NOT NULL,
            user_id UUID NOT NULL REFERENCES users(id),
            key_id UUID REFERENCES api_keys(id),
            request_id VARCHAR(128) NOT NULL,
            idempotency_key VARCHAR(128),
            idempotency_until TIMESTAMPTZ,
            request_hash CHAR(64),
            mode VARCHAR(12) NOT NULL,
            modalities TEXT[] NOT NULL,
            source_types TEXT[] NOT NULL,
            status VARCHAR(12) NOT NULL DEFAULT 'running'
                CHECK (status IN ('running','completed','partial','failed','cancelled')),
            code VARCHAR(64),
            elapsed_ms INTEGER,
            cache_hit BOOLEAN NOT NULL DEFAULT FALSE,
            text_results_returned INTEGER NOT NULL DEFAULT 0,
            image_results_returned INTEGER NOT NULL DEFAULT 0,
            image_sources_present INTEGER NOT NULL DEFAULT 0,
            pages_fetched INTEGER NOT NULL DEFAULT 0,
            research_jobs_started INTEGER NOT NULL DEFAULT 0,
            llm_tokens INTEGER NOT NULL DEFAULT 0,
            replay_payload JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            reservation_expires_at TIMESTAMPTZ NOT NULL
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX ux_usage_idempotency ON usage_events
        (tenant_id, principal_kind, principal_id, idempotency_key) WHERE idempotency_key IS NOT NULL
    """)
    op.create_index("ix_usage_tenant_created", "usage_events", ["tenant_id", "created_at", "id"])
    op.create_index("ix_usage_user_created", "usage_events", ["user_id", "created_at", "id"])
    op.create_index("ix_usage_created", "usage_events", ["created_at", "id"])
    op.execute("""
        CREATE INDEX ix_usage_running ON usage_events (tenant_id, reservation_expires_at)
        WHERE status = 'running'
    """)
    op.execute("""
        CREATE TABLE provider_runs (
            usage_id UUID NOT NULL REFERENCES usage_events(id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL,
            provider_id VARCHAR(64) NOT NULL,
            status VARCHAR(16) NOT NULL,
            elapsed_ms INTEGER NOT NULL,
            count INTEGER NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (usage_id, ordinal)
        )
    """)
    op.create_index("ix_provider_runs_created", "provider_runs", ["created_at", "provider_id"])


def downgrade():
    for table in ("provider_runs", "usage_events", "provider_policies", "quota_policies"):
        op.drop_table(table)
