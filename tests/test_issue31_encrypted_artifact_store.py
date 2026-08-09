from __future__ import annotations

import base64
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from code_review_agent import artifact_store as artifact_store_module
from code_review_agent.artifact_store import (
    ArtifactConflict,
    ArtifactIntegrityError,
    ArtifactKeyError,
    ArtifactNotFound,
    ArtifactStoreError,
    ArtifactUnavailable,
    EncryptedArtifactStore,
    LocalAesGcmKeyWrapper,
    LocalArtifactBlobBackend,
    WrappedDataKey,
)


class MemoryArtifactBlobBackend:
    """In-memory implementation proving the adapter has no path dependency."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_if_absent(self, object_id: str, encoded_envelope: bytes) -> bool:
        if object_id in self.objects:
            return False
        self.objects[object_id] = bytes(encoded_envelope)
        return True

    def get(self, object_id: str) -> bytes:
        try:
            return self.objects[object_id]
        except KeyError as exc:
            raise ArtifactNotFound("artifact object was not found") from exc

    def delete(self, object_id: str) -> bool:
        return self.objects.pop(object_id, None) is not None


class FailingArtifactBlobBackend:
    def put_if_absent(self, object_id: str, encoded_envelope: bytes) -> bool:
        raise RuntimeError("backend failure must remain private")

    def get(self, object_id: str) -> bytes:
        raise RuntimeError("backend failure must remain private")

    def delete(self, object_id: str) -> bool:
        raise RuntimeError("backend failure must remain private")


class FailingDataKeyWrapper:
    key_id = "primary-v1"

    def wrap(self, data_key: bytes, *, associated_data: bytes) -> WrappedDataKey:
        raise RuntimeError("key wrapper failure must remain private")

    def unwrap(self, wrapped_key: WrappedDataKey, *, associated_data: bytes) -> bytes:
        raise RuntimeError("key wrapper failure must remain private")


class Issue31EncryptedArtifactStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.TemporaryDirectory()
        self.key = bytes(range(32))
        self.wrapper = LocalAesGcmKeyWrapper("primary-v1", self.key)
        self.backend = LocalArtifactBlobBackend(Path(self.root.name) / "objects")
        self.store = EncryptedArtifactStore(self.backend, self.wrapper)

    def tearDown(self) -> None:
        self.root.cleanup()

    def _overwrite_local(self, object_id: str, encoded: bytes) -> None:
        self.backend._object_path(object_id).write_bytes(encoded)

    def test_encrypted_round_trip_hides_plaintext_and_master_key(self) -> None:
        payload = b"portable encrypted artifact payload"

        metadata = self.store.put("object-one", payload)
        stored = self.backend.get("object-one")

        self.assertEqual(self.store.get("object-one"), payload)
        self.assertEqual(metadata.plaintext_sha256, hashlib.sha256(payload).hexdigest())
        self.assertEqual(metadata.plaintext_size, len(payload))
        self.assertNotIn(payload, stored)
        self.assertNotIn(self.key, stored)
        self.assertIn(b'"ciphertext"', stored)
        self.assertIn(b'"wrapped_key"', stored)

    def test_each_object_uses_independent_ciphertext_and_wrapped_key(self) -> None:
        payload = b"the same content is stored twice"

        self.store.put("object-one", payload)
        self.store.put("object-two", payload)
        first = json.loads(self.backend.get("object-one"))
        second = json.loads(self.backend.get("object-two"))

        self.assertNotEqual(first["content_nonce"], second["content_nonce"])
        self.assertNotEqual(first["ciphertext"], second["ciphertext"])
        self.assertNotEqual(first["wrapped_key_nonce"], second["wrapped_key_nonce"])
        self.assertNotEqual(first["wrapped_key"], second["wrapped_key"])

    def test_tampered_ciphertext_and_metadata_are_rejected(self) -> None:
        self.store.put("object-one", b"authenticated content")
        envelope = json.loads(self.backend.get("object-one"))
        ciphertext = bytearray(base64.b64decode(envelope["ciphertext"]))
        ciphertext[-1] ^= 1
        envelope["ciphertext"] = base64.b64encode(ciphertext).decode("ascii")
        self._overwrite_local(
            "object-one", json.dumps(envelope, separators=(",", ":")).encode("utf-8")
        )

        with self.assertRaises(ArtifactIntegrityError):
            self.store.get("object-one")

        self.store.put("object-two", b"metadata content")
        metadata_envelope = json.loads(self.backend.get("object-two"))
        metadata_envelope["plaintext_sha256"] = "0" * 64
        self._overwrite_local(
            "object-two", json.dumps(metadata_envelope, separators=(",", ":")).encode("utf-8")
        )
        # The authenticated key wrapper cannot distinguish metadata tampering
        # from a wrong key, and both paths must fail closed.
        with self.assertRaises((ArtifactIntegrityError, ArtifactKeyError)):
            self.store.get("object-two")

    def test_tampered_wrapped_key_and_object_identifier_are_rejected(self) -> None:
        self.store.put("object-one", b"wrapped key content")
        wrapped_key_envelope = json.loads(self.backend.get("object-one"))
        wrapped_key = bytearray(base64.b64decode(wrapped_key_envelope["wrapped_key"]))
        wrapped_key[-1] ^= 1
        wrapped_key_envelope["wrapped_key"] = base64.b64encode(wrapped_key).decode("ascii")
        self._overwrite_local(
            "object-one", json.dumps(wrapped_key_envelope, separators=(",", ":")).encode("utf-8")
        )

        with self.assertRaises(ArtifactKeyError):
            self.store.get("object-one")

        self.store.put("object-two", b"identity binding content")
        object_key_envelope = json.loads(self.backend.get("object-two"))
        object_key_envelope["object_id"] = "object-three"
        self._overwrite_local(
            "object-two", json.dumps(object_key_envelope, separators=(",", ":")).encode("utf-8")
        )

        with self.assertRaises(ArtifactIntegrityError):
            self.store.get("object-two")

    def test_plaintext_hash_is_checked_after_successful_decryption(self) -> None:
        payload = b"hash binding is checked after decrypt"
        metadata = self.store.put("object-one", payload)
        altered = replace(metadata, plaintext_sha256="0" * 64)
        data_key = bytes(reversed(range(32)))
        content_nonce = b"c" * 12
        ciphertext = AESGCM(data_key).encrypt(
            content_nonce, payload, artifact_store_module._data_associated_data(altered)
        )
        wrapped = self.wrapper.wrap(
            data_key, associated_data=artifact_store_module._key_associated_data(altered)
        )
        encoded = artifact_store_module._encode_envelope(
            artifact_store_module._Envelope(
                metadata=altered,
                content_nonce=content_nonce,
                ciphertext=ciphertext,
                wrapped_key=wrapped,
            )
        )
        self._overwrite_local("object-one", encoded)

        with self.assertRaises(ArtifactIntegrityError):
            self.store.get("object-one")

    def test_wrong_key_identifier_and_material_are_rejected(self) -> None:
        self.store.put("object-one", b"key-bound content")
        different_id = EncryptedArtifactStore(
            self.backend, LocalAesGcmKeyWrapper("secondary-v1", self.key)
        )
        same_id_different_material = EncryptedArtifactStore(
            self.backend, LocalAesGcmKeyWrapper("primary-v1", bytes(reversed(self.key)))
        )

        with self.assertRaises(ArtifactKeyError):
            different_id.get("object-one")
        with self.assertRaises(ArtifactKeyError):
            same_id_different_material.get("object-one")

    def test_duplicate_writes_missing_objects_and_invalid_identifiers_fail_closed(self) -> None:
        self.store.put("object-one", b"immutable")
        with self.assertRaises(ArtifactConflict):
            self.store.put("object-one", b"replacement")
        with self.assertRaises(ArtifactNotFound):
            self.store.get("object-two")
        for invalid in ("../escape", "nested/object", "", "a" * 129):
            with self.subTest(invalid=invalid), self.assertRaises(ArtifactStoreError):
                self.store.put(invalid, b"content")

    def test_memory_backend_is_compatible_and_does_not_need_local_paths(self) -> None:
        backend = MemoryArtifactBlobBackend()
        store = EncryptedArtifactStore(backend, self.wrapper)

        store.put("portable-object", b"cross-host compatible bytes")

        self.assertEqual(store.get("portable-object"), b"cross-host compatible bytes")
        self.assertTrue(store.delete("portable-object"))
        self.assertFalse(store.delete("portable-object"))

    def test_local_backend_rejects_oversized_and_path_like_object_identifiers(self) -> None:
        with self.assertRaises(ArtifactStoreError):
            self.backend.put_if_absent("../escape", b"{}")
        with self.assertRaises(ArtifactStoreError):
            self.backend.put_if_absent("object-one", b"x" * (33 * 1024 * 1024))

    def test_key_wrapper_and_plaintext_validation_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            LocalAesGcmKeyWrapper("invalid/key", self.key)
        with self.assertRaises(ValueError):
            LocalAesGcmKeyWrapper("primary-v1", b"too short")
        with self.assertRaises(ArtifactKeyError):
            self.wrapper.wrap(b"too short", associated_data=b"metadata")
        with self.assertRaises(ArtifactKeyError):
            self.wrapper.unwrap(WrappedDataKey(nonce=b"short", ciphertext=b"short"), associated_data=b"")

        nonce = b"n" * 12
        wrapped_short_key = WrappedDataKey(
            nonce=nonce,
            ciphertext=AESGCM(self.key).encrypt(nonce, b"too short", b"metadata"),
        )
        with self.assertRaises(ArtifactKeyError):
            self.wrapper.unwrap(wrapped_short_key, associated_data=b"metadata")
        with self.assertRaises(ArtifactStoreError):
            self.store.put("object-one", bytearray(b"not accepted"))  # type: ignore[arg-type]

    def test_malformed_envelopes_are_rejected_before_decryption(self) -> None:
        self.store.put("object-one", b"strict JSON envelope")
        valid = json.loads(self.backend.get("object-one"))
        malformed = [
            b"",
            b"[]",
            b'{"version":1,"version":1}',
            b"{}",
        ]
        for encoded in malformed:
            with self.subTest(encoded=encoded), self.assertRaises(ArtifactIntegrityError):
                artifact_store_module._decode_envelope(encoded)

        for field, value in (
            ("version", True),
            ("algorithm", "wrong"),
            ("plaintext_sha256", "z" * 64),
            ("plaintext_size", True),
            ("content_nonce", "@@@"),
            ("wrapped_key_nonce", "YQ=="),
        ):
            malformed_envelope = dict(valid)
            malformed_envelope[field] = value
            encoded = json.dumps(malformed_envelope, separators=(",", ":")).encode("utf-8")
            with self.subTest(field=field), self.assertRaises(ArtifactIntegrityError):
                artifact_store_module._decode_envelope(encoded)

    def test_local_backend_io_failures_oversized_reads_and_delete_are_bounded(self) -> None:
        with mock.patch.object(artifact_store_module.os, "open", side_effect=OSError("private")):
            with self.assertRaises(ArtifactUnavailable):
                self.backend.put_if_absent("open-failure", b"{}")

        with mock.patch.object(artifact_store_module.os, "fsync", side_effect=OSError("private")):
            with self.assertRaises(ArtifactUnavailable):
                self.backend.put_if_absent("sync-failure", b"{}")
        self.assertFalse(self.backend._object_path("sync-failure").exists())

        self.backend._object_path("directory-object").mkdir()
        with self.assertRaises(ArtifactUnavailable):
            self.backend.get("directory-object")

        oversized_path = self.backend._object_path("oversized-object")
        oversized_path.write_bytes(b"x" * (artifact_store_module._MAX_ENVELOPE_BYTES + 1))
        with self.assertRaises(ArtifactIntegrityError):
            self.backend.get("oversized-object")

        self.assertTrue(self.backend.put_if_absent("delete-object", b"{}"))
        self.assertTrue(self.backend.delete("delete-object"))
        self.assertFalse(self.backend.delete("delete-object"))

    def test_portable_backend_and_key_wrapper_failures_do_not_escape(self) -> None:
        failing_backend_store = EncryptedArtifactStore(FailingArtifactBlobBackend(), self.wrapper)
        with self.assertRaises(ArtifactUnavailable):
            failing_backend_store.put("object-one", b"content")
        with self.assertRaises(ArtifactUnavailable):
            failing_backend_store.get("object-one")
        with self.assertRaises(ArtifactUnavailable):
            failing_backend_store.delete("object-one")

        failing_wrapper_store = EncryptedArtifactStore(MemoryArtifactBlobBackend(), FailingDataKeyWrapper())
        with self.assertRaises(ArtifactKeyError):
            failing_wrapper_store.put("object-one", b"content")

        self.store.put("object-one", b"content")
        failing_unwrap_store = EncryptedArtifactStore(self.backend, FailingDataKeyWrapper())
        with self.assertRaises(ArtifactKeyError):
            failing_unwrap_store.get("object-one")


if __name__ == "__main__":
    unittest.main()
