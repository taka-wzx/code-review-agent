"""Add PostgreSQL-durable synthetic Repair state for Phase 11A.

Revision ID: 0007_phase11a_repair
Revises: 0006_phase9f_metrics
Create Date: 2026-07-27
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0007_phase11a_repair"
down_revision = "0006_phase9f_metrics"
branch_labels = None
depends_on = None


_REPAIR_STATES = (
    "queued_plan",
    "planning",
    "awaiting_write_approval",
    "queued_execution",
    "executing",
    "awaiting_draft_pr_approval",
    "queued_publish",
    "publishing",
    "draft_published",
    "declined",
    "failed",
    "quarantined",
)


def upgrade() -> None:
    op.create_table(
        "repair_jobs",
        sa.Column("id", sa.String(96), primary_key=True),
        sa.Column("organization_id", sa.String(96), nullable=False),
        sa.Column("repository_id", sa.String(96), nullable=False),
        sa.Column("finding_sha256", sa.String(64), nullable=False),
        sa.Column("base_sha", sa.String(64), nullable=False),
        sa.Column("head_sha", sa.String(64), nullable=False),
        sa.Column("state", sa.String(48), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("checkpoint_json", sa.Text(), nullable=False),
        sa.Column("checkpoint_sha256", sa.String(64), nullable=False),
        sa.Column("current_diff_sha256", sa.String(64), nullable=True),
        sa.Column("budget_sha256", sa.String(64), nullable=False),
        sa.Column("lease_owner", sa.String(128), nullable=True),
        sa.Column("lease_token", sa.String(128), nullable=True),
        sa.Column("lease_expires_at", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("failure_code", sa.String(96), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_repair_jobs_version"),
        sa.CheckConstraint("attempt >= 1", name="ck_repair_jobs_attempt"),
        sa.CheckConstraint(
            "state IN (" + ", ".join(repr(state) for state in _REPAIR_STATES) + ")",
            name="ck_repair_jobs_state",
        ),
    )
    op.create_index(
        "ix_repair_jobs_claim", "repair_jobs", ["state", "lease_expires_at", "updated_at"]
    )
    op.create_index("ix_repair_jobs_tenant", "repair_jobs", ["organization_id", "repository_id"])

    op.create_table(
        "repair_job_checkpoints",
        sa.Column("repair_job_id", sa.String(96), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("checkpoint_sha256", sa.String(64), nullable=False),
        sa.Column("state", sa.String(48), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["repair_job_id"], ["repair_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("repair_job_id", "version"),
    )

    op.create_table(
        "repair_budgets",
        sa.Column("repair_job_id", sa.String(96), primary_key=True),
        sa.Column("snapshot_json", sa.Text(), nullable=False),
        sa.Column("snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("checkpoint_version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["repair_job_id"], ["repair_jobs.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "repair_budget_reservations",
        sa.Column("repair_job_id", sa.String(96), nullable=False),
        sa.Column("reservation_id", sa.String(128), nullable=False),
        sa.Column("tokens", sa.BigInteger(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("checkpoint_version", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["repair_job_id"], ["repair_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("repair_job_id", "reservation_id"),
        sa.CheckConstraint("tokens >= 0", name="ck_repair_reservation_tokens"),
        sa.CheckConstraint("cost_usd >= 0", name="ck_repair_reservation_cost"),
    )

    op.create_table(
        "repair_approvals",
        sa.Column("id", sa.String(96), primary_key=True),
        sa.Column("repair_job_id", sa.String(96), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("binding_json", sa.Text(), nullable=False),
        sa.Column("binding_sha256", sa.String(64), nullable=False),
        sa.Column("checkpoint_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("approver_sha256", sa.String(64), nullable=True),
        sa.Column("decided_at", sa.Float(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["repair_job_id"], ["repair_jobs.id"], ondelete="CASCADE"),
        sa.CheckConstraint("kind IN ('write', 'draft_pr')", name="ck_repair_approval_kind"),
        sa.CheckConstraint(
            "status IN ('issued', 'consumed', 'rejected')", name="ck_repair_approval_status"
        ),
    )
    op.create_index(
        "ix_repair_approvals_job_kind", "repair_approvals", ["repair_job_id", "kind", "status"]
    )

    op.create_table(
        "repair_operation_intents",
        sa.Column("repair_job_id", sa.String(96), nullable=False),
        sa.Column("operation_id", sa.String(192), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("checkpoint_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["repair_job_id"], ["repair_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("repair_job_id", "operation_id"),
    )
    op.create_table(
        "repair_receipts",
        sa.Column("repair_job_id", sa.String(96), nullable=False),
        sa.Column("receipt_kind", sa.String(32), nullable=False),
        sa.Column("receipt_id", sa.String(192), nullable=False),
        sa.Column("receipt_sha256", sa.String(64), nullable=False),
        sa.Column("checkpoint_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["repair_job_id"], ["repair_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("repair_job_id", "receipt_kind", "receipt_id"),
    )
    op.create_table(
        "repair_outbox",
        sa.Column("repair_job_id", sa.String(96), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("receipt_sha256", sa.String(64), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["repair_job_id"], ["repair_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("repair_job_id", "payload_sha256"),
        sa.CheckConstraint("status IN ('pending', 'succeeded', 'quarantined')", name="ck_repair_outbox_status"),
    )
    op.create_table(
        "repair_audit_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("repair_job_id", sa.String(96), nullable=False),
        sa.Column("event_kind", sa.String(64), nullable=False),
        sa.Column("state", sa.String(48), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("approval_kind", sa.String(24), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("failure_code", sa.String(96), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["repair_job_id"], ["repair_jobs.id"], ondelete="CASCADE"),
        sa.CheckConstraint("attempt >= 0", name="ck_repair_audit_attempt"),
    )
    op.create_index("ix_repair_audit_events_job", "repair_audit_events", ["repair_job_id", "id"])
    op.create_table(
        "repair_worker_instances",
        sa.Column("worker_id", sa.String(128), primary_key=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=False),
        sa.Column("heartbeat_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.CheckConstraint("status IN ('ready', 'draining', 'stopped')", name="ck_repair_worker_status"),
        sa.CheckConstraint("capacity >= 1", name="ck_repair_worker_capacity"),
    )


def downgrade() -> None:
    op.drop_table("repair_worker_instances")
    op.drop_index("ix_repair_audit_events_job", table_name="repair_audit_events")
    op.drop_table("repair_audit_events")
    op.drop_table("repair_outbox")
    op.drop_table("repair_receipts")
    op.drop_table("repair_operation_intents")
    op.drop_index("ix_repair_approvals_job_kind", table_name="repair_approvals")
    op.drop_table("repair_approvals")
    op.drop_table("repair_budget_reservations")
    op.drop_table("repair_budgets")
    op.drop_table("repair_job_checkpoints")
    op.drop_index("ix_repair_jobs_tenant", table_name="repair_jobs")
    op.drop_index("ix_repair_jobs_claim", table_name="repair_jobs")
    op.drop_table("repair_jobs")
