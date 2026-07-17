"""Fail-closed Docker command execution for repair worktrees.

The repair model never receives a host shell.  A caller supplies an exact
per-task argv allowlist, and this module translates an approved argv into one
hardened ``docker run`` invocation with a fixed worktree mount and no network.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import shutil
import stat
import subprocess
from threading import Lock, Thread
import time
from typing import Mapping, Protocol, Sequence
from uuid import uuid4


class SandboxError(RuntimeError):
    pass


class SandboxUnavailable(SandboxError):
    pass


class SandboxPolicyError(SandboxError):
    pass


class SandboxCleanupError(SandboxError):
    pass


@dataclass(frozen=True)
class HostProcessResult:
    returncode: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False
    output_truncated: bool = False


class ProcessExecutor(Protocol):
    def execute(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout_seconds: float,
        output_limit_bytes: int,
        stdin_bytes: bytes | None,
    ) -> HostProcessResult: ...


class _BoundedCapture:
    def __init__(self, limit: int):
        self._remaining = limit
        self._buffers = (bytearray(), bytearray())
        self._lock = Lock()
        self.truncated = False

    def add(self, stream_index: int, chunk: bytes) -> None:
        with self._lock:
            kept = chunk[: self._remaining]
            self._buffers[stream_index].extend(kept)
            self._remaining -= len(kept)
            if len(kept) != len(chunk):
                self.truncated = True

    def text(self, stream_index: int) -> str:
        return bytes(self._buffers[stream_index]).decode("utf-8", errors="replace")


class BoundedProcessExecutor:
    """The only host process primitive used by the repair sandbox.

    It drains both pipes while retaining at most one shared byte budget, uses
    ``shell=False``, and kills the Docker CLI process on timeout.  The runner
    separately removes the named container so killing the CLI cannot leave a
    workload running in the background.
    """

    def execute(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout_seconds: float,
        output_limit_bytes: int,
        stdin_bytes: bytes | None,
    ) -> HostProcessResult:
        if not argv:
            raise ValueError("process argv cannot be empty")
        _positive_finite("timeout_seconds", timeout_seconds)
        _positive_int("output_limit_bytes", output_limit_bytes)
        started = time.monotonic()
        creationflags = 0
        start_new_session = os.name != "nt"
        if os.name == "nt":
            creationflags = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP"))
        process = subprocess.Popen(
            list(argv),
            cwd=str(cwd),
            env=dict(env),
            stdin=subprocess.PIPE if stdin_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )
        if process.stdout is None or process.stderr is None:
            process.kill()
            raise SandboxError("failed to capture sandbox process output")
        capture = _BoundedCapture(output_limit_bytes)

        def drain(stream_index: int, stream: object) -> None:
            reader = stream
            while True:
                chunk = reader.read(64 * 1024)  # type: ignore[attr-defined]
                if not chunk:
                    return
                capture.add(stream_index, chunk)

        threads = (
            Thread(target=drain, args=(0, process.stdout), daemon=True),
            Thread(target=drain, args=(1, process.stderr), daemon=True),
        )
        for thread in threads:
            thread.start()
        input_errors: list[BaseException] = []

        def feed_input() -> None:
            if process.stdin is None or stdin_bytes is None:
                return
            try:
                process.stdin.write(stdin_bytes)
                process.stdin.close()
            except (BrokenPipeError, OSError) as exc:
                input_errors.append(exc)

        input_thread = Thread(target=feed_input, daemon=True)
        input_thread.start()
        timed_out = False
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            process.wait(timeout=5)
        except BaseException:
            # KeyboardInterrupt/SystemExit must not strand the Docker CLI.  The
            # runner can only remove the named container after this process is
            # known to have stopped, so always terminate and reap before the
            # interrupt is allowed to escape.
            try:
                if process.poll() is None:
                    process.kill()
            finally:
                process.wait(timeout=5)
            raise
        finally:
            for thread in threads:
                thread.join(timeout=5)
            input_thread.join(timeout=5)
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            process.stdout.close()
            process.stderr.close()
        if input_errors and not timed_out:
            raise SandboxError(f"failed to send sandbox stdin: {input_errors[0]}")
        return HostProcessResult(
            returncode=None if timed_out else process.returncode,
            stdout=capture.text(0),
            stderr=capture.text(1),
            duration_seconds=time.monotonic() - started,
            timed_out=timed_out,
            output_truncated=capture.truncated,
        )


@dataclass(frozen=True)
class CommandPolicy:
    """Exact commands and hard per-command resource limits for one task."""

    allowed_commands: frozenset[tuple[str, ...]]
    max_seconds: float = 300.0
    max_output_bytes: int = 1024 * 1024
    max_input_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        if not isinstance(self.allowed_commands, frozenset) or not self.allowed_commands:
            raise ValueError("allowed_commands must be a non-empty frozenset")
        for command in self.allowed_commands:
            _validate_argv(command)
        _positive_finite("max_seconds", self.max_seconds)
        _positive_int("max_output_bytes", self.max_output_bytes)
        _positive_int("max_input_bytes", self.max_input_bytes)

    def authorize(self, argv: Sequence[str]) -> tuple[str, ...]:
        command = _validate_argv(argv)
        if command not in self.allowed_commands:
            raise SandboxPolicyError("command is not in the exact task allowlist")
        return command


@dataclass(frozen=True)
class SandboxResult:
    operation_id: str
    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    output_truncated: bool


class SandboxTimeout(SandboxError):
    def __init__(self, operation_id: str, stdout: str, stderr: str):
        self.operation_id = operation_id
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(f"sandbox command timed out: operation_id={operation_id}")


@dataclass(frozen=True)
class ReadOnlyMount:
    source: Path
    target: str

    def __post_init__(self) -> None:
        source = _canonical_mount_source(self.source)
        if (
            not isinstance(self.target, str)
            or not self.target.startswith("/")
            or self.target == "/workspace"
            or ".." in self.target.split("/")
            or any(char in self.target for char in (",", "\n", "\r", "\x00"))
        ):
            raise SandboxPolicyError("read-only mount target is invalid")
        object.__setattr__(self, "source", source)


@dataclass(frozen=True)
class WritableMount:
    """Explicit control-plane write mount; never exposed to repair model tools."""

    source: Path
    target: str

    def __post_init__(self) -> None:
        source = _canonical_mount_source(self.source)
        _validate_extra_mount_target(self.target)
        object.__setattr__(self, "source", source)


class DockerSandboxRunner:
    """Run allowlisted commands in a locked-down, offline Docker container."""

    def __init__(
        self,
        *,
        worktree: Path,
        image: str,
        policy: CommandPolicy,
        docker_path: Path | None = None,
        executor: ProcessExecutor | None = None,
        read_only_mounts: tuple[ReadOnlyMount, ...] = (),
        writable_mounts: tuple[WritableMount, ...] = (),
        container_environment: Mapping[str, str] | None = None,
    ):
        self.worktree = _canonical_worktree(worktree)
        self.image = _validate_image(image)
        self.policy = policy
        discovered = shutil.which("docker") if docker_path is None else str(docker_path)
        self._docker_path = None if discovered is None else Path(discovered)
        if self._docker_path is not None and not self._docker_path.is_absolute():
            raise SandboxPolicyError("docker executable path must be absolute")
        self._executor = executor or BoundedProcessExecutor()
        self._read_only_mounts = _validate_read_only_mounts(read_only_mounts)
        self._writable_mounts = _validate_writable_mounts(writable_mounts)
        targets = [item.target for item in self._read_only_mounts]
        targets.extend(item.target for item in self._writable_mounts)
        if len(set(targets)) != len(targets):
            raise SandboxPolicyError("extra mount targets must be unique")
        self._container_environment = _validate_container_environment(
            container_environment or {}
        )
        self._probed = False

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float | None = None,
        stdin_bytes: bytes | None = None,
    ) -> SandboxResult:
        command = self.policy.authorize(argv)
        timeout = self.policy.max_seconds if timeout_seconds is None else timeout_seconds
        _positive_finite("timeout_seconds", timeout)
        if timeout > self.policy.max_seconds:
            raise SandboxPolicyError("requested timeout exceeds the task policy")
        if stdin_bytes is not None:
            if not isinstance(stdin_bytes, bytes):
                raise SandboxPolicyError("sandbox stdin must be bytes")
            if len(stdin_bytes) > self.policy.max_input_bytes:
                raise SandboxPolicyError("sandbox stdin exceeds the task policy")
        self._ensure_available()
        operation_id = uuid4().hex
        container_name = f"crag-repair-{operation_id}"
        docker_argv = self._docker_command(
            container_name, command, interactive=stdin_bytes is not None
        )
        try:
            outcome = self._execute(
                docker_argv,
                timeout_seconds=timeout,
                output_limit_bytes=self.policy.max_output_bytes,
                stdin_bytes=stdin_bytes,
            )
        except BaseException as exc:
            # An interrupt can arrive after Docker created the container but
            # before the CLI returned.  Removing by the preassigned name is the
            # only fail-closed way to prove that workload is no longer alive.
            try:
                self._remove_container(container_name)
            except BaseException as cleanup_exc:
                raise SandboxCleanupError(
                    "interrupted sandbox container could not be proven stopped"
                ) from cleanup_exc
            if isinstance(exc, OSError):
                raise SandboxUnavailable(f"cannot start Docker: {exc}") from exc
            raise
        if outcome.timed_out:
            self._remove_container(container_name)
            raise SandboxTimeout(operation_id, outcome.stdout, outcome.stderr)
        if outcome.returncode is None:
            raise SandboxError("Docker returned no exit code")
        return SandboxResult(
            operation_id=operation_id,
            argv=command,
            exit_code=outcome.returncode,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            duration_seconds=outcome.duration_seconds,
            output_truncated=outcome.output_truncated,
        )

    def _ensure_available(self) -> None:
        if self._probed:
            return
        if self._docker_path is None:
            raise SandboxUnavailable("Docker is not installed or not on PATH")
        try:
            outcome = self._execute(
                (
                    str(self._docker_path),
                    "version",
                    "--format",
                    "{{.Server.Version}}",
                ),
                timeout_seconds=10.0,
                output_limit_bytes=4096,
                stdin_bytes=None,
            )
        except OSError as exc:
            raise SandboxUnavailable(f"cannot probe Docker: {exc}") from exc
        if outcome.timed_out or outcome.returncode != 0 or not outcome.stdout.strip():
            detail = (outcome.stderr or outcome.stdout).strip()[:500]
            raise SandboxUnavailable(f"Docker daemon is unavailable: {detail or 'probe failed'}")
        self._probed = True

    def _docker_command(
        self,
        container_name: str,
        command: tuple[str, ...],
        *,
        interactive: bool,
    ) -> tuple[str, ...]:
        if self._docker_path is None:
            raise SandboxUnavailable("Docker is unavailable")
        mount_source = str(self.worktree)
        options = [
            str(self._docker_path),
            "run",
            "--rm",
        ]
        if interactive:
            options.append("-i")
        options.extend(
            [
            "--pull",
            "never",
            "--name",
            container_name,
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--user",
            "65532:65532",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "256",
            "--memory",
            "512m",
            "--cpus",
            "1",
            "--mount",
            f"type=bind,source={mount_source},target=/workspace",
            ]
        )
        for read_only_mount in self._read_only_mounts:
            options.extend(
                (
                    "--mount",
                    "type=bind,"
                    f"source={read_only_mount.source},"
                    f"target={read_only_mount.target},readonly",
                )
            )
        for writable_mount in self._writable_mounts:
            options.extend(
                (
                    "--mount",
                    f"type=bind,source={writable_mount.source},target={writable_mount.target}",
                )
            )
        for name, value in self._container_environment:
            options.extend(("--env", f"{name}={value}"))
        options.extend(
            [
            "--workdir",
            "/workspace",
            "--entrypoint",
            command[0],
            self.image,
            *command[1:],
            ]
        )
        return tuple(options)

    def _remove_container(self, container_name: str) -> None:
        if self._docker_path is None:
            raise SandboxCleanupError("cannot remove sandbox container without Docker")
        try:
            outcome = self._execute(
                (str(self._docker_path), "rm", "-f", container_name),
                timeout_seconds=10.0,
                output_limit_bytes=4096,
                stdin_bytes=None,
            )
        except OSError as exc:
            raise SandboxCleanupError(f"cannot remove sandbox container: {exc}") from exc
        missing = "no such container" in (outcome.stdout + outcome.stderr).casefold()
        if outcome.timed_out or (outcome.returncode != 0 and not missing):
            raise SandboxCleanupError("sandbox container could not be proven stopped")

    def _execute(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: float,
        output_limit_bytes: int,
        stdin_bytes: bytes | None,
    ) -> HostProcessResult:
        return self._executor.execute(
            argv,
            cwd=self.worktree,
            env=_scrubbed_host_environment(),
            timeout_seconds=timeout_seconds,
            output_limit_bytes=output_limit_bytes,
            stdin_bytes=stdin_bytes,
        )


def _validate_argv(argv: Sequence[str]) -> tuple[str, ...]:
    if isinstance(argv, (str, bytes)) or not isinstance(argv, Sequence) or not argv:
        raise ValueError("command argv must be a non-empty sequence of strings")
    command = tuple(argv)
    for item in command:
        if not isinstance(item, str) or not item or "\x00" in item:
            raise ValueError("every argv item must be a non-empty string without NUL")
    executable = command[0].replace("\\", "/").rsplit("/", 1)[-1].casefold()
    if executable in {
        "bash",
        "cmd",
        "cmd.exe",
        "dash",
        "fish",
        "powershell",
        "powershell.exe",
        "pwsh",
        "sh",
        "zsh",
    }:
        raise SandboxPolicyError("shell executables are prohibited in repair commands")
    snippet_flags = {
        "node": {"-e", "--eval"},
        "node.exe": {"-e", "--eval"},
        "perl": {"-e"},
        "python": {"-c"},
        "python.exe": {"-c"},
        "python3": {"-c"},
        "ruby": {"-e"},
    }
    if any(item in snippet_flags.get(executable, set()) for item in command[1:]):
        raise SandboxPolicyError("inline interpreter snippets are prohibited")
    return command


def _path_has_symlink_or_reparse_component(path: Path) -> bool:
    """Detect filesystem aliases without comparing equivalent path spellings.

    ``Path.resolve()`` may expand a Windows 8.3 component (for example,
    ``RUNNER~1``) even when no symlink or junction is present.  Comparing the
    input text with the resolved text therefore rejects normal GitHub runner
    temporary directories.  Inspecting every existing component preserves the
    reparse-point boundary while allowing the canonical spelling to differ.
    """

    absolute = Path(os.path.abspath(path))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    for component in (absolute, *absolute.parents):
        try:
            metadata = os.lstat(component)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            return True
        if int(getattr(metadata, "st_file_attributes", 0)) & reparse_flag:
            return True
    return False


def _canonical_worktree(path: Path) -> Path:
    raw = Path(path)
    try:
        has_alias = _path_has_symlink_or_reparse_component(raw)
        resolved = raw.resolve(strict=True)
        has_alias = has_alias or _path_has_symlink_or_reparse_component(raw)
    except OSError as exc:
        raise SandboxPolicyError(f"worktree cannot be resolved: {exc}") from exc
    if not resolved.is_dir():
        raise SandboxPolicyError("worktree must be an existing directory")
    if has_alias:
        raise SandboxPolicyError("worktree path must not contain symlink or junction aliases")
    if any(char in str(resolved) for char in (",", "\n", "\r", "\x00")):
        raise SandboxPolicyError("worktree path cannot be represented safely as a Docker mount")
    return resolved


def _canonical_mount_source(path: Path) -> Path:
    raw = Path(path)
    try:
        has_alias = _path_has_symlink_or_reparse_component(raw)
        resolved = raw.resolve(strict=True)
        has_alias = has_alias or _path_has_symlink_or_reparse_component(raw)
    except OSError as exc:
        raise SandboxPolicyError(f"read-only mount cannot be resolved: {exc}") from exc
    if has_alias:
        raise SandboxPolicyError("read-only mount must not use symlink or junction aliases")
    if any(char in str(resolved) for char in (",", "\n", "\r", "\x00")):
        raise SandboxPolicyError("read-only mount source cannot be represented safely")
    return resolved


def _validate_extra_mount_target(target: str) -> None:
    if (
        not isinstance(target, str)
        or not target.startswith("/")
        or target == "/workspace"
        or ".." in target.split("/")
        or any(char in target for char in (",", "\n", "\r", "\x00"))
    ):
        raise SandboxPolicyError("extra mount target is invalid")


def _validate_read_only_mounts(
    mounts: tuple[ReadOnlyMount, ...],
) -> tuple[ReadOnlyMount, ...]:
    if not isinstance(mounts, tuple) or any(
        not isinstance(item, ReadOnlyMount) for item in mounts
    ):
        raise SandboxPolicyError("read_only_mounts must be a tuple of ReadOnlyMount")
    targets = [item.target for item in mounts]
    if len(set(targets)) != len(targets):
        raise SandboxPolicyError("read-only mount targets must be unique")
    return mounts


def _validate_writable_mounts(
    mounts: tuple[WritableMount, ...],
) -> tuple[WritableMount, ...]:
    if not isinstance(mounts, tuple) or any(
        not isinstance(item, WritableMount) for item in mounts
    ):
        raise SandboxPolicyError("writable_mounts must be a tuple of WritableMount")
    targets = [item.target for item in mounts]
    if len(set(targets)) != len(targets):
        raise SandboxPolicyError("writable mount targets must be unique")
    return mounts


_ALLOWED_CONTAINER_ENVIRONMENT = frozenset(
    {
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_DIR",
        "GIT_OPTIONAL_LOCKS",
        "GIT_TERMINAL_PROMPT",
        "GIT_WORK_TREE",
        "HOME",
        "PYTHONDONTWRITEBYTECODE",
        "PYTEST_ADDOPTS",
    }
)


def _validate_container_environment(
    environment: Mapping[str, str],
) -> tuple[tuple[str, str], ...]:
    if not isinstance(environment, Mapping):
        raise SandboxPolicyError("container environment must be a mapping")
    result = []
    for name, value in environment.items():
        if name not in _ALLOWED_CONTAINER_ENVIRONMENT:
            raise SandboxPolicyError(f"container environment variable is prohibited: {name}")
        if not isinstance(value, str) or any(
            char in value for char in ("\x00", "\n", "\r")
        ):
            raise SandboxPolicyError(f"container environment value is invalid: {name}")
        result.append((name, value))
    return tuple(sorted(result))


def _validate_image(image: str) -> str:
    if (
        not isinstance(image, str)
        or not image
        or image.startswith("-")
        or any(char.isspace() or char == "\x00" for char in image)
        or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-/:@" for char in image)
    ):
        raise SandboxPolicyError("Docker image reference is invalid")
    return image


def _scrubbed_host_environment() -> dict[str, str]:
    allowed = {}
    for name in ("SystemRoot", "WINDIR"):
        value = os.environ.get(name)
        if value:
            allowed[name] = value
    return allowed


def _positive_finite(name: str, value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a real number")
    if not math.isfinite(float(value)) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")


def _positive_int(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
