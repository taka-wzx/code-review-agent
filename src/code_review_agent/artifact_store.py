"""Portable authenticated encryption for durable artifact objects.

The adapter deliberately operates on opaque bytes.  A deployment may provide
an object-store backend later, while the local backend keeps the same
create-if-absent contract for offline tests and single-host development.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Mapping, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


_ENVELOPE_VERSION = 1
_ALGORITHM = "AES-256-GCM"
_DATA_KEY_BYTES = 32
_NONCE_BYTES = 12
_MAX_PLAINTEXT_BYTES = 16 * 1024 * 1024
_MAX_ENVELOPE_BYTES = 32 * 1024 * 1024
_OBJECT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_KEY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class ArtifactStoreError(RuntimeError):
    """Base class for bounded artifact-store failures."""


class ArtifactNotFound(ArtifactStoreError):
    """The requested artifact does not exist in the configured backend."""


class ArtifactConflict(ArtifactStoreError):
    """An immutable object identifier already exists."""


class ArtifactKeyError(ArtifactStoreError):
    """The configured key cannot unwrap an artifact data key."""


class ArtifactIntegrityError(ArtifactStoreError):
    """An artifact envelope or its authenticated content is invalid."""


class ArtifactUnavailable(ArtifactStoreError):
    """The configured backend could not complete an object operation."""


@dataclass(frozen=True)
class ArtifactMetadata:
    """Safe, content-free identity returned after a successful write."""

    object_id: str
    plaintext_sha256: str
    plaintext_size: int
    key_id: str


@dataclass(frozen=True)
class WrappedDataKey:
    """Authenticated wrapped data-key material stored inside an envelope."""

    nonce: bytes
    ciphertext: bytes


def _require_wrapped_data_key(value: object) -> WrappedDataKey:
    if (
        not isinstance(value, WrappedDataKey)
        or not isinstance(value.nonce, bytes)
        or not isinstance(value.ciphertext, bytes)
        or len(value.nonce) != _NONCE_BYTES
        or len(value.ciphertext) <= _NONCE_BYTES
    ):
        raise ArtifactKeyError("artifact wrapped key is invalid")
    return value


@dataclass(frozen=True)
class _Envelope:
    metadata: ArtifactMetadata
    content_nonce: bytes
    ciphertext: bytes
    wrapped_key: WrappedDataKey


class ArtifactBlobBackend(Protocol):
    """Portable opaque-byte storage boundary for encrypted envelopes."""

    def put_if_absent(self, object_id: str, encoded_envelope: bytes) -> bool: ...

    def get(self, object_id: str) -> bytes: ...

    def delete(self, object_id: str) -> bool: ...


class DataKeyWrapper(Protocol):
    """Wrap and unwrap per-object data keys without exposing master-key bytes."""

    @property
    def key_id(self) -> str: ...

    def wrap(self, data_key: bytes, *, associated_data: bytes) -> WrappedDataKey: ...

    def unwrap(self, wrapped_key: WrappedDataKey, *, associated_data: bytes) -> bytes: ...


def _require_object_id(value: str) -> str:
    if not isinstance(value, str) or _OBJECT_ID.fullmatch(value) is None:
        raise ArtifactStoreError("artifact object identifier is invalid")
    return value


def _require_key_id(value: str) -> str:
    if not isinstance(value, str) or _KEY_ID.fullmatch(value) is None:
        raise ValueError("artifact key identifier is invalid")
    return value


def _require_plaintext(value: bytes) -> bytes:
    if not isinstance(value, bytes) or len(value) > _MAX_PLAINTEXT_BYTES:
        raise ArtifactStoreError("artifact plaintext is invalid")
    return value


def _canonical_json(value: Mapping[str, object]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _metadata_material(metadata: ArtifactMetadata) -> dict[str, object]:
    return {
        "algorithm": _ALGORITHM,
        "key_id": metadata.key_id,
        "object_id": metadata.object_id,
        "plaintext_sha256": metadata.plaintext_sha256,
        "plaintext_size": metadata.plaintext_size,
        "version": _ENVELOPE_VERSION,
    }


def _associated_data(purpose: str, metadata: ArtifactMetadata) -> bytes:
    return f"crag-artifact-store/{purpose}/v1\0".encode("ascii") + _canonical_json(
        _metadata_material(metadata)
    )


def _data_associated_data(metadata: ArtifactMetadata) -> bytes:
    return _associated_data("content", metadata)


def _key_associated_data(metadata: ArtifactMetadata) -> bytes:
    return _associated_data("key", metadata)


def _encode_binary(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _decode_binary(value: object, field: str) -> bytes:
    if not isinstance(value, str) or not value or len(value) > _MAX_ENVELOPE_BYTES * 2:
        raise ValueError(f"{field} is invalid")
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError):
        raise ValueError(f"{field} is invalid") from None


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate envelope key")
        result[key] = value
    return result


class LocalAesGcmKeyWrapper:
    """Local AES-256-GCM key wrapper used by portable offline adapters."""

    def __init__(self, key_id: str, key_material: bytes) -> None:
        self._key_id = _require_key_id(key_id)
        if not isinstance(key_material, bytes) or len(key_material) != _DATA_KEY_BYTES:
            raise ValueError("artifact key material must be exactly 32 bytes")
        self._key_material = bytes(key_material)

    @property
    def key_id(self) -> str:
        return self._key_id

    def wrap(self, data_key: bytes, *, associated_data: bytes) -> WrappedDataKey:
        if not isinstance(data_key, bytes) or len(data_key) != _DATA_KEY_BYTES:
            raise ArtifactKeyError("artifact data key is invalid")
        nonce = os.urandom(_NONCE_BYTES)
        try:
            ciphertext = AESGCM(self._key_material).encrypt(nonce, data_key, associated_data)
        except ValueError:
            raise ArtifactKeyError("artifact data key could not be wrapped") from None
        return WrappedDataKey(nonce=nonce, ciphertext=ciphertext)

    def unwrap(self, wrapped_key: WrappedDataKey, *, associated_data: bytes) -> bytes:
        wrapped_key = _require_wrapped_data_key(wrapped_key)
        try:
            data_key = AESGCM(self._key_material).decrypt(
                wrapped_key.nonce, wrapped_key.ciphertext, associated_data
            )
        except (InvalidTag, ValueError):
            raise ArtifactKeyError("artifact key could not be unwrapped") from None
        if len(data_key) != _DATA_KEY_BYTES:
            raise ArtifactKeyError("artifact unwrapped key is invalid")
        return data_key


class LocalArtifactBlobBackend:
    """Filesystem backend with immutable, atomic object creation semantics."""

    def __init__(self, root: Path) -> None:
        try:
            self._root = Path(root).resolve()
            self._root.mkdir(parents=True, exist_ok=True)
            if os.name != "nt":
                self._root.chmod(0o700)
        except OSError:
            raise ArtifactUnavailable("artifact backend is unavailable") from None

    def _object_path(self, object_id: str) -> Path:
        identifier = _require_object_id(object_id)
        path = self._root / identifier
        if path.parent != self._root:
            raise ArtifactStoreError("artifact object identifier is invalid")
        return path

    def put_if_absent(self, object_id: str, encoded_envelope: bytes) -> bool:
        if (
            not isinstance(encoded_envelope, bytes)
            or not encoded_envelope
            or len(encoded_envelope) > _MAX_ENVELOPE_BYTES
        ):
            raise ArtifactStoreError("artifact envelope is invalid")
        path = self._object_path(object_id)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return False
        except OSError:
            raise ArtifactUnavailable("artifact backend is unavailable") from None
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded_envelope)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError:
            try:
                path.unlink()
            except OSError:
                pass
            raise ArtifactUnavailable("artifact backend is unavailable") from None
        return True

    def get(self, object_id: str) -> bytes:
        path = self._object_path(object_id)
        try:
            with path.open("rb") as stream:
                encoded = stream.read(_MAX_ENVELOPE_BYTES + 1)
        except FileNotFoundError:
            raise ArtifactNotFound("artifact object was not found") from None
        except OSError:
            raise ArtifactUnavailable("artifact backend is unavailable") from None
        if len(encoded) > _MAX_ENVELOPE_BYTES:
            raise ArtifactIntegrityError("artifact envelope is invalid")
        return encoded

    def delete(self, object_id: str) -> bool:
        path = self._object_path(object_id)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        except OSError:
            raise ArtifactUnavailable("artifact backend is unavailable") from None
        return True


def _encode_envelope(envelope: _Envelope) -> bytes:
    body: dict[str, object] = {
        **_metadata_material(envelope.metadata),
        "content_nonce": _encode_binary(envelope.content_nonce),
        "ciphertext": _encode_binary(envelope.ciphertext),
        "wrapped_key": _encode_binary(envelope.wrapped_key.ciphertext),
        "wrapped_key_nonce": _encode_binary(envelope.wrapped_key.nonce),
    }
    encoded = _canonical_json(body)
    if len(encoded) > _MAX_ENVELOPE_BYTES:
        raise ArtifactStoreError("artifact envelope is too large")
    return encoded


def _decode_envelope(encoded: bytes) -> _Envelope:
    if not isinstance(encoded, bytes) or not encoded or len(encoded) > _MAX_ENVELOPE_BYTES:
        raise ArtifactIntegrityError("artifact envelope is invalid")
    try:
        decoded = json.loads(
            encoded.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
        if not isinstance(decoded, dict):
            raise ValueError("envelope root is invalid")
        expected_fields = {
            "algorithm",
            "content_nonce",
            "ciphertext",
            "key_id",
            "object_id",
            "plaintext_sha256",
            "plaintext_size",
            "version",
            "wrapped_key",
            "wrapped_key_nonce",
        }
        if set(decoded) != expected_fields:
            raise ValueError("envelope fields are invalid")
        if decoded["version"] != _ENVELOPE_VERSION or type(decoded["version"]) is not int:
            raise ValueError("envelope version is invalid")
        if decoded["algorithm"] != _ALGORITHM:
            raise ValueError("envelope algorithm is invalid")
        object_id = _require_object_id(decoded["object_id"])
        key_id = _require_key_id(decoded["key_id"])
        digest = decoded["plaintext_sha256"]
        size = decoded["plaintext_size"]
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise ValueError("envelope content hash is invalid")
        if type(size) is not int or not 0 <= size <= _MAX_PLAINTEXT_BYTES:
            raise ValueError("envelope content size is invalid")
        content_nonce = _decode_binary(decoded["content_nonce"], "content nonce")
        ciphertext = _decode_binary(decoded["ciphertext"], "ciphertext")
        wrapped_nonce = _decode_binary(decoded["wrapped_key_nonce"], "wrapped key nonce")
        wrapped_key = _decode_binary(decoded["wrapped_key"], "wrapped key")
        if (
            len(content_nonce) != _NONCE_BYTES
            or len(wrapped_nonce) != _NONCE_BYTES
            or len(ciphertext) < _NONCE_BYTES
            or len(wrapped_key) < _NONCE_BYTES
        ):
            raise ValueError("envelope encryption material is invalid")
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise ArtifactIntegrityError("artifact envelope is invalid") from None
    return _Envelope(
        metadata=ArtifactMetadata(
            object_id=object_id,
            plaintext_sha256=digest,
            plaintext_size=size,
            key_id=key_id,
        ),
        content_nonce=content_nonce,
        ciphertext=ciphertext,
        wrapped_key=WrappedDataKey(nonce=wrapped_nonce, ciphertext=wrapped_key),
    )


class EncryptedArtifactStore:
    """Encrypt immutable artifacts through a portable backend and key wrapper."""

    def __init__(self, backend: ArtifactBlobBackend, key_wrapper: DataKeyWrapper) -> None:
        self._backend = backend
        self._key_wrapper = key_wrapper
        try:
            self._key_id = _require_key_id(key_wrapper.key_id)
        except Exception:
            raise ArtifactKeyError("artifact key identifier is invalid") from None

    def put(self, object_id: str, plaintext: bytes) -> ArtifactMetadata:
        identifier = _require_object_id(object_id)
        content = _require_plaintext(plaintext)
        metadata = ArtifactMetadata(
            object_id=identifier,
            plaintext_sha256=hashlib.sha256(content).hexdigest(),
            plaintext_size=len(content),
            key_id=self._key_id,
        )
        data_key = os.urandom(_DATA_KEY_BYTES)
        content_nonce = os.urandom(_NONCE_BYTES)
        try:
            ciphertext = AESGCM(data_key).encrypt(
                content_nonce, content, _data_associated_data(metadata)
            )
        except ValueError:
            raise ArtifactStoreError("artifact plaintext could not be encrypted") from None
        try:
            wrapped_key = _require_wrapped_data_key(
                self._key_wrapper.wrap(data_key, associated_data=_key_associated_data(metadata))
            )
        except Exception:
            raise ArtifactKeyError("artifact data key could not be wrapped") from None
        encoded = _encode_envelope(
            _Envelope(
                metadata=metadata,
                content_nonce=content_nonce,
                ciphertext=ciphertext,
                wrapped_key=wrapped_key,
            )
        )
        try:
            created = self._backend.put_if_absent(identifier, encoded)
        except ArtifactStoreError:
            raise
        except Exception:
            raise ArtifactUnavailable("artifact backend is unavailable") from None
        if created is False:
            raise ArtifactConflict("artifact object already exists")
        if created is not True:
            raise ArtifactUnavailable("artifact backend is unavailable")
        return metadata

    def get(self, object_id: str) -> bytes:
        identifier = _require_object_id(object_id)
        try:
            encoded = self._backend.get(identifier)
        except ArtifactStoreError:
            raise
        except Exception:
            raise ArtifactUnavailable("artifact backend is unavailable") from None
        envelope = _decode_envelope(encoded)
        if envelope.metadata.object_id != identifier:
            raise ArtifactIntegrityError("artifact envelope identity is invalid")
        if envelope.metadata.key_id != self._key_id:
            raise ArtifactKeyError("artifact key identifier does not match")
        try:
            data_key = self._key_wrapper.unwrap(
                envelope.wrapped_key,
                associated_data=_key_associated_data(envelope.metadata),
            )
        except Exception:
            raise ArtifactKeyError("artifact key could not be unwrapped") from None
        if not isinstance(data_key, bytes) or len(data_key) != _DATA_KEY_BYTES:
            raise ArtifactKeyError("artifact unwrapped key is invalid")
        try:
            plaintext = AESGCM(data_key).decrypt(
                envelope.content_nonce,
                envelope.ciphertext,
                _data_associated_data(envelope.metadata),
            )
        except (InvalidTag, ValueError):
            raise ArtifactIntegrityError("artifact ciphertext is invalid") from None
        if (
            len(plaintext) != envelope.metadata.plaintext_size
            or hashlib.sha256(plaintext).hexdigest() != envelope.metadata.plaintext_sha256
        ):
            raise ArtifactIntegrityError("artifact plaintext integrity is invalid")
        return plaintext

    def delete(self, object_id: str) -> bool:
        identifier = _require_object_id(object_id)
        try:
            deleted = self._backend.delete(identifier)
        except ArtifactStoreError:
            raise
        except Exception:
            raise ArtifactUnavailable("artifact backend is unavailable") from None
        if type(deleted) is not bool:
            raise ArtifactUnavailable("artifact backend is unavailable")
        return deleted
