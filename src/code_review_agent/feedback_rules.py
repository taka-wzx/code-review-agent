"""Versioned repository feedback rules with immutable evaluation bindings."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Iterable, Mapping
import uuid

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine


RULE_ACTIONS = frozenset({"prioritize", "suppress", "require_verification"})
MAX_RULES = 64
MAX_RULES_BYTES = 32 * 1024
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")
_RULE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")
_CATEGORY = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
_RULE_FIELDS = frozenset({"rule_id", "category", "action", "condition", "rationale"})


class FeedbackRuleError(RuntimeError):
    """Base bounded feedback-rule failure."""


class FeedbackRuleValidationError(FeedbackRuleError):
    """The proposed rule document is invalid."""


class FeedbackRuleNotFound(FeedbackRuleError):
    """The requested scoped rule resource does not exist."""


class FeedbackRuleConflict(FeedbackRuleError):
    """A version or active-generation transition conflicts with durable state."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _bounded_string(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise FeedbackRuleValidationError(f"{field} must be a string")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise FeedbackRuleValidationError(f"{field} is empty or too long")
    return normalized


def normalize_rules(rules: Iterable[Mapping[str, Any]]) -> tuple[list[dict[str, str]], str, str]:
    if isinstance(rules, (str, bytes, Mapping)):
        raise FeedbackRuleValidationError("rules must be a sequence")
    items = list(rules)
    if not 1 <= len(items) <= MAX_RULES:
        raise FeedbackRuleValidationError("rules must contain between 1 and 64 items")
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in items:
        if not isinstance(raw, Mapping) or set(raw) != _RULE_FIELDS:
            raise FeedbackRuleValidationError("rule fields are invalid")
        rule_id = _bounded_string(raw["rule_id"], "rule_id", 64)
        category = _bounded_string(raw["category"], "category", 64).casefold()
        action = _bounded_string(raw["action"], "action", 32).casefold()
        condition = _bounded_string(raw["condition"], "condition", 256)
        rationale = _bounded_string(raw["rationale"], "rationale", 512)
        if _RULE_ID.fullmatch(rule_id) is None or _CATEGORY.fullmatch(category) is None:
            raise FeedbackRuleValidationError("rule identity or category is invalid")
        if action not in RULE_ACTIONS:
            raise FeedbackRuleValidationError("rule action is invalid")
        if rule_id.casefold() in seen:
            raise FeedbackRuleValidationError("rule IDs must be unique")
        seen.add(rule_id.casefold())
        normalized.append(
            {
                "action": action,
                "category": category,
                "condition": condition,
                "rationale": rationale,
                "rule_id": rule_id,
            }
        )
    canonical = _stable_json(normalized)
    if len(canonical.encode("utf-8")) > MAX_RULES_BYTES:
        raise FeedbackRuleValidationError("canonical rules document is too large")
    return normalized, canonical, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _scope(organization_id: str, repository_id: str) -> tuple[str, str]:
    organization = _bounded_string(organization_id, "organization_id", 64)
    repository = _bounded_string(repository_id, "repository_id", 64)
    return organization, repository


def _version(value: str) -> str:
    normalized = _bounded_string(value, "version", 64)
    if _VERSION.fullmatch(normalized) is None:
        raise FeedbackRuleValidationError("version is invalid")
    return normalized


def _row(row: Any) -> dict[str, Any]:
    return dict(row._mapping if hasattr(row, "_mapping") else row)


def _version_record(row: Any) -> dict[str, Any]:
    value = _row(row)
    return {
        "id": str(value["id"]),
        "organization_id": str(value["organization_id"]),
        "repository_id": str(value["repository_id"]),
        "version": str(value["version"]),
        "rules": json.loads(str(value["rules_json"])),
        "rules_sha256": str(value["rules_sha256"]),
        "created_by": str(value["created_by"]),
        "reason": str(value["reason"]),
        "created_at": str(value["created_at"]),
    }


def _binding_record(row: Any) -> dict[str, Any]:
    value = _row(row)
    return {
        "version_id": str(value["version_id"]),
        "version": str(value["version"]),
        "generation": int(value["generation"]),
        "rules": json.loads(str(value["rules_json"])),
        "rules_json": str(value["rules_json"]),
        "rules_sha256": str(value["rules_sha256"]),
        "bound_at": str(value.get("bound_at") or value.get("activated_at") or ""),
    }


def snapshot_identity(snapshot: Mapping[str, Any] | None) -> str | None:
    if snapshot is None:
        return None
    return "\0".join(
        (
            str(snapshot["version"]),
            str(snapshot["generation"]),
            str(snapshot["rules_sha256"]),
        )
    )


def public_binding(binding: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if binding is None:
        return None
    return {
        "version": str(binding["version"]),
        "generation": int(binding["generation"]),
        "rules_sha256": str(binding["rules_sha256"]),
        "rules": list(binding["rules"]),
        "bound_at": str(binding.get("bound_at") or ""),
    }


def render_feedback_rule_binding(binding: Mapping[str, Any] | None) -> str:
    if binding is None:
        return ""
    rules = _stable_json(binding["rules"])
    return (
        "Repository feedback rules bound at submission "
        f"(version={binding['version']}, generation={binding['generation']}, "
        f"sha256={binding['rules_sha256']}):\n{rules}"
    )


class FeedbackRuleStore:
    def __init__(self, database_or_engine: Any) -> None:
        self.engine: Engine = getattr(database_or_engine, "engine", database_or_engine)

    @staticmethod
    def _active_snapshot(
        connection: Connection,
        organization_id: str,
        repository_id: str,
        *,
        lock: bool = False,
    ) -> dict[str, Any] | None:
        suffix = " FOR UPDATE" if lock and connection.dialect.name == "postgresql" else ""
        row = connection.execute(
            text(
                "SELECT a.version_id, a.generation, a.activated_at, v.version, "
                "v.rules_json, v.rules_sha256 FROM repository_feedback_rule_active a "
                "JOIN repository_feedback_rule_versions v ON v.id=a.version_id "
                "WHERE a.organization_id=:org AND a.repository_id=:repo" + suffix
            ),
            {"org": organization_id, "repo": repository_id},
        ).first()
        return _binding_record(row) if row is not None else None

    @classmethod
    def snapshot_for_repository(
        cls,
        connection: Connection,
        organization_id: str,
        repository_id: str,
    ) -> dict[str, Any] | None:
        return cls._active_snapshot(connection, organization_id, repository_id)

    def create_version(
        self,
        *,
        organization_id: str,
        repository_id: str,
        version: str,
        rules: Iterable[Mapping[str, Any]],
        principal_id: str,
        reason: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        organization_id, repository_id = _scope(organization_id, repository_id)
        version = _version(version)
        principal_id = _bounded_string(principal_id, "principal_id", 64)
        reason = _bounded_string(reason, "reason", 512)
        _, canonical, rules_sha256 = normalize_rules(rules)
        version_id = hashlib.sha256(
            f"{organization_id}\0{repository_id}\0{version}".encode("utf-8")
        ).hexdigest()
        created_at = _iso(now or _utc_now())
        with self.engine.begin() as connection:
            repository = connection.execute(
                text(
                    "SELECT 1 FROM repositories WHERE organization_id=:org AND id=:repo "
                    "AND active=:active"
                ),
                {"org": organization_id, "repo": repository_id, "active": True},
            ).first()
            if repository is None:
                raise FeedbackRuleNotFound("repository was not found")
            connection.execute(
                text(
                    "INSERT INTO repository_feedback_rule_versions "
                    "(id, organization_id, repository_id, version, rules_json, rules_sha256, "
                    "created_by, reason, created_at) VALUES "
                    "(:id, :org, :repo, :version, :rules, :sha, :actor, :reason, :created) "
                    "ON CONFLICT (id) DO NOTHING"
                ),
                {
                    "id": version_id,
                    "org": organization_id,
                    "repo": repository_id,
                    "version": version,
                    "rules": canonical,
                    "sha": rules_sha256,
                    "actor": principal_id,
                    "reason": reason,
                    "created": created_at,
                },
            )
            row = connection.execute(
                text(
                    "SELECT * FROM repository_feedback_rule_versions WHERE "
                    "organization_id=:org AND repository_id=:repo AND version=:version"
                ),
                {"org": organization_id, "repo": repository_id, "version": version},
            ).one()
            existing = _row(row)
            if existing["rules_sha256"] != rules_sha256 or existing["rules_json"] != canonical:
                raise FeedbackRuleConflict("version content is immutable")
        return _version_record(row)

    def list_versions(self, organization_id: str, repository_id: str) -> list[dict[str, Any]]:
        organization_id, repository_id = _scope(organization_id, repository_id)
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT * FROM repository_feedback_rule_versions WHERE "
                    "organization_id=:org AND repository_id=:repo ORDER BY created_at, id"
                ),
                {"org": organization_id, "repo": repository_id},
            ).all()
        return [_version_record(row) for row in rows]

    def active(self, organization_id: str, repository_id: str) -> dict[str, Any] | None:
        organization_id, repository_id = _scope(organization_id, repository_id)
        with self.engine.connect() as connection:
            return self._active_snapshot(connection, organization_id, repository_id)

    def transition(
        self,
        *,
        organization_id: str,
        repository_id: str,
        version: str,
        action: str,
        principal_id: str,
        reason: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        organization_id, repository_id = _scope(organization_id, repository_id)
        version = _version(version)
        if action not in {"activate", "rollback"}:
            raise FeedbackRuleValidationError("transition action is invalid")
        principal_id = _bounded_string(principal_id, "principal_id", 64)
        reason = _bounded_string(reason, "reason", 512)
        occurred_at = _iso(now or _utc_now())
        with self.engine.begin() as connection:
            target_row = connection.execute(
                text(
                    "SELECT * FROM repository_feedback_rule_versions WHERE "
                    "organization_id=:org AND repository_id=:repo AND version=:version"
                ),
                {"org": organization_id, "repo": repository_id, "version": version},
            ).first()
            if target_row is None:
                raise FeedbackRuleNotFound("feedback-rule version was not found")
            target = _version_record(target_row)
            current = self._active_snapshot(
                connection, organization_id, repository_id, lock=True
            )
            if current is not None and current["version_id"] == target["id"]:
                raise FeedbackRuleConflict("feedback-rule version is already active")
            if action == "rollback":
                if current is None:
                    raise FeedbackRuleConflict("rollback requires an active version")
                prior = connection.execute(
                    text(
                        "SELECT 1 FROM repository_feedback_rule_receipts WHERE "
                        "organization_id=:org AND repository_id=:repo AND "
                        "to_version_id=:target LIMIT 1"
                    ),
                    {"org": organization_id, "repo": repository_id, "target": target["id"]},
                ).first()
                if prior is None:
                    raise FeedbackRuleConflict("rollback target was never active")
            previous_generation = int(current["generation"]) if current else 0
            generation = previous_generation + 1
            if current is None:
                inserted = connection.execute(
                    text(
                        "INSERT INTO repository_feedback_rule_active "
                        "(organization_id, repository_id, version_id, generation, activated_by, "
                        "reason, activated_at) VALUES "
                        "(:org, :repo, :version_id, :generation, :actor, :reason, :occurred) "
                        "ON CONFLICT (organization_id, repository_id) DO NOTHING"
                    ),
                    {
                        "org": organization_id,
                        "repo": repository_id,
                        "version_id": target["id"],
                        "generation": generation,
                        "actor": principal_id,
                        "reason": reason,
                        "occurred": occurred_at,
                    },
                )
                if inserted.rowcount != 1:
                    raise FeedbackRuleConflict("active generation changed concurrently")
            else:
                updated = connection.execute(
                    text(
                        "UPDATE repository_feedback_rule_active SET version_id=:version_id, "
                        "generation=:generation, activated_by=:actor, reason=:reason, "
                        "activated_at=:occurred WHERE organization_id=:org AND "
                        "repository_id=:repo AND generation=:expected"
                    ),
                    {
                        "version_id": target["id"],
                        "generation": generation,
                        "actor": principal_id,
                        "reason": reason,
                        "occurred": occurred_at,
                        "org": organization_id,
                        "repo": repository_id,
                        "expected": previous_generation,
                    },
                )
                if updated.rowcount != 1:
                    raise FeedbackRuleConflict("active generation changed concurrently")
            receipt_id = uuid.uuid4().hex
            receipt_material = {
                "action": action,
                "from_rules_sha256": current["rules_sha256"] if current else None,
                "from_version": current["version"] if current else None,
                "generation": generation,
                "organization_id": organization_id,
                "principal_id": principal_id,
                "reason": reason,
                "receipt_id": receipt_id,
                "repository_id": repository_id,
                "to_rules_sha256": target["rules_sha256"],
                "to_version": target["version"],
                "occurred_at": occurred_at,
            }
            receipt_json = _stable_json(receipt_material)
            receipt_sha256 = hashlib.sha256(receipt_json.encode("utf-8")).hexdigest()
            connection.execute(
                text(
                    "INSERT INTO repository_feedback_rule_receipts "
                    "(id, organization_id, repository_id, action, from_version_id, "
                    "from_version, from_rules_sha256, to_version_id, to_version, "
                    "to_rules_sha256, generation, principal_id, reason, occurred_at, "
                    "receipt_json, receipt_sha256) VALUES "
                    "(:id, :org, :repo, :action, :from_id, :from_version, :from_sha, "
                    ":to_id, :to_version, :to_sha, :generation, :actor, :reason, "
                    ":occurred, :receipt, :receipt_sha)"
                ),
                {
                    "id": receipt_id,
                    "org": organization_id,
                    "repo": repository_id,
                    "action": action,
                    "from_id": current["version_id"] if current else None,
                    "from_version": current["version"] if current else None,
                    "from_sha": current["rules_sha256"] if current else None,
                    "to_id": target["id"],
                    "to_version": target["version"],
                    "to_sha": target["rules_sha256"],
                    "generation": generation,
                    "actor": principal_id,
                    "reason": reason,
                    "occurred": occurred_at,
                    "receipt": receipt_json,
                    "receipt_sha": receipt_sha256,
                },
            )
            active = public_binding(
                {
                    "version": target["version"],
                    "generation": generation,
                    "rules_sha256": target["rules_sha256"],
                    "rules": target["rules"],
                    "bound_at": occurred_at,
                }
            )
        return {
            "active": active,
            "receipt": {**receipt_material, "receipt_sha256": receipt_sha256},
        }

    def list_receipts(self, organization_id: str, repository_id: str) -> list[dict[str, Any]]:
        organization_id, repository_id = _scope(organization_id, repository_id)
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT receipt_json, receipt_sha256 FROM "
                    "repository_feedback_rule_receipts WHERE organization_id=:org AND "
                    "repository_id=:repo ORDER BY generation"
                ),
                {"org": organization_id, "repo": repository_id},
            ).all()
        return [
            {**json.loads(str(row._mapping["receipt_json"])),
             "receipt_sha256": str(row._mapping["receipt_sha256"])}
            for row in rows
        ]

    @classmethod
    def bind_snapshot(
        cls,
        connection: Connection,
        *,
        organization_id: str,
        repository_id: str,
        review_job_id: str,
        snapshot: Mapping[str, Any],
        bound_at: datetime,
    ) -> dict[str, Any]:
        connection.execute(
            text(
                "INSERT INTO review_feedback_rule_bindings "
                "(review_job_id, organization_id, repository_id, version_id, version, "
                "generation, rules_json, rules_sha256, bound_at) VALUES "
                "(:job, :org, :repo, :version_id, :version, :generation, :rules, :sha, :bound) "
                "ON CONFLICT (review_job_id) DO NOTHING"
            ),
            {
                "job": review_job_id,
                "org": organization_id,
                "repo": repository_id,
                "version_id": snapshot["version_id"],
                "version": snapshot["version"],
                "generation": snapshot["generation"],
                "rules": snapshot["rules_json"],
                "sha": snapshot["rules_sha256"],
                "bound": _iso(bound_at),
            },
        )
        binding = cls.binding_for_job(connection, organization_id, review_job_id)
        if binding is None or snapshot_identity(binding) != snapshot_identity(snapshot):
            raise FeedbackRuleConflict("review feedback-rule binding conflicts")
        return binding

    @staticmethod
    def binding_for_job(
        connection: Connection, organization_id: str, review_job_id: str
    ) -> dict[str, Any] | None:
        row = connection.execute(
            text(
                "SELECT version_id, version, generation, rules_json, rules_sha256, bound_at "
                "FROM review_feedback_rule_bindings WHERE organization_id=:org AND "
                "review_job_id=:job"
            ),
            {"org": organization_id, "job": review_job_id},
        ).first()
        return _binding_record(row) if row is not None else None

    def binding(self, organization_id: str, review_job_id: str) -> dict[str, Any] | None:
        organization_id = _bounded_string(organization_id, "organization_id", 64)
        review_job_id = _bounded_string(review_job_id, "review_job_id", 64)
        with self.engine.connect() as connection:
            return self.binding_for_job(connection, organization_id, review_job_id)
