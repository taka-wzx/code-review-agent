"""Hierarchical, tenant-scoped context for code review.

RunContext is deliberately in-memory. RepositoryMemory and OrganizationPolicy
are durable only after an explicit trusted-source check. Retrieval uses
PostgreSQL FTS when available and a deterministic lexical fallback for SQLite
tests; no model output, embedding, or vector database participates.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import json
import re
import threading
from typing import Any, Iterable, Mapping

from sqlalchemy import Engine, text


_SHA = re.compile(r"[0-9a-f]{7,64}\Z")
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _tokens(text_value: str) -> int:
    return max(1, (len(text_value.encode("utf-8")) + 3) // 4)


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


class ContextMode(str, Enum):
    OFF = "off"
    CURRENT_STATIC = "current_static"
    HIERARCHICAL = "hierarchical"

    @classmethod
    def parse(cls, value: str | ContextMode) -> ContextMode:
        if isinstance(value, cls):
            return value
        try:
            return cls(value)
        except ValueError as exc:
            raise ValueError("context mode must be off, current_static, or hierarchical") from exc


class MemorySource(str, Enum):
    HUMAN_CONFIRMED = "human_confirmed"
    ADMIN_CONFIG = "admin_config"
    REPOSITORY_FILE = "repository_file"
    MODEL_OUTPUT = "model_output"


class MemoryKind(str, Enum):
    CONVENTION = "convention"
    BUILD_COMMAND = "build_command"
    TEST_COMMAND = "test_command"
    LANGUAGE = "language"
    FRAMEWORK = "framework"
    CODE_OWNER = "code_owner"
    RISK_PATH = "risk_path"
    ACCEPTED_FINDING = "accepted_finding"
    SUPPRESSION_CANDIDATE = "suppression_candidate"


class MemoryTrustError(ValueError):
    """The proposed durable write is not from a trusted, scoped source."""


@dataclass
class RunContext:
    """Mutable state for one review attempt; it has no persistence method."""

    diff_sha256: str
    source_revision: str
    token_budget: int
    current_plan: list[str] = field(default_factory=list)
    tool_summaries: list[dict[str, Any]] = field(default_factory=list)
    finder_state: dict[str, Any] = field(default_factory=dict)
    verifier_state: dict[str, Any] = field(default_factory=dict)
    token_used: int = 0
    closed: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    @classmethod
    def create(
        cls, diff_text: str, source_revision: str, token_budget: int
    ) -> RunContext:
        if token_budget < 1:
            raise ValueError("token budget must be positive")
        return cls(
            diff_sha256=hashlib.sha256(diff_text.encode("utf-8")).hexdigest(),
            source_revision=source_revision,
            token_budget=token_budget,
        )

    def set_plan(self, steps: Iterable[str]) -> None:
        normalized = [step.strip() for step in steps if isinstance(step, str) and step.strip()]
        with self._lock:
            self._ensure_open()
            self.current_plan = normalized[:32]

    def record_tool(self, component: str, name: str, result: str) -> None:
        summary = {
            "component": component[:64],
            "tool": name[:64],
            "result_chars": len(result),
            "error": result.startswith("Error:"),
        }
        with self._lock:
            self._ensure_open()
            self.tool_summaries.append(summary)
            if len(self.tool_summaries) > 128:
                del self.tool_summaries[:-128]

    def record_stage(self, component: str, **state: Any) -> None:
        bounded = {
            str(key)[:64]: value
            for key, value in state.items()
            if isinstance(value, (str, int, float, bool, type(None)))
        }
        with self._lock:
            self._ensure_open()
            target = self.verifier_state if component.startswith("verifier") else self.finder_state
            target.update(bounded)

    def consume_tokens(self, amount: int) -> bool:
        if amount < 0:
            raise ValueError("token usage cannot be negative")
        with self._lock:
            self._ensure_open()
            if self.token_used + amount > self.token_budget:
                return False
            self.token_used += amount
            return True

    @property
    def token_remaining(self) -> int:
        with self._lock:
            return max(0, self.token_budget - self.token_used)

    def close(self) -> dict[str, Any]:
        with self._lock:
            self.closed = True
            return {
                "diff_sha256": self.diff_sha256,
                "source_revision": self.source_revision,
                "plan_steps": len(self.current_plan),
                "tool_calls": len(self.tool_summaries),
                "token_budget": self.token_budget,
                "token_used": self.token_used,
            }

    def _ensure_open(self) -> None:
        if self.closed:
            raise RuntimeError("run context is closed")


@dataclass(frozen=True)
class MemoryWrite:
    organization_id: str
    repository_id: str
    kind: MemoryKind
    content: str
    source_sha: str
    source_kind: MemorySource
    created_by: str
    confirmed_by: str | None
    reason: str
    valid_from_sha: str | None = None
    valid_until_sha: str | None = None
    expires_at: datetime | None = None
    path: str | None = None
    language: str | None = None
    symbol: str | None = None
    fingerprint: str | None = None

    def validate(self) -> None:
        if not self.organization_id or not self.repository_id:
            raise MemoryTrustError("organization and repository scope are required")
        if self.source_kind is MemorySource.MODEL_OUTPUT:
            raise MemoryTrustError("model output cannot become trusted repository memory")
        if not self.content.strip() or not self.reason.strip() or not self.created_by.strip():
            raise MemoryTrustError("content, reason, and creator are required")
        if _SHA.fullmatch(self.source_sha.casefold()) is None:
            raise MemoryTrustError("a valid source SHA is required")
        for value in (self.valid_from_sha, self.valid_until_sha):
            if value is not None and _SHA.fullmatch(value.casefold()) is None:
                raise MemoryTrustError("validity range SHAs must be valid")
        if self.source_kind is MemorySource.HUMAN_CONFIRMED and not self.confirmed_by:
            raise MemoryTrustError("human-confirmed memory requires confirmed_by")
        if self.kind in {MemoryKind.ACCEPTED_FINDING, MemoryKind.SUPPRESSION_CANDIDATE}:
            if self.source_kind is not MemorySource.HUMAN_CONFIRMED or not self.fingerprint:
                raise MemoryTrustError("finding memory requires human confirmation and fingerprint")
        if self.kind is MemoryKind.SUPPRESSION_CANDIDATE and not self.repository_id:
            raise MemoryTrustError("suppression candidates are repository-scoped only")


@dataclass(frozen=True)
class MemoryRecord:
    id: str
    organization_id: str
    repository_id: str
    kind: str
    content: str
    search_text: str
    source_sha: str
    source_kind: str
    created_by: str
    confirmed_by: str | None
    reason: str
    valid_from_sha: str | None
    valid_until_sha: str | None
    expires_at: str | None
    path: str | None
    language: str | None
    symbol: str | None
    fingerprint: str | None
    created_at: str
    fts_rank: float = 0.0
    graph_rank: int = 0

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> MemoryRecord:
        values = dict(row)

        def optional(name: str) -> str | None:
            return None if values.get(name) is None else str(values[name])

        return cls(
            id=str(values["id"]),
            organization_id=str(values["organization_id"]),
            repository_id=str(values["repository_id"]),
            kind=str(values["kind"]),
            content=str(values["content"]),
            search_text=str(values.get("search_text") or ""),
            source_sha=str(values["source_sha"]),
            source_kind=str(values["source_kind"]),
            created_by=str(values["created_by"]),
            confirmed_by=optional("confirmed_by"),
            reason=str(values["reason"]),
            valid_from_sha=optional("valid_from_sha"),
            valid_until_sha=optional("valid_until_sha"),
            expires_at=optional("expires_at"),
            path=optional("path"),
            language=optional("language"),
            symbol=optional("symbol"),
            fingerprint=optional("fingerprint"),
            created_at=str(values["created_at"]),
            fts_rank=float(values.get("fts_rank") or 0.0),
            graph_rank=int(values.get("graph_rank") or 0),
        )


@dataclass(frozen=True)
class MemoryQuery:
    organization_id: str
    repository_id: str
    base_sha: str
    paths: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    lexical: str = ""
    token_budget: int = 2000
    now: datetime | None = None

    def validate(self) -> None:
        if not self.organization_id or not self.repository_id:
            raise ValueError("organization and repository scope are required")
        if _SHA.fullmatch(self.base_sha.casefold()) is None:
            raise ValueError("base SHA is invalid")
        if self.token_budget < 0:
            raise ValueError("token budget cannot be negative")


@dataclass(frozen=True)
class ContextSelection:
    text: str
    records: tuple[MemoryRecord, ...]
    token_used: int
    provenance: tuple[dict[str, str | None], ...]


@dataclass(frozen=True)
class OrganizationPolicy:
    organization_id: str
    version: str
    severity_levels: tuple[str, ...]
    forbidden_operations: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    approval_threshold: int
    retention_days: int
    cost_budget_microusd: int
    created_by: str
    reason: str
    source_sha: str
    created_at: str = ""
    invalidated_at: str | None = None

    def validate(self) -> None:
        if not self.organization_id or not self.version or not self.created_by or not self.reason:
            raise MemoryTrustError("policy scope, version, creator, and reason are required")
        if _SHA.fullmatch(self.source_sha.casefold()) is None:
            raise MemoryTrustError("policy source SHA is invalid")
        if not self.severity_levels or len(set(self.severity_levels)) != len(self.severity_levels):
            raise MemoryTrustError("policy severity levels must be unique and non-empty")
        if not 1 <= self.approval_threshold <= 100:
            raise MemoryTrustError("approval threshold must be between 1 and 100")
        if not 1 <= self.retention_days <= 3650 or self.cost_budget_microusd < 0:
            raise MemoryTrustError("policy retention or cost budget is invalid")


class RepositoryMemoryStore:
    def __init__(self, database_or_engine: Any) -> None:
        self.engine: Engine = getattr(database_or_engine, "engine", database_or_engine)

    @staticmethod
    def _entry_id(write: MemoryWrite) -> str:
        identity = _stable_json(
            {
                "organization_id": write.organization_id,
                "repository_id": write.repository_id,
                "kind": write.kind.value,
                "content": write.content,
                "source_sha": write.source_sha.casefold(),
                "path": write.path,
                "language": write.language,
                "symbol": write.symbol,
                "fingerprint": write.fingerprint,
            }
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def add(self, write: MemoryWrite, *, now: datetime | None = None) -> str:
        write.validate()
        occurred = _iso(now or _utc_now())
        entry_id = self._entry_id(write)
        search_text = " ".join(
            value for value in (write.kind.value, write.content, write.path, write.language,
                                write.symbol, write.fingerprint) if value
        )
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO repository_memory_entries "
                    "(id, organization_id, repository_id, kind, content, search_text, path, "
                    "language, symbol, fingerprint, source_sha, source_kind, created_by, "
                    "confirmed_by, reason, valid_from_sha, valid_until_sha, expires_at, "
                    "invalidated_at, created_at, updated_at) VALUES "
                    "(:id, :org, :repo, :kind, :content, :search, :path, :language, :symbol, "
                    ":fingerprint, :source, :source_kind, :created_by, :confirmed_by, :reason, "
                    ":valid_from, :valid_until, :expires, NULL, :created, :created) "
                    "ON CONFLICT (id) DO UPDATE SET confirmed_by=excluded.confirmed_by, "
                    "reason=excluded.reason, expires_at=excluded.expires_at, "
                    "invalidated_at=NULL, updated_at=excluded.updated_at"
                ),
                {
                    "id": entry_id,
                    "org": write.organization_id,
                    "repo": write.repository_id,
                    "kind": write.kind.value,
                    "content": write.content,
                    "search": search_text,
                    "path": write.path,
                    "language": write.language,
                    "symbol": write.symbol,
                    "fingerprint": write.fingerprint,
                    "source": write.source_sha.casefold(),
                    "source_kind": write.source_kind.value,
                    "created_by": write.created_by,
                    "confirmed_by": write.confirmed_by,
                    "reason": write.reason,
                    "valid_from": write.valid_from_sha,
                    "valid_until": write.valid_until_sha,
                    "expires": _iso(write.expires_at) if write.expires_at else None,
                    "created": occurred,
                },
            )
        return entry_id

    def add_feedback(
        self,
        *,
        organization_id: str,
        repository_id: str,
        decision: str,
        fingerprint: str,
        finding_hash: str,
        source_sha: str,
        principal_id: str,
        reason: str,
        retention_days: int,
        now: datetime | None = None,
    ) -> str:
        if decision not in {"accepted", "rejected"}:
            raise MemoryTrustError("only accepted or rejected feedback becomes memory")
        occurred = now or _utc_now()
        kind = (
            MemoryKind.ACCEPTED_FINDING
            if decision == "accepted"
            else MemoryKind.SUPPRESSION_CANDIDATE
        )
        content = _stable_json(
            {"decision": decision, "fingerprint": fingerprint, "finding_hash": finding_hash}
        )
        return self.add(
            MemoryWrite(
                organization_id=organization_id,
                repository_id=repository_id,
                kind=kind,
                content=content,
                source_sha=source_sha,
                source_kind=MemorySource.HUMAN_CONFIRMED,
                created_by=principal_id,
                confirmed_by=principal_id,
                reason=reason,
                expires_at=occurred + timedelta(days=retention_days),
                fingerprint=fingerprint,
            ),
            now=occurred,
        )

    def add_edge(
        self,
        *,
        organization_id: str,
        repository_id: str,
        source_sha: str,
        memory_id: str,
        relation: str,
        path: str | None = None,
        symbol: str | None = None,
        target_path: str | None = None,
        target_symbol: str | None = None,
    ) -> str:
        if _SHA.fullmatch(source_sha.casefold()) is None or not relation.strip():
            raise ValueError("graph source SHA and relation are required")
        edge_id = hashlib.sha256(
            _stable_json([organization_id, repository_id, source_sha, memory_id, relation,
                          path, symbol, target_path, target_symbol]).encode("utf-8")
        ).hexdigest()
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO repository_memory_edges "
                    "(id, organization_id, repository_id, source_sha, memory_id, relation, "
                    "path, symbol, target_path, target_symbol, created_at) VALUES "
                    "(:id, :org, :repo, :sha, :memory, :relation, :path, :symbol, "
                    ":target_path, :target_symbol, :created) ON CONFLICT (id) DO NOTHING"
                ),
                {
                    "id": edge_id,
                    "org": organization_id,
                    "repo": repository_id,
                    "sha": source_sha.casefold(),
                    "memory": memory_id,
                    "relation": relation,
                    "path": path,
                    "symbol": symbol,
                    "target_path": target_path,
                    "target_symbol": target_symbol,
                    "created": _iso(_utc_now()),
                },
            )
        return edge_id

    def _candidate_rows(self, query: MemoryQuery) -> list[dict[str, Any]]:
        now = _iso(query.now or _utc_now())
        base = {
            "org": query.organization_id,
            "repo": query.repository_id,
            "sha": query.base_sha.casefold(),
            "now": now,
            "lexical": query.lexical.strip(),
        }
        with self.engine.connect() as connection:
            if connection.dialect.name == "postgresql" and query.lexical.strip():
                statement = text(
                    "SELECT m.*, ts_rank_cd(to_tsvector('simple', m.search_text), "
                    "plainto_tsquery('simple', :lexical)) AS fts_rank "
                    "FROM repository_memory_entries m WHERE m.organization_id=:org "
                    "AND m.repository_id=:repo AND (m.source_sha=:sha OR "
                    "m.valid_from_sha=:sha OR m.valid_until_sha=:sha) "
                    "AND m.invalidated_at IS NULL AND (m.expires_at IS NULL OR m.expires_at>:now) "
                    "AND to_tsvector('simple', m.search_text) @@ "
                    "plainto_tsquery('simple', :lexical) ORDER BY fts_rank DESC, m.id"
                )
            else:
                statement = text(
                    "SELECT m.*, 0.0 AS fts_rank FROM repository_memory_entries m "
                    "WHERE m.organization_id=:org AND m.repository_id=:repo "
                    "AND (m.source_sha=:sha OR m.valid_from_sha=:sha OR m.valid_until_sha=:sha) "
                    "AND m.invalidated_at IS NULL "
                    "AND (m.expires_at IS NULL OR m.expires_at>:now) ORDER BY m.id"
                )
            rows = [dict(row._mapping) for row in connection.execute(statement, base).all()]
            graph = connection.execute(
                text(
                    "SELECT memory_id, path, symbol, target_path, target_symbol "
                    "FROM repository_memory_edges WHERE organization_id=:org "
                    "AND repository_id=:repo AND source_sha=:sha"
                ),
                base,
            ).all()
        path_set = {value.casefold() for value in query.paths}
        symbol_set = {value.casefold() for value in query.symbols}
        graph_rank: dict[str, int] = {}
        for raw in graph:
            edge = raw._mapping
            edge_paths = {str(edge[key]).casefold() for key in ("path", "target_path") if edge[key]}
            edge_symbols = {str(edge[key]).casefold() for key in ("symbol", "target_symbol") if edge[key]}
            if edge_paths & path_set or edge_symbols & symbol_set:
                memory_id = str(edge["memory_id"])
                graph_rank[memory_id] = graph_rank.get(memory_id, 0) + 1
        for row in rows:
            row["graph_rank"] = graph_rank.get(str(row["id"]), 0)
        return rows

    @staticmethod
    def _score(row: Mapping[str, Any], query: MemoryQuery) -> tuple[int, float, str]:
        paths = {value.casefold() for value in query.paths}
        languages = {value.casefold() for value in query.languages}
        symbols = {value.casefold() for value in query.symbols}
        lexical = {word.casefold() for word in _WORD.findall(query.lexical)}
        haystack = {word.casefold() for word in _WORD.findall(str(row.get("search_text") or ""))}
        score = 0
        if row.get("path") and str(row["path"]).casefold() in paths:
            score += 120
        if row.get("language") and str(row["language"]).casefold() in languages:
            score += 60
        if row.get("symbol") and str(row["symbol"]).casefold() in symbols:
            score += 100
        score += int(row.get("graph_rank") or 0) * 40
        score += len(lexical & haystack) * 10
        if row.get("kind") in {
            MemoryKind.ACCEPTED_FINDING.value,
            MemoryKind.SUPPRESSION_CANDIDATE.value,
        }:
            score += 5
        return (-score, -float(row.get("fts_rank") or 0.0), str(row["id"]))

    def retrieve(self, query: MemoryQuery) -> ContextSelection:
        query.validate()
        rows = self._candidate_rows(query)
        records = [MemoryRecord.from_row(row) for row in rows]
        records.sort(key=lambda record: self._score(record.__dict__, query))
        selected: list[MemoryRecord] = []
        rendered: list[str] = []
        provenance: list[dict[str, str | None]] = []
        used = 0
        for record in records:
            section = (
                f"### {record.kind}: {record.path or record.symbol or record.id[:12]}\n"
                f"{record.content}\n"
                f"Provenance: memory_id={record.id}; source_sha={record.source_sha}; "
                f"source={record.source_kind}; reason={record.reason}"
            )
            cost = _tokens(section)
            if used + cost > query.token_budget:
                continue
            used += cost
            selected.append(record)
            rendered.append(section)
            provenance.append(
                {
                    "memory_id": record.id,
                    "organization_id": record.organization_id,
                    "repository_id": record.repository_id,
                    "source_sha": record.source_sha,
                    "source_kind": record.source_kind,
                    "confirmed_by": record.confirmed_by,
                    "reason": record.reason,
                }
            )
        return ContextSelection(
            text="\n\n".join(rendered),
            records=tuple(selected),
            token_used=used,
            provenance=tuple(provenance),
        )

    def invalidate(
        self, organization_id: str, repository_id: str, memory_id: str, *, now: datetime | None = None
    ) -> bool:
        with self.engine.begin() as connection:
            result = connection.execute(
                text(
                    "UPDATE repository_memory_entries SET invalidated_at=:now, updated_at=:now "
                    "WHERE id=:id AND organization_id=:org AND repository_id=:repo "
                    "AND invalidated_at IS NULL"
                ),
                {"id": memory_id, "org": organization_id, "repo": repository_id,
                 "now": _iso(now or _utc_now())},
            )
        return result.rowcount == 1

    def purge_expired(self, organization_id: str, *, now: datetime | None = None) -> int:
        with self.engine.begin() as connection:
            result = connection.execute(
                text(
                    "DELETE FROM repository_memory_entries WHERE organization_id=:org "
                    "AND expires_at IS NOT NULL AND expires_at<=:now"
                ),
                {"org": organization_id, "now": _iso(now or _utc_now())},
            )
        return int(result.rowcount or 0)

    def remove_repository(self, organization_id: str, repository_id: str) -> int:
        with self.engine.begin() as connection:
            result = connection.execute(
                text(
                    "DELETE FROM repository_memory_entries WHERE organization_id=:org "
                    "AND repository_id=:repo"
                ),
                {"org": organization_id, "repo": repository_id},
            )
        return int(result.rowcount or 0)


class OrganizationPolicyStore:
    def __init__(self, database_or_engine: Any) -> None:
        self.engine: Engine = getattr(database_or_engine, "engine", database_or_engine)

    def put(self, policy: OrganizationPolicy, *, source_kind: MemorySource) -> str:
        policy.validate()
        if source_kind is not MemorySource.ADMIN_CONFIG:
            raise MemoryTrustError("organization policy requires administrator configuration")
        policy_id = hashlib.sha256(
            _stable_json([policy.organization_id, policy.version, policy.source_sha]).encode("utf-8")
        ).hexdigest()
        created = policy.created_at or _iso(_utc_now())
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO organization_policies "
                    "(id, organization_id, version, severity_json, forbidden_operations_json, "
                    "allowed_tools_json, approval_threshold, retention_days, cost_budget_microusd, "
                    "source_sha, source_kind, created_by, reason, invalidated_at, created_at) "
                    "VALUES (:id, :org, :version, :severity, :forbidden, :tools, :approval, "
                    ":retention, :budget, :sha, :source_kind, :created_by, :reason, NULL, :created) "
                    "ON CONFLICT (organization_id, version) DO UPDATE SET "
                    "severity_json=excluded.severity_json, "
                    "forbidden_operations_json=excluded.forbidden_operations_json, "
                    "allowed_tools_json=excluded.allowed_tools_json, "
                    "approval_threshold=excluded.approval_threshold, "
                    "retention_days=excluded.retention_days, "
                    "cost_budget_microusd=excluded.cost_budget_microusd, "
                    "source_sha=excluded.source_sha, source_kind=excluded.source_kind, "
                    "created_by=excluded.created_by, reason=excluded.reason, invalidated_at=NULL"
                ),
                {
                    "id": policy_id,
                    "org": policy.organization_id,
                    "version": policy.version,
                    "severity": _stable_json(policy.severity_levels),
                    "forbidden": _stable_json(policy.forbidden_operations),
                    "tools": _stable_json(policy.allowed_tools),
                    "approval": policy.approval_threshold,
                    "retention": policy.retention_days,
                    "budget": policy.cost_budget_microusd,
                    "sha": policy.source_sha.casefold(),
                    "source_kind": source_kind.value,
                    "created_by": policy.created_by,
                    "reason": policy.reason,
                    "created": created,
                },
            )
        return policy_id

    def active(self, organization_id: str, *, version: str | None = None) -> OrganizationPolicy | None:
        clause = " AND version=:version" if version is not None else ""
        parameters: dict[str, Any] = {"org": organization_id}
        if version is not None:
            parameters["version"] = version
        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT organization_id, version, severity_json, forbidden_operations_json, "
                    "allowed_tools_json, approval_threshold, retention_days, cost_budget_microusd, "
                    "created_by, reason, source_sha, created_at, invalidated_at "
                    "FROM organization_policies WHERE organization_id=:org "
                    "AND invalidated_at IS NULL" + clause + " ORDER BY created_at DESC, id DESC LIMIT 1"
                ),
                parameters,
            ).first()
        if row is None:
            return None
        value = row._mapping
        return OrganizationPolicy(
            organization_id=str(value["organization_id"]),
            version=str(value["version"]),
            severity_levels=tuple(json.loads(str(value["severity_json"]))),
            forbidden_operations=tuple(json.loads(str(value["forbidden_operations_json"]))),
            allowed_tools=tuple(json.loads(str(value["allowed_tools_json"]))),
            approval_threshold=int(value["approval_threshold"]),
            retention_days=int(value["retention_days"]),
            cost_budget_microusd=int(value["cost_budget_microusd"]),
            created_by=str(value["created_by"]),
            reason=str(value["reason"]),
            source_sha=str(value["source_sha"]),
            created_at=str(value["created_at"]),
            invalidated_at=value["invalidated_at"],
        )

    def invalidate(
        self, organization_id: str, version: str, *, now: datetime | None = None
    ) -> bool:
        with self.engine.begin() as connection:
            result = connection.execute(
                text(
                    "UPDATE organization_policies SET invalidated_at=:now "
                    "WHERE organization_id=:org AND version=:version AND invalidated_at IS NULL"
                ),
                {"org": organization_id, "version": version, "now": _iso(now or _utc_now())},
            )
        return result.rowcount == 1

    def purge_invalidated(self, organization_id: str, *, now: datetime | None = None) -> int:
        """Remove invalidated policy versions after each version's retention window."""
        current = now or _utc_now()
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT id, invalidated_at, retention_days FROM organization_policies "
                    "WHERE organization_id=:org AND invalidated_at IS NOT NULL"
                ),
                {"org": organization_id},
            ).all()
        expired_ids: list[str] = []
        for row in rows:
            invalidated = row._mapping["invalidated_at"]
            if not isinstance(invalidated, str):
                continue
            try:
                timestamp = datetime.fromisoformat(invalidated.replace("Z", "+00:00"))
            except ValueError:
                continue
            if timestamp + timedelta(days=int(row._mapping["retention_days"])) <= current:
                expired_ids.append(str(row._mapping["id"]))
        if not expired_ids:
            return 0
        with self.engine.begin() as connection:
            result = connection.execute(
                text(
                    "DELETE FROM organization_policies WHERE organization_id=:org "
                    "AND id IN (" + ",".join(f":id{i}" for i in range(len(expired_ids))) + ")"
                ),
                {"org": organization_id, **{f"id{i}": value for i, value in enumerate(expired_ids)}},
            )
        return int(result.rowcount or 0)


def render_policy(policy: OrganizationPolicy | None) -> str:
    if policy is None:
        return ""
    return (
        f"Organization policy {policy.version} (source_sha={policy.source_sha}):\n"
        f"severity={','.join(policy.severity_levels)}; "
        f"forbidden_operations={','.join(policy.forbidden_operations) or 'none'}; "
        f"allowed_tools={','.join(policy.allowed_tools) or 'none'}; "
        f"approval_threshold={policy.approval_threshold}; "
        f"retention_days={policy.retention_days}; "
        f"cost_budget_microusd={policy.cost_budget_microusd}"
    )


def repository_source_sha(repo_root: Any) -> str | None:
    """Resolve HEAD without shell execution; unavailable repositories return None."""
    head = getattr(repo_root, "joinpath", None)
    if not callable(head):
        return None
    git = repo_root.joinpath(".git")
    try:
        if git.is_file():
            pointer = git.read_text(encoding="utf-8", errors="strict").strip()
            if not pointer.startswith("gitdir: "):
                return None
            git = (repo_root / pointer[8:]).resolve()
        raw = (git / "HEAD").read_text(encoding="ascii").strip()
        if raw.startswith("ref: "):
            ref = raw[5:]
            ref_path = git / ref
            if ref_path.is_file():
                raw = ref_path.read_text(encoding="ascii").strip()
            else:
                packed = (git / "packed-refs").read_text(encoding="ascii", errors="ignore")
                raw = next((line.split(" ", 1)[0] for line in packed.splitlines()
                            if line.endswith(" " + ref)), "")
        normalized = raw.casefold()
        return normalized if _SHA.fullmatch(normalized) else None
    except (OSError, UnicodeError):
        return None
