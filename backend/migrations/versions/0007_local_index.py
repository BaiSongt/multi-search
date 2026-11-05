"""为获准的租户内文档建立本地索引。"""

from alembic import op

revision = "0007_local_index"
down_revision = "0006_evidence_jobs"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE local_documents (
            tenant_id UUID NOT NULL REFERENCES tenants(id),
            id TEXT NOT NULL CHECK (id ~ '^[0-9a-f]{64}$'),
            title TEXT NOT NULL CHECK (char_length(title) BETWEEN 1 AND 512),
            plain_text TEXT NOT NULL CHECK (octet_length(plain_text) BETWEEN 1 AND 65536),
            source_url TEXT CHECK (char_length(source_url) <= 2048),
            source_domain TEXT CHECK (char_length(source_domain) <= 253),
            language TEXT CHECK (char_length(language) <= 35),
            published_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            search_vector TSVECTOR GENERATED ALWAYS AS (
                setweight(to_tsvector('simple'::regconfig, title), 'A') ||
                setweight(to_tsvector('simple'::regconfig, plain_text), 'B')
            ) STORED,
            PRIMARY KEY (tenant_id, id)
        );
    """)
    op.execute("CREATE INDEX ix_local_documents_text ON local_documents USING GIN (search_vector);")


def downgrade():
    op.drop_table("local_documents")
