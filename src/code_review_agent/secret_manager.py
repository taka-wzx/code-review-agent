"""Versioned runtime secret loading and zero-restart client rotation."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
from typing import Any, Protocol


SECRET_SCHEMA_VERSION = "crag.runtime-secret/v1"
MAX_SECRET_FILE_BYTES = 16_384
MAX_SECRET_VALUE_BYTES = 4_096
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_FAILURE_CODES = frozenset(
    {
        "secret_source_unavailable",
        "secret_file_denied",
        "secret_file_oversized",
        "secret_payload_invalid",
        "secret_identity_mismatch",
        "secret_not_yet_valid",
        "secret_expired",
        "secret_expires_too_soon",
        "secret_generation_rollback",
        "secret_generation_conflict",
        "secret_client_build_failed",
    }
)
_EVENT_STATUSES = frozenset({"loaded", "unchanged", "rotated", "failed"})


class SecretManagerError(RuntimeError):
    """A bounded secret lifecycle failure with no credential-bearing detail."""

    def __init__(self, code: str) -> None:
        if code not in _FAILURE_CODES:
            raise ValueError("secret manager failure code is invalid")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SecretSnapshot:
    secret_id: str
    version: str
    generation: int
    not_before_utc: str
    expires_at_utc: str
    value: str = field(repr=False)

    @property
    def version_sha256(self) -> str:
        return hashlib.sha256(self.version.encode("utf-8")).hexdigest()

    @property
    def material_sha256(self) -> str:
        return hashlib.sha256(self.value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SecretRotationEvent:
    status: str
    generation: int | None
    version_sha256: str | None
    failure_code: str | None
    observed_at_utc: str

    def __post_init__(self) -> None:
        if self.status not in _EVENT_STATUSES:
            raise ValueError("secret rotation status is invalid")
        if self.generation is not None and (
            isinstance(self.generation, bool) or self.generation < 1
        ):
            raise ValueError("secret rotation generation is invalid")
        if self.version_sha256 is not None and _SHA256.fullmatch(self.version_sha256) is None:
            raise ValueError("secret rotation version hash is invalid")
        if self.failure_code is not None and self.failure_code not in _FAILURE_CODES:
            raise ValueError("secret rotation failure code is invalid")
        if self.status == "failed" and self.failure_code is None:
            raise ValueError("failed secret rotation event needs a failure code")
        if self.status != "failed" and self.failure_code is not None:
            raise ValueError("successful secret rotation event cannot have a failure code")
        _parse_utc_second(self.observed_at_utc)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "crag.secret-rotation-event/v1",
            "status": self.status,
            "generation": self.generation,
            "version_sha256": self.version_sha256,
            "failure_code": self.failure_code,
            "observed_at_utc": self.observed_at_utc,
            "secret_value_retained": False,
            "secret_path_retained": False,
        }


class SecretSource(Protocol):
    def fetch(self) -> SecretSnapshot:
        """Return the currently active secret snapshot."""


ClientBuilder = Callable[[str], tuple[Any, str]]
EventSink = Callable[[SecretRotationEvent], None]
UtcClock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_second(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_utc_second(value: Any) -> datetime:
    if not isinstance(value, str) or re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z",
        value,
    ) is None:
        raise SecretManagerError("secret_payload_invalid")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        raise SecretManagerError("secret_payload_invalid") from None


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON field")
        value[key] = item
    return value


class AtomicFileSecretManager:
    """Read one atomically replaced, versioned secret JSON file."""

    def __init__(
        self,
        path: Path,
        *,
        secret_id: str,
        minimum_ttl_seconds: int = 60,
        clock: UtcClock = _utc_now,
    ) -> None:
        if not isinstance(secret_id, str) or _IDENTIFIER.fullmatch(secret_id) is None:
            raise ValueError("secret identifier is invalid")
        if (
            isinstance(minimum_ttl_seconds, bool)
            or not isinstance(minimum_ttl_seconds, int)
            or not 0 <= minimum_ttl_seconds <= 3600
        ):
            raise ValueError("minimum secret TTL is invalid")
        self._path = Path(path)
        self._secret_id = secret_id
        self._minimum_ttl_seconds = minimum_ttl_seconds
        self._clock = clock

    def fetch(self) -> SecretSnapshot:
        raw = self._read_file()
        try:
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_object)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise SecretManagerError("secret_payload_invalid") from None
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "secret_id",
            "version",
            "generation",
            "value",
            "not_before_utc",
            "expires_at_utc",
        }:
            raise SecretManagerError("secret_payload_invalid")
        if value.get("schema_version") != SECRET_SCHEMA_VERSION:
            raise SecretManagerError("secret_payload_invalid")
        secret_id = value.get("secret_id")
        if secret_id != self._secret_id:
            raise SecretManagerError("secret_identity_mismatch")
        version = value.get("version")
        generation = value.get("generation")
        secret_value = value.get("value")
        if (
            not isinstance(version, str)
            or _IDENTIFIER.fullmatch(version) is None
            or isinstance(generation, bool)
            or not isinstance(generation, int)
            or not 1 <= generation <= 2_147_483_647
            or not isinstance(secret_value, str)
            or not secret_value
            or secret_value.strip() != secret_value
            or any(ord(character) < 32 for character in secret_value)
            or len(secret_value.encode("utf-8")) > MAX_SECRET_VALUE_BYTES
        ):
            raise SecretManagerError("secret_payload_invalid")
        not_before_value = value.get("not_before_utc")
        expires_value = value.get("expires_at_utc")
        if not isinstance(not_before_value, str) or not isinstance(expires_value, str):
            raise SecretManagerError("secret_payload_invalid")
        not_before = _parse_utc_second(not_before_value)
        expires = _parse_utc_second(expires_value)
        if expires <= not_before:
            raise SecretManagerError("secret_payload_invalid")
        current = self._clock()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        current = current.astimezone(timezone.utc)
        if current < not_before:
            raise SecretManagerError("secret_not_yet_valid")
        if current >= expires:
            raise SecretManagerError("secret_expired")
        if (expires - current).total_seconds() < self._minimum_ttl_seconds:
            raise SecretManagerError("secret_expires_too_soon")
        return SecretSnapshot(
            secret_id=secret_id,
            version=version,
            generation=generation,
            not_before_utc=not_before_value,
            expires_at_utc=expires_value,
            value=secret_value,
        )

    def _read_file(self) -> bytes:
        try:
            metadata = os.lstat(self._path)
        except OSError:
            raise SecretManagerError("secret_source_unavailable") from None
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise SecretManagerError("secret_file_denied")
        flags = os.O_RDONLY
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self._path, flags)
        except OSError:
            raise SecretManagerError("secret_source_unavailable") from None
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
            ):
                raise SecretManagerError("secret_file_denied")
            if os.name != "nt" and opened.st_mode & 0o022:
                raise SecretManagerError("secret_file_denied")
            if opened.st_size > MAX_SECRET_FILE_BYTES:
                raise SecretManagerError("secret_file_oversized")
            chunks: list[bytes] = []
            remaining = MAX_SECRET_FILE_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(remaining, 4096))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            if len(raw) > MAX_SECRET_FILE_BYTES:
                raise SecretManagerError("secret_file_oversized")
            return raw
        except OSError:
            raise SecretManagerError("secret_source_unavailable") from None
        finally:
            os.close(descriptor)


class RotatingSecretClientFactory:
    """Atomically replace a cached client when the secret generation advances."""

    def __init__(
        self,
        source: SecretSource,
        builder: ClientBuilder,
        *,
        event_sink: EventSink | None = None,
        clock: UtcClock = _utc_now,
    ) -> None:
        self._source = source
        self._builder = builder
        self._event_sink = event_sink
        self._clock = clock
        self._lock = threading.Lock()
        self._client_model: tuple[Any, str] | None = None
        self._generation: int | None = None
        self._observed_generation: int | None = None
        self._observed_version_sha256: str | None = None
        self._observed_material_sha256: str | None = None
        self._last_event: SecretRotationEvent | None = None

    def preflight(self) -> tuple[Any, str]:
        """Load and validate the initial secret before the worker starts."""
        return self()

    def __call__(self) -> tuple[Any, str]:
        try:
            snapshot = self._source.fetch()
        except SecretManagerError as exc:
            self._emit_failure(exc.code, None)
            raise
        version_sha256 = snapshot.version_sha256
        material_sha256 = snapshot.material_sha256
        with self._lock:
            current_generation = self._generation
            observed_generation = self._observed_generation
            if observed_generation is not None and snapshot.generation < observed_generation:
                error = SecretManagerError("secret_generation_rollback")
                event = self._failure_event(error.code, snapshot)
                client_model = None
            elif observed_generation is not None and snapshot.generation == observed_generation and (
                version_sha256 != self._observed_version_sha256
                or material_sha256 != self._observed_material_sha256
            ):
                error = SecretManagerError("secret_generation_conflict")
                event = self._failure_event(error.code, snapshot)
                client_model = None
            else:
                if observed_generation is None or snapshot.generation > observed_generation:
                    self._observed_generation = snapshot.generation
                    self._observed_version_sha256 = version_sha256
                    self._observed_material_sha256 = material_sha256
                if current_generation is not None and snapshot.generation == current_generation:
                    assert self._client_model is not None
                    event = self._event("unchanged", snapshot)
                    error = None
                    client_model = self._client_model
                else:
                    try:
                        built = self._builder(snapshot.value)
                    except Exception:
                        error = SecretManagerError("secret_client_build_failed")
                        event = self._failure_event(error.code, snapshot)
                        client_model = None
                    else:
                        if (
                            not isinstance(built, tuple)
                            or len(built) != 2
                            or built[0] is None
                            or not isinstance(built[1], str)
                            or not built[1]
                        ):
                            error = SecretManagerError("secret_client_build_failed")
                            event = self._failure_event(error.code, snapshot)
                            client_model = None
                        else:
                            status = "loaded" if current_generation is None else "rotated"
                            self._client_model = built
                            self._generation = snapshot.generation
                            event = self._event(status, snapshot)
                            error = None
                            client_model = built
            self._last_event = event
        self._publish(event)
        if error is not None:
            raise error from None
        assert client_model is not None
        return client_model

    def status(self) -> dict[str, Any]:
        """Return the latest redacted lifecycle event."""
        with self._lock:
            event = self._last_event
        if event is None:
            return {
                "schema_version": "crag.secret-rotation-event/v1",
                "status": "not_loaded",
                "generation": None,
                "version_sha256": None,
                "failure_code": None,
                "observed_at_utc": None,
                "secret_value_retained": False,
                "secret_path_retained": False,
            }
        return event.as_dict()

    def _emit_failure(self, code: str, snapshot: SecretSnapshot | None) -> None:
        event = self._failure_event(code, snapshot)
        with self._lock:
            self._last_event = event
        self._publish(event)

    def _failure_event(
        self,
        code: str,
        snapshot: SecretSnapshot | None,
    ) -> SecretRotationEvent:
        return SecretRotationEvent(
            status="failed",
            generation=None if snapshot is None else snapshot.generation,
            version_sha256=None if snapshot is None else snapshot.version_sha256,
            failure_code=code,
            observed_at_utc=_utc_second(self._clock()),
        )

    def _event(self, status: str, snapshot: SecretSnapshot) -> SecretRotationEvent:
        return SecretRotationEvent(
            status=status,
            generation=snapshot.generation,
            version_sha256=snapshot.version_sha256,
            failure_code=None,
            observed_at_utc=_utc_second(self._clock()),
        )

    def _publish(self, event: SecretRotationEvent) -> None:
        if self._event_sink is None:
            return
        try:
            self._event_sink(event)
        except Exception:
            return
