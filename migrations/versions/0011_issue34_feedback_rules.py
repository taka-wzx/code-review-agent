"""Add versioned repository feedback rules and evaluation bindings.

Revision ID: 0011_issue34_feedback_rules
Revises: 0010_issue33_github_webhook
Create Date: 2026-08-09
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0011_issue34_feedback_rules"
down_revision = "0010_issue33_github_webhook"
branch_labels = None
depends_on = None


def _repository_scope_constraint(name: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["organization_id", "repository_id"],
        ["repositories.organization_id", "repositories.id"],
        name=name,
        ondelete="CASCADE",
    )


def upgrade() -> None:
    op.create_table(
        "repository_feedback_rule_versions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("organization_id", sa.String(64), nullable=False),
        sa.Column("repository_id", sa.String(64), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("rules_json", sa.Text(), nullable=False),
        sa.Column("rules_sha256", sa.String(64), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(512), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        _repository_scope_constraint("fk_feedback_rule_versions_repository"),
        sa.UniqueConstraint(
            "organization_id",
            "repository_id",
            "version",
            name="uq_feedback_rule_version_scope",
        ),
        sa.CheckConstraint(
            "length(rules_sha256) = 64", name="ck_feedback_rule_version_sha256"
        ),
        sa.CheckConstraint("length(reason) > 0", name="ck_feedback_rule_version_reason"),
    )
    op.create_index(
        "ix_feedback_rule_versions_scope_created",
        "repository_feedback_rule_versions",
        ["organization_id", "repository_id", "created_at"],
    )

    op.create_table(
        "repository_feedback_rule_active",
        sa.Column("organization_id", sa.String(64), primary_key=True),
        sa.Column("repository_id", sa.String(64), primary_key=True),
        sa.Column("version_id", sa.String(64), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("activated_by", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(512), nullable=False),
        sa.Column("activated_at", sa.String(40), nullable=False),
        _repository_scope_constraint("fk_feedback_rule_active_repository"),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["repository_feedback_rule_versions.id"],
            name="fk_feedback_rule_active_version",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("generation >= 1", name="ck_feedback_rule_active_generation"),
    )

    op.create_table(
        "repository_feedback_rule_receipts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("organization_id", sa.String(64), nullable=False),
        sa.Column("repository_id", sa.String(64), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("from_version_id", sa.String(64), nullable=True),
        sa.Column("from_version", sa.String(64), nullable=True),
        sa.Column("from_rules_sha256", sa.String(64), nullable=True),
        sa.Column("to_version_id", sa.String(64), nullable=False),
        sa.Column("to_version", sa.String(64), nullable=False),
        sa.Column("to_rules_sha256", sa.String(64), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("principal_id", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(512), nullable=False),
        sa.Column("occurred_at", sa.String(40), nullable=False),
        sa.Column("receipt_json", sa.Text(), nullable=False),
        sa.Column("receipt_sha256", sa.String(64), nullable=False, unique=True),
        _repository_scope_constraint("fk_feedback_rule_receipts_repository"),
        sa.ForeignKeyConstraint(
            ["from_version_id"],
            ["repository_feedback_rule_versions.id"],
            name="fk_feedback_rule_receipt_from_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["to_version_id"],
            ["repository_feedback_rule_versions.id"],
            name="fk_feedback_rule_receipt_to_version",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "action IN ('activate','rollback')", name="ck_feedback_rule_receipt_action"
        ),
        sa.CheckConstraint("generation >= 1", name="ck_feedback_rule_receipt_generation"),
        sa.CheckConstraint(
            "length(receipt_sha256) = 64", name="ck_feedback_rule_receipt_sha256"
        ),
    )
    op.create_index(
        "ix_feedback_rule_receipts_scope_generation",
        "repository_feedback_rule_receipts",
        ["organization_id", "repository_id", "generation"],
        unique=True,
    )

    op.create_table(
        "review_feedback_rule_bindings",
        sa.Column("review_job_id", sa.String(64), primary_key=True),
        sa.Column("organization_id", sa.String(64), nullable=False),
        sa.Column("repository_id", sa.String(64), nullable=False),
        sa.Column("version_id", sa.String(64), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("rules_json", sa.Text(), nullable=False),
        sa.Column("rules_sha256", sa.String(64), nullable=False),
        sa.Column("bound_at", sa.String(40), nullable=False),
        _repository_scope_constraint("fk_review_feedback_rule_binding_repository"),
        sa.ForeignKeyConstraint(
            ["review_job_id"],
            ["review_jobs.id"],
            name="fk_review_feedback_rule_binding_job",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["repository_feedback_rule_versions.id"],
            name="fk_review_feedback_rule_binding_version",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "generation >= 1", name="ck_review_feedback_rule_binding_generation"
        ),
        sa.CheckConstraint(
            "length(rules_sha256) = 64", name="ck_review_feedback_rule_binding_sha256"
        ),
    )
    op.create_index(
        "ix_review_feedback_rule_binding_scope",
        "review_feedback_rule_bindings",
        ["organization_id", "repository_id", "version", "generation"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_review_feedback_rule_binding_scope",
        table_name="review_feedback_rule_bindings",
    )
    op.drop_table("review_feedback_rule_bindings")
    op.drop_index(
        "ix_feedback_rule_receipts_scope_generation",
        table_name="repository_feedback_rule_receipts",
    )
    op.drop_table("repository_feedback_rule_receipts")
    op.drop_table("repository_feedback_rule_active")
    op.drop_index(
        "ix_feedback_rule_versions_scope_created",
        table_name="repository_feedback_rule_versions",
    )
    op.drop_table("repository_feedback_rule_versions")
