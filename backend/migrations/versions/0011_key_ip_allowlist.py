"""为机器密钥持久化可选的客户端 IP 网络限制。"""

from alembic import op

revision = "0011_key_ip_allowlist"
down_revision = "0010_model_cost_precision"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE api_keys ADD COLUMN ip_allowlist CIDR[] NOT NULL DEFAULT '{}'::CIDR[],
            ADD CONSTRAINT ck_api_keys_ip_allowlist CHECK (
                cardinality(ip_allowlist) <= 32 AND array_position(ip_allowlist, NULL) IS NULL
            )
    """)


def downgrade():
    op.execute("LOCK TABLE api_keys IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM api_keys
                WHERE cardinality(ip_allowlist) > 0 AND revoked_at IS NULL
                    AND coalesce(rotation_expires_at, expires_at) > now()
            ) THEN
                RAISE EXCEPTION 'revoke active IP-restricted API keys before downgrading';
            END IF;
        END $$
    """)
    op.drop_constraint("ck_api_keys_ip_allowlist", "api_keys", type_="check")
    op.drop_column("api_keys", "ip_allowlist")
