"""Bounded, content-minimizing telemetry redaction.

The redactor is deliberately independent from OpenTelemetry SDKs.  Every
local or optional exporter receives only the sanitized representation returned
by this module; callers never serialize arbitrary objects with ``default=str``.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import PurePath
import re
from typing import Any, Mapping
import unicodedata


REDACTION_POLICY_VERSION = "week6-redaction-v1"
MAX_ATTRIBUTES = 64
MAX_KEY_BYTES = 128
MAX_STRING_CHARACTERS = 1024
MAX_ARRAY_ITEMS = 32
MAX_NESTED_DEPTH = 8

FORBIDDEN_CONTENT_FIELDS = frozenset(
    {
        "gen_ai.input.messages",
        "gen_ai.output.messages",
        "gen_ai.system_instructions",
        "gen_ai.tool.call.arguments",
        "gen_ai.tool.call.result",
    }
)

_BLOCKED_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "args",
        "arguments",
        "authorization",
        "content",
        "cookie",
        "credentials",
        "diff",
        "environment",
        "exception",
        "exception_message",
        "headers",
        "issue_context",
        "message",
        "messages",
        "password",
        "private_key",
        "problems",
        "prompt",
        "result",
        "secret",
        "source_text",
        "stderr",
        "stdin",
        "stdout",
        "system_instructions",
        "token",
        "tool_arguments",
        "tool_result",
    }
)
_BLOCKED_KEY_FRAGMENTS = (
    "access_token",
    "auth_header",
    "client_secret",
    "private_key",
    "refresh_token",
    "system_prompt",
)
_SAFE_TOKEN_KEYS = frozenset(
    {
        "actual_tokens",
        "cache_creation_tokens",
        "cache_hit",
        "cache_miss",
        "cache_read_tokens",
        "completion_tokens",
        "input_tokens",
        "max_tokens",
        "output_tokens",
        "prompt_tokens",
        "reasoning_tokens",
        "tokens_in",
        "tokens_out",
    }
)
_SECRET_PATTERNS = (
    re.compile(r"W6_CANARY_[A-Za-z0-9_.-]+", re.IGNORECASE),
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}", re.IGNORECASE),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{16,}", re.IGNORECASE),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"https?://[^\s/:@]+:[^\s/@]+@", re.IGNORECASE),
    re.compile(
        r"(?i)\b(?:api[_-]?key|password|secret|access[_-]?token)\s*[:=]\s*"
        r"[^\s,;]{4,}"
    ),
)
_ABSOLUTE_PATH = re.compile(
    r"^(?:[A-Za-z]:[\\/]|\\\\|/)",
    re.IGNORECASE,
)
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_KEY_NORMALIZER = re.compile(r"[^a-z0-9]+")
_SENSITIVE_PATH_COMPONENTS = frozenset(
    {
        ".env",
        ".git-credentials",
        ".npmrc",
        ".pypirc",
        "credentials",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "known_hosts",
    }
)


@dataclass(frozen=True)
class RedactionResult:
    """One sanitized value plus auditable reduction counts."""

    value: Any
    redaction_count: int = 0
    omitted_count: int = 0
    truncated: bool = False


def _normalized_key(key: str) -> str:
    return _KEY_NORMALIZER.sub("_", key.casefold()).strip("_")


def _blocked_key(key: str) -> bool:
    if key in FORBIDDEN_CONTENT_FIELDS:
        return True
    normalized = _normalized_key(key)
    if normalized in _SAFE_TOKEN_KEYS:
        return False
    return normalized in _BLOCKED_KEYS or any(
        fragment in normalized for fragment in _BLOCKED_KEY_FRAGMENTS
    )


def _safe_key(raw: Any) -> str | None:
    if not isinstance(raw, str) or not raw:
        return None
    if (
        len(raw.encode("utf-8")) > MAX_KEY_BYTES
        or _CONTROL_CHARACTERS.search(raw)
        or any(unicodedata.category(char).startswith("C") for char in raw)
    ):
        return None
    return raw


def _contains_secret(value: str) -> bool:
    compact = "".join(
        char
        for char in value
        if not char.isspace() and not unicodedata.category(char).startswith("C")
    )
    return any(
        pattern.search(value) or pattern.search(compact)
        for pattern in _SECRET_PATTERNS
    )


def _sensitive_relative_path(value: str) -> bool:
    components = [
        component.casefold()
        for component in value.replace("\\", "/").split("/")
        if component not in {"", "."}
    ]
    for component in components:
        if component == ".env.example":
            continue
        if component in _SENSITIVE_PATH_COMPONENTS:
            return True
        if component.startswith(".env."):
            return True
    return False


def _sanitize_string(value: str) -> RedactionResult:
    if _contains_secret(value):
        return RedactionResult("[REDACTED]", redaction_count=1)
    if _ABSOLUTE_PATH.match(value):
        return RedactionResult("[OMITTED:absolute-path]", redaction_count=1)
    if _sensitive_relative_path(value):
        return RedactionResult("[OMITTED:sensitive-path]", redaction_count=1)
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.replace("\n", " ").replace("\t", " ")
    normalized = _CONTROL_CHARACTERS.sub("\ufffd", normalized)
    normalized = "".join(
        "\ufffd" if unicodedata.category(char).startswith("C") else char
        for char in normalized
    )
    if len(normalized) > MAX_STRING_CHARACTERS:
        return RedactionResult(
            normalized[:MAX_STRING_CHARACTERS],
            truncated=True,
        )
    return RedactionResult(normalized)


def sanitize_value(value: Any, *, depth: int = 0) -> RedactionResult:
    """Return a deterministic JSON-safe value without retaining raw content."""

    if depth >= MAX_NESTED_DEPTH:
        return RedactionResult(
            "[OMITTED:max-depth]",
            omitted_count=1,
            truncated=True,
        )
    if value is None or isinstance(value, (bool, int)):
        return RedactionResult(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return RedactionResult("[OMITTED:non-finite]", omitted_count=1)
        return RedactionResult(value)
    if isinstance(value, str):
        return _sanitize_string(value)
    if isinstance(value, PurePath):
        return _sanitize_string(value.as_posix())
    if isinstance(value, bytes):
        return RedactionResult(f"[OMITTED:bytes:{len(value)}]", omitted_count=1)
    if isinstance(value, BaseException):
        return RedactionResult(
            f"[OMITTED:{type(value).__name__}]",
            omitted_count=1,
        )
    if isinstance(value, Mapping):
        joined = "".join(
            child for child in value.values() if isinstance(child, str)
        )
        if joined and _contains_secret(joined):
            return RedactionResult(
                "[REDACTED:split-secret]",
                redaction_count=1,
                omitted_count=len(value),
            )
        output: dict[str, Any] = {}
        redactions = omissions = 0
        truncated = False
        for index, (raw_key, child_value) in enumerate(value.items()):
            if index >= MAX_ARRAY_ITEMS:
                truncated = True
                omissions += len(value) - index
                break
            key = _safe_key(raw_key)
            if key is None:
                omissions += 1
                continue
            if _blocked_key(key):
                redactions += 1
                omissions += 1
                continue
            child = sanitize_value(child_value, depth=depth + 1)
            output[key] = child.value
            redactions += child.redaction_count
            omissions += child.omitted_count
            truncated = truncated or child.truncated
        return RedactionResult(output, redactions, omissions, truncated)
    if isinstance(value, (set, frozenset)):
        return RedactionResult(
            f"[OMITTED:{type(value).__name__}]",
            omitted_count=1,
        )
    if isinstance(value, (list, tuple)):
        values = list(value)
        joined = "".join(child for child in values if isinstance(child, str))
        if joined and _contains_secret(joined):
            return RedactionResult(
                "[REDACTED:split-secret]",
                redaction_count=1,
                omitted_count=len(values),
            )
        list_output: list[Any] = []
        redactions = omissions = 0
        truncated = len(values) > MAX_ARRAY_ITEMS
        for child_value in values[:MAX_ARRAY_ITEMS]:
            child = sanitize_value(child_value, depth=depth + 1)
            list_output.append(child.value)
            redactions += child.redaction_count
            omissions += child.omitted_count
            truncated = truncated or child.truncated
        if len(values) > MAX_ARRAY_ITEMS:
            omissions += len(values) - MAX_ARRAY_ITEMS
        return RedactionResult(list_output, redactions, omissions, truncated)
    return RedactionResult(
        f"[OMITTED:{type(value).__name__[:64]}]",
        omitted_count=1,
    )


def sanitize_attributes(attributes: Mapping[str, Any] | None) -> RedactionResult:
    """Sanitize one span/event attribute map and enforce its top-level cap."""

    if attributes is None:
        return RedactionResult({})
    if not isinstance(attributes, Mapping):
        return RedactionResult({}, omitted_count=1)
    joined = "".join(
        child for child in attributes.values() if isinstance(child, str)
    )
    if joined and _contains_secret(joined):
        return RedactionResult(
            {},
            redaction_count=1,
            omitted_count=len(attributes),
        )
    output: dict[str, Any] = {}
    redactions = omissions = 0
    truncated = False
    for index, (raw_key, value) in enumerate(attributes.items()):
        if index >= MAX_ATTRIBUTES:
            omissions += len(attributes) - index
            truncated = True
            break
        key = _safe_key(raw_key)
        if key is None:
            omissions += 1
            continue
        if _blocked_key(key):
            redactions += 1
            omissions += 1
            continue
        if value is None:
            omissions += 1
            continue
        child = sanitize_value(value, depth=1)
        output[key] = child.value
        redactions += child.redaction_count
        omissions += child.omitted_count
        truncated = truncated or child.truncated
    return RedactionResult(output, redactions, omissions, truncated)


def contains_forbidden_content(value: Any) -> bool:
    """Defensive post-redaction scan used by the canonical validator."""

    if isinstance(value, Mapping):
        for key, child in value.items():
            if isinstance(key, str) and _blocked_key(key):
                return True
            if contains_forbidden_content(child):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(contains_forbidden_content(child) for child in value)
    if isinstance(value, str):
        return bool(
            _looks_like_absolute_host_path(value)
            or _sensitive_relative_path(value)
            or _contains_secret(value)
        )
    return False


def _looks_like_absolute_host_path(value: str) -> bool:
    """Validate path shape independently from the sanitizer's compiled regex."""

    if value.startswith(("/", "\\\\")):
        return True
    return (
        len(value) >= 3
        and value[0].isalpha()
        and value[1] == ":"
        and value[2] in {"/", "\\"}
    )
