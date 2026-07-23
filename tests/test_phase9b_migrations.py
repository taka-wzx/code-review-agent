import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from code_review_agent.database import (
    Database,
    MigrationRequired,
    current_revision,
    require_schema_head,
    schema_head,
    sqlite_database_url,
    upgrade_database,
)
from code_review_agent.identity import Role
from code_review_agent.service_core import JobStore


EXPECTED_TENANT_TABLES = {
    "users",
    "memberships",
    "repositories",
    "repository_access",
    "access_credentials",
    "review_sessions",
    "review_jobs",
    "findings",
    "finding_feedback",
    "approvals",
    "audit_events",
    "webhook_deliveries",
    "provider_usage",
    "review_idempotency_keys",
    "service_quotas",
    "submission_events",
}

EXPECTED_OPERATIONAL_TABLES = {"worker_instances"}

PHASE9C_JOB_COLUMNS = {
    "submission_key",
    "idempotency_key_hash",
    "request_fingerprint",
    "payload_key",
    "head_sha",
    "queued_at",
    "available_at",
    "lease_owner",
    "lease_token",
    "lease_expires_at",
    "heartbeat_at",
    "attempt_count",
    "max_attempts",
    "last_error_category",
    "model_call_limit",
    "model_calls_reserved",
    "final_trace_key",
    "updated_at",
}

PHASE9C_JOB_STATES = {
    "received",
    "queued",
    "leased",
    "running",
    "awaiting_approval",
    "approved",
    "published",
    "declined",
    "failed",
    "dead_letter",
}


class Phase9BMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database_path = self.root / "reviews.sqlite3"
        self.database_url = sqlite_database_url(self.database_path)

    def tearDown(self):
        self.temp.cleanup()

    def downgrade_database(self, revision):
        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        config.set_main_option("sqlalchemy.url", self.database_url.replace("%", "%%"))
        command.downgrade(config, revision)

    def test_empty_database_upgrade_creates_head_and_tenant_schema(self):
        upgrade_database(self.database_url)
        self.assertEqual(current_revision(self.database_url), schema_head())
        require_schema_head(self.database_url)

        engine = create_engine(self.database_url)
        try:
            schema = inspect(engine)
            tables = set(schema.get_table_names())
            self.assertTrue(EXPECTED_TENANT_TABLES.issubset(tables))
            self.assertTrue(EXPECTED_OPERATIONAL_TABLES.issubset(tables))
            for table in EXPECTED_TENANT_TABLES:
                columns = {column["name"] for column in schema.get_columns(table)}
                self.assertIn("organization_id", columns, table)
            organization_columns = {
                column["name"] for column in schema.get_columns("organizations")
            }
            self.assertIn("id", organization_columns)

            review_columns = {
                column["name"]: column for column in schema.get_columns("review_jobs")
            }
            self.assertTrue(PHASE9C_JOB_COLUMNS.issubset(review_columns))
            for required in (
                "submission_key",
                "request_fingerprint",
                "available_at",
                "attempt_count",
                "max_attempts",
                "model_call_limit",
                "model_calls_reserved",
                "updated_at",
            ):
                self.assertFalse(review_columns[required]["nullable"], required)
            self.assertTrue(review_columns["queued_at"]["nullable"])

            review_checks = {
                item["name"]: item["sqltext"]
                for item in schema.get_check_constraints("review_jobs")
            }
            state_check = review_checks["ck_review_jobs_state"]
            for state in PHASE9C_JOB_STATES:
                self.assertIn(state, state_check)
            self.assertNotIn("succeeded", state_check)

            review_uniques = {
                item["name"]: set(item["column_names"])
                for item in schema.get_unique_constraints("review_jobs")
            }
            self.assertEqual(
                review_uniques["uq_review_jobs_org_submission_key"],
                {"organization_id", "submission_key"},
            )
            self.assertEqual(
                review_uniques["uq_review_jobs_org_idempotency_key_hash"],
                {"organization_id", "idempotency_key_hash"},
            )
            review_indexes = {item["name"] for item in schema.get_indexes("review_jobs")}
            self.assertTrue(
                {
                    "ix_review_jobs_claim",
                    "ix_review_jobs_lease_expiry",
                    "ix_review_jobs_scope_state",
                }.issubset(review_indexes)
            )

            provider_columns = {
                column["name"] for column in schema.get_columns("provider_usage")
            }
            self.assertTrue({"attempt_count", "llm_calls"}.issubset(provider_columns))
            provider_uniques = {
                item["name"]: set(item["column_names"])
                for item in schema.get_unique_constraints("provider_usage")
            }
            self.assertEqual(
                provider_uniques["uq_provider_usage_job_attempt"],
                {"review_job_id", "attempt_count"},
            )
            provider_checks = {
                item["name"] for item in schema.get_check_constraints("provider_usage")
            }
            self.assertIn("ck_provider_usage_attempt_calls", provider_checks)

            quota_columns = {
                column["name"]: column for column in schema.get_columns("service_quotas")
            }
            self.assertTrue(
                {
                    "scope_kind",
                    "max_queued_jobs",
                    "max_concurrent_jobs",
                    "submission_rate_limit",
                    "submission_window_seconds",
                    "submission_window_started_at",
                    "submission_window_count",
                    "monthly_model_call_budget",
                    "model_call_month",
                    "monthly_model_calls_used",
                    "monthly_model_calls_reserved",
                    "model_call_limit_per_job",
                }.issubset(quota_columns)
            )
            quota_checks = {
                item["name"] for item in schema.get_check_constraints("service_quotas")
            }
            self.assertTrue(
                {
                    "ck_service_quotas_scope",
                    "ck_service_quotas_limits",
                    "ck_service_quotas_counters",
                }.issubset(quota_checks)
            )
            quota_indexes = {
                item["name"]: item["unique"]
                for item in schema.get_indexes("service_quotas")
            }
            self.assertTrue(quota_indexes["uq_service_quotas_organization_scope"])
            self.assertTrue(quota_indexes["uq_service_quotas_repository_scope"])
            worker_columns = {
                column["name"] for column in schema.get_columns("worker_instances")
            }
            self.assertTrue(
                {
                    "worker_id",
                    "status",
                    "capacity",
                    "version",
                    "started_at",
                    "heartbeat_at",
                    "updated_at",
                }.issubset(worker_columns)
            )
            worker_checks = {
                item["name"] for item in schema.get_check_constraints("worker_instances")
            }
            self.assertTrue(
                {
                    "ck_worker_instances_status",
                    "ck_worker_instances_capacity",
                }.issubset(worker_checks)
            )
            submission_uniques = {
                item["name"]: set(item["column_names"])
                for item in schema.get_unique_constraints("submission_events")
            }
            self.assertEqual(
                submission_uniques["uq_submission_events_job"], {"review_job_id"}
            )
            idempotency_uniques = {
                item["name"]: set(item["column_names"])
                for item in schema.get_unique_constraints("review_idempotency_keys")
            }
            self.assertEqual(
                idempotency_uniques["uq_review_idempotency_keys_org_hash"],
                {"organization_id", "key_hash"},
            )
        finally:
            engine.dispose()

    def test_week7_database_upgrade_preserves_jobs_results_and_delivery(self):
        connection = sqlite3.connect(self.database_path)
        connection.executescript(
            """
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY,
                source_kind TEXT NOT NULL,
                repository TEXT NOT NULL,
                source_ref TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,
                source_bytes INTEGER NOT NULL,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                review_json TEXT,
                error_code TEXT
            );
            CREATE TABLE deliveries (
                delivery_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES jobs(id),
                event TEXT NOT NULL,
                received_at TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "a" * 32,
                "pull_request",
                "owner/repo",
                "7",
                "b" * 64,
                0,
                "succeeded",
                "2026-07-21T00:00:00Z",
                "2026-07-21T00:00:01Z",
                "2026-07-21T00:00:02Z",
                json.dumps({"summary": "preserved"}),
                None,
            ),
        )
        connection.execute(
            "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "c" * 32,
                "pull_request",
                "owner/repo",
                "8",
                "d" * 64,
                0,
                "running",
                "2026-07-21T00:00:03Z",
                "2026-07-21T00:00:04Z",
                None,
                None,
                None,
            ),
        )
        for job_id, state in (("e" * 32, "queued"), ("f" * 32, "running")):
            connection.execute(
                "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    "diff",
                    "owner/repo",
                    "inline",
                    "1" * 64,
                    128,
                    state,
                    "2026-07-21T00:00:05Z",
                    "2026-07-21T00:00:06Z" if state == "running" else None,
                    None,
                    None,
                    None,
                ),
            )
        connection.execute(
            "INSERT INTO deliveries VALUES (?, ?, ?, ?)",
            ("delivery-legacy", "a" * 32, "pull_request", "2026-07-21T00:00:00Z"),
        )
        connection.commit()
        connection.close()

        upgrade_database(self.database_url)

        engine = create_engine(self.database_url)
        try:
            with engine.connect() as migrated:
                job = migrated.execute(
                    text("SELECT * FROM review_jobs WHERE id=:id"), {"id": "a" * 32}
                ).mappings().one()
                interrupted = migrated.execute(
                    text("SELECT * FROM review_jobs WHERE id=:id"), {"id": "c" * 32}
                ).mappings().one()
                unavailable_inline = migrated.execute(
                    text(
                        "SELECT id, state, completed_at, error_code, last_error_category "
                        "FROM review_jobs WHERE id IN (:queued, :running) ORDER BY id"
                    ),
                    {"queued": "e" * 32, "running": "f" * 32},
                ).mappings().all()
                delivery = migrated.execute(
                    text(
                        "SELECT * FROM webhook_deliveries WHERE delivery_id=:delivery"
                    ),
                    {"delivery": "delivery-legacy"},
                ).mappings().one()
                quotas = migrated.execute(
                    text(
                        "SELECT scope_kind, max_queued_jobs FROM service_quotas "
                        "WHERE organization_id=:org ORDER BY scope_kind"
                    ),
                    {"org": "local-legacy-organization"},
                ).all()
                tables = set(inspect(migrated).get_table_names())
            self.assertEqual(job["organization_id"], "local-legacy-organization")
            self.assertEqual(json.loads(job["review_json"])["summary"], "preserved")
            self.assertEqual(job["state"], "awaiting_approval")
            self.assertEqual(job["submission_key"], "legacy:" + "a" * 32)
            self.assertEqual(job["request_fingerprint"], "b" * 64)
            self.assertEqual(job["attempt_count"], 0)
            self.assertEqual(job["max_attempts"], 3)
            self.assertEqual(job["model_call_limit"], 64)
            self.assertEqual(job["model_calls_reserved"], 0)
            self.assertIsNotNone(job["queued_at"])
            self.assertIsNotNone(job["available_at"])
            self.assertIsNotNone(job["updated_at"])
            self.assertEqual(interrupted["state"], "queued")
            self.assertEqual(interrupted["submission_key"], "legacy:" + "c" * 32)
            self.assertEqual(interrupted["request_fingerprint"], "d" * 64)
            self.assertEqual(
                [
                    (
                        row["id"],
                        row["state"],
                        row["error_code"],
                        row["last_error_category"],
                        row["completed_at"] is not None,
                    )
                    for row in unavailable_inline
                ],
                [
                    (
                        "e" * 32,
                        "failed",
                        "legacy_payload_unavailable",
                        "schema_policy",
                        True,
                    ),
                    (
                        "f" * 32,
                        "failed",
                        "legacy_payload_unavailable",
                        "schema_policy",
                        True,
                    ),
                ],
            )
            self.assertEqual(delivery["review_job_id"], "a" * 32)
            self.assertEqual(
                {str(row[0]): int(row[1]) for row in quotas},
                {"organization": 1000, "repository": 100},
            )
            self.assertNotIn("jobs", tables)
            self.assertNotIn("deliveries", tables)
        finally:
            engine.dispose()

    def test_provider_usage_attempts_are_backfilled_before_unique_constraint(self):
        upgrade_database(self.database_url, "0002_week7_legacy")
        engine = create_engine(self.database_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO organizations "
                        "(id, slug, display_name, policy_version, created_at) VALUES "
                        "('provider-org', 'provider-org', 'Provider Org', 'rbac/v1', "
                        "'2026-07-22T00:00:00Z')"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO repositories "
                        "(id, organization_id, alias, mode, budget_microusd, policy_version, "
                        "active, created_at) VALUES ('provider-repo', 'provider-org', "
                        "'owner/provider', 'shadow', NULL, 'rbac/v1', 1, "
                        "'2026-07-22T00:00:00Z')"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO review_jobs "
                        "(id, organization_id, repository_id, submitted_by, correlation_id, "
                        "source_kind, repository_alias, source_ref, source_sha256, source_bytes, "
                        "state, created_at, started_at, completed_at, review_json, error_code) "
                        "VALUES ('provider-job', 'provider-org', 'provider-repo', 'actor', "
                        "'correlation', 'pull_request', 'owner/provider', '1', :sha, 0, "
                        "'queued', '2026-07-22T00:00:00Z', NULL, NULL, NULL, NULL)"
                    ),
                    {"sha": "e" * 64},
                )
                for suffix, created in (("a", "00"), ("b", "01")):
                    connection.execute(
                        text(
                            "INSERT INTO provider_usage "
                            "(id, organization_id, repository_id, review_job_id, provider, "
                            "model, input_tokens, output_tokens, cost_microusd, pricing_version, "
                            "created_at) VALUES (:id, 'provider-org', 'provider-repo', "
                            "'provider-job', 'fake', 'fake-model', 1, 1, NULL, NULL, :created)"
                        ),
                        {
                            "id": f"usage-{suffix}",
                            "created": f"2026-07-22T00:00:{created}Z",
                        },
                    )
        finally:
            engine.dispose()

        upgrade_database(self.database_url)
        engine = create_engine(self.database_url)
        try:
            with engine.connect() as connection:
                rows = connection.execute(
                    text(
                        "SELECT id, attempt_count, llm_calls FROM provider_usage "
                        "ORDER BY id"
                    )
                ).mappings().all()
            self.assertEqual(
                [(row["id"], row["attempt_count"], row["llm_calls"]) for row in rows],
                [("usage-a", 0, 0), ("usage-b", 1, 0)],
            )
            with self.assertRaises(IntegrityError), engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO provider_usage "
                        "(id, organization_id, repository_id, review_job_id, provider, model, "
                        "input_tokens, output_tokens, cost_microusd, pricing_version, created_at, "
                        "attempt_count, llm_calls) VALUES ('usage-c', 'provider-org', "
                        "'provider-repo', 'provider-job', 'fake', 'fake-model', 0, 0, NULL, "
                        "NULL, '2026-07-22T00:00:02Z', 0, 0)"
                    )
                )
        finally:
            engine.dispose()

    def test_empty_database_phase9c_downgrade_restores_phase9b_schema(self):
        upgrade_database(self.database_url)
        self.downgrade_database("0002_week7_legacy")
        self.assertEqual(current_revision(self.database_url), "0002_week7_legacy")

        engine = create_engine(self.database_url)
        try:
            schema = inspect(engine)
            tables = set(schema.get_table_names())
            self.assertNotIn("service_quotas", tables)
            self.assertNotIn("worker_instances", tables)
            self.assertNotIn("submission_events", tables)
            self.assertNotIn("review_idempotency_keys", tables)
            review_columns = {
                column["name"] for column in schema.get_columns("review_jobs")
            }
            self.assertTrue(PHASE9C_JOB_COLUMNS.isdisjoint(review_columns))
            provider_columns = {
                column["name"] for column in schema.get_columns("provider_usage")
            }
            self.assertTrue({"attempt_count", "llm_calls"}.isdisjoint(provider_columns))
            state_checks = {
                item["name"]: item["sqltext"]
                for item in schema.get_check_constraints("review_jobs")
            }
            self.assertIn("succeeded", state_checks["ck_review_jobs_state"])
            self.assertNotIn("awaiting_approval", state_checks["ck_review_jobs_state"])
        finally:
            engine.dispose()

        upgrade_database(self.database_url)
        self.assertEqual(current_revision(self.database_url), schema_head())

    def test_service_store_refuses_unversioned_database_without_mutating_schema(self):
        connection = sqlite3.connect(self.database_path)
        connection.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
        connection.execute("INSERT INTO sentinel VALUES ('unchanged')")
        connection.commit()
        connection.close()

        with self.assertRaises(MigrationRequired):
            JobStore(self.root / "state", database_url=self.database_url, auto_migrate=False)

        connection = sqlite3.connect(self.database_path)
        try:
            self.assertEqual(connection.execute("SELECT value FROM sentinel").fetchone()[0], "unchanged")
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            connection.close()
        self.assertNotIn("review_jobs", tables)

    def test_postgres_url_selects_psycopg_dialect_without_connecting(self):
        engine = create_engine("postgresql+psycopg://user:password@127.0.0.1/database")
        try:
            self.assertEqual(engine.dialect.name, "postgresql")
            self.assertEqual(engine.dialect.driver, "psycopg")
        finally:
            engine.dispose()

    def test_composite_foreign_keys_reject_cross_tenant_lineage(self):
        upgrade_database(self.database_url)
        database = Database(self.database_url)
        try:
            org_a = database.create_organization("constraint-a", "Constraint A")
            org_b = database.create_organization("constraint-b", "Constraint B")
            repo_b = database.register_repository(org_b["id"], "owner/constraint-b")
            member_a = database.create_membership(
                org_a["id"],
                subject="reviewer-a",
                display_name="Reviewer A",
                role=Role.REVIEWER,
            )
            with self.assertRaises(IntegrityError), database.engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO repository_access "
                        "(id, organization_id, repository_id, user_id, created_at) "
                        "VALUES ('invalid-cross-tenant', :org, :repo, :user, "
                        "'2026-07-22T00:00:00Z')"
                    ),
                    {
                        "org": org_a["id"],
                        "repo": repo_b["id"],
                        "user": member_a["user_id"],
                    },
                )
        finally:
            database.close()


if __name__ == "__main__":
    unittest.main()
