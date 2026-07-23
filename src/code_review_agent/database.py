"""Versioned database lifecycle and organization-scoped identity storage."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import secrets
import sysconfig
from typing import Any, Iterable, Mapping
import uuid

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import Connection, make_url

from code_review_agent.identity import Principal, Role, token_digest


SOURCE_ROOT = Path(__file__).resolve().parents[2]


def _migration_layout() -> tuple[Path, Path]:
    source_ini = SOURCE_ROOT / "alembic.ini"
    source_scripts = SOURCE_ROOT / "migrations"
    if source_ini.is_file() and source_scripts.is_dir():
        return source_ini, source_scripts
    installed = Path(sysconfig.get_path("data")) / "code_review_agent_migrations"
    installed_ini = installed / "alembic.ini"
    if installed_ini.is_file() and (installed / "versions").is_dir():
        return installed_ini, installed
    raise DatabaseError("database migration resources are unavailable")


LOCAL_ORGANIZATION_ID = "local-development-organization"
LOCAL_USER_ID = "local-development-principal"
LOCAL_MEMBERSHIP_ID = "local-development-membership"


class DatabaseError(RuntimeError):
    """Stable database lifecycle failure."""


class MigrationRequired(DatabaseError):
    """The database is not at the application schema head."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_id() -> str:
    return uuid.uuid4().hex


def _ensure_default_service_quotas(
    connection: Connection,
    organization_id: str,
    repository_id: str | None,
    occurred_at: str,
) -> None:
    statement = text(
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
    scopes: list[tuple[str, str | None, int, int, int, int]] = [
        ("organization", None, 1000, 16, 600, 100000)
    ]
    if repository_id is not None:
        scopes.append(("repository", repository_id, 100, 2, 60, 10000))
    for kind, repo, queued, concurrent, rate, budget in scopes:
        connection.execute(
            statement,
            {
                "id": new_id(),
                "org": organization_id,
                "repo": repo,
                "kind": kind,
                "queued": queued,
                "concurrent": concurrent,
                "rate": rate,
                "budget": budget,
                "month": occurred_at[:7],
                "occurred": occurred_at,
            },
        )


def sqlite_database_url(path: Path) -> str:
    absolute = Path(path).resolve().as_posix()
    return f"sqlite+pysqlite:///{absolute}"


def _secret_file(variable: str) -> str | None:
    path_value = os.environ.get(variable)
    if not path_value:
        return None
    try:
        encoded = Path(path_value).read_bytes()
    except OSError as exc:
        raise DatabaseError(f"{variable} is unavailable") from exc
    if len(encoded) > 4096:
        raise DatabaseError(f"{variable} exceeds the supported size")
    try:
        value = encoded.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise DatabaseError(f"{variable} is not UTF-8") from exc
    if not value:
        raise DatabaseError(f"{variable} is empty")
    return value


def database_url_from_env(*, default: str | None = None) -> str:
    """Build a database URL without placing a password in Compose or argv."""

    configured = os.environ.get("CRAG_DATABASE_URL")
    if not configured:
        configured = _secret_file("CRAG_DATABASE_URL_FILE") or default
    if not configured:
        raise DatabaseError("CRAG_DATABASE_URL is required")
    password = _secret_file("CRAG_DATABASE_PASSWORD_FILE")
    if password is None:
        return configured
    try:
        parsed = make_url(configured)
    except Exception as exc:
        raise DatabaseError("CRAG_DATABASE_URL is invalid") from exc
    if parsed.drivername.startswith("sqlite"):
        raise DatabaseError("CRAG_DATABASE_PASSWORD_FILE cannot be used with SQLite")
    return parsed.set(password=password).render_as_string(hide_password=False)


def _alembic_config(database_url: str) -> Config:
    alembic_ini, migration_scripts = _migration_layout()
    config = Config(str(alembic_ini))
    config.set_main_option("script_location", str(migration_scripts))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def schema_head() -> str:
    head = ScriptDirectory.from_config(_alembic_config("sqlite://")).get_current_head()
    if head is None:
        raise DatabaseError("migration head is unavailable")
    return head


def upgrade_database(database_url: str, revision: str = "head") -> None:
    """Run the separately invoked migration lifecycle."""
    command.upgrade(_alembic_config(database_url), revision)


def current_revision(database_url: str) -> str | None:
    engine = create_database_engine(database_url)
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()


def require_schema_head(database_url: str) -> None:
    try:
        revision = current_revision(database_url)
    except Exception as exc:
        raise MigrationRequired("database schema version could not be verified") from exc
    if revision != schema_head():
        raise MigrationRequired("database migration is required before service startup")


def create_database_engine(database_url: str) -> Engine:
    engine = create_engine(database_url, future=True, pool_pre_ping=True)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def configure_sqlite(dbapi_connection, connection_record) -> None:
            del connection_record
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=10000")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return engine


def _mapping(row: Any) -> dict[str, Any]:
    return dict(row._mapping)


def _repository_id(prefix: str, organization_id: str, alias: str) -> str:
    digest = hashlib.sha256(f"{organization_id}\0{alias}".encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:32]}"


class Database:
    """Small SQLAlchemy transaction boundary shared by service adapters."""

    def __init__(self, database_url: str, *, check_schema: bool = True) -> None:
        self.database_url = database_url
        if check_schema:
            require_schema_head(database_url)
        self.engine = create_database_engine(database_url)

    def close(self) -> None:
        self.engine.dispose()

    def bootstrap_local(self, repository_aliases: Iterable[str]) -> Principal:
        """Create the deterministic loopback-only principal and repository rows."""
        now = utc_now()
        with self.engine.begin() as connection:
            exists = connection.execute(
                text("SELECT id FROM organizations WHERE id=:id"),
                {"id": LOCAL_ORGANIZATION_ID},
            ).first()
            if exists is None:
                connection.execute(
                    text(
                        "INSERT INTO organizations "
                        "(id, slug, display_name, policy_version, created_at) "
                        "VALUES (:id, 'local-development', 'Local development', "
                        "'local/v1', :created)"
                    ),
                    {"id": LOCAL_ORGANIZATION_ID, "created": now},
                )
                connection.execute(
                    text(
                        "INSERT INTO users "
                        "(id, organization_id, subject, display_name, email_hash, active, "
                        "created_at) VALUES (:id, :org, 'local:operator', "
                        "'Local operator', NULL, :active, :created)"
                    ),
                    {
                        "id": LOCAL_USER_ID,
                        "org": LOCAL_ORGANIZATION_ID,
                        "active": True,
                        "created": now,
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO memberships "
                        "(id, organization_id, user_id, role, created_at, updated_at) "
                        "VALUES (:id, :org, :user, 'maintainer', :created, :created)"
                    ),
                    {
                        "id": LOCAL_MEMBERSHIP_ID,
                        "org": LOCAL_ORGANIZATION_ID,
                        "user": LOCAL_USER_ID,
                        "created": now,
                    },
                )
            for alias in sorted(set(repository_aliases)):
                row = connection.execute(
                    text("SELECT id, organization_id FROM repositories WHERE alias=:alias"),
                    {"alias": alias},
                ).first()
                if row is None:
                    repository_id = _repository_id(
                        "local-repo", LOCAL_ORGANIZATION_ID, alias
                    )
                    connection.execute(
                        text(
                            "INSERT INTO repositories "
                            "(id, organization_id, alias, mode, budget_microusd, "
                            "policy_version, active, created_at) VALUES "
                            "(:id, :org, :alias, 'shadow', NULL, 'local/v1', :active, :created)"
                        ),
                        {
                            "id": repository_id,
                            "org": LOCAL_ORGANIZATION_ID,
                            "alias": alias,
                            "active": True,
                            "created": now,
                        },
                    )
                else:
                    repository_id = str(row._mapping["id"])
                    if row._mapping["organization_id"] != LOCAL_ORGANIZATION_ID:
                        raise DatabaseError("local repository alias belongs to another organization")
                _ensure_default_service_quotas(
                    connection,
                    LOCAL_ORGANIZATION_ID,
                    repository_id,
                    now,
                )
                access = connection.execute(
                    text(
                        "SELECT id FROM repository_access WHERE organization_id=:org "
                        "AND repository_id=:repo AND user_id=:user"
                    ),
                    {
                        "org": LOCAL_ORGANIZATION_ID,
                        "repo": repository_id,
                        "user": LOCAL_USER_ID,
                    },
                ).first()
                if access is None:
                    connection.execute(
                        text(
                            "INSERT INTO repository_access "
                            "(id, organization_id, repository_id, user_id, created_at) "
                            "VALUES (:id, :org, :repo, :user, :created)"
                        ),
                        {
                            "id": new_id(),
                            "org": LOCAL_ORGANIZATION_ID,
                            "repo": repository_id,
                            "user": LOCAL_USER_ID,
                            "created": now,
                        },
                    )
        return Principal(
            principal_id=LOCAL_USER_ID,
            user_id=LOCAL_USER_ID,
            organization_id=LOCAL_ORGANIZATION_ID,
            role=Role.MAINTAINER,
            auth_method="local_token",
            credential_id=None,
        )

    def create_organization(
        self, slug: str, display_name: str, *, policy_version: str = "rbac/v1"
    ) -> dict[str, Any]:
        record = {
            "id": new_id(),
            "slug": slug,
            "display_name": display_name,
            "policy_version": policy_version,
            "created_at": utc_now(),
        }
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO organizations "
                    "(id, slug, display_name, policy_version, created_at) "
                    "VALUES (:id, :slug, :display_name, :policy_version, :created_at)"
                ),
                record,
            )
            _ensure_default_service_quotas(
                connection,
                str(record["id"]),
                None,
                str(record["created_at"]),
            )
        return record

    def create_membership(
        self,
        organization_id: str,
        *,
        subject: str,
        display_name: str,
        role: Role,
        repository_ids: Iterable[str] = (),
    ) -> dict[str, Any]:
        now = utc_now()
        user_id = new_id()
        membership_id = new_id()
        repository_ids = tuple(dict.fromkeys(repository_ids))
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users "
                    "(id, organization_id, subject, display_name, email_hash, active, "
                    "created_at) VALUES (:id, :org, :subject, :name, NULL, :active, :created)"
                ),
                {
                    "id": user_id,
                    "org": organization_id,
                    "subject": subject,
                    "name": display_name,
                    "active": True,
                    "created": now,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO memberships "
                    "(id, organization_id, user_id, role, created_at, updated_at) "
                    "VALUES (:id, :org, :user, :role, :created, :created)"
                ),
                {
                    "id": membership_id,
                    "org": organization_id,
                    "user": user_id,
                    "role": role.value,
                    "created": now,
                },
            )
            for repository_id in repository_ids:
                owned = connection.execute(
                    text(
                        "SELECT id FROM repositories WHERE id=:repo AND organization_id=:org"
                    ),
                    {"repo": repository_id, "org": organization_id},
                ).first()
                if owned is None:
                    raise DatabaseError("repository is not in the organization")
                connection.execute(
                    text(
                        "INSERT INTO repository_access "
                        "(id, organization_id, repository_id, user_id, created_at) "
                        "VALUES (:id, :org, :repo, :user, :created)"
                    ),
                    {
                        "id": new_id(),
                        "org": organization_id,
                        "repo": repository_id,
                        "user": user_id,
                        "created": now,
                    },
                )
        return {
            "membership_id": membership_id,
            "user_id": user_id,
            "organization_id": organization_id,
            "subject": subject,
            "display_name": display_name,
            "role": role.value,
            "repository_ids": list(repository_ids),
        }

    def principal_for_user(
        self, organization_id: str, user_id: str, *, auth_method: str = "fake"
    ) -> Principal | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT u.id AS user_id, m.role FROM users u "
                    "JOIN memberships m ON m.organization_id=u.organization_id "
                    "AND m.user_id=u.id WHERE u.id=:user AND u.organization_id=:org "
                    "AND u.active=:active"
                ),
                {"user": user_id, "org": organization_id, "active": True},
            ).first()
        if row is None:
            return None
        return Principal(
            principal_id=str(row._mapping["user_id"]),
            user_id=str(row._mapping["user_id"]),
            organization_id=organization_id,
            role=Role(str(row._mapping["role"])),
            auth_method=auth_method,
        )

    def list_members(self, organization_id: str) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT m.id AS membership_id, u.id AS user_id, u.subject, "
                    "u.display_name, u.active, m.role, m.created_at, m.updated_at "
                    "FROM memberships m JOIN users u ON u.id=m.user_id "
                    "AND u.organization_id=m.organization_id "
                    "WHERE m.organization_id=:org ORDER BY u.subject"
                ),
                {"org": organization_id},
            ).all()
            access_rows = connection.execute(
                text(
                    "SELECT user_id, repository_id FROM repository_access "
                    "WHERE organization_id=:org ORDER BY repository_id"
                ),
                {"org": organization_id},
            ).all()
        access: dict[str, list[str]] = {}
        for row in access_rows:
            access.setdefault(str(row._mapping["user_id"]), []).append(
                str(row._mapping["repository_id"])
            )
        result = []
        for row in rows:
            item = _mapping(row)
            item["repository_ids"] = access.get(str(item["user_id"]), [])
            result.append(item)
        return result

    def update_membership(
        self,
        organization_id: str,
        membership_id: str,
        *,
        role: Role,
        repository_ids: Iterable[str],
    ) -> dict[str, Any] | None:
        now = utc_now()
        repository_ids = tuple(dict.fromkeys(repository_ids))
        with self.engine.begin() as connection:
            row = connection.execute(
                text(
                    "SELECT user_id FROM memberships WHERE id=:id AND organization_id=:org"
                ),
                {"id": membership_id, "org": organization_id},
            ).first()
            if row is None:
                return None
            user_id = str(row._mapping["user_id"])
            connection.execute(
                text(
                    "UPDATE memberships SET role=:role, updated_at=:updated "
                    "WHERE id=:id AND organization_id=:org"
                ),
                {
                    "role": role.value,
                    "updated": now,
                    "id": membership_id,
                    "org": organization_id,
                },
            )
            connection.execute(
                text(
                    "DELETE FROM repository_access WHERE organization_id=:org "
                    "AND user_id=:user"
                ),
                {"org": organization_id, "user": user_id},
            )
            for repository_id in repository_ids:
                owned = connection.execute(
                    text(
                        "SELECT id FROM repositories WHERE id=:repo AND organization_id=:org"
                    ),
                    {"repo": repository_id, "org": organization_id},
                ).first()
                if owned is None:
                    raise DatabaseError("repository is not in the organization")
                connection.execute(
                    text(
                        "INSERT INTO repository_access "
                        "(id, organization_id, repository_id, user_id, created_at) "
                        "VALUES (:id, :org, :repo, :user, :created)"
                    ),
                    {
                        "id": new_id(),
                        "org": organization_id,
                        "repo": repository_id,
                        "user": user_id,
                        "created": now,
                    },
                )
        return {
            "membership_id": membership_id,
            "user_id": user_id,
            "organization_id": organization_id,
            "role": role.value,
            "repository_ids": list(repository_ids),
        }

    def register_repository(
        self,
        organization_id: str,
        alias: str,
        *,
        mode: str = "shadow",
        budget_microusd: int | None = None,
        policy_version: str = "rbac/v1",
    ) -> dict[str, Any]:
        record = {
            "id": new_id(),
            "organization_id": organization_id,
            "alias": alias,
            "mode": mode,
            "budget_microusd": budget_microusd,
            "policy_version": policy_version,
            "active": True,
            "created_at": utc_now(),
        }
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO repositories "
                    "(id, organization_id, alias, mode, budget_microusd, policy_version, "
                    "active, created_at) VALUES (:id, :organization_id, :alias, :mode, "
                    ":budget_microusd, :policy_version, :active, :created_at)"
                ),
                record,
            )
            _ensure_default_service_quotas(
                connection,
                organization_id,
                str(record["id"]),
                str(record["created_at"]),
            )
        return record

    def list_repositories(self, organization_id: str) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT id, organization_id, alias, mode, budget_microusd, "
                    "policy_version, active, created_at FROM repositories "
                    "WHERE organization_id=:org ORDER BY alias"
                ),
                {"org": organization_id},
            ).all()
        return [_mapping(row) for row in rows]

    def update_repository(
        self,
        organization_id: str,
        repository_id: str,
        *,
        mode: str,
        budget_microusd: int | None,
        policy_version: str,
    ) -> dict[str, Any] | None:
        with self.engine.begin() as connection:
            result = connection.execute(
                text(
                    "UPDATE repositories SET mode=:mode, budget_microusd=:budget, "
                    "policy_version=:policy WHERE id=:id AND organization_id=:org"
                ),
                {
                    "mode": mode,
                    "budget": budget_microusd,
                    "policy": policy_version,
                    "id": repository_id,
                    "org": organization_id,
                },
            )
            if result.rowcount != 1:
                return None
            row = connection.execute(
                text(
                    "SELECT id, organization_id, alias, mode, budget_microusd, "
                    "policy_version, active, created_at FROM repositories "
                    "WHERE id=:id AND organization_id=:org"
                ),
                {"id": repository_id, "org": organization_id},
            ).one()
        return _mapping(row)

    def authorized_repository(
        self, principal: Principal, identity: str
    ) -> dict[str, Any] | None:
        parameters = {
            "org": principal.organization_id,
            "identity": identity.casefold(),
            "user": principal.user_id,
            "active": True,
        }
        access_clause = ""
        if principal.role is not Role.ORG_ADMIN:
            access_clause = (
                " AND EXISTS (SELECT 1 FROM repository_access a "
                "WHERE a.organization_id=r.organization_id AND a.repository_id=r.id "
                "AND a.user_id=:user)"
            )
        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT r.id, r.organization_id, r.alias, r.mode, r.budget_microusd, "
                    "r.policy_version, r.active FROM repositories r "
                    "WHERE r.organization_id=:org AND r.active=:active "
                    "AND (lower(r.alias)=:identity OR r.id=:identity)" + access_clause
                ),
                parameters,
            ).first()
        return None if row is None else _mapping(row)

    def repository_for_webhook(self, alias: str) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT id, organization_id, alias, mode, policy_version "
                    "FROM repositories WHERE lower(alias)=:alias AND active=:active"
                ),
                {"alias": alias.casefold(), "active": True},
            ).first()
        return None if row is None else _mapping(row)

    def create_credential(
        self, principal: Principal, *, expires_in_seconds: int
    ) -> dict[str, Any]:
        raw_token = "crag_" + secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        credential_id = new_id()
        record = {
            "id": credential_id,
            "organization_id": principal.organization_id,
            "user_id": principal.user_id,
            "token_hash": token_digest(raw_token),
            "token_prefix": raw_token[:12],
            "expires_at": (now + timedelta(seconds=expires_in_seconds)).isoformat().replace(
                "+00:00", "Z"
            ),
            "created_at": now.isoformat().replace("+00:00", "Z"),
        }
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO access_credentials "
                    "(id, organization_id, user_id, token_hash, token_prefix, expires_at, "
                    "revoked_at, last_used_at, created_at) VALUES "
                    "(:id, :organization_id, :user_id, :token_hash, :token_prefix, "
                    ":expires_at, NULL, NULL, :created_at)"
                ),
                record,
            )
        return {
            "credential_id": credential_id,
            "organization_id": principal.organization_id,
            "user_id": principal.user_id,
            "token": raw_token,
            "token_prefix": record["token_prefix"],
            "expires_at": record["expires_at"],
            "created_at": record["created_at"],
        }

    def authenticate_token(self, token: str) -> Principal | None:
        digest = token_digest(token)
        now = utc_now()
        with self.engine.begin() as connection:
            row = connection.execute(
                text(
                    "SELECT c.id AS credential_id, c.token_hash, c.organization_id, "
                    "c.user_id, c.expires_at, c.revoked_at, m.role FROM access_credentials c "
                    "JOIN users u ON u.id=c.user_id AND u.organization_id=c.organization_id "
                    "JOIN memberships m ON m.user_id=u.id "
                    "AND m.organization_id=u.organization_id "
                    "WHERE c.token_hash=:digest AND u.active=:active"
                ),
                {"digest": digest, "active": True},
            ).first()
            if row is None:
                return None
            item = row._mapping
            if (
                item["revoked_at"] is not None
                or str(item["expires_at"]) <= now
                or not secrets.compare_digest(str(item["token_hash"]), digest)
            ):
                return None
            connection.execute(
                text("UPDATE access_credentials SET last_used_at=:now WHERE id=:id"),
                {"now": now, "id": item["credential_id"]},
            )
        return Principal(
            principal_id=str(item["user_id"]),
            user_id=str(item["user_id"]),
            organization_id=str(item["organization_id"]),
            role=Role(str(item["role"])),
            auth_method="api_token",
            credential_id=str(item["credential_id"]),
        )

    def revoke_credential(
        self, principal: Principal, credential_id: str, *, allow_any_user: bool
    ) -> bool:
        with self.engine.begin() as connection:
            parameters = {
                "id": credential_id,
                "org": principal.organization_id,
                "user": principal.user_id,
                "revoked": utc_now(),
            }
            owner_clause = "" if allow_any_user else " AND user_id=:user"
            result = connection.execute(
                text(
                    "UPDATE access_credentials SET revoked_at=:revoked "
                    "WHERE id=:id AND organization_id=:org AND revoked_at IS NULL"
                    + owner_clause
                ),
                parameters,
            )
        return result.rowcount == 1

    def audit(
        self,
        *,
        principal: Principal,
        action: str,
        resource_type: str,
        resource_id: str,
        decision: str,
        correlation_id: str,
        policy_version: str = "rbac/v1",
        repository_id: str | None = None,
        reason_code: str | None = None,
    ) -> str:
        event_id = new_id()
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO audit_events "
                    "(id, organization_id, principal_id, repository_id, credential_id, "
                    "auth_method, action, resource_type, resource_id, decision, reason_code, "
                    "policy_version, occurred_at_utc, correlation_id) VALUES "
                    "(:id, :org, :principal, :repo, :credential, :auth, :action, "
                    ":resource_type, :resource_id, :decision, :reason, :policy, :occurred, "
                    ":correlation)"
                ),
                {
                    "id": event_id,
                    "org": principal.organization_id,
                    "principal": principal.principal_id,
                    "repo": repository_id,
                    "credential": principal.credential_id,
                    "auth": principal.auth_method,
                    "action": action,
                    "resource_type": resource_type,
                    "resource_id": resource_id[:128],
                    "decision": decision,
                    "reason": reason_code,
                    "policy": policy_version,
                    "occurred": utc_now(),
                    "correlation": correlation_id[:128],
                },
            )
        return event_id

    def list_audit_events(
        self, organization_id: str, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT id, principal_id, organization_id, repository_id, "
                    "credential_id, auth_method, action, resource_type, resource_id, "
                    "decision, reason_code, policy_version, occurred_at_utc, correlation_id "
                    "FROM audit_events WHERE organization_id=:org "
                    "ORDER BY occurred_at_utc DESC, id DESC LIMIT :limit"
                ),
                {"org": organization_id, "limit": limit},
            ).all()
        return [_mapping(row) for row in rows]

    def finding_for_principal(
        self, principal: Principal, finding_id: str
    ) -> dict[str, Any] | None:
        repository_access = ""
        if principal.role is not Role.ORG_ADMIN:
            repository_access = (
                " AND EXISTS (SELECT 1 FROM repository_access a WHERE "
                "a.organization_id=f.organization_id AND a.repository_id=f.repository_id "
                "AND a.user_id=:user)"
            )
        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT f.id, f.organization_id, f.repository_id, f.review_job_id, "
                    "f.fingerprint, f.content_sha256, f.path, f.line, f.severity, f.category, "
                    "f.status, f.payload_json, f.created_at FROM findings f "
                    "WHERE f.id=:id AND f.organization_id=:org" + repository_access
                ),
                {
                    "id": finding_id,
                    "org": principal.organization_id,
                    "user": principal.user_id,
                },
            ).first()
        return None if row is None else _mapping(row)

    def findings_for_review(
        self, principal: Principal, review_job_id: str
    ) -> list[dict[str, Any]]:
        access_clause = ""
        if principal.role is not Role.ORG_ADMIN:
            access_clause = (
                " AND EXISTS (SELECT 1 FROM repository_access a WHERE "
                "a.organization_id=f.organization_id AND a.repository_id=f.repository_id "
                "AND a.user_id=:user)"
            )
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT f.id, f.organization_id, f.repository_id, f.review_job_id, "
                    "f.fingerprint, f.content_sha256, f.path, f.line, f.severity, f.category, "
                    "f.status, f.payload_json, f.created_at FROM findings f "
                    "WHERE f.review_job_id=:job AND f.organization_id=:org" + access_clause
                ),
                {
                    "job": review_job_id,
                    "org": principal.organization_id,
                    "user": principal.user_id,
                },
            ).all()
        return [_mapping(row) for row in rows]

    def finding_detail(
        self, principal: Principal, finding_id: str
    ) -> dict[str, Any] | None:
        finding = self.finding_for_principal(principal, finding_id)
        if finding is None:
            return None
        with self.engine.connect() as connection:
            feedback = connection.execute(
                text(
                    "SELECT id, principal_id, decision, reason, created_at "
                    "FROM finding_feedback WHERE finding_id=:finding "
                    "AND organization_id=:org ORDER BY created_at, id"
                ),
                {"finding": finding_id, "org": principal.organization_id},
            ).all()
            approvals = connection.execute(
                text(
                    "SELECT id, principal_id, decision, content_sha256, policy_version, "
                    "created_at FROM approvals WHERE finding_id=:finding "
                    "AND organization_id=:org ORDER BY created_at, id"
                ),
                {"finding": finding_id, "org": principal.organization_id},
            ).all()
        finding["feedback"] = [_mapping(row) for row in feedback]
        finding["approvals"] = [_mapping(row) for row in approvals]
        return finding

    def create_feedback(
        self,
        principal: Principal,
        finding: Mapping[str, Any],
        *,
        decision: str,
        reason: str | None,
    ) -> dict[str, Any]:
        record = {
            "id": new_id(),
            "organization_id": principal.organization_id,
            "repository_id": finding["repository_id"],
            "finding_id": finding["id"],
            "principal_id": principal.user_id,
            "decision": decision,
            "reason": reason,
            "created_at": utc_now(),
        }
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO finding_feedback "
                    "(id, organization_id, repository_id, finding_id, principal_id, "
                    "decision, reason, created_at) VALUES (:id, :organization_id, "
                    ":repository_id, :finding_id, :principal_id, :decision, :reason, "
                    ":created_at)"
                ),
                record,
            )
        return record

    def decide_finding(
        self,
        principal: Principal,
        finding: Mapping[str, Any],
        *,
        decision: str,
        policy_version: str,
    ) -> dict[str, Any]:
        record = {
            "id": new_id(),
            "organization_id": principal.organization_id,
            "repository_id": finding["repository_id"],
            "finding_id": finding["id"],
            "principal_id": principal.user_id,
            "decision": decision,
            "content_sha256": finding["content_sha256"],
            "policy_version": policy_version,
            "created_at": utc_now(),
        }
        status = "approved" if decision == "approved" else "rejected"
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO approvals "
                    "(id, organization_id, repository_id, finding_id, principal_id, "
                    "decision, content_sha256, policy_version, created_at) VALUES "
                    "(:id, :organization_id, :repository_id, :finding_id, :principal_id, "
                    ":decision, :content_sha256, :policy_version, :created_at)"
                ),
                record,
            )
            connection.execute(
                text(
                    "UPDATE findings SET status=:status WHERE id=:id "
                    "AND organization_id=:org"
                ),
                {"status": status, "id": finding["id"], "org": principal.organization_id},
            )
        return record


def _default_database_url() -> str:
    if os.environ.get("CRAG_DATABASE_URL") or os.environ.get("CRAG_DATABASE_URL_FILE"):
        return database_url_from_env()
    configured_state = os.environ.get("CRAG_STATE_DIR")
    state = (
        Path(configured_state)
        if configured_state
        else Path.home() / ".crag" / "service"
    )
    state.mkdir(parents=True, exist_ok=True)
    return database_url_from_env(default=sqlite_database_url(state / "reviews.sqlite3"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage the code-review-agent database schema outside service workers"
    )
    parser.add_argument(
        "command", choices=["upgrade", "current", "check"], help="schema operation"
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="SQLAlchemy URL (defaults to CRAG_DATABASE_URL or local state SQLite)",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    database_url = args.database_url or _default_database_url()
    if args.command == "upgrade":
        upgrade_database(database_url)
        return
    revision = current_revision(database_url)
    if args.command == "current":
        print(revision or "unversioned")
        return
    require_schema_head(database_url)
    print(schema_head())


if __name__ == "__main__":
    main()
