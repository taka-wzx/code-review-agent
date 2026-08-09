"""Persist GitHub App webhook delivery acknowledgements.

Revision ID: 0010_issue33_github_webhook
Revises: 0009_issue27_published_feedback
Create Date: 2026-08-08
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0010_issue33_github_webhook"
down_revision = "0009_issue27_published_feedback"
branch_labels = None
depends_on = None


_DELIVERY_STATUSES = (
    "processing",
    "pong",
    "ignored",
    "installation_active",
    "installation_suspended",
    "installation_deleted",
    "review_queued",
)

_DELIVERY_REASONS = (
    "unsupported_event",
    "installation_unknown",
    "installation_inactive",
    "installation_account_mismatch",
    "installation_identity_mismatch",
    "installation_transition_denied",
)


def upgrade() -> None:
    op.create_table(
        "github_app_installations",
        sa.Column("installation_id", sa.String(32), primary_key=True),
        sa.Column("app_id", sa.BigInteger(), nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("last_delivery_id", sa.String(128), nullable=False),
        sa.Column("last_payload_sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("updated_at", sa.String(40), nullable=False),
        sa.CheckConstraint("app_id > 0", name="ck_github_app_installations_app_id"),
        sa.CheckConstraint("account_id > 0", name="ck_github_app_installations_account_id"),
        sa.CheckConstraint(
            "state IN ('active','suspended','deleted')",
            name="ck_github_app_installations_state",
        ),
        sa.CheckConstraint(
            "length(last_payload_sha256) = 64",
            name="ck_github_app_installations_payload_sha256",
        ),
    )
    op.create_table(
        "github_webhook_deliveries",
        sa.Column("delivery_id", sa.String(128), primary_key=True),
        sa.Column("event", sa.String(64), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(48), nullable=True),
        sa.Column("review_job_id", sa.String(96), nullable=True),
        sa.Column("installation_id", sa.String(32), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.CheckConstraint(
            "length(payload_sha256) = 64",
            name="ck_github_webhook_deliveries_payload_sha256",
        ),
        sa.CheckConstraint(
            "status IN (" + ", ".join(repr(status) for status in _DELIVERY_STATUSES) + ")",
            name="ck_github_webhook_deliveries_status",
        ),
        sa.CheckConstraint(
            "reason IS NULL OR reason IN ("
            + ", ".join(repr(reason) for reason in _DELIVERY_REASONS)
            + ")",
            name="ck_github_webhook_deliveries_reason",
        ),
        sa.CheckConstraint(
            "http_status >= 200 AND http_status < 300",
            name="ck_github_webhook_deliveries_http_status",
        ),
    )
    op.create_index(
        "ix_github_webhook_deliveries_event_created_at",
        "github_webhook_deliveries",
        ["event", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_github_webhook_deliveries_event_created_at",
        table_name="github_webhook_deliveries",
    )
    op.drop_table("github_webhook_deliveries")
    op.drop_table("github_app_installations")
