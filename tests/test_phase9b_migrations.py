import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

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
}


class Phase9BMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database_path = self.root / "reviews.sqlite3"
        self.database_url = sqlite_database_url(self.database_path)

    def tearDown(self):
        self.temp.cleanup()

    def test_empty_database_upgrade_creates_head_and_tenant_schema(self):
        upgrade_database(self.database_url)
        self.assertEqual(current_revision(self.database_url), schema_head())
        require_schema_head(self.database_url)

        engine = create_engine(self.database_url)
        try:
            schema = inspect(engine)
            tables = set(schema.get_table_names())
            self.assertTrue(EXPECTED_TENANT_TABLES.issubset(tables))
            for table in EXPECTED_TENANT_TABLES:
                columns = {column["name"] for column in schema.get_columns(table)}
                self.assertIn("organization_id", columns, table)
            organization_columns = {
                column["name"] for column in schema.get_columns("organizations")
            }
            self.assertIn("id", organization_columns)
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
                delivery = migrated.execute(
                    text(
                        "SELECT * FROM webhook_deliveries WHERE delivery_id=:delivery"
                    ),
                    {"delivery": "delivery-legacy"},
                ).mappings().one()
                tables = set(inspect(migrated).get_table_names())
            self.assertEqual(job["organization_id"], "local-legacy-organization")
            self.assertEqual(json.loads(job["review_json"])["summary"], "preserved")
            self.assertEqual(delivery["review_job_id"], "a" * 32)
            self.assertNotIn("jobs", tables)
            self.assertNotIn("deliveries", tables)
        finally:
            engine.dispose()

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
