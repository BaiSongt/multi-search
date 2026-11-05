"""持久化证据、图片归属和可恢复任务。"""

from alembic import op

revision = "0006_evidence_jobs"
down_revision = "0005_operations"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE api_keys DROP CONSTRAINT api_keys_scopes_check,
            ADD CONSTRAINT api_keys_scopes_check CHECK (cardinality(scopes) > 0 AND
                scopes <@ ARRAY['search:text','search:image','fetch','research']::TEXT[]);
    """)
    op.execute("""
        ALTER TABLE usage_events ADD COLUMN operation TEXT NOT NULL DEFAULT 'search'
            CHECK (operation IN ('search','fetch','research'));
    """)
    op.execute("""
        CREATE TABLE evidence_records (
            tenant_id UUID NOT NULL REFERENCES tenants(id),
            evidence_id TEXT NOT NULL,
            usage_id UUID NOT NULL REFERENCES usage_events(id) ON DELETE CASCADE,
            record JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, evidence_id)
        );
    """)
    op.execute("""
        CREATE TABLE image_observations (
            id UUID PRIMARY KEY,
            usage_id UUID NOT NULL REFERENCES usage_events(id) ON DELETE CASCADE,
            tenant_id UUID NOT NULL REFERENCES tenants(id),
            source_page_url TEXT,
            original_url TEXT NOT NULL,
            thumbnail_url TEXT,
            rights_status TEXT NOT NULL CHECK (rights_status IN
                ('unknown','declared_unverified','verified_license','conflict')),
            source_page_verified BOOLEAN NOT NULL,
            retrieved_at TIMESTAMPTZ NOT NULL,
            rights_checked_at TIMESTAMPTZ,
            metadata JSONB NOT NULL
        );
    """)
    op.execute("""
        CREATE INDEX ix_image_observations_tenant_time
            ON image_observations (tenant_id, retrieved_at DESC, id DESC);
    """)
    op.execute("""
        CREATE INDEX ix_image_observations_time
            ON image_observations (retrieved_at DESC, id DESC);
    """)
    op.execute("""
        CREATE TABLE search_jobs (
            id UUID PRIMARY KEY REFERENCES usage_events(id) ON DELETE CASCADE,
            tenant_id UUID NOT NULL REFERENCES tenants(id),
            owner_id UUID NOT NULL REFERENCES users(id),
            principal_id UUID NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('fetch','research')),
            status TEXT NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued','running','cancelling','completed','partial','failed','cancelled')),
            idempotency_key TEXT,
            request_hash CHAR(64) NOT NULL,
            payload JSONB NOT NULL,
            progress JSONB NOT NULL,
            result JSONB,
            error JSONB,
            attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            max_attempts INTEGER NOT NULL DEFAULT 2 CHECK (max_attempts BETWEEN 1 AND 3),
            lease_token UUID,
            lease_expires_at TIMESTAMPTZ,
            deadline_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """)
    op.execute("""
        CREATE UNIQUE INDEX ux_search_jobs_idempotency ON search_jobs
            (tenant_id, principal_id, kind, idempotency_key) WHERE idempotency_key IS NOT NULL;
    """)
    op.execute("""
        CREATE INDEX ix_search_jobs_active ON search_jobs (status, created_at, id)
            WHERE status IN ('queued','running','cancelling');
    """)
    op.execute("""
        CREATE INDEX ix_search_jobs_tenant_created ON search_jobs (tenant_id, created_at DESC, id DESC);
    """)


def downgrade():
    for table in ("search_jobs", "image_observations", "evidence_records"):
        op.drop_table(table)
    op.execute("""
        ALTER TABLE usage_events DROP COLUMN operation;
    """)
    op.execute("""
        UPDATE api_keys SET scopes = array_remove(array_remove(scopes, 'fetch'), 'research')
            WHERE scopes && ARRAY['fetch','research']::TEXT[]
                AND scopes && ARRAY['search:text','search:image']::TEXT[];
    """)
    op.execute("""
        UPDATE api_keys SET scopes = ARRAY['search:text']::TEXT[], revoked_at = COALESCE(revoked_at, now())
            WHERE scopes && ARRAY['fetch','research']::TEXT[];
    """)
    op.execute("""
        ALTER TABLE api_keys DROP CONSTRAINT api_keys_scopes_check,
            ADD CONSTRAINT api_keys_scopes_check CHECK (cardinality(scopes) > 0 AND
                scopes <@ ARRAY['search:text','search:image']::TEXT[]);
    """)
