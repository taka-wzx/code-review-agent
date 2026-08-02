"""Offline Phase 11C Gate B freeze generator.

This program deliberately has no HTTP client or provider SDK.  It turns local,
already-created evidence into sealed, redacted DIAGNOSTIC or HEADLINE_COHORT
authorization material.  It never prints credential bytes, rendered Compose,
runtime inventory input, or provider evidence content.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import sys
import tarfile
from typing import Any, Mapping, NoReturn, Sequence

import phase11c_gateb_headline_cohort_executor as executor


FREEZE_SCHEMA_VERSION = "phase11c-gateb-execution-freeze/v1"
PREFLIGHT_SCHEMA_VERSION = "phase11c-gateb-preflight/v1"
RUNTIME_SCHEMA_VERSION = "phase11c-gateb-runtime-config/v1"
TARIFF_SCHEMA_VERSION = "phase11c-gateb-tariff-manifest/v1"
RUNTIME_EVIDENCE_SCHEMA_VERSION = "phase11c-gateb-runtime-evidence/v1"

BASELINE_MASTER_SHA = "4af4b2756e8d2de6764d08e17a6e12040e24975e"
BASELINE_CI_RUN = 30_451_250_259
BASELINE_CI_ATTEMPT = 1
BASELINE_CI_CONCLUSION = "success"
PHASE11B_ACCEPTANCE_REPORT_SHA256 = "354398234ee34773f26b1811ece62a5ccc7ed9fd18472adb11e1907bec25c6f7"
PHASE11B_AUTHORIZATION_SHA256 = "73c8367ce00ce4ad77798dbd1bcbf0f3995528096b18924f2a198ba290796745"
PHASE11B_RUNTIME_CONFIG_SHA256 = "e1a3d3adadc78ab0b11e8d28b60ba05552c503edf7b91661e895d24cb5ea8bdc"
ALIYUN_INSTANCE_ID = "i-bp12vpivp8pdpr0uq7uf"
ALIYUN_REGION = "cn-hangzhou"
POLICY_URL = "https://docs.bigmodel.cn/cn/terms/service-agreement"
RETENTION_POLICY_URL = "https://docs.bigmodel.cn/cn/terms/privacy-policy"

MAX_CREDENTIAL_BYTES = 4_096
ZERO_SHA256 = "0" * 64
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_AUTHORIZATION_ID = re.compile(r"p11c-gateb-[0-9a-f]{32}\Z")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_COMPOSE_KEY = re.compile(r"[A-Za-z0-9_.-]+\Z")
_COMPOSE_INTEGER = re.compile(r"-?[0-9]+\Z")
_COMPOSE_FLOAT = re.compile(r"-?(?:[0-9]+\.[0-9]*|[0-9]*\.[0-9]+)\Z")
_COMPOSE_MEMORY = re.compile(r"([0-9]+)([kmgt](?:i?b)?|b)\Z", re.IGNORECASE)

EXECUTION_SOURCE_FILES = (
    "Dockerfile.phase11c-gateb-headline",
    "compose.phase11c-gateb-headline.yml",
    "phase11c_gateb_headline_cohort_executor.py",
    "phase11c_gateb_freeze.py",
    "schemas/phase11c-gateb-protocol-diagnostic-authorization.schema.json",
    "schemas/phase11c-gateb-protocol-diagnostic-receipt.schema.json",
    "schemas/phase11c-gateb-headline-cohort-authorization.schema.json",
    "schemas/phase11c-gateb-headline-cohort-target-receipt.schema.json",
    "schemas/phase11c-gateb-headline-cohort-receipt.schema.json",
    "schemas/phase11c-gateb-headline-cohort-ledger.schema.json",
    "schemas/phase11c-gateb-execution-freeze.schema.json",
    "schemas/phase11c-gateb-preflight.schema.json",
)

PREFLIGHT_FIELDS = frozenset(
    {
        "schema_version", "phase_id", "stage", "preflight_verdict_sha256", "execution_freeze_sha256",
        "freeze_subject_sha256", "authorization_id", "baseline", "policy_url", "retention_policy_url",
        "policy_reviewed_at_utc", "authorization_window_start_utc", "authorization_window_end_utc", "checks",
        "canary_allowed", "real_run_recommended_now", "blocking_reason_codes", "redaction_applied", "snapshot_immutability",
    }
)
EXECUTION_FREEZE_FIELDS = frozenset(
    {
        "schema_version", "phase_id", "stage", "execution_freeze_sha256", "freeze_subject_sha256", "authorization_id",
        "runtime_config_sha256", "provider_tariff_manifest_sha256", "bindings", "owners", "baseline",
        "credential_delivery_mode", "provider_policy_accepted", "owner_reconfirmed", "kill_switch_bound",
        "authorization_window_start_utc", "authorization_window_end_utc", "budget_microcny", "snapshot_immutability", "redaction_applied",
    }
)
FREEZE_BINDING_FIELDS = frozenset(
    {
        "runtime_config_sha256", "executable_commit_sha", "executable_source_sha256", "source_tree_sha256",
        "source_archive_sha256", "dockerfile_sha256", "compose_sha256", "image_sha256", "deployment_sha256",
        "runtime_identity_sha256", "cohort_manifest_sha256", "provider_policy_evidence_sha256",
        "provider_tariff_evidence_sha256", "provider_tariff_manifest_sha256", "credential_fingerprint_sha256",
    }
)
PREFLIGHT_CHECK_FIELDS = frozenset(
    {
        "baseline_verified", "offline_fixtures_verified", "redaction_verified", "endpoint_tls_verified",
        "redirect_denied", "retry_policy_verified", "budget_reservation_verified", "cohort_nonoverlap_verified",
        "credential_file_security_verified", "provider_policy_accepted", "authorization_window_valid",
        "kill_switch_bound", "publisher_fake_only", "binding_hashes_match",
    }
)


class FreezeError(ValueError):
    """Stable, content-free offline freeze rejection."""


def _fail(code: str) -> NoReturn:
    raise FreezeError(code)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _expect_sha256(value: Any, code: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        _fail(code)
    return value


def _expect_commit(value: Any, code: str) -> str:
    if not isinstance(value, str) or _COMMIT.fullmatch(value) is None:
        _fail(code)
    return value


def _expect_mapping(value: Any, code: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(code)
    return value


def _expect_exact_keys(value: Mapping[str, Any], expected: set[str], code: str) -> None:
    if set(value) != expected:
        _fail(code)


def _parse_utc(value: Any, code: str) -> datetime:
    if not isinstance(value, str):
        _fail(code)
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise FreezeError(code) from exc
    return parsed


def _seal(document: Mapping[str, Any], field: str) -> dict[str, Any]:
    sealed = deepcopy(dict(document))
    sealed[field] = ""
    sealed[field] = _sha256_bytes(executor.canonical_json(sealed))
    return sealed


def _validate_seal(document: Mapping[str, Any], field: str, code: str) -> None:
    actual = _expect_sha256(document.get(field), code)
    expected = dict(document)
    expected[field] = ""
    if actual != _sha256_bytes(executor.canonical_json(expected)):
        _fail(code)


def _regular_file(path: Path, code: str) -> os.stat_result:
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise FreezeError(code) from exc
    if not stat.S_ISREG(metadata.st_mode):
        _fail(code)
    return metadata


def sha256_file(path: Path, *, code: str) -> str:
    _regular_file(path, code)
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1 << 20), b""):
                digest.update(block)
    except OSError as exc:
        raise FreezeError(code) from exc
    return digest.hexdigest()


def normalized_utf8_sha256(path: Path, *, code: str) -> str:
    _regular_file(path, code)
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise FreezeError(code) from exc
    return normalized_utf8_bytes_sha256(content, code=code)


def normalized_utf8_bytes_sha256(content: bytes, *, code: str) -> str:
    try:
        normalized = content.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    except UnicodeDecodeError as exc:
        raise FreezeError(code) from exc
    return _sha256_bytes(normalized)


def _source_root(root: Path) -> Path:
    resolved = root.resolve()
    if (resolved / "phase11c_gateb_headline_cohort_executor.py").resolve() != Path(executor.__file__).resolve():
        _fail("source_root_executor_mismatch")
    return resolved


def source_tree_manifest(root: Path) -> dict[str, Any]:
    source_root = _source_root(root)
    files: list[dict[str, str]] = []
    for relative in EXECUTION_SOURCE_FILES:
        path = source_root / relative
        files.append({"path": relative, "sha256": normalized_utf8_sha256(path, code="source_tree_file_invalid")})
    document = {
        "schema_version": "phase11c-gateb-source-tree/v1",
        "phase_id": executor.PHASE_ID,
        "files": files,
        "source_tree_sha256": "",
    }
    return _seal(document, "source_tree_sha256")


def validate_source_archive(path: Path, *, source_root: Path) -> str:
    """Require the freeze archive to contain the exact executable source files."""

    archive_sha256 = sha256_file(path, code="source_archive_invalid")
    manifest = source_tree_manifest(source_root)
    expected_hashes = {item["path"]: item["sha256"] for item in manifest["files"]}
    observed: dict[str, str] = {}
    try:
        with tarfile.open(path, "r:*") as archive:
            for member in archive.getmembers():
                normalized = member.name.removeprefix("./")
                pieces = tuple(part for part in normalized.split("/") if part not in {"", "."})
                if normalized.startswith("/") or ".." in pieces:
                    _fail("source_archive_member_path_invalid")
                if member.isdir():
                    continue
                if not pieces:
                    _fail("source_archive_member_path_invalid")
                relative = "/".join(pieces)
                if not member.isreg() or relative in observed:
                    _fail("source_archive_member_invalid")
                if relative not in expected_hashes:
                    _fail("source_archive_member_unexpected")
                source = archive.extractfile(member)
                if source is None:
                    _fail("source_archive_member_invalid")
                with source:
                    content = source.read()
                observed[relative] = normalized_utf8_bytes_sha256(content, code="source_archive_member_encoding_invalid")
                if relative == "phase11c_gateb_headline_cohort_executor.py":
                    if observed[relative] != executor.source_sha256():
                        _fail("source_archive_executor_mismatch")
    except FreezeError:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise FreezeError("source_archive_invalid") from exc
    if observed != expected_hashes:
        _fail("source_archive_source_tree_mismatch")
    return archive_sha256


def _normalize_image_sha256(value: Any) -> str:
    if not isinstance(value, str):
        _fail("image_sha256_invalid")
    text = value.removeprefix("sha256:")
    return _expect_sha256(text, "image_sha256_invalid")


def _compose_commentless_line(value: str) -> str:
    """Remove YAML comments without allowing comment text to become data."""

    quote: str | None = None
    escaped = False
    for index, character in enumerate(value):
        if quote == '"':
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if quote == "'":
            if character == quote:
                # YAML escapes a single quote in a single-quoted scalar by
                # doubling it.  The next quote is part of the scalar.
                if index + 1 < len(value) and value[index + 1] == quote:
                    continue
                quote = None
            continue
        if character in {'"', "'"}:
            quote = character
        elif character == "#" and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip()
    if quote is not None or escaped:
        _fail("rendered_deployment_yaml_invalid")
    return value.rstrip()


def _compose_lines(rendered: bytes) -> list[tuple[int, str, int]]:
    try:
        text = rendered.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FreezeError("rendered_deployment_encoding_invalid") from exc
    if text.startswith("\ufeff"):
        _fail("rendered_deployment_yaml_invalid")
    lines: list[tuple[int, str, int]] = []
    for line_number, raw in enumerate(text.replace("\r\n", "\n").replace("\r", "\n").split("\n"), 1):
        if "\t" in raw:
            _fail("rendered_deployment_yaml_invalid")
        stripped = _compose_commentless_line(raw)
        if not stripped.strip():
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        if indent % 2 or stripped[indent:] in {"---", "..."}:
            _fail("rendered_deployment_yaml_invalid")
        lines.append((indent, stripped[indent:], line_number))
    if not lines:
        _fail("rendered_deployment_yaml_invalid")
    return lines


def _compose_mapping_token(value: str, *, strict: bool) -> tuple[str, str] | None:
    """Split a YAML mapping token, recognizing only ``key: `` syntax.

    A colon without following whitespace is a scalar colon (for example the
    ``:`` in ``sha256:...`` or ``no-new-privileges:true``), not a mapping
    delimiter.  This keeps list scalar values from being reinterpreted as
    mappings by the small dependency-free parser below.
    """

    quote: str | None = None
    escaped = False
    for index, character in enumerate(value):
        if quote == '"':
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if quote == "'":
            if character == quote:
                if index + 1 < len(value) and value[index + 1] == quote:
                    continue
                quote = None
            continue
        if character in {'"', "'"}:
            quote = character
            continue
        if character != ":" or (index + 1 < len(value) and not value[index + 1].isspace()):
            continue
        key = value[:index].strip()
        if _COMPOSE_KEY.fullmatch(key) is None:
            if strict:
                _fail("rendered_deployment_yaml_invalid")
            return None
        return key, value[index + 1 :].strip()
    if quote is not None or escaped:
        _fail("rendered_deployment_yaml_invalid")
    if strict:
        _fail("rendered_deployment_yaml_invalid")
    return None


def _compose_split_inline(value: str) -> list[str]:
    parts: list[str] = []
    start = 0
    quote: str | None = None
    escaped = False
    depth = 0
    for index, character in enumerate(value):
        if quote == '"':
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if quote == "'":
            if character == quote:
                if index + 1 < len(value) and value[index + 1] == quote:
                    continue
                quote = None
            continue
        if character in {'"', "'"}:
            quote = character
        elif character in "[{":
            depth += 1
        elif character in "]}":
            depth -= 1
            if depth < 0:
                _fail("rendered_deployment_yaml_invalid")
        elif character == "," and depth == 0:
            parts.append(value[start:index].strip())
            start = index + 1
    if quote is not None or escaped or depth != 0:
        _fail("rendered_deployment_yaml_invalid")
    parts.append(value[start:].strip())
    if any(not part for part in parts):
        _fail("rendered_deployment_yaml_invalid")
    return parts


def _compose_scalar(value: str) -> Any:
    text = value.strip()
    if not text:
        return None
    if text[0] == '"':
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError) as exc:
            raise FreezeError("rendered_deployment_yaml_invalid") from exc
        if not isinstance(parsed, str):
            _fail("rendered_deployment_yaml_invalid")
        return parsed
    if text[0] == "'":
        if len(text) < 2 or text[-1] != "'":
            _fail("rendered_deployment_yaml_invalid")
        return text[1:-1].replace("''", "'")
    if text.startswith("["):
        if not text.endswith("]"):
            _fail("rendered_deployment_yaml_invalid")
        inner = text[1:-1].strip()
        return [] if not inner else [_compose_scalar(part) for part in _compose_split_inline(inner)]
    if text in {"true", "True"}:
        return True
    if text in {"false", "False"}:
        return False
    if text in {"null", "Null", "NULL", "~"}:
        return None
    if _COMPOSE_INTEGER.fullmatch(text):
        try:
            return int(text)
        except ValueError as exc:
            raise FreezeError("rendered_deployment_yaml_invalid") from exc
    if _COMPOSE_FLOAT.fullmatch(text):
        try:
            return float(text)
        except ValueError as exc:
            raise FreezeError("rendered_deployment_yaml_invalid") from exc
    if text[0] in "&*!{}|>%@`" or text.startswith("!!"):
        _fail("rendered_deployment_yaml_invalid")
    return text


def _compose_block(lines: list[tuple[int, str, int]], index: int, indent: int) -> tuple[Any, int]:
    if index >= len(lines) or lines[index][0] != indent:
        _fail("rendered_deployment_yaml_invalid")
    if lines[index][1].startswith("-"):
        return _compose_sequence(lines, index, indent)
    return _compose_mapping(lines, index, indent)


def _compose_mapping(lines: list[tuple[int, str, int]], index: int, indent: int) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    while index < len(lines) and lines[index][0] == indent and not lines[index][1].startswith("-"):
        key, raw_value = _compose_mapping_token(lines[index][1], strict=True) or ("", "")
        if key in result:
            _fail("rendered_deployment_duplicate_key")
        index += 1
        if raw_value:
            result[key] = _compose_scalar(raw_value)
            continue
        if index < len(lines) and lines[index][0] > indent:
            child_indent = lines[index][0]
            result[key], index = _compose_block(lines, index, child_indent)
        else:
            result[key] = None
    return result, index


def _compose_sequence(lines: list[tuple[int, str, int]], index: int, indent: int) -> tuple[list[Any], int]:
    result: list[Any] = []
    while index < len(lines) and lines[index][0] == indent and lines[index][1].startswith("-"):
        content = lines[index][1][1:].strip()
        index += 1
        if not content:
            if index < len(lines) and lines[index][0] > indent:
                child_indent = lines[index][0]
                item, index = _compose_block(lines, index, child_indent)
            else:
                item = None
            result.append(item)
            continue
        token = _compose_mapping_token(content, strict=False)
        if token is None:
            item = _compose_scalar(content)
            if index < len(lines) and lines[index][0] > indent:
                _fail("rendered_deployment_yaml_invalid")
            result.append(item)
            continue
        key, raw_value = token
        item_mapping: dict[str, Any] = {}
        if raw_value:
            item_mapping[key] = _compose_scalar(raw_value)
        elif index < len(lines) and lines[index][0] > indent:
            child_indent = lines[index][0]
            item_mapping[key], index = _compose_block(lines, index, child_indent)
        else:
            item_mapping[key] = None
        if index < len(lines) and lines[index][0] > indent:
            continuation_indent = lines[index][0]
            continuation, index = _compose_mapping(lines, index, continuation_indent)
            for continuation_key, continuation_value in continuation.items():
                if continuation_key in item_mapping:
                    _fail("rendered_deployment_duplicate_key")
                item_mapping[continuation_key] = continuation_value
        result.append(item_mapping)
    return result, index


def _parse_rendered_compose(rendered: bytes) -> dict[str, Any]:
    lines = _compose_lines(rendered)
    value, index = _compose_block(lines, 0, lines[0][0])
    if index != len(lines) or not isinstance(value, dict):
        _fail("rendered_deployment_yaml_invalid")
    return value


def _memory_limit_matches(value: Any, expected_bytes: int) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value == expected_bytes
    if not isinstance(value, str):
        return False
    text = value.strip().lower()
    if text.isdigit():
        return int(text) == expected_bytes
    match = _COMPOSE_MEMORY.fullmatch(text)
    if match is None:
        return False
    amount = int(match.group(1))
    suffix = match.group(2)
    if suffix == "b":
        multiplier = 1
    elif suffix.startswith("k"):
        multiplier = 1024
    elif suffix.startswith("m"):
        multiplier = 1024**2
    elif suffix.startswith("g"):
        multiplier = 1024**3
    else:
        multiplier = 1024**4
    return amount * multiplier == expected_bytes


def _cpu_limit_matches(value: Any, expected: float) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return False
    try:
        return float(value) == expected
    except (TypeError, ValueError):
        return False


def _validate_compose_mounts(service: Mapping[str, Any], expected: Sequence[Mapping[str, Any]]) -> None:
    mounts = service.get("volumes")
    if not isinstance(mounts, list) or len(mounts) != len(expected):
        _fail("rendered_deployment_mounts_invalid")
    for observed, wanted in zip(mounts, expected):
        if not isinstance(observed, dict) or set(observed) != set(wanted):
            _fail("rendered_deployment_mounts_invalid")
        if observed != dict(wanted):
            _fail("rendered_deployment_mounts_invalid")


def _validate_compose_service(
    service_name: str,
    service: Any,
    *,
    image_sha256: str,
    command: str,
    network_mode: str,
    resource_limit: tuple[int, int, float],
    mounts: Sequence[Mapping[str, Any]],
    profile: str | None,
) -> None:
    if not isinstance(service, dict):
        _fail("rendered_deployment_service_invalid")
    expected_keys = {
        "image", "pull_policy", "user", "command", "restart", "network_mode", "read_only",
        "cap_drop", "security_opt", "pids_limit", "mem_limit", "cpus", "tmpfs", "volumes",
    }
    if profile is not None:
        expected_keys.add("profiles")
    if set(service) != expected_keys:
        _fail("rendered_deployment_service_keys_invalid")
    image = service["image"]
    if not isinstance(image, str) or not image.startswith("sha256:"):
        _fail("rendered_deployment_image_invalid")
    if _normalize_image_sha256(image) != image_sha256:
        _fail("rendered_deployment_image_mismatch")
    if service["pull_policy"] != "never":
        _fail("rendered_deployment_pull_policy_invalid")
    if service["user"] != "0:0" or service["command"] != [command] or service["restart"] != "no":
        _fail("rendered_deployment_service_drift")
    if service["network_mode"] != network_mode:
        _fail("rendered_deployment_network_invalid")
    if service["read_only"] is not True or service["cap_drop"] != ["ALL"]:
        _fail("rendered_deployment_isolation_invalid")
    if service["security_opt"] != ["no-new-privileges:true"]:
        _fail("rendered_deployment_isolation_invalid")
    pids, memory, cpus = resource_limit
    if service["pids_limit"] != pids or not _memory_limit_matches(service["mem_limit"], memory):
        _fail("rendered_deployment_resource_invalid")
    if not _cpu_limit_matches(service["cpus"], cpus):
        _fail("rendered_deployment_resource_invalid")
    if service["tmpfs"] != ["/tmp:rw,noexec,nosuid,nodev,size=4m,mode=0700"]:
        _fail("rendered_deployment_tmpfs_invalid")
    if profile is not None and service["profiles"] != [profile]:
        _fail("rendered_deployment_profile_invalid")
    _validate_compose_mounts(service, mounts)


def _validate_rendered_compose(rendered: bytes, *, image_sha256: str) -> dict[str, Any]:
    """Parse and validate the complete rendered Compose object.

    The source Compose file is not evidence: only this fully rendered object
    is bound to the deployment hash.  Every service and mount is checked, so a
    digest or security option in a comment/unrelated extension cannot satisfy
    the freeze.
    """

    normalized_image = _normalize_image_sha256(image_sha256)
    compose = _parse_rendered_compose(rendered)
    if set(compose) - {"name", "services", "volumes"} or "services" not in compose or "volumes" not in compose:
        _fail("rendered_deployment_structure_invalid")
    if "name" in compose and (not isinstance(compose["name"], str) or not compose["name"]):
        _fail("rendered_deployment_structure_invalid")
    services = compose["services"]
    if not isinstance(services, dict):
        _fail("rendered_deployment_services_invalid")
    expected_service_names = {
        "gateb-protocol-diagnostic",
        "gateb-protocol-headline",
        "gateb-protocol-recovery",
        "gateb-protocol-diagnostic-recovery",
    }
    if set(services) != expected_service_names:
        _fail("rendered_deployment_services_invalid")
    # Compose evidence is rendered on Linux even when the offline freeze
    # command is reviewed on Windows.  Normalize pathlib's host separators
    # before comparing the fixed protocol paths.
    def compose_path(path: Path) -> str:
        return str(path).replace("\\", "/")

    credential = compose_path(executor.CREDENTIAL_PATH)
    diagnostic_authorization = compose_path(executor.DIAGNOSTIC_AUTHORIZATION_PATH)
    diagnostic_approval = compose_path(executor.DIAGNOSTIC_APPROVAL_PATH)
    diagnostic_execution_freeze = compose_path(executor.DIAGNOSTIC_EXECUTION_FREEZE_PATH)
    diagnostic_preflight = compose_path(executor.DIAGNOSTIC_PREFLIGHT_PATH)
    headline_authorization = compose_path(executor.AUTHORIZATION_PATH)
    headline_approval = compose_path(executor.APPROVAL_PATH)
    headline_execution_freeze = compose_path(executor.HEADLINE_EXECUTION_FREEZE_PATH)
    headline_preflight = compose_path(executor.HEADLINE_PREFLIGHT_PATH)
    state_target = compose_path(executor.STATE_DIRECTORY)
    state_mount: Mapping[str, Any] = {"type": "volume", "source": "gateb-protocol-state", "target": state_target}
    diagnostic_mounts: list[Mapping[str, Any]] = [
        {"type": "bind", "source": credential, "target": credential, "read_only": True},
        {"type": "bind", "source": diagnostic_authorization, "target": diagnostic_authorization, "read_only": True},
        {"type": "bind", "source": diagnostic_approval, "target": diagnostic_approval, "read_only": True},
        {"type": "bind", "source": diagnostic_execution_freeze, "target": diagnostic_execution_freeze, "read_only": True},
        {"type": "bind", "source": diagnostic_preflight, "target": diagnostic_preflight, "read_only": True},
        state_mount,
    ]
    headline_mounts: list[Mapping[str, Any]] = [
        {"type": "bind", "source": credential, "target": credential, "read_only": True},
        {"type": "bind", "source": diagnostic_authorization, "target": diagnostic_authorization, "read_only": True},
        {"type": "bind", "source": diagnostic_execution_freeze, "target": diagnostic_execution_freeze, "read_only": True},
        {"type": "bind", "source": diagnostic_preflight, "target": diagnostic_preflight, "read_only": True},
        {"type": "bind", "source": headline_authorization, "target": headline_authorization, "read_only": True},
        {"type": "bind", "source": headline_approval, "target": headline_approval, "read_only": True},
        {"type": "bind", "source": headline_execution_freeze, "target": headline_execution_freeze, "read_only": True},
        {"type": "bind", "source": headline_preflight, "target": headline_preflight, "read_only": True},
        state_mount,
    ]
    recovery_mounts: list[Mapping[str, Any]] = [
        {"type": "bind", "source": headline_authorization, "target": headline_authorization, "read_only": True},
        state_mount,
    ]
    diagnostic_recovery_mounts: list[Mapping[str, Any]] = [
        {"type": "bind", "source": diagnostic_authorization, "target": diagnostic_authorization, "read_only": True},
        state_mount,
    ]
    _validate_compose_service(
        "gateb-protocol-diagnostic", services["gateb-protocol-diagnostic"], image_sha256=normalized_image,
        command="run-diagnostic", network_mode="bridge", resource_limit=(32, 128 * 1024**2, 0.50),
        mounts=diagnostic_mounts, profile=None,
    )
    _validate_compose_service(
        "gateb-protocol-headline", services["gateb-protocol-headline"], image_sha256=normalized_image,
        command="run-headline", network_mode="bridge", resource_limit=(32, 128 * 1024**2, 0.50),
        mounts=headline_mounts, profile="headline",
    )
    _validate_compose_service(
        "gateb-protocol-recovery", services["gateb-protocol-recovery"], image_sha256=normalized_image,
        command="recover-headline", network_mode="none", resource_limit=(16, 64 * 1024**2, 0.25),
        mounts=recovery_mounts, profile="recovery",
    )
    _validate_compose_service(
        "gateb-protocol-diagnostic-recovery", services["gateb-protocol-diagnostic-recovery"], image_sha256=normalized_image,
        command="recover-diagnostic", network_mode="none", resource_limit=(16, 64 * 1024**2, 0.25),
        mounts=diagnostic_recovery_mounts, profile="recovery",
    )
    volumes = compose["volumes"]
    if not isinstance(volumes, dict) or set(volumes) != {"gateb-protocol-state"}:
        _fail("rendered_deployment_volumes_invalid")
    state_volume = volumes["gateb-protocol-state"]
    if not isinstance(state_volume, dict) or state_volume != {"name": "phase11c-gateb-headline-cohort-state-v1"}:
        _fail("rendered_deployment_state_volume_invalid")
    return compose


def _secure_credential_fingerprint(path: Path) -> str:
    """Read a Linux root-owned regular key once, retaining only its digest."""

    if not sys.platform.startswith("linux") or not path.is_absolute() or _O_NOFOLLOW == 0:
        _fail("credential_platform_or_path_invalid")
    current = Path(path.anchor)
    try:
        for part in path.parts[1:]:
            current /= part
            if stat.S_ISLNK(os.lstat(current).st_mode):
                _fail("credential_symlink_denied")
        descriptor = os.open(path, os.O_RDONLY | _O_CLOEXEC | _O_NOFOLLOW)
    except FreezeError:
        raise
    except OSError as exc:
        raise FreezeError("credential_open_failed") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != 0
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
            or not 1 <= before.st_size <= MAX_CREDENTIAL_BYTES
        ):
            _fail("credential_metadata_denied")
        content = bytearray()
        while len(content) <= MAX_CREDENTIAL_BYTES:
            block = os.read(descriptor, min(4096, MAX_CREDENTIAL_BYTES + 1 - len(content)))
            if not block:
                break
            content.extend(block)
        if len(content) != before.st_size or len(content) > MAX_CREDENTIAL_BYTES:
            _fail("credential_size_changed")
        after = os.fstat(descriptor)
        path_after = os.lstat(path)
        if (before.st_dev, before.st_ino, before.st_size) != (after.st_dev, after.st_ino, after.st_size) or (
            before.st_dev,
            before.st_ino,
            before.st_size,
        ) != (path_after.st_dev, path_after.st_ino, path_after.st_size):
            _fail("credential_identity_changed")
        return _sha256_bytes(bytes(content))
    finally:
        os.close(descriptor)


RUNTIME_EVIDENCE_FIELDS = {
    "schema_version",
    "instance_id_sha256",
    "region_sha256",
    "os_release_sha256",
    "kernel_release_sha256",
    "docker_server_sha256",
    "image_sha256",
}


def build_runtime_evidence(
    *, image_sha256: str, instance_id: str, region: str, os_release_bytes: bytes, docker_server_bytes: bytes, kernel_release: str
) -> dict[str, Any]:
    if instance_id != ALIYUN_INSTANCE_ID or region != ALIYUN_REGION or not isinstance(kernel_release, str):
        _fail("runtime_identity_scope_mismatch")
    return {
        "schema_version": RUNTIME_EVIDENCE_SCHEMA_VERSION,
        "instance_id_sha256": _sha256_bytes(instance_id.encode("ascii")),
        "region_sha256": _sha256_bytes(region.encode("ascii")),
        "os_release_sha256": _sha256_bytes(os_release_bytes),
        "kernel_release_sha256": _sha256_bytes(kernel_release.encode("utf-8")),
        "docker_server_sha256": _sha256_bytes(docker_server_bytes),
        "image_sha256": _normalize_image_sha256(image_sha256),
    }


def validate_runtime_evidence(value: Any, *, image_sha256: str) -> dict[str, Any]:
    evidence = _expect_mapping(value, "runtime_evidence_invalid")
    _expect_exact_keys(evidence, RUNTIME_EVIDENCE_FIELDS, "runtime_evidence_keys_invalid")
    if evidence["schema_version"] != RUNTIME_EVIDENCE_SCHEMA_VERSION:
        _fail("runtime_evidence_schema_invalid")
    for field in RUNTIME_EVIDENCE_FIELDS - {"schema_version"}:
        _expect_sha256(evidence[field], "runtime_evidence_hash_invalid")
    if evidence["image_sha256"] != image_sha256:
        _fail("runtime_evidence_image_mismatch")
    if evidence["instance_id_sha256"] != _sha256_bytes(ALIYUN_INSTANCE_ID.encode("ascii")):
        _fail("runtime_evidence_instance_mismatch")
    if evidence["region_sha256"] != _sha256_bytes(ALIYUN_REGION.encode("ascii")):
        _fail("runtime_evidence_region_mismatch")
    return evidence


def runtime_identity_document(evidence: Mapping[str, Any]) -> dict[str, Any]:
    document = {"schema_version": "phase11c-gateb-runtime-identity/v1", **dict(evidence), "runtime_identity_sha256": ""}
    return _seal(document, "runtime_identity_sha256")


@dataclass(frozen=True)
class FreezeMaterials:
    executable_source_sha256: str
    executable_commit_sha: str
    source_tree_sha256: str
    source_archive_sha256: str
    dockerfile_sha256: str
    compose_sha256: str
    image_sha256: str
    deployment_sha256: str
    runtime_identity_sha256: str
    provider_policy_evidence_sha256: str
    provider_tariff_evidence_sha256: str
    credential_fingerprint_sha256: str


def collect_materials(
    *,
    source_root: Path,
    source_archive: Path,
    executable_commit_sha: str,
    image_sha256: str,
    rendered_compose: Path,
    runtime_evidence: Mapping[str, Any],
    policy_evidence: Path,
    tariff_evidence: Path,
    credential_fingerprint_sha256: str,
) -> FreezeMaterials:
    root = _source_root(source_root)
    normalized_image = _normalize_image_sha256(image_sha256)
    tree = source_tree_manifest(root)
    try:
        rendered = rendered_compose.read_bytes()
    except OSError as exc:
        raise FreezeError("rendered_deployment_read_failed") from exc
    _validate_rendered_compose(rendered, image_sha256=normalized_image)
    runtime = validate_runtime_evidence(runtime_evidence, image_sha256=normalized_image)
    runtime_identity = runtime_identity_document(runtime)
    source = root / "phase11c_gateb_headline_cohort_executor.py"
    return FreezeMaterials(
        executable_source_sha256=normalized_utf8_sha256(source, code="executable_source_invalid"),
        executable_commit_sha=_expect_commit(executable_commit_sha, "executable_commit_sha_invalid"),
        source_tree_sha256=tree["source_tree_sha256"],
        source_archive_sha256=validate_source_archive(source_archive, source_root=root),
        dockerfile_sha256=sha256_file(root / "Dockerfile.phase11c-gateb-headline", code="dockerfile_invalid"),
        compose_sha256=sha256_file(root / "compose.phase11c-gateb-headline.yml", code="compose_invalid"),
        image_sha256=normalized_image,
        deployment_sha256=_sha256_bytes(rendered),
        runtime_identity_sha256=runtime_identity["runtime_identity_sha256"],
        provider_policy_evidence_sha256=sha256_file(policy_evidence, code="policy_evidence_invalid"),
        provider_tariff_evidence_sha256=sha256_file(tariff_evidence, code="tariff_evidence_invalid"),
        credential_fingerprint_sha256=_expect_sha256(credential_fingerprint_sha256, "credential_fingerprint_invalid"),
    )


def build_runtime_config(materials: FreezeMaterials) -> dict[str, Any]:
    document = {
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "phase_id": executor.PHASE_ID,
        "runtime_config_sha256": "",
        "executable_commit_sha": materials.executable_commit_sha,
        "executable_source_sha256": materials.executable_source_sha256,
        "source_tree_sha256": materials.source_tree_sha256,
        "source_archive_sha256": materials.source_archive_sha256,
        "dockerfile_sha256": materials.dockerfile_sha256,
        "compose_sha256": materials.compose_sha256,
        "image_sha256": materials.image_sha256,
        "deployment_sha256": materials.deployment_sha256,
        "runtime_identity_sha256": materials.runtime_identity_sha256,
        "provider": executor.PROVIDER,
        "request_model_id": executor.REQUEST_MODEL_ID,
        "snapshot_immutability": False,
        "api_surface": executor.API_SURFACE,
        "endpoint_sha256": executor.endpoint_sha256(),
        "tls_certificate_verification": True,
        "redirect_policy": "deny",
        "proxy_policy": "deny_implicit_inheritance",
        "sdk_retries": 0,
        "transport_retries": 0,
        "concurrency": 1,
        "publisher_mode": "fake_dry_run",
        "local_raw_retention": False,
    }
    return _seal(document, "runtime_config_sha256")


def build_tariff_manifest(*, materials: FreezeMaterials, observed_at_utc: str, effective_date: str) -> dict[str, Any]:
    _parse_utc(observed_at_utc, "tariff_observed_at_invalid")
    if not isinstance(effective_date, str) or _DATE.fullmatch(effective_date) is None:
        _fail("tariff_effective_date_invalid")
    document = {
        "schema_version": TARIFF_SCHEMA_VERSION,
        "phase_id": executor.PHASE_ID,
        "tariff_manifest_sha256": "",
        "provider": executor.PROVIDER,
        "request_model_id": executor.REQUEST_MODEL_ID,
        "currency": "micro-CNY",
        "integer_accounting_only": True,
        "input_rate_microcny_per_million": executor.INPUT_RATE_MICROCNY_PER_MILLION,
        "cached_input_rate_microcny_per_million": executor.CACHED_INPUT_RATE_MICROCNY_PER_MILLION,
        "output_rate_microcny_per_million": executor.OUTPUT_RATE_MICROCNY_PER_MILLION,
        "tariff_evidence_sha256": materials.provider_tariff_evidence_sha256,
        "observed_at_utc": observed_at_utc,
        "effective_date": effective_date,
    }
    return _seal(document, "tariff_manifest_sha256")


def _freeze_bindings(*, materials: FreezeMaterials, runtime: Mapping[str, Any], tariff: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "runtime_config_sha256": runtime["runtime_config_sha256"],
        "executable_commit_sha": materials.executable_commit_sha,
        "executable_source_sha256": materials.executable_source_sha256,
        "source_tree_sha256": materials.source_tree_sha256,
        "source_archive_sha256": materials.source_archive_sha256,
        "dockerfile_sha256": materials.dockerfile_sha256,
        "compose_sha256": materials.compose_sha256,
        "image_sha256": materials.image_sha256,
        "deployment_sha256": materials.deployment_sha256,
        "runtime_identity_sha256": materials.runtime_identity_sha256,
        "cohort_manifest_sha256": executor.cohort_manifest_sha256(),
        "provider_policy_evidence_sha256": materials.provider_policy_evidence_sha256,
        "provider_tariff_evidence_sha256": materials.provider_tariff_evidence_sha256,
        "provider_tariff_manifest_sha256": tariff["tariff_manifest_sha256"],
        "credential_fingerprint_sha256": materials.credential_fingerprint_sha256,
    }


def _freeze_subject_sha256(
    *, stage: str, materials: FreezeMaterials, runtime: Mapping[str, Any], tariff: Mapping[str, Any], window_start_utc: str, window_end_utc: str
) -> str:
    if stage not in {"DIAGNOSTIC", "HEADLINE_COHORT"}:
        _fail("freeze_stage_invalid")
    return _sha256_bytes(
        executor.canonical_json(
            {
                "phase_id": executor.PHASE_ID,
                "stage": stage,
                "bindings": _freeze_bindings(materials=materials, runtime=runtime, tariff=tariff),
                "authorization_window_start_utc": window_start_utc,
                "authorization_window_end_utc": window_end_utc,
                "budget_microcny": executor.DIAGNOSTIC_BUDGET_MICROCNY if stage == "DIAGNOSTIC" else executor.HEADLINE_BUDGET_MICROCNY,
            }
        )
    )


def _authorization_id(*, stage: str, freeze_subject_sha256: str) -> str:
    subject = _expect_sha256(freeze_subject_sha256, "freeze_subject_sha256_invalid")
    suffix = _sha256_bytes(executor.canonical_json({"phase_id": executor.PHASE_ID, "stage": stage, "freeze_subject_sha256": subject}))[:32]
    return f"p11c-gateb-{suffix}"


def _baseline() -> dict[str, Any]:
    return {
        "master_sha": BASELINE_MASTER_SHA,
        "ci_run_id": BASELINE_CI_RUN,
        "ci_attempt": BASELINE_CI_ATTEMPT,
        "ci_conclusion": BASELINE_CI_CONCLUSION,
        "phase11b_acceptance_report_sha256": PHASE11B_ACCEPTANCE_REPORT_SHA256,
        "phase11b_authorization_sha256": PHASE11B_AUTHORIZATION_SHA256,
        "phase11b_runtime_config_sha256": PHASE11B_RUNTIME_CONFIG_SHA256,
        "phase11b_status": "accepted",
    }


def build_preflight(
    *,
    execution_freeze: Mapping[str, Any],
    policy_url: str,
    retention_policy_url: str,
    policy_reviewed_at_utc: str,
) -> dict[str, Any]:
    if policy_url != POLICY_URL or retention_policy_url != RETENTION_POLICY_URL:
        _fail("preflight_identity_invalid")
    _parse_utc(policy_reviewed_at_utc, "policy_reviewed_at_invalid")
    freeze = validate_execution_freeze(execution_freeze)
    checks: dict[str, bool] = {
        "baseline_verified": True,
        "offline_fixtures_verified": True,
        "redaction_verified": True,
        "endpoint_tls_verified": True,
        "redirect_denied": True,
        "retry_policy_verified": True,
        "budget_reservation_verified": True,
        "cohort_nonoverlap_verified": True,
        "credential_file_security_verified": True,
        "provider_policy_accepted": True,
        "authorization_window_valid": True,
        "kill_switch_bound": True,
        "publisher_fake_only": True,
        "binding_hashes_match": True,
    }
    document = {
        "schema_version": PREFLIGHT_SCHEMA_VERSION,
        "phase_id": executor.PHASE_ID,
        "stage": freeze["stage"],
        "preflight_verdict_sha256": "",
        "execution_freeze_sha256": freeze["execution_freeze_sha256"],
        "freeze_subject_sha256": freeze["freeze_subject_sha256"],
        "authorization_id": freeze["authorization_id"],
        "baseline": _baseline(),
        "policy_url": policy_url,
        "retention_policy_url": retention_policy_url,
        "policy_reviewed_at_utc": policy_reviewed_at_utc,
        "authorization_window_start_utc": freeze["authorization_window_start_utc"],
        "authorization_window_end_utc": freeze["authorization_window_end_utc"],
        "checks": checks,
        "canary_allowed": True,
        "real_run_recommended_now": True,
        "blocking_reason_codes": [],
        "redaction_applied": True,
        "snapshot_immutability": False,
    }
    return _seal(document, "preflight_verdict_sha256")


def validate_preflight(value: Any) -> dict[str, Any]:
    document = _expect_mapping(value, "preflight_invalid")
    _expect_exact_keys(document, set(PREFLIGHT_FIELDS), "preflight_keys_invalid")
    if document["schema_version"] != PREFLIGHT_SCHEMA_VERSION or document["phase_id"] != executor.PHASE_ID:
        _fail("preflight_identity_invalid")
    if document["stage"] not in {"DIAGNOSTIC", "HEADLINE_COHORT"} or not _AUTHORIZATION_ID.fullmatch(document["authorization_id"]):
        _fail("preflight_identity_invalid")
    _expect_sha256(document["execution_freeze_sha256"], "preflight_execution_freeze_sha_invalid")
    _expect_sha256(document["freeze_subject_sha256"], "preflight_freeze_subject_sha_invalid")
    if document["baseline"] != _baseline() or document["policy_url"] != POLICY_URL or document["retention_policy_url"] != RETENTION_POLICY_URL:
        _fail("preflight_binding_invalid")
    start = _parse_utc(document["authorization_window_start_utc"], "preflight_window_invalid")
    end = _parse_utc(document["authorization_window_end_utc"], "preflight_window_invalid")
    if start >= end or (end - start).total_seconds() > 30 * 60:
        _fail("preflight_window_invalid")
    checks = _expect_mapping(document["checks"], "preflight_checks_invalid")
    if set(checks) != set(PREFLIGHT_CHECK_FIELDS) or not all(item is True for item in checks.values()):
        _fail("preflight_checks_failed")
    if document["canary_allowed"] is not True or document["real_run_recommended_now"] is not True or document["blocking_reason_codes"] != []:
        _fail("preflight_not_allowed")
    if document["redaction_applied"] is not True or document["snapshot_immutability"] is not False:
        _fail("preflight_safety_flags_invalid")
    _validate_seal(document, "preflight_verdict_sha256", "preflight_sha256_mismatch")
    return document


def _common_authorization_fields(
    *, materials: FreezeMaterials, runtime: Mapping[str, Any], tariff: Mapping[str, Any], authorization_id: str,
    execution_freeze_sha256: str, window_start_utc: str, window_end_utc: str, now_utc: datetime
) -> dict[str, Any]:
    now = now_utc.astimezone(timezone.utc)
    start = _parse_utc(window_start_utc, "authorization_window_start_invalid")
    end = _parse_utc(window_end_utc, "authorization_window_end_invalid")
    if start <= now or start >= end or (end - start).total_seconds() > 30 * 60:
        _fail("authorization_window_invalid")
    if not _AUTHORIZATION_ID.fullmatch(authorization_id):
        _fail("authorization_id_invalid")
    return {
        "authorization_id": authorization_id,
        "execution_freeze_sha256": _expect_sha256(execution_freeze_sha256, "execution_freeze_sha256_invalid"),
        "executable_source_sha256": materials.executable_source_sha256,
        "executable_commit_sha": materials.executable_commit_sha,
        "source_tree_sha256": materials.source_tree_sha256,
        "source_archive_sha256": materials.source_archive_sha256,
        "dockerfile_sha256": materials.dockerfile_sha256,
        "compose_sha256": materials.compose_sha256,
        "image_sha256": materials.image_sha256,
        "deployment_sha256": materials.deployment_sha256,
        "runtime_config_sha256": runtime["runtime_config_sha256"],
        "runtime_identity_sha256": materials.runtime_identity_sha256,
        "aliyun_runtime_identity_sha256": materials.runtime_identity_sha256,
        "provider_policy_evidence_sha256": materials.provider_policy_evidence_sha256,
        "provider_tariff_evidence_sha256": materials.provider_tariff_evidence_sha256,
        "provider_tariff_manifest_sha256": tariff["tariff_manifest_sha256"],
        "credential_fingerprint_sha256": materials.credential_fingerprint_sha256,
        "authorization_window_start_utc": window_start_utc,
        "authorization_window_end_utc": window_end_utc,
    }


def _auth004_nonoverlap_evidence_sha256() -> str:
    return _sha256_bytes(
        executor.canonical_json(
            {
                "source_kind": "deterministic_synthetic",
                "auth004_intersection_count": 0,
                "parent_gate_a_cohort_manifest_sha256": executor.GATE_A_COHORT_MANIFEST_SHA256,
                "target_bindings": list(executor.HEADLINE_TARGETS),
            }
        )
    )


def _execution_freeze(
    *, stage: str, materials: FreezeMaterials, runtime: Mapping[str, Any], tariff: Mapping[str, Any],
    freeze_subject_sha256: str, authorization_id: str, window_start_utc: str, window_end_utc: str
) -> dict[str, Any]:
    document = {
        "schema_version": FREEZE_SCHEMA_VERSION,
        "phase_id": executor.PHASE_ID,
        "stage": stage,
        "execution_freeze_sha256": "",
        "freeze_subject_sha256": _expect_sha256(freeze_subject_sha256, "freeze_subject_sha256_invalid"),
        "authorization_id": authorization_id,
        "runtime_config_sha256": runtime["runtime_config_sha256"],
        "provider_tariff_manifest_sha256": tariff["tariff_manifest_sha256"],
        "bindings": _freeze_bindings(materials=materials, runtime=runtime, tariff=tariff),
        "owners": {
            "authorization": executor.OWNER_ACCOUNT,
            "revocation": executor.OWNER_ACCOUNT,
            "kill_switch": executor.OWNER_ACCOUNT,
            "incident": executor.OWNER_ACCOUNT,
            "cleanup": executor.OWNER_ACCOUNT,
        },
        "baseline": _baseline(),
        "credential_delivery_mode": "fixed_linux_ecs_one_time_file",
        "provider_policy_accepted": True,
        "owner_reconfirmed": True,
        "kill_switch_bound": True,
        "authorization_window_start_utc": window_start_utc,
        "authorization_window_end_utc": window_end_utc,
        "budget_microcny": executor.DIAGNOSTIC_BUDGET_MICROCNY if stage == "DIAGNOSTIC" else executor.HEADLINE_BUDGET_MICROCNY,
        "snapshot_immutability": False,
        "redaction_applied": True,
    }
    return _seal(document, "execution_freeze_sha256")


def validate_execution_freeze(value: Any) -> dict[str, Any]:
    document = _expect_mapping(value, "execution_freeze_invalid")
    _expect_exact_keys(document, set(EXECUTION_FREEZE_FIELDS), "execution_freeze_keys_invalid")
    if document["schema_version"] != FREEZE_SCHEMA_VERSION or document["phase_id"] != executor.PHASE_ID:
        _fail("execution_freeze_identity_invalid")
    if document["stage"] not in {"DIAGNOSTIC", "HEADLINE_COHORT"} or not _AUTHORIZATION_ID.fullmatch(document["authorization_id"]):
        _fail("execution_freeze_identity_invalid")
    for field in ("freeze_subject_sha256", "runtime_config_sha256", "provider_tariff_manifest_sha256"):
        _expect_sha256(document[field], "execution_freeze_hash_invalid")
    bindings = _expect_mapping(document["bindings"], "execution_freeze_bindings_invalid")
    if set(bindings) != set(FREEZE_BINDING_FIELDS):
        _fail("execution_freeze_bindings_invalid")
    for field in FREEZE_BINDING_FIELDS - {"executable_commit_sha"}:
        _expect_sha256(bindings[field], "execution_freeze_bindings_invalid")
    _expect_commit(bindings["executable_commit_sha"], "execution_freeze_bindings_invalid")
    if (
        bindings["runtime_config_sha256"] != document["runtime_config_sha256"]
        or bindings["provider_tariff_manifest_sha256"] != document["provider_tariff_manifest_sha256"]
    ):
        _fail("execution_freeze_bindings_invalid")
    owners = _expect_mapping(document["owners"], "execution_freeze_owners_invalid")
    if set(owners) != {"authorization", "revocation", "kill_switch", "incident", "cleanup"} or set(owners.values()) != {executor.OWNER_ACCOUNT}:
        _fail("execution_freeze_owners_invalid")
    if document["snapshot_immutability"] is not False or document["redaction_applied"] is not True:
        _fail("execution_freeze_safety_flags_invalid")
    if document["baseline"] != _baseline():
        _fail("execution_freeze_baseline_invalid")
    start = _parse_utc(document["authorization_window_start_utc"], "execution_freeze_window_invalid")
    end = _parse_utc(document["authorization_window_end_utc"], "execution_freeze_window_invalid")
    if start >= end or (end - start).total_seconds() > 30 * 60:
        _fail("execution_freeze_window_invalid")
    if (
        document["credential_delivery_mode"] != "fixed_linux_ecs_one_time_file"
        or document["provider_policy_accepted"] is not True
        or document["owner_reconfirmed"] is not True
        or document["kill_switch_bound"] is not True
        or document["budget_microcny"] != (executor.DIAGNOSTIC_BUDGET_MICROCNY if document["stage"] == "DIAGNOSTIC" else executor.HEADLINE_BUDGET_MICROCNY)
    ):
        _fail("execution_freeze_invariant_invalid")
    expected_subject = _sha256_bytes(
        executor.canonical_json(
            {
                "phase_id": executor.PHASE_ID,
                "stage": document["stage"],
                "bindings": bindings,
                "authorization_window_start_utc": document["authorization_window_start_utc"],
                "authorization_window_end_utc": document["authorization_window_end_utc"],
                "budget_microcny": document["budget_microcny"],
            }
        )
    )
    if document["freeze_subject_sha256"] != expected_subject or document["authorization_id"] != _authorization_id(
        stage=document["stage"], freeze_subject_sha256=expected_subject
    ):
        _fail("execution_freeze_derivation_invalid")
    _validate_seal(document, "execution_freeze_sha256", "execution_freeze_sha256_mismatch")
    return document


def freeze_diagnostic(
    *,
    materials: FreezeMaterials,
    window_start_utc: str,
    window_end_utc: str,
    policy_url: str,
    retention_policy_url: str,
    policy_reviewed_at_utc: str,
    tariff_observed_at_utc: str,
    tariff_effective_date: str,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    now = now_utc or datetime.now(timezone.utc)
    runtime = build_runtime_config(materials)
    tariff = build_tariff_manifest(materials=materials, observed_at_utc=tariff_observed_at_utc, effective_date=tariff_effective_date)
    subject = _freeze_subject_sha256(
        stage="DIAGNOSTIC", materials=materials, runtime=runtime, tariff=tariff,
        window_start_utc=window_start_utc, window_end_utc=window_end_utc,
    )
    authorization_id = _authorization_id(stage="DIAGNOSTIC", freeze_subject_sha256=subject)
    freeze = _execution_freeze(
        stage="DIAGNOSTIC", materials=materials, runtime=runtime, tariff=tariff,
        freeze_subject_sha256=subject, authorization_id=authorization_id,
        window_start_utc=window_start_utc, window_end_utc=window_end_utc,
    )
    freeze = validate_execution_freeze(freeze)
    preflight = build_preflight(
        execution_freeze=freeze,
        policy_url=policy_url,
        retention_policy_url=retention_policy_url,
        policy_reviewed_at_utc=policy_reviewed_at_utc,
    )
    preflight = validate_preflight(preflight)
    candidate = executor.build_diagnostic_authorization_template()
    candidate.update(
        _common_authorization_fields(
            materials=materials,
            runtime=runtime,
            tariff=tariff,
            authorization_id=authorization_id,
            execution_freeze_sha256=freeze["execution_freeze_sha256"],
            window_start_utc=window_start_utc,
            window_end_utc=window_end_utc,
            now_utc=now,
        )
    )
    candidate["preflight_verdict_sha256"] = preflight["preflight_verdict_sha256"]
    authorization = executor.seal_diagnostic_authorization(
        candidate,
        executable_source_digest=materials.executable_source_sha256,
        now_utc=now,
    )
    return {
        "authorization": authorization,
        "runtime_config": runtime,
        "tariff_manifest": tariff,
        "preflight": preflight,
        "execution_freeze": freeze,
        "approval_binding_sha256": executor.diagnostic_approval_binding_sha256(authorization),
    }


def freeze_headline(
    *,
    materials: FreezeMaterials,
    window_start_utc: str,
    window_end_utc: str,
    policy_url: str,
    retention_policy_url: str,
    policy_reviewed_at_utc: str,
    tariff_observed_at_utc: str,
    tariff_effective_date: str,
    diagnostic_authorization: Mapping[str, Any],
    diagnostic_receipt: Mapping[str, Any],
    diagnostic_execution_freeze: Mapping[str, Any],
    diagnostic_preflight: Mapping[str, Any],
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    now = now_utc or datetime.now(timezone.utc)
    runtime = build_runtime_config(materials)
    tariff = build_tariff_manifest(materials=materials, observed_at_utc=tariff_observed_at_utc, effective_date=tariff_effective_date)
    subject = _freeze_subject_sha256(
        stage="HEADLINE_COHORT", materials=materials, runtime=runtime, tariff=tariff,
        window_start_utc=window_start_utc, window_end_utc=window_end_utc,
    )
    authorization_id = _authorization_id(stage="HEADLINE_COHORT", freeze_subject_sha256=subject)
    freeze = _execution_freeze(
        stage="HEADLINE_COHORT", materials=materials, runtime=runtime, tariff=tariff,
        freeze_subject_sha256=subject, authorization_id=authorization_id,
        window_start_utc=window_start_utc, window_end_utc=window_end_utc,
    )
    freeze = validate_execution_freeze(freeze)
    preflight = build_preflight(
        execution_freeze=freeze,
        policy_url=policy_url,
        retention_policy_url=retention_policy_url,
        policy_reviewed_at_utc=policy_reviewed_at_utc,
    )
    preflight = validate_preflight(preflight)
    diagnostic = executor.validate_completed_diagnostic_receipt(diagnostic_receipt)
    validated_diagnostic_authorization = executor.validate_diagnostic_authorization(
        diagnostic_authorization,
        executable_source_digest=materials.executable_source_sha256,
        now_utc=now,
        require_active_window=False,
        allow_expired_window=True,
    )
    validated_diagnostic_freeze = validate_execution_freeze(diagnostic_execution_freeze)
    validated_diagnostic_preflight = validate_preflight(diagnostic_preflight)
    if (
        validated_diagnostic_freeze["stage"] != "DIAGNOSTIC"
        or validated_diagnostic_freeze["authorization_id"] != validated_diagnostic_authorization["authorization_id"]
        or validated_diagnostic_freeze["execution_freeze_sha256"] != validated_diagnostic_authorization["execution_freeze_sha256"]
        or validated_diagnostic_preflight["stage"] != "DIAGNOSTIC"
        or validated_diagnostic_preflight["authorization_id"] != validated_diagnostic_authorization["authorization_id"]
        or validated_diagnostic_preflight["execution_freeze_sha256"] != validated_diagnostic_freeze["execution_freeze_sha256"]
        or validated_diagnostic_preflight["preflight_verdict_sha256"] != validated_diagnostic_authorization["preflight_verdict_sha256"]
    ):
        _fail("diagnostic_freeze_preflight_binding_mismatch")
    for field in (
        "executable_source_sha256", "executable_commit_sha", "source_tree_sha256", "source_archive_sha256",
        "dockerfile_sha256", "compose_sha256", "image_sha256", "deployment_sha256", "runtime_identity_sha256",
        "provider_policy_evidence_sha256", "provider_tariff_evidence_sha256", "provider_tariff_manifest_sha256",
        "credential_fingerprint_sha256",
    ):
        if validated_diagnostic_freeze["bindings"][field] != validated_diagnostic_authorization[field]:
            _fail("diagnostic_execution_freeze_binding_mismatch")
    if validated_diagnostic_freeze["runtime_config_sha256"] != validated_diagnostic_authorization["runtime_config_sha256"]:
        _fail("diagnostic_execution_freeze_binding_mismatch")
    candidate = executor.build_authorization_template()
    candidate.update(
        _common_authorization_fields(
            materials=materials,
            runtime=runtime,
            tariff=tariff,
            authorization_id=authorization_id,
            execution_freeze_sha256=freeze["execution_freeze_sha256"],
            window_start_utc=window_start_utc,
            window_end_utc=window_end_utc,
            now_utc=now,
        )
    )
    candidate.update(
        {
            "diagnostic_receipt_sha256": diagnostic["receipt_sha256"],
            "diagnostic_authorization_sha256": validated_diagnostic_authorization["authorization_sha256"],
            "diagnostic_approval_binding_sha256": diagnostic["approval_binding_sha256"],
            "auth004_nonoverlap_evidence_sha256": _auth004_nonoverlap_evidence_sha256(),
            "preflight_verdict_sha256": preflight["preflight_verdict_sha256"],
        }
    )
    authorization = executor.seal_authorization(
        candidate,
        executable_source_digest=materials.executable_source_sha256,
        now_utc=now,
    )
    executor.validate_headline_diagnostic_lineage(authorization, validated_diagnostic_authorization, diagnostic, now_utc=now)
    return {
        "authorization": authorization,
        "runtime_config": runtime,
        "tariff_manifest": tariff,
        "preflight": preflight,
        "execution_freeze": freeze,
        "approval_binding_sha256": executor.approval_binding_sha256(authorization),
    }


def _read_json_file(path: Path, code: str) -> Any:
    try:
        return executor.strict_json_loads(path.read_bytes())
    except (OSError, executor.HeadlineCohortError) as exc:
        raise FreezeError(code) from exc


def _write_document(path: Path, value: Mapping[str, Any], mode: int) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.lstat(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise FreezeError("freeze_output_write_failed") from exc
    else:
        _fail("freeze_output_already_exists")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_CLOEXEC, mode)
        payload = executor.canonical_json(value) + b"\n"
        os.write(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path)
        os.chmod(path, mode)
    except OSError as exc:
        raise FreezeError("freeze_output_write_failed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def write_freeze_output(output_dir: Path, result: Mapping[str, Any], *, stage: str) -> None:
    if stage not in {"DIAGNOSTIC", "HEADLINE_COHORT"}:
        _fail("freeze_output_stage_invalid")
    _write_document(output_dir / "authorization.json", result["authorization"], 0o400)
    _write_document(output_dir / "runtime-config.json", result["runtime_config"], 0o600)
    _write_document(output_dir / "tariff-manifest.json", result["tariff_manifest"], 0o600)
    _write_document(output_dir / "preflight.json", result["preflight"], 0o600)
    _write_document(output_dir / "execution-freeze.json", result["execution_freeze"], 0o600)


def _summary(result: Mapping[str, Any], *, stage: str) -> dict[str, Any]:
    binding = result["approval_binding_sha256"]
    text = executor.expected_diagnostic_approval_text(binding) if stage == "DIAGNOSTIC" else executor.expected_approval_text(binding)
    return {
        "stage": stage,
        "authorization_id": result["authorization"]["authorization_id"],
        "authorization_sha256": result["authorization"]["authorization_sha256"],
        "preflight_verdict_sha256": result["preflight"]["preflight_verdict_sha256"],
        "execution_freeze_sha256": result["execution_freeze"]["execution_freeze_sha256"],
        "approval_binding_sha256": binding,
        "expected_approval_text": text,
    }


def _common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--source-archive", type=Path, required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--image-sha256", required=True)
    parser.add_argument("--rendered-compose", type=Path, required=True)
    parser.add_argument("--runtime-evidence", type=Path, required=True)
    parser.add_argument("--policy-evidence", type=Path, required=True)
    parser.add_argument("--tariff-evidence", type=Path, required=True)
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--window-start-utc", required=True)
    parser.add_argument("--window-end-utc", required=True)
    parser.add_argument("--policy-url", required=True)
    parser.add_argument("--retention-policy-url", required=True)
    parser.add_argument("--policy-reviewed-at-utc", required=True)
    parser.add_argument("--tariff-observed-at-utc", required=True)
    parser.add_argument("--tariff-effective-date", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)


def _materials_from_args(args: argparse.Namespace) -> FreezeMaterials:
    image = _normalize_image_sha256(args.image_sha256)
    runtime = _read_json_file(args.runtime_evidence, "runtime_evidence_read_failed")
    return collect_materials(
        source_root=args.source_root,
        source_archive=args.source_archive,
        executable_commit_sha=args.commit_sha,
        image_sha256=image,
        rendered_compose=args.rendered_compose,
        runtime_evidence=runtime,
        policy_evidence=args.policy_evidence,
        tariff_evidence=args.tariff_evidence,
        credential_fingerprint_sha256=_secure_credential_fingerprint(args.credential_file),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    diagnostic = commands.add_parser("freeze-diagnostic")
    _common_arguments(diagnostic)
    headline = commands.add_parser("freeze-headline")
    _common_arguments(headline)
    headline.add_argument("--diagnostic-authorization", type=Path, required=True)
    headline.add_argument("--diagnostic-receipt", type=Path, required=True)
    headline.add_argument("--diagnostic-execution-freeze", type=Path, required=True)
    headline.add_argument("--diagnostic-preflight", type=Path, required=True)
    runtime = commands.add_parser("capture-runtime-evidence")
    runtime.add_argument("--image-sha256", required=True)
    runtime.add_argument("--instance-id", required=True)
    runtime.add_argument("--region", required=True)
    runtime.add_argument("--os-release", type=Path, default=Path("/etc/os-release"))
    runtime.add_argument("--docker-server-evidence", type=Path, required=True)
    runtime.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "capture-runtime-evidence":
            evidence = build_runtime_evidence(
                image_sha256=args.image_sha256,
                instance_id=args.instance_id,
                region=args.region,
                os_release_bytes=args.os_release.read_bytes(),
                docker_server_bytes=args.docker_server_evidence.read_bytes(),
                kernel_release=platform.release(),
            )
            _write_document(args.output, evidence, 0o600)
            print(executor.canonical_json({"runtime_identity_sha256": runtime_identity_document(evidence)["runtime_identity_sha256"]}).decode("ascii"))
            return 0
        materials = _materials_from_args(args)
        common = {
            "materials": materials,
            "window_start_utc": args.window_start_utc,
            "window_end_utc": args.window_end_utc,
            "policy_url": args.policy_url,
            "retention_policy_url": args.retention_policy_url,
            "policy_reviewed_at_utc": args.policy_reviewed_at_utc,
            "tariff_observed_at_utc": args.tariff_observed_at_utc,
            "tariff_effective_date": args.tariff_effective_date,
        }
        if args.command == "freeze-diagnostic":
            result = freeze_diagnostic(**common)
            stage = "DIAGNOSTIC"
        else:
            result = freeze_headline(
                **common,
                diagnostic_authorization=_read_json_file(args.diagnostic_authorization, "diagnostic_authorization_read_failed"),
                diagnostic_receipt=_read_json_file(args.diagnostic_receipt, "diagnostic_receipt_read_failed"),
                diagnostic_execution_freeze=_read_json_file(args.diagnostic_execution_freeze, "diagnostic_execution_freeze_read_failed"),
                diagnostic_preflight=_read_json_file(args.diagnostic_preflight, "diagnostic_preflight_read_failed"),
            )
            stage = "HEADLINE_COHORT"
        write_freeze_output(args.output_dir, result, stage=stage)
        print(executor.canonical_json(_summary(result, stage=stage)).decode("ascii"))
        return 0
    except (FreezeError, executor.HeadlineCohortError) as exc:
        print(executor.canonical_json({"error_code": str(exc)}).decode("ascii"))
        return 2
    except Exception:
        print(executor.canonical_json({"error_code": "internal_failure"}).decode("ascii"))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
