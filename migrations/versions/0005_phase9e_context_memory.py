"""Add trusted repository memory and organization policy storage.

Revision ID: 0005_phase9e
Revises: 0004_phase9d
Create Date: 2026-07-24
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0005_phase9e"
down_revision = "0004_phase9d"
branch_labels = None
depends_on = None


MEMORY_KINDS = (
    "convention",
    "build_command",
    "test_command",
    "language",
    "framework",
    "code_owner",
    "risk_path",
    "accepted_finding",
    "suppression_candidate",
)
MEMORY_SOURCES = ("human_confirmed", "admin_config", "repository_file")


def upgrade() -> None:
    op.create_table(
        "repository_memory_entries",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("organization_id", sa.String(64), nullable=False),
        sa.Column("repository_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.Column("path", sa.String(512), nullable=True),
        sa.Column("language", sa.String(64), nullable=True),
        sa.Column("symbol", sa.String(256), nullable=True),
        sa.Column("fingerprint", sa.String(128), nullable=True),
        sa.Column("source_sha", sa.String(64), nullable=False),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("confirmed_by", sa.String(128), nullable=True),
        sa.Column("reason", sa.String(512), nullable=False),
        sa.Column("valid_from_sha", sa.String(64), nullable=True),
        sa.Column("valid_until_sha", sa.String(64), nullable=True),
        sa.Column("expires_at", sa.String(40), nullable=True),
        sa.Column("invalidated_at", sa.String(40), nullable=True),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("updated_at", sa.String(40), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "repository_id"],
            ["repositories.organization_id", "repositories.id"],
            name="fk_repository_memory_org_repo",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "kind IN (" + ",".join(repr(value) for value in MEMORY_KINDS) + ")",
            name="ck_repository_memory_kind",
        ),
        sa.CheckConstraint(
            "source_kind IN (" + ",".join(repr(value) for value in MEMORY_SOURCES) + ")",
            name="ck_repository_memory_source",
        ),
        sa.CheckConstraint("length(reason) > 0", name="ck_repository_memory_reason"),
    )
    op.create_index(
        "ix_repository_memory_scope_revision",
        "repository_memory_entries",
        ["organization_id", "repository_id", "source_sha", "invalidated_at", "expires_at"],
    )
    op.create_index(
        "ix_repository_memory_path_symbol",
        "repository_memory_entries",
        ["organization_id", "repository_id", "path", "language", "symbol"],
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE INDEX ix_repository_memory_fts ON repository_memory_entries "
            "USING GIN (to_tsvector('simple', search_text))"
        )

    op.create_table(
        "repository_memory_edges",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("organization_id", sa.String(64), nullable=False),
        sa.Column("repository_id", sa.String(64), nullable=False),
        sa.Column("source_sha", sa.String(64), nullable=False),
        sa.Column("memory_id", sa.String(64), nullable=False),
        sa.Column("relation", sa.String(32), nullable=False),
        sa.Column("path", sa.String(512), nullable=True),
        sa.Column("symbol", sa.String(256), nullable=True),
        sa.Column("target_path", sa.String(512), nullable=True),
        sa.Column("target_symbol", sa.String(256), nullable=True),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "repository_id"],
            ["repositories.organization_id", "repositories.id"],
            name="fk_repository_memory_edges_org_repo",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["memory_id"], ["repository_memory_entries.id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "ix_repository_memory_edges_lookup",
        "repository_memory_edges",
        ["organization_id", "repository_id", "source_sha", "path", "symbol"],
    )

    op.create_table(
        "organization_policies",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("organization_id", sa.String(64), nullable=False),
        sa.Column("version", sa.String(128), nullable=False),
        sa.Column("severity_json", sa.Text(), nullable=False),
        sa.Column("forbidden_operations_json", sa.Text(), nullable=False),
        sa.Column("allowed_tools_json", sa.Text(), nullable=False),
        sa.Column("approval_threshold", sa.Integer(), nullable=False),
        sa.Column("retention_days", sa.Integer(), nullable=False),
        sa.Column("cost_budget_microusd", sa.BigInteger(), nullable=False),
        sa.Column("source_sha", sa.String(64), nullable=False),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("reason", sa.String(512), nullable=False),
        sa.Column("invalidated_at", sa.String(40), nullable=True),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint("approval_threshold BETWEEN 1 AND 100", name="ck_policy_approval"),
        sa.CheckConstraint("retention_days BETWEEN 1 AND 3650", name="ck_policy_retention"),
        sa.CheckConstraint("cost_budget_microusd >= 0", name="ck_policy_cost"),
        sa.CheckConstraint("source_kind='admin_config'", name="ck_policy_source"),
        sa.UniqueConstraint("organization_id", "version", name="uq_org_policy_version"),
    )
    op.create_index(
        "ix_organization_policies_active",
        "organization_policies",
        ["organization_id", "invalidated_at", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_organization_policies_active", table_name="organization_policies")
    op.drop_table("organization_policies")
    op.drop_index("ix_repository_memory_edges_lookup", table_name="repository_memory_edges")
    op.drop_table("repository_memory_edges")
    if op.get_bind().dialect.name == "postgresql":
        op.drop_index("ix_repository_memory_fts", table_name="repository_memory_entries")
    op.drop_index("ix_repository_memory_path_symbol", table_name="repository_memory_entries")
    op.drop_index("ix_repository_memory_scope_revision", table_name="repository_memory_entries")
    op.drop_table("repository_memory_entries")
