"""Add a hash-only durable GitHub sandbox publication ledger.

Revision ID: 0008_phase11b_github_canary
Revises: 0007_phase11a_repair
Create Date: 2026-07-28
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0008_phase11b_github_canary"
down_revision = "0007_phase11a_repair"
branch_labels = None
depends_on = None


_STATES = (
    "publish_intent_recorded",
    "branch_push_requested",
    "branch_push_observed",
    "draft_pr_requested",
    "draft_pr_observed",
    "receipt_reconciled",
    "quarantined",
)
_FAILURES = (
    "auth_401",
    "permission_403",
    "missing_404",
    "conflict_409",
    "validation_422",
    "rate_limited",
    "server_5xx",
    "timeout",
    "base_drift",
    "ref_collision",
    "branch_protected",
    "token_revoked",
    "token_expired",
    "ambiguous_result",
    "receipt_mismatch",
    "repository_mismatch",
    "installation_mismatch",
    "authorization_expired",
    "authorization_mismatch",
    "endpoint_denied",
    "redirect_denied",
    "budget_exhausted",
    "other",
)
_ENDPOINTS = (
    "repository_read",
    "ref_read",
    "blob_read",
    "tree_read",
    "commit_read",
    "blob_create",
    "tree_create",
    "commit_create",
    "ref_create",
    "draft_pr_list",
    "draft_pr_create",
    "draft_pr_read",
)


def upgrade() -> None:
    op.create_table(
        "github_canary_authorization_budgets",
        sa.Column("authorization_id", sa.String(128), primary_key=True),
        sa.Column("authorization_sha256", sa.String(64), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("mutation_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("read_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("branch_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("commit_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("draft_pr_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.CheckConstraint(
            "request_count >= 0 AND mutation_count >= 0 AND read_count >= 0 "
            "AND branch_count >= 0 AND commit_count >= 0 AND draft_pr_count >= 0",
            name="ck_github_canary_authorization_budget_counts",
        ),
    )
    op.create_table(
        "github_canary_approvals",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("repair_job_id", sa.String(96), nullable=False),
        sa.Column("organization_id", sa.String(96), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("binding_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("approver_sha256", sa.String(64), nullable=True),
        sa.Column("decided_at", sa.Float(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["repair_job_id"], ["repair_jobs.id"], ondelete="CASCADE"),
        sa.CheckConstraint("kind IN ('write','draft_pr')", name="ck_github_canary_approval_kind"),
        sa.CheckConstraint(
            "status IN ('issued','consumed','rejected')",
            name="ck_github_canary_approval_status",
        ),
    )
    op.create_index(
        "ix_github_canary_approval_job",
        "github_canary_approvals",
        ["repair_job_id", "kind", "status"],
    )
    op.create_table(
        "github_canary_publications",
        sa.Column("idempotency_key", sa.String(128), primary_key=True),
        sa.Column("repair_job_id", sa.String(96), nullable=False),
        sa.Column("authorization_id", sa.String(128), nullable=False),
        sa.Column("canary_case_id", sa.String(32), nullable=False),
        sa.Column("authorization_sha256", sa.String(64), nullable=False),
        sa.Column("binding_sha256", sa.String(64), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("real_github_writes", sa.Boolean(), nullable=False),
        sa.Column("state", sa.String(40), nullable=False),
        sa.Column("failure_code", sa.String(40), nullable=True),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("mutation_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("read_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("branch_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("commit_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("draft_pr_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("receipt_sha256", sa.String(64), nullable=True),
        sa.Column("branch_sha256", sa.String(64), nullable=True),
        sa.Column("commit_sha", sa.String(40), nullable=True),
        sa.Column("draft_pr_sha256", sa.String(64), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["repair_job_id"], ["repair_jobs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "authorization_id", "canary_case_id", name="uq_github_canary_authorization_case"
        ),
        sa.CheckConstraint(
            "state IN (" + ", ".join(repr(state) for state in _STATES) + ")",
            name="ck_github_canary_publication_state",
        ),
        sa.CheckConstraint(
            "canary_case_id IN ('normal','crash_after_branch','crash_after_draft_pr')",
            name="ck_github_canary_publication_case",
        ),
        sa.CheckConstraint(
            "failure_code IS NULL OR failure_code IN ("
            + ", ".join(repr(code) for code in _FAILURES)
            + ")",
            name="ck_github_canary_publication_failure",
        ),
        sa.CheckConstraint(
            "request_count >= 0 AND mutation_count >= 0 AND read_count >= 0 "
            "AND branch_count >= 0 AND commit_count >= 0 AND draft_pr_count >= 0",
            name="ck_github_canary_publication_counts",
        ),
    )
    op.create_index(
        "ix_github_canary_publication_job",
        "github_canary_publications",
        ["repair_job_id", "created_at"],
    )
    op.create_table(
        "github_canary_requests",
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_index", sa.Integer(), nullable=False),
        sa.Column("operation_key", sa.String(160), nullable=False),
        sa.Column("endpoint", sa.String(32), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("is_mutation", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("failure_code", sa.String(40), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("response_sha256", sa.String(64), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["idempotency_key"],
            ["github_canary_publications.idempotency_key"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("idempotency_key", "request_index"),
        sa.UniqueConstraint(
            "idempotency_key", "operation_key", name="uq_github_canary_request_operation"
        ),
        sa.CheckConstraint("request_index >= 1", name="ck_github_canary_request_index"),
        sa.CheckConstraint(
            "status IN ('requested','observed','ambiguous','failed')",
            name="ck_github_canary_request_status",
        ),
        sa.CheckConstraint(
            "endpoint IN (" + ", ".join(repr(endpoint) for endpoint in _ENDPOINTS) + ")",
            name="ck_github_canary_request_endpoint",
        ),
        sa.CheckConstraint(
            "failure_code IS NULL OR failure_code IN ("
            + ", ".join(repr(code) for code in _FAILURES)
            + ")",
            name="ck_github_canary_request_failure",
        ),
        sa.CheckConstraint(
            "http_status IS NULL OR (http_status >= 100 AND http_status <= 599)",
            name="ck_github_canary_request_http_status",
        ),
    )


def downgrade() -> None:
    op.drop_table("github_canary_requests")
    op.drop_index(
        "ix_github_canary_publication_job", table_name="github_canary_publications"
    )
    op.drop_table("github_canary_publications")
    op.drop_index("ix_github_canary_approval_job", table_name="github_canary_approvals")
    op.drop_table("github_canary_approvals")
    op.drop_table("github_canary_authorization_budgets")
