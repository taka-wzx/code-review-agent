"""Add identity-free cumulative aggregates for Phase 9F metrics.

Revision ID: 0006_phase9f_metrics
Revises: 0005_phase9e
Create Date: 2026-07-25
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0006_phase9f_metrics"
down_revision = "0005_phase9e"
branch_labels = None
depends_on = None


_COUNTERS = (
    ("idempotency_hits_total", "{}"),
    ("unauthorized_operations_total", '{"operation":"publish"}'),
    ("unauthorized_operations_total", '{"operation":"approval"}'),
    ("unauthorized_operations_total", '{"operation":"feedback"}'),
    ("unauthorized_operations_total", '{"operation":"other"}'),
    ("approval_validation_failures_total", '{"reason":"replay"}'),
    ("approval_validation_failures_total", '{"reason":"mismatch"}'),
    ("approval_validation_failures_total", '{"reason":"expired"}'),
    ("approval_validation_failures_total", '{"reason":"consumed"}'),
    ("approval_validation_failures_total", '{"reason":"other"}'),
)

_WEBHOOK_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0)


def upgrade() -> None:
    op.create_table(
        "production_metric_counters",
        sa.Column("metric_name", sa.String(96), nullable=False),
        sa.Column("labels_json", sa.String(256), nullable=False),
        sa.Column("value", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.PrimaryKeyConstraint("metric_name", "labels_json"),
        sa.CheckConstraint("value >= 0", name="ck_production_metric_counter_value"),
    )
    op.create_table(
        "production_metric_histogram_totals",
        sa.Column("metric_name", sa.String(96), primary_key=True),
        sa.Column("sample_count", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("sample_sum", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.CheckConstraint(
            "sample_count >= 0 AND sample_sum >= 0",
            name="ck_production_metric_histogram_total",
        ),
    )
    op.create_table(
        "production_metric_histogram_buckets",
        sa.Column("metric_name", sa.String(96), nullable=False),
        sa.Column("bucket_order", sa.Integer(), nullable=False),
        sa.Column("le_text", sa.String(32), nullable=False),
        sa.Column("upper_bound", sa.Float(), nullable=True),
        sa.Column("sample_count", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.PrimaryKeyConstraint("metric_name", "bucket_order"),
        sa.CheckConstraint("sample_count >= 0", name="ck_production_metric_bucket_count"),
    )
    bind = op.get_bind()
    counter_insert = sa.text(
        "INSERT INTO production_metric_counters (metric_name, labels_json, value) "
        "VALUES (:name, :labels, 0)"
    )
    for name, labels in _COUNTERS:
        bind.execute(counter_insert, {"name": name, "labels": labels})
    bind.execute(
        sa.text(
            "INSERT INTO production_metric_histogram_totals "
            "(metric_name, sample_count, sample_sum) VALUES "
            "('webhook_ack_seconds', 0, 0)"
        )
    )
    bucket_insert = sa.text(
        "INSERT INTO production_metric_histogram_buckets "
        "(metric_name, bucket_order, le_text, upper_bound, sample_count) "
        "VALUES ('webhook_ack_seconds', :position, :text, :bound, 0)"
    )
    for position, bound in enumerate(_WEBHOOK_BUCKETS):
        bind.execute(
            bucket_insert,
            {"position": position, "text": format(bound, "g"), "bound": bound},
        )
    bind.execute(
        bucket_insert,
        {
            "position": len(_WEBHOOK_BUCKETS),
            "text": "+Inf",
            "bound": None,
        },
    )


def downgrade() -> None:
    op.drop_table("production_metric_histogram_buckets")
    op.drop_table("production_metric_histogram_totals")
    op.drop_table("production_metric_counters")
