"""Create the Phase 9B organization-scoped production schema.

Revision ID: 0001_phase9b
Revises:
Create Date: 2026-07-22
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0001_phase9b"
down_revision = None
branch_labels = None
depends_on = None


def _identity_columns() -> list[sa.Column]:
    return [
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "organization_id",
            sa.String(64),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("slug", sa.String(128), nullable=False, unique=True),
        sa.Column("display_name", sa.String(256), nullable=False),
        sa.Column("policy_version", sa.String(128), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
    )
    op.create_table(
        "users",
        *_identity_columns(),
        sa.Column("subject", sa.String(256), nullable=False),
        sa.Column("display_name", sa.String(256), nullable=False),
        sa.Column("email_hash", sa.String(64), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.UniqueConstraint("organization_id", "subject", name="uq_users_org_subject"),
        sa.UniqueConstraint("organization_id", "id", name="uq_users_org_id"),
    )
    op.create_table(
        "memberships",
        *_identity_columns(),
        sa.Column(
            "user_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("updated_at", sa.String(40), nullable=False),
        sa.CheckConstraint(
            "role IN ('org_admin','maintainer','reviewer','viewer')",
            name="ck_memberships_role",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["users.organization_id", "users.id"],
            name="fk_memberships_org_user",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("organization_id", "user_id", name="uq_memberships_org_user"),
    )
    op.create_table(
        "repositories",
        *_identity_columns(),
        sa.Column("alias", sa.String(256), nullable=False, unique=True),
        sa.Column("mode", sa.String(32), nullable=False, server_default="shadow"),
        sa.Column("budget_microusd", sa.BigInteger(), nullable=True),
        sa.Column("policy_version", sa.String(128), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.CheckConstraint(
            "mode IN ('shadow','guarded_publish')", name="ck_repositories_mode"
        ),
        sa.UniqueConstraint("organization_id", "id", name="uq_repositories_org_id"),
    )
    op.create_table(
        "repository_access",
        *_identity_columns(),
        sa.Column(
            "repository_id",
            sa.String(64),
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "repository_id"],
            ["repositories.organization_id", "repositories.id"],
            name="fk_repository_access_org_repo",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["users.organization_id", "users.id"],
            name="fk_repository_access_org_user",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "organization_id", "repository_id", "user_id", name="uq_repository_access"
        ),
    )
    op.create_table(
        "access_credentials",
        *_identity_columns(),
        sa.Column(
            "user_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("token_prefix", sa.String(16), nullable=False),
        sa.Column("expires_at", sa.String(40), nullable=False),
        sa.Column("revoked_at", sa.String(40), nullable=True),
        sa.Column("last_used_at", sa.String(40), nullable=True),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["users.organization_id", "users.id"],
            name="fk_access_credentials_org_user",
            ondelete="CASCADE",
        ),
    )
    op.create_table(
        "review_jobs",
        *_identity_columns(),
        sa.Column(
            "repository_id",
            sa.String(64),
            sa.ForeignKey("repositories.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("submitted_by", sa.String(64), nullable=False),
        sa.Column("correlation_id", sa.String(128), nullable=False),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("repository_alias", sa.String(256), nullable=False),
        sa.Column("source_ref", sa.String(256), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("source_bytes", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("started_at", sa.String(40), nullable=True),
        sa.Column("completed_at", sa.String(40), nullable=True),
        sa.Column("review_json", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.CheckConstraint(
            "state IN ('queued','running','succeeded','failed')",
            name="ck_review_jobs_state",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "repository_id"],
            ["repositories.organization_id", "repositories.id"],
            name="fk_review_jobs_org_repo",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("organization_id", "id", name="uq_review_jobs_org_id"),
    )
    op.create_index(
        "ix_review_jobs_org_repo_created",
        "review_jobs",
        ["organization_id", "repository_id", "created_at"],
    )
    op.create_table(
        "review_sessions",
        *_identity_columns(),
        sa.Column(
            "repository_id",
            sa.String(64),
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "review_job_id",
            sa.String(64),
            sa.ForeignKey("review_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("started_at", sa.String(40), nullable=False),
        sa.Column("last_active_at", sa.String(40), nullable=False),
        sa.Column("ended_at", sa.String(40), nullable=True),
        sa.ForeignKeyConstraint(
            ["organization_id", "repository_id"],
            ["repositories.organization_id", "repositories.id"],
            name="fk_review_sessions_org_repo",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "review_job_id"],
            ["review_jobs.organization_id", "review_jobs.id"],
            name="fk_review_sessions_org_job",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["users.organization_id", "users.id"],
            name="fk_review_sessions_org_user",
            ondelete="CASCADE",
        ),
    )
    op.create_table(
        "findings",
        *_identity_columns(),
        sa.Column(
            "repository_id",
            sa.String(64),
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "review_job_id",
            sa.String(64),
            sa.ForeignKey("review_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("fingerprint", sa.String(128), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("path", sa.String(512), nullable=True),
        sa.Column("line", sa.Integer(), nullable=True),
        sa.Column("severity", sa.String(32), nullable=True),
        sa.Column("category", sa.String(128), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "repository_id"],
            ["repositories.organization_id", "repositories.id"],
            name="fk_findings_org_repo",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "review_job_id"],
            ["review_jobs.organization_id", "review_jobs.id"],
            name="fk_findings_org_job",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "review_job_id",
            "fingerprint",
            "content_sha256",
            name="uq_findings_version",
        ),
        sa.UniqueConstraint("organization_id", "id", name="uq_findings_org_id"),
    )
    op.create_table(
        "finding_feedback",
        *_identity_columns(),
        sa.Column(
            "repository_id",
            sa.String(64),
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "finding_id",
            sa.String(64),
            sa.ForeignKey("findings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "principal_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(64), nullable=True),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.CheckConstraint(
            "decision IN ('accepted','rejected')", name="ck_finding_feedback_decision"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "repository_id"],
            ["repositories.organization_id", "repositories.id"],
            name="fk_finding_feedback_org_repo",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "finding_id"],
            ["findings.organization_id", "findings.id"],
            name="fk_finding_feedback_org_finding",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "principal_id"],
            ["users.organization_id", "users.id"],
            name="fk_finding_feedback_org_principal",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "organization_id", "finding_id", "principal_id", name="uq_finding_feedback_actor"
        ),
    )
    op.create_table(
        "approvals",
        *_identity_columns(),
        sa.Column(
            "repository_id",
            sa.String(64),
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "finding_id",
            sa.String(64),
            sa.ForeignKey("findings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "principal_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("policy_version", sa.String(128), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.CheckConstraint(
            "decision IN ('approved','rejected')", name="ck_approvals_decision"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "repository_id"],
            ["repositories.organization_id", "repositories.id"],
            name="fk_approvals_org_repo",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "finding_id"],
            ["findings.organization_id", "findings.id"],
            name="fk_approvals_org_finding",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "principal_id"],
            ["users.organization_id", "users.id"],
            name="fk_approvals_org_principal",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("organization_id", "finding_id", name="uq_approvals_finding"),
    )
    op.create_table(
        "audit_events",
        *_identity_columns(),
        sa.Column("principal_id", sa.String(64), nullable=False),
        sa.Column("repository_id", sa.String(64), nullable=True),
        sa.Column("credential_id", sa.String(64), nullable=True),
        sa.Column("auth_method", sa.String(32), nullable=False),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=False),
        sa.Column("resource_id", sa.String(128), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=True),
        sa.Column("policy_version", sa.String(128), nullable=False),
        sa.Column("occurred_at_utc", sa.String(40), nullable=False),
        sa.Column("correlation_id", sa.String(128), nullable=False),
        sa.CheckConstraint(
            "decision IN ('allow','deny','error')", name="ck_audit_events_decision"
        ),
    )
    op.create_index(
        "ix_audit_events_org_time",
        "audit_events",
        ["organization_id", "occurred_at_utc", "id"],
    )
    op.create_table(
        "webhook_deliveries",
        *_identity_columns(),
        sa.Column(
            "repository_id",
            sa.String(64),
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "review_job_id",
            sa.String(64),
            sa.ForeignKey("review_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("delivery_id", sa.String(128), nullable=False, unique=True),
        sa.Column("event", sa.String(64), nullable=False),
        sa.Column("received_at", sa.String(40), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "repository_id"],
            ["repositories.organization_id", "repositories.id"],
            name="fk_webhook_deliveries_org_repo",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "review_job_id"],
            ["review_jobs.organization_id", "review_jobs.id"],
            name="fk_webhook_deliveries_org_job",
            ondelete="CASCADE",
        ),
    )
    op.create_table(
        "provider_usage",
        *_identity_columns(),
        sa.Column(
            "repository_id",
            sa.String(64),
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "review_job_id",
            sa.String(64),
            sa.ForeignKey("review_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cost_microusd", sa.BigInteger(), nullable=True),
        sa.Column("pricing_version", sa.String(128), nullable=True),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "repository_id"],
            ["repositories.organization_id", "repositories.id"],
            name="fk_provider_usage_org_repo",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "review_job_id"],
            ["review_jobs.organization_id", "review_jobs.id"],
            name="fk_provider_usage_org_job",
            ondelete="CASCADE",
        ),
    )


def downgrade() -> None:
    for table in (
        "provider_usage",
        "webhook_deliveries",
        "audit_events",
        "approvals",
        "finding_feedback",
        "findings",
        "review_sessions",
        "review_jobs",
        "access_credentials",
        "repository_access",
        "repositories",
        "memberships",
        "users",
        "organizations",
    ):
        op.drop_table(table)
