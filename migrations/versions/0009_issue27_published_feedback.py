"""Bind developer feedback to published finding versions.

Revision ID: 0009_issue27_published_feedback
Revises: 0008_phase11b_github_canary
Create Date: 2026-08-07
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0009_issue27_published_feedback"
down_revision = "0008_phase11b_github_canary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("finding_feedback") as batch:
        batch.add_column(sa.Column("publish_approval_id", sa.String(64), nullable=True))
        batch.add_column(sa.Column("published_payload_sha256", sa.String(64), nullable=True))
        batch.add_column(sa.Column("published_head_sha", sa.String(64), nullable=True))
        batch.add_column(sa.Column("published_finding_sha256", sa.String(64), nullable=True))
        batch.create_foreign_key(
            "fk_finding_feedback_publish_approval",
            "publish_approvals",
            ["publish_approval_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_check_constraint(
            "ck_finding_feedback_published_payload_sha256",
            "published_payload_sha256 IS NULL OR length(published_payload_sha256) = 64",
        )
        batch.create_check_constraint(
            "ck_finding_feedback_published_head_sha",
            "published_head_sha IS NULL OR length(published_head_sha) BETWEEN 40 AND 64",
        )
        batch.create_check_constraint(
            "ck_finding_feedback_published_finding_sha256",
            "published_finding_sha256 IS NULL OR length(published_finding_sha256) = 64",
        )
    op.create_index(
        "ix_finding_feedback_published_identity",
        "finding_feedback",
        ["organization_id", "published_finding_sha256"],
    )


def downgrade() -> None:
    op.drop_index("ix_finding_feedback_published_identity", table_name="finding_feedback")
    with op.batch_alter_table("finding_feedback") as batch:
        batch.drop_constraint("ck_finding_feedback_published_finding_sha256", type_="check")
        batch.drop_constraint("ck_finding_feedback_published_head_sha", type_="check")
        batch.drop_constraint("ck_finding_feedback_published_payload_sha256", type_="check")
        batch.drop_constraint("fk_finding_feedback_publish_approval", type_="foreignkey")
        batch.drop_column("published_finding_sha256")
        batch.drop_column("published_head_sha")
        batch.drop_column("published_payload_sha256")
        batch.drop_column("publish_approval_id")
