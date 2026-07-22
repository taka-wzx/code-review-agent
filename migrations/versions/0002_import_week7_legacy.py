"""Import Week 7 SQLite jobs into an isolated legacy tenant.

Revision ID: 0002_week7_legacy
Revises: 0001_phase9b
Create Date: 2026-07-22
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib

from alembic import op
import sqlalchemy as sa


revision = "0002_week7_legacy"
down_revision = "0001_phase9b"
branch_labels = None
depends_on = None

LEGACY_ORGANIZATION_ID = "local-legacy-organization"
LEGACY_USER_ID = "local-legacy-operator"
LEGACY_MEMBERSHIP_ID = "local-legacy-membership"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _repository_id(alias: str) -> str:
    return "legacy-repo-" + hashlib.sha256(alias.encode("utf-8")).hexdigest()[:32]


def _delivery_row_id(delivery_id: str) -> str:
    return "legacy-delivery-" + hashlib.sha256(delivery_id.encode("utf-8")).hexdigest()[:32]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "jobs" not in inspector.get_table_names():
        return

    now = _now()
    bind.execute(
        sa.text(
            "INSERT INTO organizations "
            "(id, slug, display_name, policy_version, created_at) "
            "VALUES (:id, :slug, :name, :policy, :created)"
        ),
        {
            "id": LEGACY_ORGANIZATION_ID,
            "slug": "local-legacy",
            "name": "Migrated Week 7 local data",
            "policy": "legacy/week7",
            "created": now,
        },
    )
    bind.execute(
        sa.text(
            "INSERT INTO users "
            "(id, organization_id, subject, display_name, email_hash, active, created_at) "
            "VALUES (:id, :org, :subject, :name, NULL, :active, :created)"
        ),
        {
            "id": LEGACY_USER_ID,
            "org": LEGACY_ORGANIZATION_ID,
            "subject": "legacy:week7-operator",
            "name": "Week 7 local operator",
            "active": True,
            "created": now,
        },
    )
    bind.execute(
        sa.text(
            "INSERT INTO memberships "
            "(id, organization_id, user_id, role, created_at, updated_at) "
            "VALUES (:id, :org, :user, 'org_admin', :created, :updated)"
        ),
        {
            "id": LEGACY_MEMBERSHIP_ID,
            "org": LEGACY_ORGANIZATION_ID,
            "user": LEGACY_USER_ID,
            "created": now,
            "updated": now,
        },
    )

    jobs = list(bind.execute(sa.text("SELECT * FROM jobs")).mappings())
    aliases = sorted({str(row["repository"]) for row in jobs})
    for alias in aliases:
        bind.execute(
            sa.text(
                "INSERT INTO repositories "
                "(id, organization_id, alias, mode, budget_microusd, policy_version, "
                "active, created_at) "
                "VALUES (:id, :org, :alias, 'shadow', NULL, :policy, :active, :created)"
            ),
            {
                "id": _repository_id(alias),
                "org": LEGACY_ORGANIZATION_ID,
                "alias": alias,
                "policy": "legacy/week7",
                "active": True,
                "created": now,
            },
        )

    for row in jobs:
        alias = str(row["repository"])
        bind.execute(
            sa.text(
                "INSERT INTO review_jobs "
                "(id, organization_id, repository_id, submitted_by, correlation_id, "
                "source_kind, repository_alias, source_ref, source_sha256, source_bytes, "
                "state, created_at, started_at, completed_at, review_json, error_code) "
                "VALUES (:id, :org, :repo, :actor, :correlation, :kind, :alias, :ref, "
                ":sha, :bytes, :state, :created, :started, :completed, :review, :error)"
            ),
            {
                "id": row["id"],
                "org": LEGACY_ORGANIZATION_ID,
                "repo": _repository_id(alias),
                "actor": LEGACY_USER_ID,
                "correlation": f"legacy-{row['id']}",
                "kind": row["source_kind"],
                "alias": alias,
                "ref": row["source_ref"],
                "sha": row["source_sha256"],
                "bytes": row["source_bytes"],
                "state": row["state"],
                "created": row["created_at"],
                "started": row["started_at"],
                "completed": row["completed_at"],
                "review": row["review_json"],
                "error": row["error_code"],
            },
        )

    if "deliveries" in inspector.get_table_names():
        deliveries = list(bind.execute(sa.text("SELECT * FROM deliveries")).mappings())
        jobs_by_id = {str(row["id"]): row for row in jobs}
        for row in deliveries:
            job = jobs_by_id[str(row["job_id"])]
            alias = str(job["repository"])
            bind.execute(
                sa.text(
                    "INSERT INTO webhook_deliveries "
                    "(id, organization_id, repository_id, review_job_id, delivery_id, "
                    "event, received_at) "
                    "VALUES (:id, :org, :repo, :job, :delivery, :event, :received)"
                ),
                {
                    "id": _delivery_row_id(str(row["delivery_id"])),
                    "org": LEGACY_ORGANIZATION_ID,
                    "repo": _repository_id(alias),
                    "job": row["job_id"],
                    "delivery": row["delivery_id"],
                    "event": row["event"],
                    "received": row["received_at"],
                },
            )
        op.drop_table("deliveries")
    op.drop_table("jobs")


def downgrade() -> None:
    # A lossy downgrade would be more dangerous than an explicit restore from
    # the pre-migration backup. The application rollback contract therefore
    # uses backup/restore for this structural legacy import.
    raise RuntimeError(
        "0002_week7_legacy requires restoring the pre-migration database backup"
    )
