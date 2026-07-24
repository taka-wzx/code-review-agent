"""Protocol-neutral asynchronous review service used by HTTP and MCP adapters."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable, Iterable, Mapping, Protocol
import uuid

from code_review_agent.agent import run_review
from code_review_agent.approval_publish import (
    DryRunPublisher,
    PublicationError,
    PublishReceipt,
    PublishRequest,
    Publisher,
)
from code_review_agent.database import database_url_from_env, sqlite_database_url
from code_review_agent.context_memory import (
    ContextMode,
    MemorySource,
    OrganizationPolicy,
    OrganizationPolicyStore,
    RepositoryMemoryStore,
)
from code_review_agent.identity import (
    Permission,
    PermissionDenied,
    Principal,
    Role,
    current_correlation_id,
)
from code_review_agent.llm import make_client
from code_review_agent.tracelog import Trace, tev
from code_review_agent import service_queue as durable_queue


SCHEMA_VERSION = "crag.service/v1alpha1"
MAX_DIFF_BYTES = 512 * 1024
MAX_TRACE_BYTES = 4 * 1024 * 1024
MAX_RESULT_BYTES = 2 * 1024 * 1024
MAX_PR_REF_CHARS = 256
_REPOSITORY = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,62}[A-Za-z0-9])?/"
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?\Z"
)
_PR_URL = re.compile(
    r"https://github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)/pull/(?P<number>\d+)/?\Z",
    re.IGNORECASE,
)
_JOB_ID = re.compile(r"[0-9a-f]{32}\Z")


ServiceError = durable_queue.ServiceError
InvalidRequest = durable_queue.InvalidRequest
JobNotFound = durable_queue.JobNotFound
AuthorizationDenied = durable_queue.AuthorizationDenied
ServiceClosed = durable_queue.ServiceClosed
StateDirectoryInUse = durable_queue.StateDirectoryInUse
IdempotencyConflict = durable_queue.IdempotencyConflict
LeaseLost = durable_queue.LeaseLost
QuotaExceeded = durable_queue.QuotaExceeded
QueueFull = durable_queue.QueueFull
SubmissionRateLimited = durable_queue.SubmissionRateLimited
ModelBudgetExhausted = durable_queue.ModelBudgetExhausted
JobState = durable_queue.JobState


class ExternalCommandError(RuntimeError):
    """A bounded failure from an external command-line dependency."""


class ModelCallBudgetExceeded(RuntimeError):
    """Raised before a provider request would exceed the job call budget."""


class ApprovalConflict(IdempotencyConflict):
    code = "approval_conflict"


class PublisherFailed(ServiceError):
    code = "publisher_failed"


def _without_sdk_retries(target: Any) -> Any:
    """Disable hidden SDK attempts so one budget unit is one HTTP attempt."""

    with_options = getattr(target, "with_options", None)
    if not callable(with_options):
        # Test/client adapters without an SDK retry layer are already single-attempt.
        return target
    configured = with_options(max_retries=0)
    if getattr(configured, "max_retries", None) != 0:
        raise RuntimeError("provider SDK retries could not be disabled")
    return configured


class _BudgetedCompletions:
    def __init__(self, target: Any, limit: int, lock: threading.Lock) -> None:
        self._target = target
        self._limit = limit
        self._lock = lock
        self.calls = 0

    def create(self, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            if self.calls >= self._limit:
                raise ModelCallBudgetExceeded("model call budget is exhausted")
            self.calls += 1
        return self._target.create(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target, name)


class _BudgetedChat:
    def __init__(self, target: Any, completions: _BudgetedCompletions) -> None:
        self._target = target
        self.completions = completions

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target, name)


class _BudgetedClient:
    def __init__(self, target: Any, limit: int) -> None:
        self._target = target
        lock = threading.Lock()
        target_chat = getattr(target, "chat", None)
        target_completions = getattr(target_chat, "completions", None)
        self._completions = (
            _BudgetedCompletions(target_completions, limit, lock)
            if target_completions is not None
            else None
        )
        self.chat = (
            _BudgetedChat(target_chat, self._completions)
            if target_chat is not None and self._completions is not None
            else target_chat
        )

    @property
    def calls(self) -> int:
        return self._completions.calls if self._completions is not None else 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target, name)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _job_id(value: str) -> str:
    if not isinstance(value, str) or not _JOB_ID.fullmatch(value):
        raise JobNotFound("review job was not found")
    return value


def normalize_repository(value: str) -> str:
    if not isinstance(value, str) or not _REPOSITORY.fullmatch(value):
        raise InvalidRequest("repository must be an owner/repo alias")
    return value.casefold()


def normalize_pr_ref(repository: str, value: str | int) -> str:
    raw = str(value).strip()
    if not raw or len(raw) > MAX_PR_REF_CHARS:
        raise InvalidRequest("pull_request is invalid")
    if raw.isdigit() and int(raw) > 0:
        return str(int(raw))
    match = _PR_URL.fullmatch(raw)
    if match is None:
        raise InvalidRequest("pull_request must be a positive number or exact GitHub PR URL")
    if int(match.group("number")) <= 0:
        raise InvalidRequest("pull_request must be positive")
    url_repository = f"{match.group('owner')}/{match.group('repo')}".casefold()
    if url_repository != normalize_repository(repository):
        raise InvalidRequest("pull_request URL does not match the registered repository")
    return str(int(match.group("number")))


def validate_diff(value: str) -> tuple[str, int]:
    if not isinstance(value, str) or not value.strip():
        raise InvalidRequest("diff must be a non-empty string")
    size = len(value.encode("utf-8"))
    if size > MAX_DIFF_BYTES:
        raise InvalidRequest(f"diff exceeds the {MAX_DIFF_BYTES}-byte limit")
    if not any(line.startswith(("diff --git ", "--- ", "+++ ", "@@ ")) for line in value.splitlines()):
        raise InvalidRequest("diff does not look like a unified diff")
    return hashlib.sha256(value.encode("utf-8")).hexdigest(), size


@dataclass(frozen=True)
class RepositoryRegistry:
    paths: Mapping[str, Path]

    @classmethod
    def from_json(cls, raw: str) -> "RepositoryRegistry":
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise InvalidRequest("CRAG_REPOSITORIES_JSON is not valid JSON") from exc
        if not isinstance(parsed, dict) or not parsed:
            raise InvalidRequest("CRAG_REPOSITORIES_JSON must be a non-empty object")
        paths: dict[str, Path] = {}
        for alias, raw_path in parsed.items():
            normalized = normalize_repository(alias)
            if normalized in paths:
                raise InvalidRequest("repository aliases must be unique case-insensitively")
            if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
                raise InvalidRequest("registered repository paths must be absolute")
            try:
                path = Path(raw_path).resolve(strict=True)
            except OSError as exc:
                raise InvalidRequest("a registered repository path does not exist") from exc
            if not (path / ".git").exists():
                raise InvalidRequest("a registered repository path is not a Git checkout")
            paths[normalized] = path
        return cls(paths)

    def resolve(self, repository: str) -> tuple[str, Path]:
        alias = normalize_repository(repository)
        path = self.paths.get(alias)
        if path is None:
            raise InvalidRequest("repository is not registered")
        return alias, path


@dataclass(frozen=True)
class ReviewRequest:
    job_id: str
    source_kind: str
    repository: str
    repo_root: Path
    source_ref: str
    diff: str | None = None
    organization_id: str = ""
    repository_id: str = ""
    principal_id: str = ""
    head_sha: str | None = None
    attempt_count: int = 0
    model_call_limit: int = 64


class ReviewRunner(Protocol):
    def __call__(self, request: ReviewRequest, trace_path: Path) -> dict[str, Any]: ...


def _safe_failure(exc: BaseException) -> str:
    name = type(exc).__name__.casefold()
    if "authentication" in name or isinstance(exc, SystemExit):
        return "configuration"
    if "ratelimit" in name:
        return "rate_limit"
    if isinstance(exc, ModelCallBudgetExceeded) or "budget" in name:
        return "budget_exhausted"
    if "timeout" in name:
        return "timeout"
    if "connection" in name or "apistatus" in name:
        return "provider"
    if isinstance(exc, (ExternalCommandError, FileNotFoundError, subprocess.SubprocessError)):
        return "external_command"
    return "internal"


class DefaultReviewRunner:
    """Run the existing Review Agent while preserving its trace semantics."""

    def __init__(
        self,
        *,
        client_factory: Callable[[], tuple[Any, str]] = make_client,
        process_factory: Callable[..., Any] = subprocess.Popen,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        memory_store: RepositoryMemoryStore | None = None,
        policy_store: OrganizationPolicyStore | None = None,
        context_mode: ContextMode | str = ContextMode.HIERARCHICAL,
    ) -> None:
        self._client_factory = client_factory
        self._process_factory = process_factory
        self._clock = clock
        self._sleep = sleep
        self._memory_store = memory_store
        self._policy_store = policy_store
        self._context_mode = ContextMode.parse(context_mode)

    @staticmethod
    def _command_environment() -> dict[str, str]:
        environment = dict(os.environ)
        for name in (
            "CRAG_DATABASE_URL",
            "CRAG_DATABASE_URL_FILE",
            "CRAG_DATABASE_PASSWORD_FILE",
            "CRAG_SERVICE_TOKEN",
            "CRAG_SERVICE_TOKEN_FILE",
            "CRAG_WEBHOOK_SECRET",
            "CRAG_WEBHOOK_SECRET_FILE",
            "DEEPSEEK_API_KEY",
            "DEEPSEEK_API_KEY_FILE",
            "GLM_API_KEY",
            "GLM_API_KEY_FILE",
            "ZHIPUAI_API_KEY",
            "ZHIPUAI_API_KEY_FILE",
            "OPENAI_API_KEY",
        ):
            environment.pop(name, None)
        return environment

    def _gh_output(self, request: ReviewRequest, arguments: list[str], limit: int) -> bytes:
        with tempfile.TemporaryFile() as output:
            try:
                proc = self._process_factory(
                    arguments,
                    cwd=request.repo_root,
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    env=self._command_environment(),
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise ExternalCommandError("GitHub command failed") from exc
            deadline = self._clock() + 60
            while proc.poll() is None:
                if os.fstat(output.fileno()).st_size > limit:
                    proc.kill()
                    proc.wait()
                    raise ExternalCommandError("GitHub response is too large")
                if self._clock() >= deadline:
                    proc.kill()
                    proc.wait()
                    raise ExternalCommandError("GitHub command timed out")
                self._sleep(0.01)
            if proc.returncode != 0:
                raise ExternalCommandError("GitHub command failed")
            output.seek(0)
            encoded = output.read(limit + 1)
        if not encoded.strip() or len(encoded) > limit:
            raise ExternalCommandError("GitHub response is empty or too large")
        return encoded

    def _pr_head_sha(self, request: ReviewRequest) -> str:
        encoded = self._gh_output(
            request,
            ["gh", "pr", "view", request.source_ref, "--json", "headRefOid", "--jq", ".headRefOid"],
            128,
        )
        head_sha = encoded.decode("ascii", errors="ignore").strip().casefold()
        if re.fullmatch(r"[0-9a-f]{40,64}", head_sha) is None:
            raise ExternalCommandError("GitHub returned an invalid head SHA")
        return head_sha

    def _pr_diff(self, request: ReviewRequest) -> str:
        expected_head = request.head_sha.casefold() if request.head_sha else None
        if expected_head is not None and self._pr_head_sha(request) != expected_head:
            raise InvalidRequest("pull request head no longer matches the submitted snapshot")
        encoded = self._gh_output(
            request,
            ["gh", "pr", "diff", request.source_ref],
            MAX_DIFF_BYTES,
        )
        if expected_head is not None and self._pr_head_sha(request) != expected_head:
            raise InvalidRequest("pull request head changed while the diff was fetched")
        diff = encoded.decode("utf-8", errors="replace")
        return diff

    def __call__(self, request: ReviewRequest, trace_path: Path) -> dict[str, Any]:
        trace = Trace(
            trace_path,
            run_id=request.job_id,
            root_attributes={
                "crag.service.schema": SCHEMA_VERSION,
                "crag.service.source": request.source_kind,
                "crag.service.repository": request.repository,
                "crag.service.organization_id": request.organization_id,
                "crag.service.repository_id": request.repository_id,
                "crag.service.principal_id": request.principal_id,
            },
        )
        error: tuple[str, str] | None = None
        try:
            diff = request.diff if request.diff is not None else self._pr_diff(request)
            client, model = self._client_factory()
            client = _without_sdk_retries(client)
            budgeted_client: Any = _BudgetedClient(client, request.model_call_limit)
            tev(trace, "meta", provider=os.environ.get("LLM_PROVIDER", "deepseek"), model=model)
            return run_review(
                budgeted_client,
                diff,
                request.repo_root,
                model,
                trace=trace,
                context_mode=self._context_mode,
                memory_store=self._memory_store,
                policy_store=self._policy_store,
                organization_id=request.organization_id,
                repository_id=request.repository_id,
                source_revision=request.head_sha,
            )
        except BaseException as exc:
            error = (type(exc).__name__, _safe_failure(exc))
            raise
        finally:
            if error is None:
                trace.close()
            else:
                trace.close(error_type=error[0], error_category=error[1])


# The Week 7 JobStore import surface now points at the durable Phase 9C implementation.
JobStore = durable_queue.JobStore




class ReviewService:
    def __init__(
        self,
        registry: RepositoryRegistry,
        store: JobStore,
        *,
        runner: ReviewRunner | None = None,
        workers: int = 2,
        local_mode: bool = True,
        publisher: Publisher | None = None,
    ) -> None:
        if isinstance(workers, bool) or not 1 <= workers <= 8:
            raise ValueError("workers must be between 1 and 8")
        self.registry = registry
        self.store = store
        self.runner = runner
        self.local_mode = local_mode
        self.publisher = publisher or DryRunPublisher()
        if local_mode:
            self.store.bootstrap_local(self.registry.paths.keys())
        self._lock = threading.Lock()
        self._accepting = True
        self._embedded_worker: Any | None = None
        # A supplied runner is an explicit local/test compatibility harness.
        # Environment-created API services never pass one and therefore never
        # claim or execute review work.
        if runner is not None:
            from code_review_agent.worker import ReviewWorker

            self._embedded_worker = ReviewWorker(
                registry,
                store,
                runner=runner,
                worker_id=f"embedded-{uuid.uuid4().hex}",
                concurrency=workers,
                poll_seconds=0.05,
                heartbeat_seconds=0.25,
                lease_seconds=5.0,
                shutdown_grace_seconds=3.0,
            )
            self._embedded_worker.start()

    def _principal(self, principal: Principal | None) -> Principal:
        resolved = principal or self.store.local_principal
        if resolved is None:
            raise AuthorizationDenied("authenticated principal is required")
        return resolved

    def _audit(
        self,
        principal: Principal,
        action: str,
        resource_type: str,
        resource_id: str,
        decision: str,
        *,
        repository_id: str | None = None,
        reason_code: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        self.store.database.audit(
            principal=principal,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            decision=decision,
            repository_id=repository_id,
            reason_code=reason_code,
            correlation_id=correlation_id or current_correlation_id(uuid.uuid4().hex),
        )

    def _require(self, principal: Principal, permission: Permission, action: str) -> None:
        try:
            principal.require(permission)
        except PermissionDenied as exc:
            self._audit(
                principal,
                action,
                "organization",
                principal.organization_id,
                "deny",
                reason_code="role_denied",
            )
            raise AuthorizationDenied("operation is not permitted") from exc

    def _repository(
        self,
        principal: Principal,
        repository: str,
        permission: Permission,
        action: str,
    ) -> tuple[str, Path, Mapping[str, Any]]:
        self._require(principal, permission, action)
        alias = normalize_repository(repository)
        record = self.store.database.authorized_repository(principal, alias)
        if record is None:
            self._audit(
                principal,
                action,
                "repository",
                alias,
                "deny",
                reason_code="not_found",
            )
            raise InvalidRequest("repository is not registered")
        _, root = self.registry.resolve(alias)
        return alias, root, record

    def _ensure_accepting(self) -> None:
        if not self._accepting:
            raise ServiceClosed("review service is shutting down")

    @property
    def accepting(self) -> bool:
        with self._lock:
            return self._accepting

    @staticmethod
    def _idempotency_hash(organization_id: str, value: str | None) -> str | None:
        if value is None:
            return None
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
            raise InvalidRequest("idempotency key is invalid")
        return hashlib.sha256(f"{organization_id}\0{value}".encode("utf-8")).hexdigest()

    @staticmethod
    def _submission_key(
        organization_id: str,
        repository_id: str,
        policy_version: str,
        source_kind: str,
        source_identity: str,
    ) -> str:
        material = "\0".join(
            (
                organization_id,
                repository_id,
                policy_version,
                source_kind,
                source_identity,
            )
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def submit_diff(
        self,
        repository: str,
        diff: str,
        *,
        principal: Principal | None = None,
        correlation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_accepting()
        actor = self._principal(principal)
        alias, _, repository_record = self._repository(
            actor, repository, Permission.SUBMIT_REVIEW, "review.submit"
        )
        digest, size = validate_diff(diff)
        correlation = correlation_id or uuid.uuid4().hex
        repository_id = str(repository_record["id"])
        with self._lock:
            self._ensure_accepting()
        job_id, duplicate = self.store.create(
            source_kind="diff",
            repository=alias,
            source_ref="inline",
            source_sha256=digest,
            source_bytes=size,
            organization_id=actor.organization_id,
            repository_id=repository_id,
            submitted_by=actor.user_id,
            correlation_id=correlation,
            submission_key=self._submission_key(
                actor.organization_id,
                repository_id,
                str(repository_record["policy_version"]),
                "diff",
                digest,
            ),
            idempotency_key_hash=self._idempotency_hash(
                actor.organization_id, idempotency_key
            ),
            payload=diff,
        )
        self._audit(
            actor,
            "review.submit",
            "review_job",
            job_id,
            "allow",
            repository_id=repository_id,
            correlation_id=correlation,
        )
        return {**self.store.get(job_id, actor), "duplicate": duplicate}

    def submit_pr(
        self,
        repository: str,
        pull_request: str | int,
        *,
        delivery_id: str | None = None,
        principal: Principal | None = None,
        correlation_id: str | None = None,
        idempotency_key: str | None = None,
        head_sha: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        self._ensure_accepting()
        actor = self._principal(principal)
        alias, _, repository_record = self._repository(
            actor, repository, Permission.SUBMIT_REVIEW, "review.submit"
        )
        reference = normalize_pr_ref(alias, pull_request)
        if head_sha is not None:
            head_sha = head_sha.casefold()
            if re.fullmatch(r"[0-9a-f]{40,64}", head_sha) is None:
                raise InvalidRequest("head SHA is invalid")
        if head_sha is None and idempotency_key is None and delivery_id is None:
            raise InvalidRequest("head SHA or idempotency key is required")
        digest = hashlib.sha256(
            f"{alias}\0{reference}\0{head_sha or ''}".encode()
        ).hexdigest()
        if delivery_id is not None:
            if not isinstance(delivery_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,128}", delivery_id):
                raise InvalidRequest("delivery ID is invalid")
        correlation = correlation_id or uuid.uuid4().hex
        repository_id = str(repository_record["id"])
        with self._lock:
            self._ensure_accepting()
        job_id, duplicate = self.store.create(
            source_kind="pull_request",
            repository=alias,
            source_ref=reference,
            source_sha256=digest,
            source_bytes=0,
            delivery_id=delivery_id,
            organization_id=actor.organization_id,
            repository_id=repository_id,
            submitted_by=actor.user_id,
            correlation_id=correlation,
            submission_key=self._submission_key(
                actor.organization_id,
                repository_id,
                str(repository_record["policy_version"]),
                "pull_request",
                f"{reference}\0{head_sha or ''}",
            ),
            idempotency_key_hash=self._idempotency_hash(
                actor.organization_id, idempotency_key
            ),
            head_sha=head_sha,
        )
        self._audit(
            actor,
            "review.submit",
            "review_job",
            job_id,
            "allow",
            repository_id=repository_id,
            correlation_id=correlation,
        )
        return self.store.get(job_id, actor), duplicate

    def submit_webhook_pr(
        self,
        repository: str,
        pull_request: str | int,
        *,
        delivery_id: str,
        correlation_id: str | None = None,
        head_sha: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        self._ensure_accepting()
        alias, _ = self.registry.resolve(repository)
        repository_record = self.store.database.repository_for_webhook(alias)
        if repository_record is None:
            raise InvalidRequest("repository is not registered")
        reference = normalize_pr_ref(alias, pull_request)
        if head_sha is None or re.fullmatch(r"[0-9a-fA-F]{40,64}", head_sha) is None:
            raise InvalidRequest("webhook head SHA is required")
        head_sha = head_sha.casefold()
        digest = hashlib.sha256(f"{alias}\0{reference}\0{head_sha}".encode()).hexdigest()
        if not re.fullmatch(r"[A-Za-z0-9-]{1,128}", delivery_id):
            raise InvalidRequest("delivery ID is invalid")
        correlation = correlation_id or uuid.uuid4().hex
        organization_id = str(repository_record["organization_id"])
        repository_id = str(repository_record["id"])
        with self._lock:
            self._ensure_accepting()
        job_id, duplicate = self.store.create(
            source_kind="pull_request",
            repository=alias,
            source_ref=reference,
            source_sha256=digest,
            source_bytes=0,
            delivery_id=delivery_id,
            organization_id=organization_id,
            repository_id=repository_id,
            submitted_by="github-webhook",
            correlation_id=correlation,
            submission_key=self._submission_key(
                organization_id,
                repository_id,
                str(repository_record["policy_version"]),
                "pull_request",
                f"{reference}\0{head_sha}",
            ),
            idempotency_key_hash=self._idempotency_hash(
                organization_id, f"github:{delivery_id}"
            ),
            head_sha=head_sha,
        )
        webhook_principal = Principal(
            principal_id="github-webhook",
            user_id="github-webhook",
            organization_id=str(repository_record["organization_id"]),
            role=Role.VIEWER,
            auth_method="webhook_hmac",
        )
        self._audit(
            webhook_principal,
            "webhook.review.submit",
            "review_job",
            job_id,
            "allow",
            repository_id=str(repository_record["id"]),
            correlation_id=correlation,
        )
        return self.store.get(job_id), duplicate

    def get(
        self, job_id: str, *, principal: Principal | None = None
    ) -> dict[str, Any]:
        actor = self._principal(principal)
        self._require(actor, Permission.READ, "review.read")
        job = self.store.get(job_id, actor)
        self._audit(
            actor,
            "review.read",
            "review_job",
            job_id,
            "allow",
            repository_id=str(job["repository_id"]),
        )
        return job

    def get_trace(self, job_id: str, *, principal: Principal | None = None) -> str:
        actor = self._principal(principal)
        self._require(actor, Permission.READ, "trace.read")
        trace = self.store.read_trace(job_id, actor)
        job = self.store.get(job_id, actor)
        self._audit(
            actor,
            "trace.read",
            "review_trace",
            job_id,
            "allow",
            repository_id=str(job["repository_id"]),
        )
        return trace

    @staticmethod
    def _finding_response(record: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(record)
        payload = result.pop("payload_json", None)
        if isinstance(payload, str):
            result["finding"] = json.loads(payload)
        return result

    def list_findings(
        self, job_id: str, *, principal: Principal | None = None
    ) -> list[dict[str, Any]]:
        actor = self._principal(principal)
        self._require(actor, Permission.READ, "finding.list")
        job = self.store.get(job_id, actor)
        records = self.store.database.findings_for_review(actor, job_id)
        self._audit(
            actor,
            "finding.list",
            "review_job",
            job_id,
            "allow",
            repository_id=str(job["repository_id"]),
        )
        return [self._finding_response(record) for record in records]

    def get_finding(
        self, finding_id: str, *, principal: Principal | None = None
    ) -> dict[str, Any]:
        actor = self._principal(principal)
        self._require(actor, Permission.READ, "finding.read")
        record = self.store.database.finding_detail(actor, finding_id)
        if record is None:
            raise JobNotFound("finding was not found")
        self._audit(
            actor,
            "finding.read",
            "finding",
            finding_id,
            "allow",
            repository_id=str(record["repository_id"]),
        )
        return self._finding_response(record)

    def principal_record(self, principal: Principal | None = None) -> dict[str, Any]:
        actor = self._principal(principal)
        return {
            "principal_id": actor.principal_id,
            "user_id": actor.user_id,
            "organization_id": actor.organization_id,
            "role": actor.role.value,
            "auth_method": actor.auth_method,
            "credential_id": actor.credential_id,
        }

    def list_members(self, principal: Principal | None = None) -> list[dict[str, Any]]:
        actor = self._principal(principal)
        self._require(actor, Permission.MANAGE_MEMBERS, "membership.list")
        return self.store.database.list_members(actor.organization_id)

    def create_member(
        self,
        *,
        subject: str,
        display_name: str,
        role: Role,
        repository_ids: Iterable[str],
        principal: Principal | None = None,
    ) -> dict[str, Any]:
        actor = self._principal(principal)
        self._require(actor, Permission.MANAGE_MEMBERS, "membership.create")
        record = self.store.database.create_membership(
            actor.organization_id,
            subject=subject,
            display_name=display_name,
            role=role,
            repository_ids=repository_ids,
        )
        self._audit(
            actor,
            "membership.create",
            "membership",
            str(record["membership_id"]),
            "allow",
        )
        return record

    def update_member(
        self,
        membership_id: str,
        *,
        role: Role,
        repository_ids: Iterable[str],
        principal: Principal | None = None,
    ) -> dict[str, Any]:
        actor = self._principal(principal)
        self._require(actor, Permission.MANAGE_MEMBERS, "membership.update")
        own_membership = next(
            (
                item
                for item in self.store.database.list_members(actor.organization_id)
                if item["membership_id"] == membership_id
            ),
            None,
        )
        if own_membership is not None and own_membership["user_id"] == actor.user_id:
            self._audit(
                actor,
                "membership.update",
                "membership",
                membership_id,
                "deny",
                reason_code="self_role_change",
            )
            raise AuthorizationDenied("self role changes are not permitted")
        record = self.store.database.update_membership(
            actor.organization_id,
            membership_id,
            role=role,
            repository_ids=repository_ids,
        )
        if record is None:
            raise JobNotFound("membership was not found")
        self._audit(
            actor,
            "membership.update",
            "membership",
            membership_id,
            "allow",
        )
        return record

    def list_repositories(
        self, principal: Principal | None = None
    ) -> list[dict[str, Any]]:
        actor = self._principal(principal)
        self._require(actor, Permission.MANAGE_REPOSITORIES, "repository.list")
        return self.store.database.list_repositories(actor.organization_id)

    def register_repository(
        self,
        repository: str,
        *,
        mode: str,
        budget_microusd: int | None,
        policy_version: str,
        principal: Principal | None = None,
    ) -> dict[str, Any]:
        actor = self._principal(principal)
        self._require(actor, Permission.MANAGE_REPOSITORIES, "repository.create")
        alias, _ = self.registry.resolve(repository)
        record = self.store.database.register_repository(
            actor.organization_id,
            alias,
            mode=mode,
            budget_microusd=budget_microusd,
            policy_version=policy_version,
        )
        self._audit(
            actor,
            "repository.create",
            "repository",
            str(record["id"]),
            "allow",
            repository_id=str(record["id"]),
            reason_code=None,
        )
        return record

    def update_repository(
        self,
        repository_id: str,
        *,
        mode: str,
        budget_microusd: int | None,
        policy_version: str,
        principal: Principal | None = None,
    ) -> dict[str, Any]:
        actor = self._principal(principal)
        self._require(actor, Permission.MANAGE_REPOSITORIES, "repository.update")
        record = self.store.database.update_repository(
            actor.organization_id,
            repository_id,
            mode=mode,
            budget_microusd=budget_microusd,
            policy_version=policy_version,
        )
        if record is None:
            raise JobNotFound("repository was not found")
        self._audit(
            actor,
            "repository.update",
            "repository",
            repository_id,
            "allow",
            repository_id=repository_id,
        )
        return record

    def get_service_quota(
        self,
        *,
        repository_id: str | None = None,
        principal: Principal | None = None,
    ) -> dict[str, Any]:
        actor = self._principal(principal)
        self._require(actor, Permission.MANAGE_REPOSITORIES, "service_quota.read")
        if repository_id is not None:
            repository = self.store.database.authorized_repository(actor, repository_id)
            if repository is None:
                raise JobNotFound("repository was not found")
        return self.store.get_quota(
            actor.organization_id, repository_id=repository_id
        )

    def update_service_quota(
        self,
        *,
        repository_id: str | None = None,
        principal: Principal | None = None,
        **values: int | None,
    ) -> dict[str, Any]:
        actor = self._principal(principal)
        self._require(actor, Permission.MANAGE_REPOSITORIES, "service_quota.update")
        if repository_id is not None:
            repository = self.store.database.authorized_repository(actor, repository_id)
            if repository is None:
                raise JobNotFound("repository was not found")
        record = self.store.configure_quota(
            actor.organization_id, repository_id=repository_id, **values
        )
        self._audit(
            actor,
            "service_quota.update",
            "repository" if repository_id is not None else "organization",
            repository_id or actor.organization_id,
            "allow",
            repository_id=repository_id,
        )
        return record

    def create_credential(
        self,
        *,
        expires_in_seconds: int,
        principal: Principal | None = None,
    ) -> dict[str, Any]:
        actor = self._principal(principal)
        record = self.store.database.create_credential(
            actor, expires_in_seconds=expires_in_seconds
        )
        self._audit(
            actor,
            "credential.create",
            "access_credential",
            str(record["credential_id"]),
            "allow",
        )
        return record

    def revoke_credential(
        self, credential_id: str, *, principal: Principal | None = None
    ) -> None:
        actor = self._principal(principal)
        allow_any = actor.allows(Permission.MANAGE_CREDENTIALS)
        if not self.store.database.revoke_credential(
            actor, credential_id, allow_any_user=allow_any
        ):
            raise JobNotFound("credential was not found")
        self._audit(
            actor,
            "credential.revoke",
            "access_credential",
            credential_id,
            "allow",
        )

    def list_audit(
        self, *, limit: int, principal: Principal | None = None
    ) -> list[dict[str, Any]]:
        actor = self._principal(principal)
        self._require(actor, Permission.READ_AUDIT, "audit.list")
        return self.store.database.list_audit_events(actor.organization_id, limit=limit)

    def get_organization_policy(
        self, *, principal: Principal | None = None
    ) -> dict[str, Any] | None:
        actor = self._principal(principal)
        self._require(actor, Permission.READ, "organization.policy.read")
        policy = OrganizationPolicyStore(self.store.database).active(actor.organization_id)
        self._audit(
            actor,
            "organization.policy.read",
            "organization",
            actor.organization_id,
            "allow",
        )
        if policy is None:
            return None
        return {
            "organization_id": policy.organization_id,
            "version": policy.version,
            "severity_levels": list(policy.severity_levels),
            "forbidden_operations": list(policy.forbidden_operations),
            "allowed_tools": list(policy.allowed_tools),
            "approval_threshold": policy.approval_threshold,
            "retention_days": policy.retention_days,
            "cost_budget_microusd": policy.cost_budget_microusd,
            "source_sha": policy.source_sha,
            "created_by": policy.created_by,
            "reason": policy.reason,
            "created_at": policy.created_at,
            "invalidated_at": policy.invalidated_at,
        }

    def put_organization_policy(
        self,
        *,
        version: str,
        severity_levels: Iterable[str],
        forbidden_operations: Iterable[str],
        allowed_tools: Iterable[str],
        approval_threshold: int,
        retention_days: int,
        cost_budget_microusd: int,
        source_sha: str,
        reason: str,
        principal: Principal | None = None,
    ) -> dict[str, Any]:
        actor = self._principal(principal)
        self._require(actor, Permission.MANAGE_POLICY, "organization.policy.write")
        policy = OrganizationPolicy(
            organization_id=actor.organization_id,
            version=version,
            severity_levels=tuple(severity_levels),
            forbidden_operations=tuple(forbidden_operations),
            allowed_tools=tuple(allowed_tools),
            approval_threshold=approval_threshold,
            retention_days=retention_days,
            cost_budget_microusd=cost_budget_microusd,
            created_by=actor.user_id,
            reason=reason,
            source_sha=source_sha,
        )
        policy_id = OrganizationPolicyStore(self.store.database).put(
            policy, source_kind=MemorySource.ADMIN_CONFIG
        )
        self._audit(
            actor,
            "organization.policy.write",
            "organization_policy",
            policy_id,
            "allow",
        )
        return self.get_organization_policy(principal=actor) or {}

    def invalidate_organization_policy(
        self, version: str, *, principal: Principal | None = None
    ) -> None:
        actor = self._principal(principal)
        self._require(actor, Permission.MANAGE_POLICY, "organization.policy.invalidate")
        changed = OrganizationPolicyStore(self.store.database).invalidate(
            actor.organization_id, version
        )
        if not changed:
            raise JobNotFound("organization policy was not found")
        self._audit(
            actor,
            "organization.policy.invalidate",
            "organization_policy",
            version,
            "allow",
        )

    def submit_feedback(
        self,
        finding_id: str,
        *,
        decision: str,
        reason: str | None = None,
        rationale: str | None = None,
        principal: Principal | None = None,
    ) -> dict[str, Any]:
        actor = self._principal(principal)
        self._require(actor, Permission.SUBMIT_FEEDBACK, "finding.feedback")
        if decision not in {"accepted", "rejected", "uncertain", "fixed", "duplicate"}:
            raise InvalidRequest("feedback decision is invalid")
        if reason is not None and rationale is not None:
            raise InvalidRequest("feedback rationale is ambiguous")
        rationale = rationale if rationale is not None else reason
        if rationale is not None and (
            not isinstance(rationale, str) or len(rationale) > 512
        ):
            raise InvalidRequest("feedback rationale is invalid")
        finding = self.store.database.finding_for_principal(actor, finding_id)
        if finding is None:
            raise JobNotFound("finding was not found")
        record = self.store.database.create_feedback(
            actor, finding, decision=decision, rationale=rationale
        )
        self._audit(
            actor,
            "finding.feedback",
            "finding",
            finding_id,
            "allow",
            repository_id=str(finding["repository_id"]),
        )
        return record

    @staticmethod
    def _raise_approval_error(error: PublicationError) -> None:
        if error.code == "approval_not_found":
            raise JobNotFound("review job was not found") from error
        if error.code in {
            "approval_consumed",
            "approval_replayed",
            "approval_expired",
            "approval_payload_mismatch",
            "approval_not_pending",
        }:
            raise ApprovalConflict("approval is not valid for the current review") from error
        raise InvalidRequest("review cannot be approved for publication") from error

    def list_pending_approvals(
        self, *, principal: Principal | None = None
    ) -> list[dict[str, Any]]:
        actor = self._principal(principal)
        self._require(actor, Permission.READ, "publication.pending.list")
        try:
            records = self.store.database.pending_publish_reviews(actor)
        except PublicationError as error:
            self._raise_approval_error(error)
        self._audit(
            actor,
            "publication.pending.list",
            "organization",
            actor.organization_id,
            "allow",
        )
        return records

    def _finish_publication(
        self, actor: Principal, record: dict[str, Any]
    ) -> dict[str, Any]:
        publish = record.pop("_publish", None)
        if not isinstance(publish, dict):
            return record
        request = PublishRequest(
            organization_id=actor.organization_id,
            repository_id=str(record["repository_id"]),
            review_job_id=str(record["review_job_id"]),
            repository_alias=str(publish["repository_alias"]),
            pull_request=str(publish["pull_request"]),
            head_sha=str(record["head_sha"]),
            payload=publish["payload"],
            payload_sha256=str(record["payload_sha256"]),
            idempotency_key=str(publish["idempotency_key"]),
        )
        receipt: PublishReceipt | None = None
        try:
            receipt = self.publisher.publish(request)
        except Exception:
            try:
                receipt = self.publisher.lookup(request.idempotency_key)
            except Exception:
                receipt = None
        if receipt is None:
            self.store.database.finish_publish_attempt(
                approval_id=str(record["approval_id"]),
                review_job_id=str(record["review_job_id"]),
                receipt_id=None,
                error_code="publisher_failed",
            )
            self._audit(
                actor,
                "publication.publish",
                "review_job",
                str(record["review_job_id"]),
                "error",
                repository_id=str(record["repository_id"]),
                reason_code="publisher_failed",
            )
            raise PublisherFailed("publisher did not return an idempotent receipt")
        self.store.database.finish_publish_attempt(
            approval_id=str(record["approval_id"]),
            review_job_id=str(record["review_job_id"]),
            receipt_id=receipt.receipt_id,
            error_code=None,
        )
        self._audit(
            actor,
            "publication.publish",
            "review_job",
            str(record["review_job_id"]),
            "allow",
            repository_id=str(record["repository_id"]),
        )
        record["state"] = "published"
        return record

    def decide_review_publication(
        self,
        review_job_id: str,
        *,
        decision: str,
        payload_sha256: str,
        nonce: str,
        principal: Principal | None = None,
    ) -> dict[str, Any]:
        actor = self._principal(principal)
        self._require(actor, Permission.APPROVE_PUBLICATION, "publication.decide")
        try:
            record = self.store.database.decide_publish_review(
                actor,
                review_job_id,
                decision=decision,
                payload_sha256=payload_sha256,
                nonce=nonce,
            )
        except PublicationError as error:
            self._raise_approval_error(error)
        self._audit(
            actor,
            f"publication.{decision}",
            "review_job",
            review_job_id,
            "allow",
            repository_id=str(record["repository_id"]),
        )
        return self._finish_publication(actor, record)

    def list_review_approvals(
        self, review_job_id: str, *, principal: Principal | None = None
    ) -> list[dict[str, Any]]:
        actor = self._principal(principal)
        self._require(actor, Permission.READ, "publication.approval.list")
        job = self.store.get(review_job_id, actor)
        records = self.store.database.publish_approvals_for_review(actor, review_job_id)
        self._audit(
            actor,
            "publication.approval.list",
            "review_job",
            review_job_id,
            "allow",
            repository_id=str(job["repository_id"]),
        )
        return records

    def list_finding_feedback(
        self, finding_id: str, *, principal: Principal | None = None
    ) -> list[dict[str, Any]]:
        actor = self._principal(principal)
        self._require(actor, Permission.READ, "finding.feedback.list")
        finding = self.store.database.finding_detail(actor, finding_id)
        if finding is None:
            raise JobNotFound("finding was not found")
        self._audit(
            actor,
            "finding.feedback.list",
            "finding",
            finding_id,
            "allow",
            repository_id=str(finding["repository_id"]),
        )
        return list(finding["feedback"])

    def decide_finding(
        self,
        finding_id: str,
        *,
        decision: str,
        principal: Principal | None = None,
    ) -> dict[str, Any]:
        actor = self._principal(principal)
        self._require(actor, Permission.DECIDE_FINDING, "finding.decide")
        finding = self.store.database.finding_for_principal(actor, finding_id)
        if finding is None:
            raise JobNotFound("finding was not found")
        repository = self.store.database.authorized_repository(
            actor, str(finding["repository_id"])
        )
        if repository is None:
            raise JobNotFound("finding was not found")
        record = self.store.database.decide_finding(
            actor,
            finding,
            decision=decision,
            policy_version=str(repository["policy_version"]),
        )
        self._audit(
            actor,
            "finding.decide",
            "finding",
            finding_id,
            "allow",
            repository_id=str(finding["repository_id"]),
        )
        return record

    def shutdown(self, *, wait: bool = True) -> None:
        with self._lock:
            was_accepting = self._accepting
            self._accepting = False
        if self._embedded_worker is not None and (was_accepting or wait):
            self._embedded_worker.shutdown(wait=wait)
        if wait:
            self.store.close()


def create_review_service_from_env(*, runner: ReviewRunner | None = None) -> ReviewService:
    raw_repositories = os.environ.get("CRAG_REPOSITORIES_JSON", "")
    registry = RepositoryRegistry.from_json(raw_repositories)
    configured_state = os.environ.get("CRAG_STATE_DIR")
    state = (
        Path(configured_state)
        if configured_state
        else Path.home() / ".crag" / "service"
    )
    try:
        workers = int(os.environ.get("CRAG_SERVICE_WORKERS", "2"))
    except ValueError as exc:
        raise InvalidRequest("CRAG_SERVICE_WORKERS must be an integer") from exc
    database_url = database_url_from_env(
        default=sqlite_database_url(state / "reviews.sqlite3")
    )
    local_mode = os.environ.get("CRAG_ALLOW_LOCAL_TOKEN", "").casefold() in {
        "1",
        "true",
        "yes",
    }
    auto_migrate_requested = os.environ.get("CRAG_AUTO_MIGRATE", "").casefold() in {
        "1",
        "true",
        "yes",
    }
    if auto_migrate_requested:
        raise InvalidRequest(
            "CRAG_AUTO_MIGRATE is unsupported; run crag-db upgrade explicitly"
        )
    store = JobStore(
        state,
        database_url=database_url,
        auto_migrate=False,
        job_data_dir=Path(os.environ.get("CRAG_JOB_DATA_DIR", state / "jobs")),
        trace_dir=Path(os.environ.get("CRAG_TRACE_DIR", state / "traces")),
    )
    return ReviewService(
        registry,
        store,
        runner=runner,
        workers=workers,
        local_mode=local_mode,
    )
