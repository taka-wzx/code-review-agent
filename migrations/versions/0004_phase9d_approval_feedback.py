"""Add guarded publication, Finding lineage, and feedback metric storage.

Revision ID: 0004_phase9d
Revises: 0003_phase9c_durable_queue
Create Date: 2026-07-24
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0004_phase9d"
down_revision = "0003_phase9c_durable_queue"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("findings") as batch:
        batch.add_column(sa.Column("evidence_sha256", sa.String(64), nullable=True))
        batch.add_column(sa.Column("source_revision", sa.String(64), nullable=True))
    op.execute("UPDATE findings SET evidence_sha256=content_sha256")
    op.execute(
        "UPDATE findings SET source_revision=(SELECT COALESCE(j.head_sha, j.source_sha256) "
        "FROM review_jobs j WHERE j.id=findings.review_job_id)"
    )
    with op.batch_alter_table("findings") as batch:
        batch.alter_column("evidence_sha256", existing_type=sa.String(64), nullable=False)
        batch.alter_column("source_revision", existing_type=sa.String(64), nullable=False)
    op.create_index(
        "ix_findings_org_job_lineage",
        "findings",
        ["organization_id", "review_job_id", "content_sha256", "evidence_sha256"],
    )

    with op.batch_alter_table("finding_feedback") as batch:
        batch.drop_constraint("ck_finding_feedback_decision", type_="check")
        batch.add_column(sa.Column("finding_hash", sa.String(64), nullable=True))
        batch.alter_column("reason", existing_type=sa.String(64), type_=sa.String(512))
    op.execute(
        "UPDATE finding_feedback SET finding_hash=(SELECT content_sha256 FROM findings "
        "WHERE findings.id=finding_feedback.finding_id)"
    )
    with op.batch_alter_table("finding_feedback") as batch:
        batch.alter_column("finding_hash", existing_type=sa.String(64), nullable=False)
        batch.create_check_constraint(
            "ck_finding_feedback_decision",
            "decision IN ('accepted','rejected','uncertain','fixed','duplicate')",
        )

    op.create_table(
        "publish_proposals",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("organization_id", sa.String(64), nullable=False),
        sa.Column("repository_id", sa.String(64), nullable=False),
        sa.Column("review_job_id", sa.String(64), nullable=False),
        sa.Column("head_sha", sa.String(64), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("finding_set_sha256", sa.String(64), nullable=False),
        sa.Column("policy_version", sa.String(128), nullable=False),
        sa.Column("nonce", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.String(40), nullable=False),
        sa.Column("invalidated_at", sa.String(40), nullable=True),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "repository_id"],
            ["repositories.organization_id", "repositories.id"],
            name="fk_publish_proposals_org_repo",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "review_job_id"],
            ["review_jobs.organization_id", "review_jobs.id"],
            name="fk_publish_proposals_org_job",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "review_job_id",
            "payload_sha256",
            "policy_version",
            name="uq_publish_proposals_binding",
        ),
    )
    op.create_index(
        "ix_publish_proposals_pending",
        "publish_proposals",
        ["organization_id", "review_job_id", "invalidated_at", "expires_at"],
    )

    op.create_table(
        "publish_approvals",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("organization_id", sa.String(64), nullable=False),
        sa.Column("repository_id", sa.String(64), nullable=False),
        sa.Column("review_job_id", sa.String(64), nullable=False),
        sa.Column("proposal_id", sa.String(64), nullable=False),
        sa.Column("principal_id", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("head_sha", sa.String(64), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("policy_version", sa.String(128), nullable=False),
        sa.Column("nonce", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.String(40), nullable=False),
        sa.Column("used_at", sa.String(40), nullable=True),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "repository_id"],
            ["repositories.organization_id", "repositories.id"],
            name="fk_publish_approvals_org_repo",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "review_job_id"],
            ["review_jobs.organization_id", "review_jobs.id"],
            name="fk_publish_approvals_org_job",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "principal_id"],
            ["users.organization_id", "users.id"],
            name="fk_publish_approvals_org_principal",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["proposal_id"], ["publish_proposals.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "decision IN ('approved','rejected')", name="ck_publish_approvals_decision"
        ),
        sa.UniqueConstraint("organization_id", "review_job_id", name="uq_publish_approvals_job"),
    )

    op.create_table(
        "publish_attempts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("organization_id", sa.String(64), nullable=False),
        sa.Column("repository_id", sa.String(64), nullable=False),
        sa.Column("review_job_id", sa.String(64), nullable=False),
        sa.Column("approval_id", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("receipt_sha256", sa.String(64), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("completed_at", sa.String(40), nullable=True),
        sa.ForeignKeyConstraint(
            ["organization_id", "repository_id"],
            ["repositories.organization_id", "repositories.id"],
            name="fk_publish_attempts_org_repo",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "review_job_id"],
            ["review_jobs.organization_id", "review_jobs.id"],
            name="fk_publish_attempts_org_job",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["approval_id"], ["publish_approvals.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "status IN ('prepared','succeeded','failed')", name="ck_publish_attempts_status"
        ),
        sa.UniqueConstraint("organization_id", "review_job_id", name="uq_publish_attempts_job"),
        sa.UniqueConstraint("idempotency_key", name="uq_publish_attempts_idempotency"),
    )

    op.create_table(
        "metric_events",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("organization_id", sa.String(64), nullable=False),
        sa.Column("repository_id", sa.String(64), nullable=False),
        sa.Column("review_job_id", sa.String(64), nullable=True),
        sa.Column("finding_id", sa.String(64), nullable=True),
        sa.Column("approval_id", sa.String(64), nullable=True),
        sa.Column("principal_id", sa.String(64), nullable=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("subject_sha256", sa.String(64), nullable=True),
        sa.Column("occurred_at", sa.String(40), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "repository_id"],
            ["repositories.organization_id", "repositories.id"],
            name="fk_metric_events_org_repo",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "review_job_id"],
            ["review_jobs.organization_id", "review_jobs.id"],
            name="fk_metric_events_org_job",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_metric_events_org_repo_time",
        "metric_events",
        ["organization_id", "repository_id", "occurred_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_metric_events_org_repo_time", table_name="metric_events")
    op.drop_table("metric_events")
    op.drop_table("publish_attempts")
    op.drop_table("publish_approvals")
    op.drop_index("ix_publish_proposals_pending", table_name="publish_proposals")
    op.drop_table("publish_proposals")
    with op.batch_alter_table("finding_feedback") as batch:
        batch.drop_constraint("ck_finding_feedback_decision", type_="check")
        batch.drop_column("finding_hash")
        batch.alter_column("reason", existing_type=sa.String(512), type_=sa.String(64))
        batch.create_check_constraint(
            "ck_finding_feedback_decision", "decision IN ('accepted','rejected')"
        )
    op.drop_index("ix_findings_org_job_lineage", table_name="findings")
    with op.batch_alter_table("findings") as batch:
        batch.drop_column("source_revision")
        batch.drop_column("evidence_sha256")
