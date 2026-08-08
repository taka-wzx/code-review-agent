"""Phase 11C Gate B one-shot deterministic synthetic provider executor.

The executable is deliberately narrow: one fixed GLM endpoint, one fixed request,
one fixed Linux credential path, one durable state directory, and no retry.  A
sealed authorization and the exact one-use approval text must both be present before
any credential access or network attempt.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import ssl
import stat
import sys
from typing import Any, Callable, Mapping, NoReturn, Protocol, Sequence

try:  # Linux-only at execution time; importability is retained for offline Windows tests.
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - exercised only on non-POSIX Python builds
    _fcntl = None  # type: ignore[assignment]

fcntl: Any = _fcntl


PHASE_ID = "phase11c-gateb-one-shot-executor-v1"
AUTHORIZATION_SCHEMA_VERSION = "phase11c-gateb-one-shot-authorization/v1"
RECEIPT_SCHEMA_VERSION = "phase11c-gateb-one-shot-receipt/v1"
APPROVAL_BINDING_SCHEMA_VERSION = "phase11c-gateb-one-shot-approval-binding/v1"
STATE_SCHEMA_VERSION = "phase11c-gateb-one-shot-state/v1"

OWNER_ACCOUNT = "taka-wzx"
PROVIDER = "glm"
REQUEST_MODEL_ID = "glm-5.2"
API_SURFACE = "chat.completions.create"
ENDPOINT_ID = "glm_standard_v4"
ENDPOINT_HOST = "open.bigmodel.cn"
ENDPOINT_PORT = 443
ENDPOINT_PATH = "/api/paas/v4/chat/completions"
TERMINAL_TOKEN = "PHASE11C_GATEB_OK"

CREDENTIAL_PATH = Path("/run/crag-gateb/glm_api_key")
AUTHORIZATION_PATH = Path("/run/crag-gateb/authorization.json")
APPROVAL_PATH = Path("/run/crag-gateb/approval.txt")
STATE_DIRECTORY = Path("/var/lib/crag-gateb")
STATE_PATH = STATE_DIRECTORY / "state.json"
RECEIPT_PATH = STATE_DIRECTORY / "receipt.json"
LOCK_PATH = STATE_DIRECTORY / "state.lock"

CREDENTIAL_FINGERPRINT_SHA256 = (
    "0f2b300e874ecbd0c4d14b9d5b5d381e601fae658d14c400540cd1533581d6ff"
)
TARIFF_EVIDENCE_SHA256 = "cafc2a706e0d1da40d79db2e4f464df75db42b06240812e164a75f532043e70c"
TARIFF_OBSERVED_UTC = "2026-07-30T09:26:31Z"

MAX_LOGICAL_CALLS = 1
MAX_HTTP_ATTEMPTS = 1
MAX_INPUT_TOKENS = 2_000
MAX_OUTPUT_TOKENS = 128
INPUT_RATE_MICROCNY_PER_MILLION = 8_000_000
CACHED_INPUT_RATE_MICROCNY_PER_MILLION = 2_000_000
OUTPUT_RATE_MICROCNY_PER_MILLION = 28_000_000
DIAGNOSTIC_BUDGET_MICROCNY = 19_584
AGGREGATE_BUDGET_MICROCNY = 15_000_000
HTTP_TIMEOUT_SECONDS = 60
MAX_PROVIDER_RESPONSE_BYTES = 262_144
MAX_CONTROL_FILE_BYTES = 65_536
MAX_CREDENTIAL_BYTES = 4_096
MAX_PROVIDER_USAGE_COUNTER = 1_000_000
ZERO_SHA256 = "0" * 64
PENDING_FREEZE = "PENDING_FREEZE"
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_FCHMOD = getattr(os, "fchmod", None)

# Literal bytes make the dispatched payload immutable and avoid serializer drift.
REQUEST_BODY = (
    b'{"max_tokens":128,"messages":[{"content":"This is a deterministic synthetic '
    b'protocol canary. Return exactly PHASE11C_GATEB_OK and no other text.","role":"user"}],'
    b'"model":"glm-5.2","stream":false,"temperature":0.01,"thinking":{"type":"disabled"}}'
)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\Z")


class GateBOneShotError(ValueError):
    """Stable, non-secret failure code for a refused or failed one-shot run."""


def _fail(code: str) -> NoReturn:
    raise GateBOneShotError(code)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonicalize(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value < 0:
            _fail("negative_integer")
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, float):
        _fail("floating_point")
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                _fail("non_string_json_key")
            result[key] = _canonicalize(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]
    _fail("unsupported_canonical_type")


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        _canonicalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def strict_json_loads(value: str | bytes) -> Any:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                _fail("duplicate_json_key")
            result[key] = item
        return result

    try:
        parsed = json.loads(value, object_pairs_hook=reject_duplicate_keys)
    except GateBOneShotError:
        raise
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise GateBOneShotError("invalid_json") from exc
    return _canonicalize(parsed)


def source_sha256() -> str:
    """Hash this executable only, with cross-platform newline normalization."""

    normalized = Path(__file__).read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return sha256_bytes(normalized.encode("utf-8"))


def request_sha256() -> str:
    return sha256_bytes(REQUEST_BODY)


def endpoint_sha256() -> str:
    return sha256_bytes(
        canonical_json(
            {
                "host": ENDPOINT_HOST,
                "method": "POST",
                "path": ENDPOINT_PATH,
                "port": ENDPOINT_PORT,
                "tls": True,
            }
        )
    )


def cohort_sha256() -> str:
    return sha256_bytes(
        canonical_json(
            {
                "cohort": "single_deterministic_synthetic_protocol_canary",
                "expected_terminal_sha256": sha256_bytes(TERMINAL_TOKEN.encode("ascii")),
                "records": 1,
            }
        )
    )


def _expect_mapping(value: Any, code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _fail(code)
    return dict(value)


def _expect_exact_keys(value: Mapping[str, Any], expected: frozenset[str], code: str) -> None:
    if set(value) != expected:
        _fail(code)


def _expect_sha256(value: Any, code: str, *, allow_zero: bool = True) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        _fail(code)
    if not allow_zero and value == ZERO_SHA256:
        _fail(code)
    return value


def _expect_nonnegative_int(value: Any, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail(code)
    return value


def _expect_bool(value: Any, code: str) -> bool:
    if not isinstance(value, bool):
        _fail(code)
    return value


def _parse_utc(value: Any, code: str) -> datetime:
    if not isinstance(value, str):
        _fail(code)
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise GateBOneShotError(code) from exc
    return parsed.replace(tzinfo=timezone.utc)


def _ceil_div(numerator: int, denominator: int) -> int:
    return (numerator + denominator - 1) // denominator


def worst_case_microcny(
    *, input_tokens: int, output_tokens: int, input_rate: int, output_rate: int
) -> int:
    for value in (input_tokens, output_tokens, input_rate, output_rate):
        _expect_nonnegative_int(value, "invalid_cost_component")
    return _ceil_div(input_tokens * input_rate, 1_000_000) + _ceil_div(
        output_tokens * output_rate, 1_000_000
    )


def _seal(value: Mapping[str, Any], field: str) -> dict[str, Any]:
    result = deepcopy(dict(value))
    if result.get(field) != "":
        _fail("invalid_unsealed_document")
    result[field] = sha256_bytes(canonical_json(result))
    return result


def _validate_seal(value: Mapping[str, Any], field: str, code: str) -> None:
    document = dict(value)
    observed = _expect_sha256(document.get(field), code, allow_zero=False)
    document[field] = ""
    if sha256_bytes(canonical_json(document)) != observed:
        _fail(code)


AUTHORIZATION_FIELDS = frozenset(
    {
        "schema_version",
        "phase_id",
        "stage",
        "authorization_status",
        "authorization_sha256",
        "executable_source_sha256",
        "source_tree_sha256",
        "dockerfile_sha256",
        "compose_sha256",
        "image_sha256",
        "deployment_sha256",
        "runtime_identity_sha256",
        "cohort_sha256",
        "provider",
        "request_model_id",
        "api_surface",
        "endpoint_id",
        "endpoint_sha256",
        "request_sha256",
        "provider_policy_evidence_sha256",
        "provider_policy_accepted",
        "provider_tariff_evidence_sha256",
        "tariff_observed_utc",
        "tariff_effective_date_waived",
        "credential_delivery_mode",
        "credential_fingerprint_sha256",
        "owner_account",
        "owner_reconfirmed",
        "kill_switch_bound",
        "authorization_window_start_utc",
        "authorization_window_end_utc",
        "max_logical_calls",
        "max_http_attempts",
        "max_input_tokens",
        "max_output_tokens",
        "input_rate_microcny_per_million",
        "cached_input_rate_microcny_per_million",
        "output_rate_microcny_per_million",
        "diagnostic_budget_microcny",
        "aggregate_budget_microcny",
        "sdk_retries",
        "transport_retries",
        "concurrency",
        "local_raw_retention",
        "live_execution_enabled",
    }
)


def build_authorization_template() -> dict[str, Any]:
    return {
        "schema_version": AUTHORIZATION_SCHEMA_VERSION,
        "phase_id": PHASE_ID,
        "stage": "DIAGNOSTIC",
        "authorization_status": "frozen_pending_exact_approval",
        "authorization_sha256": "",
        "executable_source_sha256": source_sha256(),
        "source_tree_sha256": PENDING_FREEZE,
        "dockerfile_sha256": PENDING_FREEZE,
        "compose_sha256": PENDING_FREEZE,
        "image_sha256": PENDING_FREEZE,
        "deployment_sha256": PENDING_FREEZE,
        "runtime_identity_sha256": PENDING_FREEZE,
        "cohort_sha256": cohort_sha256(),
        "provider": PROVIDER,
        "request_model_id": REQUEST_MODEL_ID,
        "api_surface": API_SURFACE,
        "endpoint_id": ENDPOINT_ID,
        "endpoint_sha256": endpoint_sha256(),
        "request_sha256": request_sha256(),
        "provider_policy_evidence_sha256": PENDING_FREEZE,
        "provider_policy_accepted": True,
        "provider_tariff_evidence_sha256": TARIFF_EVIDENCE_SHA256,
        "tariff_observed_utc": TARIFF_OBSERVED_UTC,
        "tariff_effective_date_waived": True,
        "credential_delivery_mode": "fixed_linux_ecs_one_time_file",
        "credential_fingerprint_sha256": CREDENTIAL_FINGERPRINT_SHA256,
        "owner_account": OWNER_ACCOUNT,
        "owner_reconfirmed": True,
        "kill_switch_bound": True,
        "authorization_window_start_utc": PENDING_FREEZE,
        "authorization_window_end_utc": PENDING_FREEZE,
        "max_logical_calls": MAX_LOGICAL_CALLS,
        "max_http_attempts": MAX_HTTP_ATTEMPTS,
        "max_input_tokens": MAX_INPUT_TOKENS,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "input_rate_microcny_per_million": INPUT_RATE_MICROCNY_PER_MILLION,
        "cached_input_rate_microcny_per_million": CACHED_INPUT_RATE_MICROCNY_PER_MILLION,
        "output_rate_microcny_per_million": OUTPUT_RATE_MICROCNY_PER_MILLION,
        "diagnostic_budget_microcny": DIAGNOSTIC_BUDGET_MICROCNY,
        "aggregate_budget_microcny": AGGREGATE_BUDGET_MICROCNY,
        "sdk_retries": 0,
        "transport_retries": 0,
        "concurrency": 1,
        "local_raw_retention": False,
        "live_execution_enabled": True,
    }


def _validate_authorization_common(
    value: Any,
    *,
    executable_source_digest: str,
    now_utc: datetime,
    sealed: bool,
    require_active_window: bool,
) -> dict[str, Any]:
    authorization = _expect_mapping(value, "invalid_authorization")
    _expect_exact_keys(authorization, AUTHORIZATION_FIELDS, "invalid_authorization_keys")
    if authorization["schema_version"] != AUTHORIZATION_SCHEMA_VERSION:
        _fail("authorization_schema_version_mismatch")
    if authorization["phase_id"] != PHASE_ID or authorization["stage"] != "DIAGNOSTIC":
        _fail("authorization_identity_mismatch")
    if authorization["authorization_status"] != "frozen_pending_exact_approval":
        _fail("authorization_status_mismatch")
    if sealed:
        _validate_seal(authorization, "authorization_sha256", "authorization_sha256_mismatch")
    elif authorization["authorization_sha256"] != "":
        _fail("authorization_not_unsealed")

    source_digest = _expect_sha256(
        authorization["executable_source_sha256"], "invalid_executable_source_sha256", allow_zero=False
    )
    if source_digest != _expect_sha256(
        executable_source_digest, "invalid_expected_source_sha256", allow_zero=False
    ):
        _fail("executable_source_sha256_drift")
    for field in (
        "source_tree_sha256",
        "dockerfile_sha256",
        "compose_sha256",
        "image_sha256",
        "deployment_sha256",
        "runtime_identity_sha256",
        "provider_policy_evidence_sha256",
    ):
        _expect_sha256(authorization[field], f"invalid_{field}", allow_zero=False)
    expected_text = {
        "cohort_sha256": cohort_sha256(),
        "provider": PROVIDER,
        "request_model_id": REQUEST_MODEL_ID,
        "api_surface": API_SURFACE,
        "endpoint_id": ENDPOINT_ID,
        "endpoint_sha256": endpoint_sha256(),
        "request_sha256": request_sha256(),
        "provider_tariff_evidence_sha256": TARIFF_EVIDENCE_SHA256,
        "tariff_observed_utc": TARIFF_OBSERVED_UTC,
        "credential_delivery_mode": "fixed_linux_ecs_one_time_file",
        "credential_fingerprint_sha256": CREDENTIAL_FINGERPRINT_SHA256,
        "owner_account": OWNER_ACCOUNT,
    }
    for field, expected_text_value in expected_text.items():
        if authorization[field] != expected_text_value:
            _fail(f"{field}_mismatch")
    if not _OWNER.fullmatch(authorization["owner_account"]):
        _fail("invalid_owner_account")
    for field in (
        "provider_policy_accepted",
        "tariff_effective_date_waived",
        "owner_reconfirmed",
        "kill_switch_bound",
        "live_execution_enabled",
    ):
        if _expect_bool(authorization[field], f"invalid_{field}") is not True:
            _fail(f"{field}_not_true")
    if _expect_bool(authorization["local_raw_retention"], "invalid_local_raw_retention") is not False:
        _fail("local_raw_retention_forbidden")

    expected_integers = {
        "max_logical_calls": MAX_LOGICAL_CALLS,
        "max_http_attempts": MAX_HTTP_ATTEMPTS,
        "max_input_tokens": MAX_INPUT_TOKENS,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "input_rate_microcny_per_million": INPUT_RATE_MICROCNY_PER_MILLION,
        "cached_input_rate_microcny_per_million": CACHED_INPUT_RATE_MICROCNY_PER_MILLION,
        "output_rate_microcny_per_million": OUTPUT_RATE_MICROCNY_PER_MILLION,
        "diagnostic_budget_microcny": DIAGNOSTIC_BUDGET_MICROCNY,
        "aggregate_budget_microcny": AGGREGATE_BUDGET_MICROCNY,
        "sdk_retries": 0,
        "transport_retries": 0,
        "concurrency": 1,
    }
    for field, expected_integer in expected_integers.items():
        if _expect_nonnegative_int(authorization[field], f"invalid_{field}") != expected_integer:
            _fail(f"{field}_mismatch")
    calculated_budget = worst_case_microcny(
        input_tokens=MAX_INPUT_TOKENS,
        output_tokens=MAX_OUTPUT_TOKENS,
        input_rate=INPUT_RATE_MICROCNY_PER_MILLION,
        output_rate=OUTPUT_RATE_MICROCNY_PER_MILLION,
    )
    if calculated_budget != DIAGNOSTIC_BUDGET_MICROCNY:
        _fail("internal_budget_constant_mismatch")
    if DIAGNOSTIC_BUDGET_MICROCNY > AGGREGATE_BUDGET_MICROCNY:
        _fail("aggregate_budget_exceeded")

    if now_utc.tzinfo is None:
        _fail("invalid_now_utc")
    now = now_utc.astimezone(timezone.utc)
    start = _parse_utc(authorization["authorization_window_start_utc"], "invalid_window_start_utc")
    end = _parse_utc(authorization["authorization_window_end_utc"], "invalid_window_end_utc")
    if start >= end or end - start > timedelta(minutes=30):
        _fail("authorization_window_invalid")
    if require_active_window:
        if not start <= now < end:
            _fail("authorization_window_not_active")
    elif now >= end:
        _fail("authorization_window_expired")
    return authorization


def seal_authorization(
    value: Any, *, executable_source_digest: str | None = None, now_utc: datetime | None = None
) -> dict[str, Any]:
    source_digest = executable_source_digest or source_sha256()
    now = now_utc or datetime.now(timezone.utc)
    candidate = _validate_authorization_common(
        value,
        executable_source_digest=source_digest,
        now_utc=now,
        sealed=False,
        require_active_window=False,
    )
    return _seal(candidate, "authorization_sha256")


def validate_authorization(
    value: Any,
    *,
    executable_source_digest: str | None = None,
    now_utc: datetime | None = None,
    require_active_window: bool = True,
) -> dict[str, Any]:
    return _validate_authorization_common(
        value,
        executable_source_digest=executable_source_digest or source_sha256(),
        now_utc=now_utc or datetime.now(timezone.utc),
        sealed=True,
        require_active_window=require_active_window,
    )


def approval_binding_sha256(authorization: Mapping[str, Any]) -> str:
    _expect_sha256(authorization.get("authorization_sha256"), "invalid_authorization_sha256", allow_zero=False)
    return sha256_bytes(
        canonical_json(
            {
                "authorization_sha256": authorization["authorization_sha256"],
                "phase_id": PHASE_ID,
                "schema_version": APPROVAL_BINDING_SCHEMA_VERSION,
                "stage": "DIAGNOSTIC",
            }
        )
    )


def expected_approval_text(binding_sha256: str) -> str:
    digest = _expect_sha256(binding_sha256, "invalid_approval_binding_sha256", allow_zero=False)
    return f"APPROVE PHASE11C DIAGNOSTIC {digest}"


def validate_approval_text(approval_text: Any, binding_sha256: str) -> None:
    if not isinstance(approval_text, str) or approval_text != expected_approval_text(binding_sha256):
        _fail("diagnostic_approval_text_mismatch")


@dataclass(frozen=True)
class CredentialMetadata:
    regular_file: bool
    owner_uid: int
    mode: int
    link_count: int
    size_bytes: int
    device: int
    inode: int


def validate_credential_metadata(metadata: CredentialMetadata) -> None:
    if not isinstance(metadata, CredentialMetadata):
        _fail("credential_metadata_invalid")
    for value in (
        metadata.owner_uid,
        metadata.mode,
        metadata.link_count,
        metadata.size_bytes,
        metadata.device,
        metadata.inode,
    ):
        _expect_nonnegative_int(value, "credential_metadata_invalid")
    if metadata.regular_file is not True:
        _fail("credential_not_regular_file")
    if metadata.owner_uid != 0 or metadata.mode != 0o600:
        _fail("credential_permissions_denied")
    if metadata.link_count != 1:
        _fail("credential_link_count_denied")
    if not 1 <= metadata.size_bytes <= MAX_CREDENTIAL_BYTES:
        _fail("credential_size_invalid")


def _metadata_from_stat(value: os.stat_result) -> CredentialMetadata:
    return CredentialMetadata(
        regular_file=stat.S_ISREG(value.st_mode),
        owner_uid=value.st_uid,
        mode=stat.S_IMODE(value.st_mode),
        link_count=value.st_nlink,
        size_bytes=value.st_size,
        device=value.st_dev,
        inode=value.st_ino,
    )


def _require_linux(code: str) -> None:
    if (
        not sys.platform.startswith("linux")
        or fcntl is None
        or not isinstance(_O_CLOEXEC, int)
        or _O_CLOEXEC <= 0
        or not isinstance(_O_NOFOLLOW, int)
        or _O_NOFOLLOW <= 0
    ):
        _fail(code)


def _assert_absolute_no_symlinks(path: Path, code: str) -> None:
    if not path.is_absolute():
        _fail(code)
    current = Path(path.anchor)
    try:
        for part in path.parts[1:]:
            current = current / part
            if stat.S_ISLNK(os.lstat(current).st_mode):
                _fail(code)
    except GateBOneShotError:
        raise
    except OSError as exc:
        raise GateBOneShotError(code) from exc


class CredentialReader(Protocol):
    def read(self, on_opened: Callable[[], None]) -> str: ...


class FixedCredentialReader:
    """Read only the frozen ECS credential path with metadata and TOCTOU checks."""

    def read(self, on_opened: Callable[[], None]) -> str:
        _require_linux("credential_platform_unsupported")
        if CREDENTIAL_PATH != Path("/run/crag-gateb/glm_api_key"):
            _fail("credential_path_drift")
        _assert_absolute_no_symlinks(CREDENTIAL_PATH, "credential_symlink_or_path_denied")
        flags = os.O_RDONLY | _O_CLOEXEC | _O_NOFOLLOW
        try:
            descriptor = os.open(CREDENTIAL_PATH, flags)
        except OSError as exc:
            raise GateBOneShotError("credential_open_failed") from exc
        try:
            on_opened()
            before = _metadata_from_stat(os.fstat(descriptor))
            validate_credential_metadata(before)
            raw = bytearray()
            while len(raw) <= MAX_CREDENTIAL_BYTES:
                chunk = os.read(descriptor, min(512, MAX_CREDENTIAL_BYTES + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
            if len(raw) > MAX_CREDENTIAL_BYTES:
                _fail("credential_size_invalid")
            after = _metadata_from_stat(os.fstat(descriptor))
            validate_credential_metadata(after)
            _assert_absolute_no_symlinks(CREDENTIAL_PATH, "credential_symlink_or_path_denied")
            path_after = _metadata_from_stat(os.lstat(CREDENTIAL_PATH))
            validate_credential_metadata(path_after)
            identity_before = (before.device, before.inode, before.size_bytes)
            identity_after = (after.device, after.inode, after.size_bytes)
            identity_path = (path_after.device, path_after.inode, path_after.size_bytes)
            if identity_before != identity_after or identity_after != identity_path:
                _fail("credential_identity_changed")
            if sha256_bytes(bytes(raw)) != CREDENTIAL_FINGERPRINT_SHA256:
                _fail("credential_fingerprint_mismatch")
            try:
                decoded = bytes(raw).decode("ascii")
            except UnicodeDecodeError as exc:
                raise GateBOneShotError("credential_encoding_invalid") from exc
            key = decoded.strip("\r\n")
            if not key or key != key.strip() or any(character.isspace() for character in key):
                _fail("credential_format_invalid")
            return key
        finally:
            os.close(descriptor)


@dataclass(frozen=True)
class HttpResult:
    status_code: int
    body: bytes


class ProviderTransport(Protocol):
    def dispatch(self, api_key: str) -> HttpResult: ...


class FixedHTTPSProviderTransport:
    """One direct system-TLS request; it has no proxy, redirect, or retry path."""

    def dispatch(self, api_key: str) -> HttpResult:
        if not isinstance(api_key, str) or not api_key:
            _fail("credential_format_invalid")
        context = ssl.create_default_context()
        connection = http.client.HTTPSConnection(
            ENDPOINT_HOST,
            ENDPOINT_PORT,
            timeout=HTTP_TIMEOUT_SECONDS,
            context=context,
        )
        try:
            connection.request(
                "POST",
                ENDPOINT_PATH,
                body=REQUEST_BODY,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            body = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
            if len(body) > MAX_PROVIDER_RESPONSE_BYTES:
                _fail("provider_response_too_large")
            return HttpResult(status_code=response.status, body=body)
        except GateBOneShotError:
            raise
        except (OSError, TimeoutError, ssl.SSLError, http.client.HTTPException) as exc:
            raise GateBOneShotError("provider_transport_failure") from exc
        finally:
            connection.close()


@dataclass(frozen=True)
class ParsedProviderResponse:
    assistant_content_sha256: str
    terminal_match: bool
    usage_known: bool
    input_tokens_used: int
    output_tokens_used: int


def parse_provider_response(body: bytes) -> ParsedProviderResponse:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                _fail("provider_response_duplicate_key")
            result[key] = item
        return result

    def reject_constant(_: str) -> None:
        _fail("provider_response_invalid_json")

    try:
        payload = json.loads(
            body,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_constant,
        )
    except GateBOneShotError:
        raise
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise GateBOneShotError("provider_response_invalid_json") from exc
    if not isinstance(payload, dict):
        _fail("provider_response_schema_invalid")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        _fail("provider_response_schema_invalid")
    message = choices[0].get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        _fail("provider_response_schema_invalid")
    content = message["content"]
    content_digest = sha256_bytes(content.encode("utf-8"))
    terminal_match = content.strip() == TERMINAL_TOKEN

    usage = payload.get("usage")
    if usage is None:
        return ParsedProviderResponse(content_digest, terminal_match, False, 0, 0)
    if not isinstance(usage, dict):
        _fail("provider_usage_schema_invalid")
    input_tokens = usage.get("prompt_tokens")
    output_tokens = usage.get("completion_tokens")
    input_count = _expect_nonnegative_int(input_tokens, "provider_usage_schema_invalid")
    output_count = _expect_nonnegative_int(output_tokens, "provider_usage_schema_invalid")
    if input_count > MAX_PROVIDER_USAGE_COUNTER or output_count > MAX_PROVIDER_USAGE_COUNTER:
        _fail("provider_usage_schema_invalid")
    return ParsedProviderResponse(content_digest, terminal_match, True, input_count, output_count)


STATE_FIELDS = frozenset(
    {
        "schema_version",
        "phase_id",
        "state_sha256",
        "authorization_sha256",
        "approval_binding_sha256",
        "execution_status",
        "terminal_category",
        "approval_consumed",
        "budget_reserved",
        "credential_file_opened",
        "credential_validated",
        "http_attempt_recorded",
        "logical_call_count",
        "provider_call_count",
        "http_attempt_count",
        "reserved_input_tokens",
        "reserved_output_tokens",
        "reserved_microcny",
        "usage_known",
        "input_tokens_used",
        "output_tokens_used",
        "estimated_microcny",
        "http_status_class",
        "provider_response_sha256",
        "assistant_content_sha256",
        "terminal_match",
        "raw_retained",
    }
)

STATE_ORDER = {
    "approval_consumed": 0,
    "budget_reserved": 1,
    "credential_opened": 2,
    "credential_validated": 3,
    "http_attempted": 4,
    "terminal": 5,
}

TERMINAL_CATEGORIES = frozenset(
    {
        "none",
        "provider_terminal_match",
        "provider_terminal_mismatch",
        "credential_validation_failed",
        "provider_transport_failure",
        "provider_response_too_large",
        "redirect_refused",
        "http_status_failure",
        "provider_response_invalid_json",
        "provider_response_schema_invalid",
        "provider_usage_schema_invalid",
        "provider_usage_cap_exceeded",
        "internal_failure",
    }
)
HTTP_STATUS_CLASSES = frozenset({"none", "2xx", "3xx", "4xx", "5xx", "other"})


def _new_state(authorization_sha: str, binding_sha: str) -> dict[str, Any]:
    return _seal(
        {
            "schema_version": STATE_SCHEMA_VERSION,
            "phase_id": PHASE_ID,
            "state_sha256": "",
            "authorization_sha256": authorization_sha,
            "approval_binding_sha256": binding_sha,
            "execution_status": "approval_consumed",
            "terminal_category": "none",
            "approval_consumed": True,
            "budget_reserved": False,
            "credential_file_opened": False,
            "credential_validated": False,
            "http_attempt_recorded": False,
            "logical_call_count": 1,
            "provider_call_count": 0,
            "http_attempt_count": 0,
            "reserved_input_tokens": 0,
            "reserved_output_tokens": 0,
            "reserved_microcny": 0,
            "usage_known": False,
            "input_tokens_used": 0,
            "output_tokens_used": 0,
            "estimated_microcny": 0,
            "http_status_class": "none",
            "provider_response_sha256": ZERO_SHA256,
            "assistant_content_sha256": ZERO_SHA256,
            "terminal_match": False,
            "raw_retained": False,
        },
        "state_sha256",
    )


def validate_state(value: Any) -> dict[str, Any]:
    state_value = _expect_mapping(value, "invalid_state")
    _expect_exact_keys(state_value, STATE_FIELDS, "invalid_state_keys")
    if state_value["schema_version"] != STATE_SCHEMA_VERSION or state_value["phase_id"] != PHASE_ID:
        _fail("state_identity_mismatch")
    status = state_value["execution_status"]
    if status not in STATE_ORDER:
        _fail("state_status_invalid")
    if state_value["terminal_category"] not in TERMINAL_CATEGORIES:
        _fail("state_terminal_category_invalid")
    if state_value["http_status_class"] not in HTTP_STATUS_CLASSES:
        _fail("state_http_status_class_invalid")
    for field in (
        "authorization_sha256",
        "approval_binding_sha256",
        "provider_response_sha256",
        "assistant_content_sha256",
    ):
        _expect_sha256(state_value[field], f"invalid_state_{field}")
    for field in (
        "approval_consumed",
        "budget_reserved",
        "credential_file_opened",
        "credential_validated",
        "http_attempt_recorded",
        "usage_known",
        "terminal_match",
        "raw_retained",
    ):
        _expect_bool(state_value[field], f"invalid_state_{field}")
    for field in (
        "logical_call_count",
        "provider_call_count",
        "http_attempt_count",
        "reserved_input_tokens",
        "reserved_output_tokens",
        "reserved_microcny",
        "input_tokens_used",
        "output_tokens_used",
        "estimated_microcny",
    ):
        _expect_nonnegative_int(state_value[field], f"invalid_state_{field}")
    if state_value["approval_consumed"] is not True or state_value["logical_call_count"] != 1:
        _fail("state_approval_invariant_failed")
    if state_value["raw_retained"] is not False:
        _fail("state_raw_retention_forbidden")
    if status == "approval_consumed" and state_value["budget_reserved"] is not False:
        _fail("state_status_flag_mismatch")
    if status in {
        "budget_reserved",
        "credential_opened",
        "credential_validated",
        "http_attempted",
        "terminal",
    } and state_value["budget_reserved"] is not True:
        _fail("state_status_flag_mismatch")
    if status == "budget_reserved" and state_value["credential_file_opened"] is not False:
        _fail("state_status_flag_mismatch")
    if status == "credential_opened" and (
        state_value["credential_file_opened"] is not True
        or state_value["credential_validated"] is not False
    ):
        _fail("state_status_flag_mismatch")
    if status == "credential_validated" and (
        state_value["credential_validated"] is not True
        or state_value["http_attempt_recorded"] is not False
    ):
        _fail("state_status_flag_mismatch")
    if status == "http_attempted" and state_value["http_attempt_recorded"] is not True:
        _fail("state_status_flag_mismatch")
    if status == "terminal":
        if state_value["terminal_category"] == "none":
            _fail("state_terminal_category_missing")
    elif state_value["terminal_category"] != "none":
        _fail("state_premature_terminal_category")
    if state_value["budget_reserved"]:
        if (
            state_value["reserved_input_tokens"] != MAX_INPUT_TOKENS
            or state_value["reserved_output_tokens"] != MAX_OUTPUT_TOKENS
            or state_value["reserved_microcny"] != DIAGNOSTIC_BUDGET_MICROCNY
        ):
            _fail("state_budget_invariant_failed")
    elif any(
        state_value[field] != 0
        for field in ("reserved_input_tokens", "reserved_output_tokens", "reserved_microcny")
    ):
        _fail("state_unreserved_budget_nonzero")
    if state_value["credential_validated"] and not state_value["credential_file_opened"]:
        _fail("state_credential_order_invalid")
    if state_value["http_attempt_recorded"]:
        if not state_value["credential_validated"]:
            _fail("state_http_order_invalid")
        if state_value["http_attempt_count"] != 1 or state_value["provider_call_count"] != 1:
            _fail("state_http_count_invalid")
    elif state_value["http_attempt_count"] != 0 or state_value["provider_call_count"] != 0:
        _fail("state_unattempted_count_nonzero")
    if state_value["usage_known"] is False and (
        state_value["input_tokens_used"] != 0 or state_value["output_tokens_used"] != 0
    ):
        _fail("state_unknown_usage_nonzero")
    _validate_seal(state_value, "state_sha256", "state_sha256_mismatch")
    return state_value


def _transition_state(current: Mapping[str, Any], **changes: Any) -> dict[str, Any]:
    previous = validate_state(current)
    unknown = set(changes) - STATE_FIELDS
    if unknown or "state_sha256" in changes:
        _fail("state_transition_keys_invalid")
    updated = dict(previous)
    updated.update(changes)
    if STATE_ORDER.get(updated["execution_status"], -1) < STATE_ORDER[previous["execution_status"]]:
        _fail("state_transition_rollback")
    for field in (
        "approval_consumed",
        "budget_reserved",
        "credential_file_opened",
        "credential_validated",
        "http_attempt_recorded",
    ):
        if previous[field] is True and updated[field] is not True:
            _fail("state_transition_rollback")
    updated["state_sha256"] = ""
    return validate_state(_seal(updated, "state_sha256"))


class StateStore(Protocol):
    @property
    def state(self) -> dict[str, Any] | None: ...

    def begin(self, authorization_sha: str, binding_sha: str) -> None: ...

    def transition(self, **changes: Any) -> None: ...

    def write_receipt(self, receipt: Mapping[str, Any]) -> None: ...


class InMemoryStateStore:
    """Offline fake with the same monotonic semantics as the Linux durable store."""

    def __init__(self) -> None:
        self._state: dict[str, Any] | None = None
        self.receipt: dict[str, Any] | None = None
        self.events: list[str] = []

    @property
    def state(self) -> dict[str, Any] | None:
        return deepcopy(self._state)

    def begin(self, authorization_sha: str, binding_sha: str) -> None:
        if self._state is not None:
            _fail("one_shot_already_consumed")
        self._state = _new_state(authorization_sha, binding_sha)
        self.events.append("approval_consumed")

    def transition(self, **changes: Any) -> None:
        if self._state is None:
            _fail("state_not_started")
        self._state = _transition_state(self._state, **changes)
        self.events.append(self._state["execution_status"])

    def write_receipt(self, receipt: Mapping[str, Any]) -> None:
        if self.receipt is not None:
            _fail("receipt_already_written")
        self.receipt = validate_receipt(receipt)
        self.events.append("receipt_written")


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            _fail("state_persistence_failure")
        offset += written


def _path_entry_exists(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise GateBOneShotError("state_persistence_failure") from exc
    return True


class FileStateStore:
    """Linux file-lock and fsync-backed state store at the single fixed directory."""

    def __init__(self) -> None:
        self._state: dict[str, Any] | None = None
        self._lock_descriptor: int | None = None

    @property
    def state(self) -> dict[str, Any] | None:
        return deepcopy(self._state)

    def __enter__(self) -> FileStateStore:
        _require_linux("state_store_platform_unsupported")
        try:
            STATE_DIRECTORY.mkdir(mode=0o700, parents=True, exist_ok=True)
            _assert_absolute_no_symlinks(STATE_DIRECTORY, "state_directory_symlink_denied")
            directory_stat = os.lstat(STATE_DIRECTORY)
            if not stat.S_ISDIR(directory_stat.st_mode) or directory_stat.st_uid != 0:
                _fail("state_directory_metadata_denied")
            os.chmod(STATE_DIRECTORY, 0o700)
            flags = os.O_RDWR | os.O_CREAT | _O_CLOEXEC | _O_NOFOLLOW
            self._lock_descriptor = os.open(LOCK_PATH, flags, 0o600)
            if _FCHMOD is None:
                _fail("state_store_platform_unsupported")
            _FCHMOD(self._lock_descriptor, 0o600)
            fcntl.flock(self._lock_descriptor, fcntl.LOCK_EX)
            lock_stat = os.fstat(self._lock_descriptor)
            if lock_stat.st_uid != 0 or stat.S_IMODE(lock_stat.st_mode) != 0o600:
                _fail("state_lock_metadata_denied")
            if _path_entry_exists(STATE_PATH) or _path_entry_exists(RECEIPT_PATH):
                _fail("one_shot_already_consumed")
            return self
        except GateBOneShotError:
            self._close_lock()
            raise
        except OSError as exc:
            self._close_lock()
            raise GateBOneShotError("state_persistence_failure") from exc

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self._close_lock()

    def _close_lock(self) -> None:
        if self._lock_descriptor is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(self._lock_descriptor, fcntl.LOCK_UN)
            finally:
                os.close(self._lock_descriptor)
                self._lock_descriptor = None

    def _atomic_write(self, destination: Path, value: Mapping[str, Any]) -> None:
        if self._lock_descriptor is None:
            _fail("state_lock_not_held")
        payload = canonical_json(value) + b"\n"
        temporary = STATE_DIRECTORY / f".{destination.name}.{os.getpid()}.tmp"
        descriptor: int | None = None
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_CLOEXEC | _O_NOFOLLOW
            descriptor = os.open(temporary, flags, 0o600)
            _write_all(descriptor, payload)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary, destination)
            directory_descriptor = os.open(STATE_DIRECTORY, os.O_RDONLY | _O_CLOEXEC)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except GateBOneShotError:
            raise
        except OSError as exc:
            raise GateBOneShotError("state_persistence_failure") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                if temporary.exists():
                    temporary.unlink()
            except OSError:
                pass

    def begin(self, authorization_sha: str, binding_sha: str) -> None:
        if self._state is not None or _path_entry_exists(STATE_PATH) or _path_entry_exists(RECEIPT_PATH):
            _fail("one_shot_already_consumed")
        state_value = _new_state(authorization_sha, binding_sha)
        self._atomic_write(STATE_PATH, state_value)
        self._state = state_value

    def transition(self, **changes: Any) -> None:
        if self._state is None:
            _fail("state_not_started")
        updated = _transition_state(self._state, **changes)
        self._atomic_write(STATE_PATH, updated)
        self._state = updated

    def write_receipt(self, receipt: Mapping[str, Any]) -> None:
        validated = validate_receipt(receipt)
        if _path_entry_exists(RECEIPT_PATH):
            _fail("receipt_already_written")
        self._atomic_write(RECEIPT_PATH, validated)


RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "phase_id",
        "stage",
        "receipt_sha256",
        "authorization_sha256",
        "approval_binding_sha256",
        "execution_status",
        "terminal_category",
        "logical_call_count",
        "provider_call_count",
        "http_attempt_count",
        "reserved_input_tokens",
        "reserved_output_tokens",
        "reserved_microcny",
        "credential_file_opened",
        "credential_validated",
        "usage_known",
        "input_tokens_used",
        "output_tokens_used",
        "estimated_microcny",
        "http_status_class",
        "provider_response_sha256",
        "assistant_content_sha256",
        "terminal_match",
        "raw_retained",
        "retry_count",
    }
)

EXECUTION_STATUSES = frozenset({"completed", "inconclusive", "failed"})


def build_receipt(state_value: Mapping[str, Any], execution_status: str) -> dict[str, Any]:
    state_data = validate_state(state_value)
    if state_data["execution_status"] != "terminal":
        _fail("receipt_before_terminal_state")
    if execution_status not in EXECUTION_STATUSES:
        _fail("receipt_execution_status_invalid")
    return _seal(
        {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "phase_id": PHASE_ID,
            "stage": "DIAGNOSTIC",
            "receipt_sha256": "",
            "authorization_sha256": state_data["authorization_sha256"],
            "approval_binding_sha256": state_data["approval_binding_sha256"],
            "execution_status": execution_status,
            "terminal_category": state_data["terminal_category"],
            "logical_call_count": state_data["logical_call_count"],
            "provider_call_count": state_data["provider_call_count"],
            "http_attempt_count": state_data["http_attempt_count"],
            "reserved_input_tokens": state_data["reserved_input_tokens"],
            "reserved_output_tokens": state_data["reserved_output_tokens"],
            "reserved_microcny": state_data["reserved_microcny"],
            "credential_file_opened": state_data["credential_file_opened"],
            "credential_validated": state_data["credential_validated"],
            "usage_known": state_data["usage_known"],
            "input_tokens_used": state_data["input_tokens_used"],
            "output_tokens_used": state_data["output_tokens_used"],
            "estimated_microcny": state_data["estimated_microcny"],
            "http_status_class": state_data["http_status_class"],
            "provider_response_sha256": state_data["provider_response_sha256"],
            "assistant_content_sha256": state_data["assistant_content_sha256"],
            "terminal_match": state_data["terminal_match"],
            "raw_retained": False,
            "retry_count": 0,
        },
        "receipt_sha256",
    )


def validate_receipt(value: Any) -> dict[str, Any]:
    receipt = _expect_mapping(value, "invalid_receipt")
    _expect_exact_keys(receipt, RECEIPT_FIELDS, "invalid_receipt_keys")
    if (
        receipt["schema_version"] != RECEIPT_SCHEMA_VERSION
        or receipt["phase_id"] != PHASE_ID
        or receipt["stage"] != "DIAGNOSTIC"
    ):
        _fail("receipt_identity_mismatch")
    if receipt["execution_status"] not in EXECUTION_STATUSES:
        _fail("receipt_execution_status_invalid")
    if receipt["terminal_category"] not in TERMINAL_CATEGORIES - {"none"}:
        _fail("receipt_terminal_category_invalid")
    if receipt["http_status_class"] not in HTTP_STATUS_CLASSES:
        _fail("receipt_http_status_class_invalid")
    for field in (
        "receipt_sha256",
        "authorization_sha256",
        "approval_binding_sha256",
        "provider_response_sha256",
        "assistant_content_sha256",
    ):
        _expect_sha256(receipt[field], f"invalid_receipt_{field}")
    for field in (
        "logical_call_count",
        "provider_call_count",
        "http_attempt_count",
        "reserved_input_tokens",
        "reserved_output_tokens",
        "reserved_microcny",
        "input_tokens_used",
        "output_tokens_used",
        "estimated_microcny",
        "retry_count",
    ):
        _expect_nonnegative_int(receipt[field], f"invalid_receipt_{field}")
    for field in (
        "credential_file_opened",
        "credential_validated",
        "usage_known",
        "terminal_match",
        "raw_retained",
    ):
        _expect_bool(receipt[field], f"invalid_receipt_{field}")
    if receipt["logical_call_count"] != 1 or receipt["retry_count"] != 0:
        _fail("receipt_call_count_invalid")
    if (
        receipt["provider_call_count"] not in {0, 1}
        or receipt["http_attempt_count"] not in {0, 1}
        or receipt["provider_call_count"] != receipt["http_attempt_count"]
    ):
        _fail("receipt_attempt_count_invalid")
    if (
        receipt["reserved_input_tokens"] != MAX_INPUT_TOKENS
        or receipt["reserved_output_tokens"] != MAX_OUTPUT_TOKENS
        or receipt["reserved_microcny"] != DIAGNOSTIC_BUDGET_MICROCNY
    ):
        _fail("receipt_reservation_invalid")
    if receipt["credential_validated"] and not receipt["credential_file_opened"]:
        _fail("receipt_credential_order_invalid")
    if receipt["http_attempt_count"] == 1 and not receipt["credential_validated"]:
        _fail("receipt_http_order_invalid")
    if receipt["usage_known"] is False and (
        receipt["input_tokens_used"] != 0 or receipt["output_tokens_used"] != 0
    ):
        _fail("receipt_unknown_usage_nonzero")
    if receipt["raw_retained"] is not False:
        _fail("receipt_raw_retention_forbidden")
    if receipt["execution_status"] == "completed":
        if (
            receipt["terminal_category"] != "provider_terminal_match"
            or receipt["terminal_match"] is not True
            or receipt["http_status_class"] != "2xx"
            or receipt["http_attempt_count"] != 1
        ):
            _fail("receipt_completed_invariant_failed")
    if receipt["execution_status"] == "inconclusive" and (
        receipt["terminal_category"] != "provider_terminal_mismatch"
        or receipt["terminal_match"] is not False
    ):
        _fail("receipt_inconclusive_invariant_failed")
    _validate_seal(receipt, "receipt_sha256", "receipt_sha256_mismatch")
    return receipt


def _status_class(status_code: int) -> str:
    if isinstance(status_code, bool) or not isinstance(status_code, int) or status_code < 100:
        return "other"
    if 200 <= status_code <= 299:
        return "2xx"
    if 300 <= status_code <= 399:
        return "3xx"
    if 400 <= status_code <= 499:
        return "4xx"
    if 500 <= status_code <= 599:
        return "5xx"
    return "other"


def _terminal_receipt(
    store: StateStore,
    *,
    category: str,
    execution_status: str,
    **changes: Any,
) -> dict[str, Any]:
    if category not in TERMINAL_CATEGORIES - {"none"}:
        category = "internal_failure"
        execution_status = "failed"
    store.transition(execution_status="terminal", terminal_category=category, **changes)
    state_value = store.state
    if state_value is None:
        _fail("state_not_started")
    receipt = validate_receipt(build_receipt(state_value, execution_status))
    store.write_receipt(receipt)
    return receipt


def _failure_category(code: str) -> str:
    if code.startswith("credential_"):
        return "credential_validation_failed"
    if code == "provider_transport_failure":
        return code
    if code == "provider_response_too_large":
        return code
    if code in {
        "provider_response_invalid_json",
        "provider_response_duplicate_key",
    }:
        return "provider_response_invalid_json"
    if code == "provider_usage_schema_invalid":
        return code
    if code == "provider_response_schema_invalid":
        return code
    return "internal_failure"


def execute_one_shot(
    authorization: Mapping[str, Any],
    approval_text: str,
    *,
    store: StateStore,
    credential_reader: CredentialReader,
    transport: ProviderTransport,
    now_utc: datetime | None = None,
    executable_source_digest: str | None = None,
) -> dict[str, Any]:
    """Execute exactly once after validation, with every irreversible step persisted."""

    validated = validate_authorization(
        authorization,
        executable_source_digest=executable_source_digest or source_sha256(),
        now_utc=now_utc or datetime.now(timezone.utc),
        require_active_window=True,
    )
    binding_sha = approval_binding_sha256(validated)
    validate_approval_text(approval_text, binding_sha)
    store.begin(validated["authorization_sha256"], binding_sha)
    store.transition(
        execution_status="budget_reserved",
        budget_reserved=True,
        reserved_input_tokens=MAX_INPUT_TOKENS,
        reserved_output_tokens=MAX_OUTPUT_TOKENS,
        reserved_microcny=DIAGNOSTIC_BUDGET_MICROCNY,
    )

    def record_credential_opened() -> None:
        current = store.state
        if current is None or current["credential_file_opened"] is True:
            _fail("credential_open_callback_invalid")
        store.transition(execution_status="credential_opened", credential_file_opened=True)

    try:
        api_key = credential_reader.read(record_credential_opened)
        if (
            not isinstance(api_key, str)
            or not api_key
            or api_key != api_key.strip()
            or any(character.isspace() for character in api_key)
        ):
            _fail("credential_format_invalid")
        store.transition(execution_status="credential_validated", credential_validated=True)
    except GateBOneShotError as exc:
        return _terminal_receipt(
            store,
            category=_failure_category(str(exc)),
            execution_status="failed",
        )
    except Exception:
        return _terminal_receipt(store, category="internal_failure", execution_status="failed")

    store.transition(
        execution_status="http_attempted",
        http_attempt_recorded=True,
        http_attempt_count=1,
        provider_call_count=1,
    )
    try:
        result = transport.dispatch(api_key)
    except GateBOneShotError as exc:
        return _terminal_receipt(
            store,
            category=_failure_category(str(exc)),
            execution_status="failed",
        )
    except Exception:
        return _terminal_receipt(store, category="internal_failure", execution_status="failed")

    if (
        not isinstance(result, HttpResult)
        or isinstance(result.status_code, bool)
        or not isinstance(result.status_code, int)
        or not isinstance(result.body, bytes)
    ):
        return _terminal_receipt(store, category="internal_failure", execution_status="failed")
    if len(result.body) > MAX_PROVIDER_RESPONSE_BYTES:
        return _terminal_receipt(
            store,
            category="provider_response_too_large",
            execution_status="failed",
        )
    status_class = _status_class(result.status_code)
    response_digest = sha256_bytes(result.body)
    if status_class == "3xx":
        return _terminal_receipt(
            store,
            category="redirect_refused",
            execution_status="failed",
            http_status_class=status_class,
            provider_response_sha256=response_digest,
        )
    if status_class != "2xx":
        return _terminal_receipt(
            store,
            category="http_status_failure",
            execution_status="failed",
            http_status_class=status_class,
            provider_response_sha256=response_digest,
        )
    try:
        parsed = parse_provider_response(result.body)
    except GateBOneShotError as exc:
        return _terminal_receipt(
            store,
            category=_failure_category(str(exc)),
            execution_status="failed",
            http_status_class=status_class,
            provider_response_sha256=response_digest,
        )
    except Exception:
        return _terminal_receipt(
            store,
            category="internal_failure",
            execution_status="failed",
            http_status_class=status_class,
            provider_response_sha256=response_digest,
        )

    estimated = DIAGNOSTIC_BUDGET_MICROCNY
    if parsed.usage_known:
        estimated = worst_case_microcny(
            input_tokens=parsed.input_tokens_used,
            output_tokens=parsed.output_tokens_used,
            input_rate=INPUT_RATE_MICROCNY_PER_MILLION,
            output_rate=OUTPUT_RATE_MICROCNY_PER_MILLION,
        )
    usage_changes = {
        "http_status_class": status_class,
        "provider_response_sha256": response_digest,
        "assistant_content_sha256": parsed.assistant_content_sha256,
        "usage_known": parsed.usage_known,
        "input_tokens_used": parsed.input_tokens_used,
        "output_tokens_used": parsed.output_tokens_used,
        "estimated_microcny": estimated,
        "terminal_match": parsed.terminal_match,
    }
    if parsed.usage_known and (
        parsed.input_tokens_used > MAX_INPUT_TOKENS or parsed.output_tokens_used > MAX_OUTPUT_TOKENS
    ):
        return _terminal_receipt(
            store,
            category="provider_usage_cap_exceeded",
            execution_status="failed",
            **usage_changes,
        )
    if parsed.terminal_match:
        return _terminal_receipt(
            store,
            category="provider_terminal_match",
            execution_status="completed",
            **usage_changes,
        )
    return _terminal_receipt(
        store,
        category="provider_terminal_mismatch",
        execution_status="inconclusive",
        **usage_changes,
    )


def _read_fixed_control_file(path: Path, *, maximum_bytes: int, exact_mode: int) -> bytes:
    _require_linux("control_file_platform_unsupported")
    _assert_absolute_no_symlinks(path, "control_file_symlink_or_path_denied")
    try:
        descriptor = os.open(path, os.O_RDONLY | _O_CLOEXEC | _O_NOFOLLOW)
    except OSError as exc:
        raise GateBOneShotError("control_file_open_failed") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or stat.S_IMODE(metadata.st_mode) != exact_mode
            or metadata.st_nlink != 1
            or not 1 <= metadata.st_size <= maximum_bytes
        ):
            _fail("control_file_metadata_denied")
        payload = bytearray()
        while len(payload) <= maximum_bytes:
            chunk = os.read(descriptor, min(4096, maximum_bytes + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > maximum_bytes:
            _fail("control_file_size_invalid")
        after = os.fstat(descriptor)
        _assert_absolute_no_symlinks(path, "control_file_symlink_or_path_denied")
        path_after = os.lstat(path)
        if (metadata.st_dev, metadata.st_ino, metadata.st_size) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
        ) or (after.st_dev, after.st_ino, after.st_size) != (
            path_after.st_dev,
            path_after.st_ino,
            path_after.st_size,
        ):
            _fail("control_file_identity_changed")
        return bytes(payload)
    finally:
        os.close(descriptor)


def read_fixed_authorization(*, require_active_window: bool) -> dict[str, Any]:
    payload = _read_fixed_control_file(AUTHORIZATION_PATH, maximum_bytes=MAX_CONTROL_FILE_BYTES, exact_mode=0o400)
    return validate_authorization(
        strict_json_loads(payload),
        require_active_window=require_active_window,
    )


def run_from_fixed_files() -> dict[str, Any]:
    authorization = read_fixed_authorization(require_active_window=True)
    approval_bytes = _read_fixed_control_file(APPROVAL_PATH, maximum_bytes=256, exact_mode=0o400)
    try:
        approval_text = approval_bytes.decode("ascii")
    except UnicodeDecodeError as exc:
        raise GateBOneShotError("diagnostic_approval_encoding_invalid") from exc
    with FileStateStore() as store:
        return execute_one_shot(
            authorization,
            approval_text,
            store=store,
            credential_reader=FixedCredentialReader(),
            transport=FixedHTTPSProviderTransport(),
        )


def _read_stdin_bounded() -> bytes:
    payload = sys.stdin.buffer.read(MAX_CONTROL_FILE_BYTES + 1)
    if len(payload) > MAX_CONTROL_FILE_BYTES:
        _fail("stdin_document_too_large")
    return payload


def _print_safe_json(value: Mapping[str, Any]) -> None:
    print(canonical_json(value).decode("ascii"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("print-template")
    commands.add_parser("seal-authorization")
    commands.add_parser("print-approval-binding")
    commands.add_parser("run")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "print-template":
            _print_safe_json(build_authorization_template())
            return 0
        if args.command == "seal-authorization":
            _print_safe_json(seal_authorization(strict_json_loads(_read_stdin_bounded())))
            return 0
        if args.command == "print-approval-binding":
            authorization = read_fixed_authorization(require_active_window=False)
            binding = approval_binding_sha256(authorization)
            _print_safe_json(
                {
                    "approval_binding_sha256": binding,
                    "authorization_sha256": authorization["authorization_sha256"],
                    "expected_approval_text": expected_approval_text(binding),
                }
            )
            return 0
        receipt = run_from_fixed_files()
        _print_safe_json(receipt)
        return 0 if receipt["execution_status"] == "completed" else 3
    except GateBOneShotError as exc:
        _print_safe_json({"error_code": str(exc)})
        return 2
    except Exception:
        _print_safe_json({"error_code": "internal_failure"})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
