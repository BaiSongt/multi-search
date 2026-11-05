"""为可轮换的浏览器凭据添加稳定的会话管理标识。"""

from alembic import op

revision = "0012_session_management"
down_revision = "0011_key_ip_allowlist"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE browser_sessions ADD COLUMN management_id UUID NOT NULL DEFAULT gen_random_uuid()
    """)
    op.execute("""
        CREATE UNIQUE INDEX ux_browser_sessions_active_management
        ON browser_sessions (tenant_id, user_id, management_id) WHERE revoked_at IS NULL
    """)
    op.create_index(
        "ix_browser_sessions_management_cursor",
        "browser_sessions",
        ["tenant_id", "user_id", "management_id", "created_at"],
    )


def downgrade():
    op.drop_index("ix_browser_sessions_management_cursor", table_name="browser_sessions")
    op.drop_index("ux_browser_sessions_active_management", table_name="browser_sessions")
    op.drop_column("browser_sessions", "management_id")
