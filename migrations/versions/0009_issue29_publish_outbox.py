"""Add publish outbox in-flight and quarantine states.

Revision ID: 0009_issue29_publish_outbox
Revises: 0008_phase11b_github_canary
Create Date: 2026-08-07
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0009_issue29_publish_outbox"
down_revision = "0008_phase11b_github_canary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("publish_attempts") as batch:
        batch.add_column(sa.Column("publishing_started_at", sa.String(40), nullable=True))
        batch.add_column(sa.Column("reconcile_after", sa.String(40), nullable=True))
        batch.drop_constraint("ck_publish_attempts_status", type_="check")
        batch.create_check_constraint(
            "ck_publish_attempts_status",
            "status IN ('prepared','publishing','succeeded','failed','quarantined')",
        )


def downgrade() -> None:
    op.execute(
        "UPDATE publish_attempts SET status='failed', "
        "error_code=COALESCE(error_code, 'publisher_ambiguous'), "
        "completed_at=COALESCE(completed_at, created_at) "
        "WHERE status IN ('publishing','quarantined')"
    )
    with op.batch_alter_table("publish_attempts") as batch:
        batch.drop_constraint("ck_publish_attempts_status", type_="check")
        batch.drop_column("reconcile_after")
        batch.drop_column("publishing_started_at")
        batch.create_check_constraint(
            "ck_publish_attempts_status",
            "status IN ('prepared','succeeded','failed')",
        )
