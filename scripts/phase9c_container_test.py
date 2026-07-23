"""Offline Compose acceptance for the Phase 9C durable service.

The harness creates a filtered Docker build context from an explicit allowlist,
so neither local nor CI image builds traverse the frozen evaluation tree.  All
review work uses the deterministic fake runner.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "compose.service.yml"
BUILD_FILES = (
    "Dockerfile",
    "Dockerfile.service",
    "LICENSE",
    "README.md",
    "alembic.ini",
    "pyproject.toml",
    "requirements.lock",
)
BUILD_TREE_ROOTS = ("migrations", "src")
PHASE9C_BUILD_FILES = (
    "migrations/versions/0003_phase9c_durable_queue.py",
    "src/code_review_agent/service_queue.py",
    "src/code_review_agent/worker.py",
)
_SENSITIVE: list[str] = []
_PRIVATE_PATHS: list[str] = [str(ROOT)]


class HarnessError(RuntimeError):
    pass


def _command_name(command: str) -> str:
    return command.replace("\\", "/").rsplit("/", 1)[-1]


def _redact(value: str) -> str:
    redacted = value
    for secret in sorted(_SENSITIVE, key=len, reverse=True):
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    for private_path in sorted(_PRIVATE_PATHS, key=len, reverse=True):
        if private_path:
            redacted = redacted.replace(private_path, "[HOST_PATH]")
            redacted = redacted.replace(private_path.replace("\\", "/"), "[HOST_PATH]")
    return redacted


def _diagnostic_tail(value: str, *, lines: int = 80, characters: int = 8000) -> str:
    tail = "\n".join(value.splitlines()[-lines:])
    return _redact(tail[-characters:])


def _run(
    command: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
    check: bool = True,
    timeout: float = 180.0,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            list(command),
            cwd=ROOT,
            env=dict(env) if env is not None else None,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise HarnessError(
            f"container command could not run: {_command_name(command[0])}"
        ) from exc
    if check and result.returncode != 0:
        output = _diagnostic_tail((result.stdout + "\n" + result.stderr).strip())
        raise HarnessError(
            f"container command failed ({result.returncode}) during "
            f"{_command_name(command[0])}\n{output}"
        )
    return result


def _is_allowed_tree_file(relative: str) -> bool:
    parts = Path(relative).parts
    if (
        len(parts) == 3
        and parts[:2] == ("src", "code_review_agent")
        and Path(parts[-1]).suffix == ".py"
    ):
        return True
    if relative.replace("\\", "/") in {
        "migrations/README.md",
        "migrations/env.py",
        "migrations/script.py.mako",
    }:
        return True
    return (
        len(parts) == 3
        and parts[:2] == ("migrations", "versions")
        and Path(parts[-1]).suffix == ".py"
    )


def _tracked_build_files() -> tuple[str, ...]:
    result = _run(("git", "ls-files", "-z", "--", *BUILD_TREE_ROOTS), timeout=30)
    files = tuple(item for item in result.stdout.split("\0") if item)
    disallowed = sorted(item for item in files if not _is_allowed_tree_file(item))
    if disallowed:
        raise HarnessError(
            "tracked build input is outside the explicit source allowlist: "
            + ", ".join(disallowed)
        )
    return files


def _copy_build_file(relative: str, destination: Path) -> None:
    normalized = relative.replace("\\", "/")
    source = ROOT.joinpath(*normalized.split("/"))
    cursor = ROOT
    for component in normalized.split("/"):
        cursor /= component
        if cursor.is_symlink():
            raise HarnessError(f"build input must not be a symlink: {normalized}")
    if not source.is_file():
        raise HarnessError(f"required build input is missing: {normalized}")
    try:
        source.resolve(strict=True).relative_to(ROOT.resolve(strict=True))
    except ValueError as exc:
        raise HarnessError(f"build input escapes the repository: {normalized}") from exc
    target = destination.joinpath(*normalized.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def _validate_build_context(destination: Path, expected: set[str]) -> None:
    actual: set[str] = set()
    host_needles = {
        str(ROOT).encode("utf-8").lower(),
        str(ROOT).replace("\\", "/").encode("utf-8").lower(),
    }
    for path in destination.rglob("*"):
        if path.is_symlink():
            raise HarnessError("filtered build context contains a symlink")
        if not path.is_file():
            continue
        relative = path.relative_to(destination).as_posix()
        actual.add(relative)
        if path.suffix == ".pyc" or "__pycache__" in path.parts:
            raise HarnessError("filtered build context contains Python bytecode")
        data = path.read_bytes().lower()
        if any(needle and needle in data for needle in host_needles):
            raise HarnessError(
                f"filtered build input contains the repository host path: {relative}"
            )
    if actual != expected:
        raise HarnessError("filtered build context did not match the explicit file set")


def prepare_context(destination: Path) -> None:
    destination = destination.resolve()
    try:
        destination.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise HarnessError("filtered build context destination already exists") from exc
    tree_files = set(_tracked_build_files()) | set(PHASE9C_BUILD_FILES)
    expected = {item.replace("\\", "/") for item in BUILD_FILES} | {
        item.replace("\\", "/") for item in tree_files
    }
    for relative in sorted(expected):
        _copy_build_file(relative, destination)
    _validate_build_context(destination, expected)


def _compose_command() -> tuple[str, ...]:
    docker = shutil.which("docker")
    if docker is not None:
        candidate = (docker, "compose")
        if _run((*candidate, "version"), check=False, timeout=15).returncode == 0:
            return candidate
    standalone = shutil.which("docker-compose")
    if standalone is None and os.name == "nt":
        bundled = Path(
            os.environ.get("ProgramFiles", r"C:\Program Files")
        ) / "Docker" / "Docker" / "resources" / "bin" / "docker-compose.exe"
        if bundled.is_file():
            standalone = str(bundled)
    if standalone is not None:
        candidate = (standalone,)
        if _run((*candidate, "version"), check=False, timeout=15).returncode == 0:
            return candidate
    raise HarnessError("Docker Compose v2 is unavailable")


def _docker_command() -> str:
    docker = shutil.which("docker")
    if docker is None and os.name == "nt":
        bundled = Path(
            os.environ.get("ProgramFiles", r"C:\Program Files")
        ) / "Docker" / "Docker" / "resources" / "bin" / "docker.exe"
        if bundled.is_file():
            docker = str(bundled)
    if docker is None:
        raise HarnessError("Docker CLI is unavailable")
    return docker


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stream:
        stream.bind(("127.0.0.1", 0))
        return int(stream.getsockname()[1])


def _write_secret(path: Path, value: str) -> None:
    path.write_text(value + "\n", encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)


_API_CLIENT = r"""
import hashlib
import hmac
import json
from pathlib import Path
import sys
import urllib.error
import urllib.request

request = json.load(sys.stdin)
method = request.get("method", "GET")
path = request["path"]
data = request.get("json")
body = None if data is None else json.dumps(data, separators=(",", ":")).encode()
headers = {"Accept": "application/json"}
if body is not None:
    headers["Content-Type"] = "application/json"
if request.get("webhook"):
    secret = Path("/run/secrets/webhook_secret").read_bytes().strip()
    headers.update(request["headers"])
    headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(
        secret, body or b"", hashlib.sha256
    ).hexdigest()
else:
    token = Path("/run/secrets/service_token").read_text(encoding="utf-8").strip()
    headers["Authorization"] = "Bearer " + token
    headers.update(request.get("headers", {}))
url = "http://127.0.0.1:8000" + path
outgoing = urllib.request.Request(url, data=body, headers=headers, method=method)
try:
    response = urllib.request.urlopen(outgoing, timeout=5)
except urllib.error.HTTPError as exc:
    response = exc
raw = response.read()
try:
    payload = json.loads(raw) if raw else None
except json.JSONDecodeError:
    payload = raw.decode("utf-8", errors="replace")
print(json.dumps({"status": response.status, "headers": dict(response.headers), "body": payload}))
"""


_SECRET_SCAN = r"""
from pathlib import Path

paths = [
    Path("/run/secrets/postgres_password"),
    Path("/run/secrets/webhook_secret"),
    Path("/run/secrets/service_token"),
    Path("/run/secrets/provider_api_key"),
]
needles = [path.read_bytes().strip() for path in paths if path.is_file()]
for trace in Path("/var/lib/crag/traces").glob("*.jsonl"):
    data = trace.read_bytes()
    if any(needle and needle in data for needle in needles):
        raise SystemExit("runtime secret found in trace")
print("trace-secret-scan-ok")
"""


_RUNTIME_IDENTITY_SCAN = r"""
from pathlib import Path

status = {}
for line in Path("/proc/1/status").read_text(encoding="ascii").splitlines():
    key, _, value = line.partition(":")
    if key in {"Uid", "CapEff"}:
        status[key] = value.strip().split()[0]
if status.get("Uid") != "1000" or status.get("CapEff") != "0000000000000000":
    raise SystemExit(f"service process identity is not non-root/capability-free: {status}")
print("runtime-identity-scan-ok")
"""


_IMAGE_HOST_PATH_SCAN = r"""
import json
from pathlib import Path
import sys

needles = [item.encode("utf-8").lower() for item in json.load(sys.stdin)]
for path in Path("/app").rglob("*"):
    if not path.is_file():
        continue
    data = path.read_bytes().lower()
    if any(needle and needle in data for needle in needles):
        raise SystemExit("host repository path found in image content")
print("image-host-path-scan-ok")
"""


class ComposeHarness:
    def __init__(
        self,
        compose: tuple[str, ...],
        docker: str,
        environment: Mapping[str, str],
    ) -> None:
        self.compose_prefix = compose
        self.docker = docker
        self.environment = dict(environment)

    def compose(
        self,
        *arguments: str,
        input_text: str | None = None,
        check: bool = True,
        timeout: float = 180.0,
    ) -> subprocess.CompletedProcess[str]:
        return _run(
            (*self.compose_prefix, "-f", str(COMPOSE_FILE), *arguments),
            env=self.environment,
            input_text=input_text,
            check=check,
            timeout=timeout,
        )

    def docker_run(
        self,
        *arguments: str,
        input_text: str | None = None,
        check: bool = True,
        timeout: float = 60.0,
    ) -> subprocess.CompletedProcess[str]:
        return _run(
            (self.docker, *arguments),
            env=self.environment,
            input_text=input_text,
            check=check,
            timeout=timeout,
        )

    def api(self, envelope: Mapping[str, Any]) -> dict[str, Any]:
        result = self.compose(
            "exec",
            "-T",
            "api",
            "python",
            "-c",
            _API_CLIENT,
            input_text=json.dumps(envelope),
            timeout=30,
        )
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        if not lines:
            raise HarnessError("container API client returned no result")
        try:
            parsed = json.loads(lines[-1])
        except json.JSONDecodeError as exc:
            raise HarnessError("container API client returned malformed JSON") from exc
        if not isinstance(parsed, dict):
            raise HarnessError("container API client result is not an object")
        return parsed

    def wait_ready(self, expected: int, *, timeout: float = 40.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            try:
                last = self.api({"path": "/readyz"})
            except HarnessError:
                time.sleep(0.25)
                continue
            if int(last.get("status", 0)) == expected:
                return last
            time.sleep(0.25)
        raise HarnessError(f"readiness did not become {expected}: {last}")

    def wait_state(
        self,
        job_id: str,
        expected: set[str],
        *,
        timeout: float = 45.0,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            response = self.api({"path": f"/v1/reviews/{job_id}"})
            if response.get("status") == 200 and isinstance(response.get("body"), dict):
                last = response["body"]
                if str(last.get("state")) in expected:
                    return last
            time.sleep(0.2)
        raise HarnessError(f"job {job_id} did not enter {sorted(expected)}: {last}")

    def sql(self, statement: str) -> str:
        result = self.compose(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "crag",
            "-d",
            "crag",
            "-Atqc",
            statement,
            timeout=30,
        )
        return result.stdout.strip()


def _assert_status(response: Mapping[str, Any], expected: int, label: str) -> None:
    if int(response.get("status", 0)) != expected:
        raise HarnessError(f"{label} returned {response}")


def _run_acceptance() -> dict[str, Any]:
    compose = _compose_command()
    docker = _docker_command()
    project = "crag9c" + secrets.token_hex(5)
    with tempfile.TemporaryDirectory(prefix="crag-phase9c-container-") as temporary:
        root = Path(temporary)
        _PRIVATE_PATHS.append(str(root))
        context = root / "build-context"
        prepare_context(context)
        secrets_dir = root / "secrets"
        secrets_dir.mkdir()
        repositories = root / "repositories"
        repository = repositories / "repo"
        repository.mkdir(parents=True)
        (repository / ".git").mkdir()

        values = {
            "postgres": secrets.token_urlsafe(32),
            "webhook": secrets.token_urlsafe(32),
            "service": secrets.token_urlsafe(40),
            "provider": "fake-run-noncredential-" + secrets.token_hex(16),
        }
        _SENSITIVE[:] = list(values.values())
        paths: dict[str, Path] = {}
        for name, value in values.items():
            path = secrets_dir / name
            _write_secret(path, value)
            paths[name] = path

        environment = dict(os.environ)
        environment.update(
            {
                "COMPOSE_PROJECT_NAME": project,
                "CRAG_BUILD_CONTEXT": str(context),
                "CRAG_POSTGRES_PASSWORD_FILE": str(paths["postgres"]),
                "CRAG_WEBHOOK_SECRET_FILE": str(paths["webhook"]),
                "CRAG_SERVICE_TOKEN_FILE": str(paths["service"]),
                "CRAG_PROVIDER_API_KEY_FILE": str(paths["provider"]),
                "CRAG_REPOSITORY_ROOT": str(repositories),
                "CRAG_REPOSITORIES_JSON": json.dumps(
                    {"owner/container": "/repositories/repo"}, separators=(",", ":")
                ),
                "CRAG_ALLOW_LOCAL_TOKEN": "true",
                "CRAG_SERVICE_HOST": "127.0.0.1",
                "CRAG_PUBLISHED_PORT": str(_free_port()),
                "CRAG_WORKER_RUNNER": "fake",
                "CRAG_WORKER_CONCURRENCY": "1",
                "CRAG_FAKE_RUN_SECONDS": "8",
                "CRAG_JOB_LEASE_SECONDS": "3",
                "CRAG_JOB_HEARTBEAT_SECONDS": "0.5",
                "CRAG_WORKER_POLL_SECONDS": "0.1",
                "CRAG_WORKER_STALE_SECONDS": "2",
                "CRAG_SHUTDOWN_GRACE_SECONDS": "3",
                "CRAG_CONTAINER_STOP_GRACE_PERIOD": "8s",
                "CRAG_RECEIVED_TIMEOUT_SECONDS": "5",
            }
        )
        harness = ComposeHarness(compose, docker, environment)
        worker_ids: list[str] = []
        try:
            rendered = harness.compose("config", timeout=30).stdout
            if any(secret in rendered for secret in _SENSITIVE):
                raise HarnessError("runtime secret appeared in rendered Compose configuration")
            harness.compose("build", "api", timeout=900)
            harness.compose("up", "-d", "postgres", timeout=180)
            harness.compose(
                "--profile", "migration", "run", "--rm", "migrate", timeout=180
            )
            harness.compose("up", "-d", "--scale", "worker=2", "api", "worker", timeout=180)
            harness.wait_ready(200)
            health = harness.api({"path": "/healthz"})
            _assert_status(health, 200, "liveness")

            workers = harness.compose("ps", "-q", "worker").stdout.splitlines()
            worker_ids = [item.strip() for item in workers if item.strip()]
            if len(worker_ids) != 2:
                raise HarnessError(f"expected two worker containers, found {len(worker_ids)}")

            recovery_diff = (
                "diff --git a/recovery.py b/recovery.py\n"
                "--- a/recovery.py\n+++ b/recovery.py\n@@ -1 +1 @@\n-old = 1\n+new = 2\n"
            )
            submitted = harness.api(
                {
                    "method": "POST",
                    "path": "/v1/reviews/diff",
                    "headers": {"Idempotency-Key": "container-recovery"},
                    "json": {"repository": "owner/container", "diff": recovery_diff},
                }
            )
            _assert_status(submitted, 202, "durable submission")
            body = submitted.get("body")
            if not isinstance(body, dict):
                raise HarnessError("submission response body is malformed")
            job_id = str(body["review_id"])
            repository_id = str(body["repository_id"])
            harness.wait_state(job_id, {"running"})
            owner = harness.sql(
                f"SELECT lease_owner FROM review_jobs WHERE id='{job_id}'"
            )
            claimed_container: str | None = None
            for container_id in worker_ids:
                hostname = harness.docker_run(
                    "inspect", "--format", "{{.Config.Hostname}}", container_id
                ).stdout.strip()
                if owner == f"worker-{hostname}":
                    claimed_container = container_id
                    break
            if claimed_container is None:
                raise HarnessError(f"could not map lease owner {owner!r} to a worker")
            harness.docker_run("update", "--restart=no", claimed_container)
            harness.docker_run("stop", "--time", "0", claimed_container)
            recovered = harness.wait_state(job_id, {"awaiting_approval"}, timeout=40)
            if int(recovered.get("attempt_count", 0)) < 2:
                raise HarnessError("worker death did not create a recovered attempt")

            replay = harness.api(
                {
                    "method": "POST",
                    "path": "/v1/reviews/diff",
                    "headers": {"Idempotency-Key": "container-recovery"},
                    "json": {"repository": "owner/container", "diff": recovery_diff},
                }
            )
            _assert_status(replay, 202, "idempotent replay")
            replay_body = replay.get("body")
            if not isinstance(replay_body, dict) or not replay_body.get("duplicate"):
                raise HarnessError("idempotent replay was not marked duplicate")
            if str(replay_body.get("review_id")) != job_id:
                raise HarnessError("idempotent replay changed the logical job")

            webhook_payload = {
                "action": "opened",
                "repository": {"full_name": "owner/container"},
                "pull_request": {"number": 17, "head": {"sha": "c" * 40}},
            }
            webhook_headers = {
                "Content-Type": "application/json",
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "container-delivery-a",
            }
            before = time.monotonic()
            webhook = harness.api(
                {
                    "method": "POST",
                    "path": "/webhooks/github",
                    "json": webhook_payload,
                    "headers": webhook_headers,
                    "webhook": True,
                }
            )
            webhook_ack_seconds = time.monotonic() - before
            _assert_status(webhook, 202, "webhook submission")
            if webhook_ack_seconds >= 5:
                raise HarnessError("webhook handler waited for fake Review execution")
            webhook_headers["X-GitHub-Delivery"] = "container-delivery-b"
            webhook_replay = harness.api(
                {
                    "method": "POST",
                    "path": "/webhooks/github",
                    "json": webhook_payload,
                    "headers": webhook_headers,
                    "webhook": True,
                }
            )
            _assert_status(webhook_replay, 202, "webhook replay")
            first_webhook = webhook.get("body")
            second_webhook = webhook_replay.get("body")
            if (
                not isinstance(first_webhook, dict)
                or not isinstance(second_webhook, dict)
                or not second_webhook.get("duplicate")
                or first_webhook.get("review_id") != second_webhook.get("review_id")
            ):
                raise HarnessError("webhook replay created another logical job")
            webhook_job_id = str(first_webhook["review_id"])
            harness.wait_state(
                webhook_job_id, {"running", "awaiting_approval"}, timeout=15
            )

            for container_id in worker_ids:
                harness.docker_run("update", "--restart=no", container_id)
            running_workers = harness.compose("ps", "-q", "worker").stdout.splitlines()
            if running_workers:
                harness.docker_run("stop", "--time", "3", *running_workers, timeout=30)
            harness.wait_ready(503, timeout=12)

            harness.sql(
                "UPDATE service_quotas SET max_queued_jobs=1 "
                "WHERE scope_kind='repository' AND repository_id='"
                + repository_id
                + "'"
            )
            queued = harness.api(
                {
                    "method": "POST",
                    "path": "/v1/reviews/diff",
                    "json": {
                        "repository": "owner/container",
                        "diff": recovery_diff.replace("old = 1", "old = 3"),
                    },
                }
            )
            _assert_status(queued, 202, "queue fill submission")
            queue_body = queued.get("body")
            if not isinstance(queue_body, dict):
                raise HarnessError("queue fill response is malformed")
            queue_job_id = str(queue_body["review_id"])
            overflow = harness.api(
                {
                    "method": "POST",
                    "path": "/v1/reviews/diff",
                    "json": {
                        "repository": "owner/container",
                        "diff": recovery_diff.replace("old = 1", "old = 4"),
                    },
                }
            )
            _assert_status(overflow, 429, "queue overflow")
            overflow_body = overflow.get("body")
            if not isinstance(overflow_body, dict) or (
                overflow_body.get("error", {}).get("code") != "queue_full"
            ):
                raise HarnessError("queue overflow did not return stable queue_full")

            harness.docker_run("start", *worker_ids, timeout=60)
            harness.wait_ready(200, timeout=20)
            harness.wait_state(queue_job_id, {"awaiting_approval"}, timeout=35)
            harness.wait_state(webhook_job_id, {"awaiting_approval"}, timeout=35)

            attempts = int(
                harness.sql(
                    "SELECT COUNT(*) FROM provider_usage WHERE review_job_id='"
                    + job_id
                    + "'"
                )
            )
            if attempts != 2:
                raise HarnessError(f"recovered job recorded {attempts} attempts, expected 2")
            distinct_attempts = int(
                harness.sql(
                    "SELECT COUNT(DISTINCT attempt_count) FROM provider_usage "
                    "WHERE review_job_id='" + job_id + "'"
                )
            )
            if distinct_attempts != attempts:
                raise HarnessError("recovered job recorded duplicate attempt accounting")

            lock_check = harness.compose(
                "exec",
                "-T",
                "api",
                "python",
                "-c",
                "from pathlib import Path; "
                "raise SystemExit(1 if Path('/var/lib/crag/state/.service.lock').exists() else 0)",
                timeout=20,
            )
            if lock_check.returncode != 0:
                raise HarnessError("state-directory process lock exists")

            harness.compose(
                "exec", "-T", "api", "python", "-c", _SECRET_SCAN, timeout=30
            )
            for container_id in worker_ids:
                harness.docker_run(
                    "exec", container_id, "python", "-c", _SECRET_SCAN, timeout=30
                )
            harness.compose(
                "exec", "-T", "api", "python", "-c", _RUNTIME_IDENTITY_SCAN, timeout=30
            )
            for container_id in worker_ids:
                harness.docker_run(
                    "exec", container_id, "python", "-c", _RUNTIME_IDENTITY_SCAN, timeout=30
                )

            image_id = harness.compose("images", "-q", "api").stdout.strip()
            harness.docker_run(
                "run",
                "--rm",
                "-i",
                "--entrypoint",
                "python",
                image_id,
                "-c",
                _IMAGE_HOST_PATH_SCAN,
                input_text=json.dumps(
                    [str(ROOT), str(ROOT).replace("\\", "/")],
                    separators=(",", ":"),
                ),
                timeout=60,
            )

            graceful = harness.api(
                {
                    "method": "POST",
                    "path": "/v1/reviews/diff",
                    "json": {
                        "repository": "owner/container",
                        "diff": recovery_diff.replace("old = 1", "old = 5"),
                    },
                }
            )
            _assert_status(graceful, 202, "graceful stop submission")
            graceful_body = graceful.get("body")
            if not isinstance(graceful_body, dict):
                raise HarnessError("graceful stop response is malformed")
            harness.wait_state(str(graceful_body["review_id"]), {"running"})
            harness.compose("stop", "worker", timeout=30)
            for container_id in worker_ids:
                state = json.loads(
                    harness.docker_run(
                        "inspect", "--format", "{{json .State}}", container_id
                    ).stdout
                )
                if state.get("ExitCode") != 0 or state.get("OOMKilled"):
                    raise HarnessError("worker did not exit cleanly within container grace")

            evidence = rendered
            evidence += harness.compose("logs", "--no-color", timeout=60).stdout
            for container_id in worker_ids:
                evidence += harness.docker_run("inspect", container_id).stdout
            evidence += harness.docker_run("history", "--no-trunc", image_id).stdout
            leaked = [name for name, value in values.items() if value in evidence]
            if leaked:
                raise HarnessError(
                    "runtime secret appeared in config/log/inspect/history: "
                    + ", ".join(sorted(leaked))
                )

            return {
                "schema_version": "crag.phase9c.container/v1",
                "workers": len(worker_ids),
                "recovered_attempts": attempts,
                "webhook_duplicate": True,
                "webhook_ack_seconds": round(webhook_ack_seconds, 6),
                "queue_overflow_code": "queue_full",
                "explicit_migration": True,
                "secret_scan": "passed",
                "image_host_path_scan": "passed",
                "graceful_stop": "passed",
                "passed": True,
            }
        except BaseException:
            diagnostics = harness.compose(
                "logs", "--no-color", check=False, timeout=30
            )
            if diagnostics.stdout or diagnostics.stderr:
                print(_redact(diagnostics.stdout + diagnostics.stderr), file=sys.stderr)
            raise
        finally:
            harness.compose(
                "down",
                "--volumes",
                "--remove-orphans",
                check=False,
                timeout=120,
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the offline Phase 9C container gate")
    parser.add_argument(
        "--prepare-context",
        type=Path,
        help="copy the allowlisted Docker build inputs to a new directory and exit",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        if args.prepare_context is not None:
            prepare_context(args.prepare_context)
            print(
                json.dumps(
                    {
                        "schema_version": "crag.phase9c.build-context/v1",
                        "passed": True,
                    },
                    sort_keys=True,
                )
            )
            return
        print(json.dumps(_run_acceptance(), sort_keys=True))
    except HarnessError as exc:
        raise SystemExit(_redact(str(exc))) from exc


if __name__ == "__main__":
    main()
