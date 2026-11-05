"""限定近期审计风险信号的索引范围。"""

from alembic import op

revision = "0013_audit_risk_index"
down_revision = "0012_session_management"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE INDEX ix_audit_risk_time ON audit_events (occurred_at DESC, id DESC)
        WHERE action IN (
            'member.update', 'invitation.accept', 'user.provision', 'key.issue', 'tenant.update'
        )
    """)


def downgrade():
    op.drop_index("ix_audit_risk_time", table_name="audit_events")
