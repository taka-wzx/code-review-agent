"""Add the Phase 9C durable queue, leases, quotas, and worker registry.

Revision ID: 0003_phase9c_durable_queue
Revises: 0002_week7_legacy
Create Date: 2026-07-22
"""
from __future__ import annotations

from alembic import op
from datetime import datetime, timezone
import sqlalchemy as sa
import uuid


revision = "0003_phase9c_durable_queue"
down_revision = "0002_week7_legacy"
branch_labels = None
depends_on = None


_JOB_STATES = (
    "'received','queued','leased','running','awaiting_approval','approved',"
    "'published','declined','failed','dead_letter'"
)


def _backfill_review_jobs() -> None:
    bind = op.get_bind()
    timestamp_source = "COALESCE(completed_at, started_at, created_at)"
    if bind.dialect.name == "postgresql":
        created_at = "CAST(created_at AS TIMESTAMP WITH TIME ZONE)"
        updated_at = f"CAST({timestamp_source} AS TIMESTAMP WITH TIME ZONE)"
    else:
        created_at = "created_at"
        updated_at = timestamp_source
    bind.execute(
        sa.text(
            "UPDATE review_jobs SET "
            "state=CASE "
            "WHEN state='succeeded' THEN 'awaiting_approval' "
            "WHEN state IN ('queued','running') AND source_kind<>'pull_request' "
            "THEN 'failed' "
            "WHEN state='running' THEN 'queued' "
            "ELSE state END, "
            "completed_at=CASE WHEN state IN ('queued','running') "
            "AND source_kind<>'pull_request' THEN "
            f"{timestamp_source} ELSE completed_at END, "
            "error_code=CASE WHEN state IN ('queued','running') "
            "AND source_kind<>'pull_request' THEN 'legacy_payload_unavailable' "
            "ELSE error_code END, "
            "submission_key='legacy:' || id, "
            "request_fingerprint=source_sha256, "
            f"queued_at={created_at}, available_at={created_at}, updated_at={updated_at}, "
            "attempt_count=COALESCE(attempt_count, 0), "
            "max_attempts=COALESCE(max_attempts, 3), "
            "last_error_category=CASE WHEN state IN ('queued','running') "
            "AND source_kind<>'pull_request' THEN 'schema_policy' "
            "ELSE last_error_category END, "
            "model_call_limit=COALESCE(model_call_limit, 64), "
            "model_calls_reserved=COALESCE(model_calls_reserved, 0)"
        )
    )


def _seed_service_quotas() -> None:
    bind = op.get_bind()
    occurred = datetime.now(timezone.utc)
    month = occurred.strftime("%Y-%m")
    statement = sa.text(
        "INSERT INTO service_quotas "
        "(id, organization_id, repository_id, scope_kind, max_queued_jobs, "
        "max_concurrent_jobs, submission_rate_limit, submission_window_seconds, "
        "submission_window_started_at, submission_window_count, "
        "monthly_model_call_budget, model_call_month, monthly_model_calls_used, "
        "monthly_model_calls_reserved, model_call_limit_per_job, created_at, updated_at) "
        "VALUES (:id, :org, :repo, :kind, :queued, :concurrent, :rate, 60, "
        ":occurred, 0, :budget, :month, 0, 0, 64, :occurred, :occurred) "
        "ON CONFLICT DO NOTHING"
    )
    organizations = bind.execute(sa.text("SELECT id FROM organizations")).scalars()
    for organization_id in organizations:
        bind.execute(
            statement,
            {
                "id": uuid.uuid4().hex,
                "org": str(organization_id),
                "repo": None,
                "kind": "organization",
                "queued": 1000,
                "concurrent": 16,
                "rate": 600,
                "budget": 100000,
                "month": month,
                "occurred": occurred,
            },
        )
    repositories = bind.execute(
        sa.text("SELECT id, organization_id FROM repositories")
    ).mappings()
    for repository in repositories:
        bind.execute(
            statement,
            {
                "id": uuid.uuid4().hex,
                "org": str(repository["organization_id"]),
                "repo": str(repository["id"]),
                "kind": "repository",
                "queued": 100,
                "concurrent": 2,
                "rate": 60,
                "budget": 10000,
                "month": month,
                "occurred": occurred,
            },
        )


def _backfill_provider_attempts() -> None:
    op.get_bind().execute(
        sa.text(
            "WITH ranked AS ("
            "SELECT id, ROW_NUMBER() OVER ("
            "PARTITION BY organization_id, review_job_id ORDER BY created_at, id"
            ") - 1 AS attempt_ordinal FROM provider_usage"
            ") UPDATE provider_usage SET attempt_count=("
            "SELECT attempt_ordinal FROM ranked WHERE ranked.id=provider_usage.id"
            ")"
        )
    )


def upgrade() -> None:
    # Dropping the old check in its own batch lets SQLite copy legacy
    # succeeded/running rows before they are mapped to the new state set.
    with op.batch_alter_table("review_jobs") as batch:
        batch.drop_constraint("ck_review_jobs_state", type_="check")
        batch.add_column(sa.Column("submission_key", sa.String(128), nullable=True))
        batch.add_column(sa.Column("idempotency_key_hash", sa.String(64), nullable=True))
        batch.add_column(sa.Column("request_fingerprint", sa.String(64), nullable=True))
        batch.add_column(sa.Column("payload_key", sa.String(512), nullable=True))
        batch.add_column(sa.Column("head_sha", sa.String(64), nullable=True))
        batch.add_column(sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("available_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("lease_owner", sa.String(128), nullable=True))
        batch.add_column(sa.Column("lease_token", sa.String(64), nullable=True))
        batch.add_column(sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(
            sa.Column("attempt_count", sa.Integer(), nullable=True, server_default=sa.text("0"))
        )
        batch.add_column(
            sa.Column("max_attempts", sa.Integer(), nullable=True, server_default=sa.text("3"))
        )
        batch.add_column(sa.Column("last_error_category", sa.String(32), nullable=True))
        batch.add_column(
            sa.Column("model_call_limit", sa.Integer(), nullable=True, server_default=sa.text("64"))
        )
        batch.add_column(
            sa.Column(
                "model_calls_reserved", sa.Integer(), nullable=True, server_default=sa.text("0")
            )
        )
        batch.add_column(sa.Column("final_trace_key", sa.String(512), nullable=True))
        batch.add_column(sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))

    _backfill_review_jobs()

    with op.batch_alter_table("review_jobs") as batch:
        batch.alter_column(
            "submission_key", existing_type=sa.String(128), nullable=False
        )
        batch.alter_column(
            "request_fingerprint", existing_type=sa.String(64), nullable=False
        )
        batch.alter_column(
            "available_at", existing_type=sa.DateTime(timezone=True), nullable=False
        )
        batch.alter_column(
            "attempt_count",
            existing_type=sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        )
        batch.alter_column(
            "max_attempts",
            existing_type=sa.Integer(),
            nullable=False,
            server_default=sa.text("3"),
        )
        batch.alter_column(
            "model_call_limit",
            existing_type=sa.Integer(),
            nullable=False,
            server_default=sa.text("64"),
        )
        batch.alter_column(
            "model_calls_reserved",
            existing_type=sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        )
        batch.alter_column(
            "updated_at", existing_type=sa.DateTime(timezone=True), nullable=False
        )
        batch.create_check_constraint(
            "ck_review_jobs_state", f"state IN ({_JOB_STATES})"
        )
        batch.create_check_constraint(
            "ck_review_jobs_attempt_limits", "attempt_count >= 0 AND max_attempts >= 1"
        )
        batch.create_check_constraint(
            "ck_review_jobs_model_calls",
            "model_call_limit >= 1 AND model_calls_reserved >= 0 "
            "AND model_calls_reserved <= model_call_limit",
        )
        batch.create_unique_constraint(
            "uq_review_jobs_org_submission_key", ["organization_id", "submission_key"]
        )
        batch.create_unique_constraint(
            "uq_review_jobs_org_idempotency_key_hash",
            ["organization_id", "idempotency_key_hash"],
        )

    queued = sa.text("state = 'queued'")
    leased = sa.text("state IN ('leased','running')")
    op.create_index(
        "ix_review_jobs_claim",
        "review_jobs",
        ["available_at", "queued_at", "id"],
        postgresql_where=queued,
        sqlite_where=queued,
    )
    op.create_index(
        "ix_review_jobs_lease_expiry",
        "review_jobs",
        ["lease_expires_at", "id"],
        postgresql_where=leased,
        sqlite_where=leased,
    )
    op.create_index(
        "ix_review_jobs_scope_state",
        "review_jobs",
        ["organization_id", "repository_id", "state", "lease_expires_at"],
    )

    with op.batch_alter_table("provider_usage") as batch:
        batch.add_column(
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0"))
        )
        batch.add_column(
            sa.Column("llm_calls", sa.Integer(), nullable=False, server_default=sa.text("0"))
        )

    _backfill_provider_attempts()

    with op.batch_alter_table("provider_usage") as batch:
        batch.create_check_constraint(
            "ck_provider_usage_attempt_calls", "attempt_count >= 0 AND llm_calls >= 0"
        )
        batch.create_unique_constraint(
            "uq_provider_usage_job_attempt",
            ["review_job_id", "attempt_count"],
        )

    op.create_table(
        "service_quotas",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "organization_id",
            sa.String(64),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("repository_id", sa.String(64), nullable=True),
        sa.Column("scope_kind", sa.String(16), nullable=False),
        sa.Column(
            "max_queued_jobs", sa.Integer(), nullable=False, server_default=sa.text("100")
        ),
        sa.Column(
            "max_concurrent_jobs", sa.Integer(), nullable=False, server_default=sa.text("2")
        ),
        sa.Column(
            "submission_rate_limit", sa.Integer(), nullable=False, server_default=sa.text("60")
        ),
        sa.Column(
            "submission_window_seconds",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("60"),
        ),
        sa.Column("submission_window_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "submission_window_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "monthly_model_call_budget",
            sa.BigInteger(),
            nullable=True,
            server_default=sa.text("10000"),
        ),
        sa.Column("model_call_month", sa.String(7), nullable=True),
        sa.Column(
            "monthly_model_calls_used",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "monthly_model_calls_reserved",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "model_call_limit_per_job",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("64"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "(scope_kind='organization' AND repository_id IS NULL) OR "
            "(scope_kind='repository' AND repository_id IS NOT NULL)",
            name="ck_service_quotas_scope",
        ),
        sa.CheckConstraint(
            "max_queued_jobs BETWEEN 1 AND 100000 AND "
            "max_concurrent_jobs BETWEEN 1 AND 64 AND "
            "submission_rate_limit BETWEEN 1 AND 100000 AND "
            "submission_window_seconds BETWEEN 1 AND 86400 AND "
            "(monthly_model_call_budget IS NULL OR "
            "monthly_model_call_budget BETWEEN 1 AND 1000000000) AND "
            "model_call_limit_per_job BETWEEN 1 AND 256",
            name="ck_service_quotas_limits",
        ),
        sa.CheckConstraint(
            "submission_window_count >= 0 AND monthly_model_calls_used >= 0 "
            "AND monthly_model_calls_reserved >= 0",
            name="ck_service_quotas_counters",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "repository_id"],
            ["repositories.organization_id", "repositories.id"],
            name="fk_service_quotas_org_repo",
            ondelete="CASCADE",
        ),
    )
    organization_scope = sa.text("scope_kind = 'organization'")
    repository_scope = sa.text("scope_kind = 'repository'")
    op.create_index(
        "uq_service_quotas_organization_scope",
        "service_quotas",
        ["organization_id"],
        unique=True,
        postgresql_where=organization_scope,
        sqlite_where=organization_scope,
    )
    op.create_index(
        "uq_service_quotas_repository_scope",
        "service_quotas",
        ["organization_id", "repository_id"],
        unique=True,
        postgresql_where=repository_scope,
        sqlite_where=repository_scope,
    )
    _seed_service_quotas()

    op.create_table(
        "review_idempotency_keys",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "organization_id",
            sa.String(64),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("review_job_id", sa.String(64), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "review_job_id"],
            ["review_jobs.organization_id", "review_jobs.id"],
            name="fk_review_idempotency_keys_org_job",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "key_hash",
            name="uq_review_idempotency_keys_org_hash",
        ),
    )
    op.create_index(
        "ix_review_idempotency_keys_job",
        "review_idempotency_keys",
        ["organization_id", "review_job_id"],
    )

    op.create_table(
        "worker_instances",
        sa.Column("worker_id", sa.String(128), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "heartbeat_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "status IN ('starting','ready','draining','stopped')",
            name="ck_worker_instances_status",
        ),
        sa.CheckConstraint(
            "capacity BETWEEN 1 AND 64", name="ck_worker_instances_capacity"
        ),
    )
    op.create_index(
        "ix_worker_instances_status_heartbeat",
        "worker_instances",
        ["status", "heartbeat_at"],
    )

    op.create_table(
        "submission_events",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "organization_id",
            sa.String(64),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
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
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "repository_id"],
            ["repositories.organization_id", "repositories.id"],
            name="fk_submission_events_org_repo",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "review_job_id"],
            ["review_jobs.organization_id", "review_jobs.id"],
            name="fk_submission_events_org_job",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("review_job_id", name="uq_submission_events_job"),
    )
    op.create_index(
        "ix_submission_events_org_repo_time",
        "submission_events",
        ["organization_id", "repository_id", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_table("submission_events")
    op.drop_table("worker_instances")
    op.drop_table("review_idempotency_keys")
    op.drop_table("service_quotas")

    with op.batch_alter_table("provider_usage") as batch:
        batch.drop_constraint("uq_provider_usage_job_attempt", type_="unique")
        batch.drop_constraint("ck_provider_usage_attempt_calls", type_="check")
        batch.drop_column("llm_calls")
        batch.drop_column("attempt_count")

    op.drop_index("ix_review_jobs_scope_state", table_name="review_jobs")
    op.drop_index("ix_review_jobs_lease_expiry", table_name="review_jobs")
    op.drop_index("ix_review_jobs_claim", table_name="review_jobs")

    with op.batch_alter_table("review_jobs") as batch:
        batch.drop_constraint("uq_review_jobs_org_idempotency_key_hash", type_="unique")
        batch.drop_constraint("uq_review_jobs_org_submission_key", type_="unique")
        batch.drop_constraint("ck_review_jobs_model_calls", type_="check")
        batch.drop_constraint("ck_review_jobs_attempt_limits", type_="check")
        batch.drop_constraint("ck_review_jobs_state", type_="check")

    op.get_bind().execute(
        sa.text(
            "UPDATE review_jobs SET state=CASE "
            "WHEN state IN ('awaiting_approval','approved','published') THEN 'succeeded' "
            "WHEN state IN ('received','leased','running') THEN 'queued' "
            "WHEN state IN ('declined','dead_letter') THEN 'failed' "
            "ELSE state END"
        )
    )

    with op.batch_alter_table("review_jobs") as batch:
        batch.drop_column("updated_at")
        batch.drop_column("final_trace_key")
        batch.drop_column("model_calls_reserved")
        batch.drop_column("model_call_limit")
        batch.drop_column("last_error_category")
        batch.drop_column("max_attempts")
        batch.drop_column("attempt_count")
        batch.drop_column("heartbeat_at")
        batch.drop_column("lease_expires_at")
        batch.drop_column("lease_token")
        batch.drop_column("lease_owner")
        batch.drop_column("available_at")
        batch.drop_column("queued_at")
        batch.drop_column("head_sha")
        batch.drop_column("payload_key")
        batch.drop_column("request_fingerprint")
        batch.drop_column("idempotency_key_hash")
        batch.drop_column("submission_key")
        batch.create_check_constraint(
            "ck_review_jobs_state", "state IN ('queued','running','succeeded','failed')"
        )
