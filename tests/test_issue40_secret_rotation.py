"""Contract tests for runtime provider-secret injection and rotation."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from code_review_agent import worker as worker_module
from code_review_agent.secret_manager import (
    MAX_SECRET_FILE_BYTES,
    AtomicFileSecretManager,
    RotatingSecretClientFactory,
    SecretManagerError,
    SecretSnapshot,
)
from code_review_agent.service_queue import JobStore


FIXED_NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
SECRET_ID = "crag.provider.glm.api-key"
MATERIAL_ONE = "fixture-material-one"
MATERIAL_TWO = "fixture-material-two"


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _payload(
    *,
    generation: int = 1,
    version: str = "version-1",
    value: str = MATERIAL_ONE,
    secret_id: str = SECRET_ID,
    now: datetime = FIXED_NOW,
    not_before: datetime | None = None,
    expires: datetime | None = None,
) -> dict[str, object]:
    return {
        "schema_version": "crag.runtime-secret/v1",
        "secret_id": secret_id,
        "version": version,
        "generation": generation,
        "value": value,
        "not_before_utc": _utc_text(not_before or now - timedelta(minutes=1)),
        "expires_at_utc": _utc_text(expires or now + timedelta(hours=1)),
    }


def _write_payload(path: Path, payload: dict[str, object]) -> None:
    path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    if os.name != "nt":
        path.chmod(0o600)


def _atomic_replace(path: Path, payload: dict[str, object]) -> None:
    staged = path.with_name(path.name + ".next")
    _write_payload(staged, payload)
    os.replace(staged, path)


def _snapshot(
    generation: int,
    *,
    version: str | None = None,
    value: str | None = None,
) -> SecretSnapshot:
    return SecretSnapshot(
        secret_id=SECRET_ID,
        version=version or f"version-{generation}",
        generation=generation,
        not_before_utc="2026-08-08T11:59:00Z",
        expires_at_utc="2026-08-08T13:00:00Z",
        value=value or f"fixture-material-{generation}",
    )


class MutableSource:
    def __init__(self, current: SecretSnapshot | SecretManagerError) -> None:
        self.current = current

    def fetch(self) -> SecretSnapshot:
        if isinstance(self.current, SecretManagerError):
            raise self.current
        return self.current


class AtomicFileSecretManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.path = self.root / "provider-secret.json"

    def manager(self, *, minimum_ttl_seconds: int = 60) -> AtomicFileSecretManager:
        return AtomicFileSecretManager(
            self.path,
            secret_id=SECRET_ID,
            minimum_ttl_seconds=minimum_ttl_seconds,
            clock=lambda: FIXED_NOW,
        )

    def assert_bounded_failure(self, expected: str) -> None:
        with self.assertRaises(SecretManagerError) as raised:
            self.manager().fetch()
        self.assertEqual(raised.exception.code, expected)
        rendered = f"{raised.exception!r} {raised.exception}"
        self.assertNotIn(str(self.path), rendered)
        self.assertNotIn(MATERIAL_ONE, rendered)

    def test_secure_read_returns_redacted_snapshot(self) -> None:
        _write_payload(self.path, _payload())

        snapshot = self.manager().fetch()

        self.assertEqual(snapshot.secret_id, SECRET_ID)
        self.assertEqual(snapshot.generation, 1)
        self.assertEqual(snapshot.value, MATERIAL_ONE)
        self.assertEqual(len(snapshot.version_sha256), 64)
        self.assertNotIn(MATERIAL_ONE, repr(snapshot))

    def test_malformed_identity_and_oversized_payloads_fail_bounded(self) -> None:
        self.path.write_bytes(b"{")
        self.assert_bounded_failure("secret_payload_invalid")

        self.path.write_text(
            '{"schema_version":"crag.runtime-secret/v1","schema_version":"duplicate"}',
            encoding="utf-8",
        )
        self.assert_bounded_failure("secret_payload_invalid")

        _write_payload(self.path, _payload(secret_id="crag.provider.other.api-key"))
        self.assert_bounded_failure("secret_identity_mismatch")

        self.path.write_bytes(b"x" * (MAX_SECRET_FILE_BYTES + 1))
        self.assert_bounded_failure("secret_file_oversized")

        self.path.unlink()
        self.assert_bounded_failure("secret_source_unavailable")

    def test_future_expired_and_short_lived_payloads_are_rejected(self) -> None:
        cases = (
            (
                "secret_not_yet_valid",
                _payload(not_before=FIXED_NOW + timedelta(seconds=1)),
            ),
            (
                "secret_expired",
                _payload(expires=FIXED_NOW),
            ),
            (
                "secret_expires_too_soon",
                _payload(expires=FIXED_NOW + timedelta(seconds=59)),
            ),
        )
        for expected, payload in cases:
            with self.subTest(expected=expected):
                _write_payload(self.path, payload)
                self.assert_bounded_failure(expected)

    def test_symlink_and_group_writable_file_are_denied(self) -> None:
        target = self.root / "target.json"
        _write_payload(target, _payload())
        try:
            self.path.symlink_to(target)
        except OSError:
            pass
        else:
            self.assert_bounded_failure("secret_file_denied")
            self.path.unlink()

        if os.name != "nt":
            _write_payload(self.path, _payload())
            self.path.chmod(0o620)
            self.assert_bounded_failure("secret_file_denied")


class RotatingSecretClientFactoryTests(unittest.TestCase):
    def test_unchanged_reuses_client_and_rotation_preserves_inflight_client(self) -> None:
        source = MutableSource(_snapshot(1, value=MATERIAL_ONE))
        builds: list[str] = []

        def build(value: str) -> tuple[SimpleNamespace, str]:
            builds.append(value)
            return SimpleNamespace(material=value), "frozen-model"

        factory = RotatingSecretClientFactory(source, build, clock=lambda: FIXED_NOW)
        first, _ = factory.preflight()
        unchanged, _ = factory()
        source.current = _snapshot(2, value=MATERIAL_TWO)
        rotated, _ = factory()

        self.assertIs(unchanged, first)
        self.assertIsNot(rotated, first)
        self.assertEqual(first.material, MATERIAL_ONE)
        self.assertEqual(rotated.material, MATERIAL_TWO)
        self.assertEqual(builds, [MATERIAL_ONE, MATERIAL_TWO])

    def test_concurrent_rotation_builds_higher_generation_once(self) -> None:
        source = MutableSource(_snapshot(1, value=MATERIAL_ONE))
        builds: list[str] = []
        builds_lock = threading.Lock()

        def build(value: str) -> tuple[SimpleNamespace, str]:
            with builds_lock:
                builds.append(value)
            return SimpleNamespace(material=value), "frozen-model"

        factory = RotatingSecretClientFactory(source, build, clock=lambda: FIXED_NOW)
        factory.preflight()
        source.current = _snapshot(2, value=MATERIAL_TWO)
        barrier = threading.Barrier(8)

        def load() -> SimpleNamespace:
            barrier.wait()
            client, _ = factory()
            return client

        with ThreadPoolExecutor(max_workers=8) as executor:
            clients = list(executor.map(lambda _: load(), range(8)))

        self.assertTrue(all(client is clients[0] for client in clients))
        self.assertEqual(builds, [MATERIAL_ONE, MATERIAL_TWO])

    def test_rollback_conflict_and_failed_build_never_return_cached_client(self) -> None:
        source = MutableSource(_snapshot(2, value=MATERIAL_ONE))

        def build(value: str) -> tuple[SimpleNamespace, str]:
            if value == MATERIAL_TWO:
                raise RuntimeError(f"builder exposed {value} at a forbidden path")
            return SimpleNamespace(material=value), "frozen-model"

        events = []
        factory = RotatingSecretClientFactory(
            source,
            build,
            event_sink=events.append,
            clock=lambda: FIXED_NOW,
        )
        cached, _ = factory.preflight()

        cases = (
            (_snapshot(1), "secret_generation_rollback"),
            (
                _snapshot(2, version="version-conflict", value=MATERIAL_ONE),
                "secret_generation_conflict",
            ),
            (_snapshot(3, value=MATERIAL_TWO), "secret_client_build_failed"),
        )
        for current, expected in cases:
            with self.subTest(expected=expected):
                source.current = current
                with self.assertRaises(SecretManagerError) as raised:
                    factory()
                self.assertEqual(raised.exception.code, expected)
                self.assertIsNone(raised.exception.__cause__)
                self.assertNotIn(MATERIAL_TWO, str(raised.exception))

        self.assertEqual(cached.material, MATERIAL_ONE)
        status = json.dumps(factory.status(), sort_keys=True)
        event_text = json.dumps([event.as_dict() for event in events], sort_keys=True)
        for forbidden in (MATERIAL_ONE, MATERIAL_TWO, "forbidden path"):
            self.assertNotIn(forbidden, status)
            self.assertNotIn(forbidden, event_text)
        self.assertEqual(factory.status()["failure_code"], "secret_client_build_failed")

    def test_expired_source_does_not_fall_back_to_cached_client(self) -> None:
        source = MutableSource(_snapshot(1))
        factory = RotatingSecretClientFactory(
            source,
            lambda value: (SimpleNamespace(material=value), "frozen-model"),
            clock=lambda: FIXED_NOW,
        )
        factory.preflight()
        source.current = SecretManagerError("secret_expired")

        with self.assertRaisesRegex(SecretManagerError, "secret_expired"):
            factory()
        self.assertEqual(factory.status()["failure_code"], "secret_expired")

    def test_failed_candidate_blocks_rollback_and_same_generation_rewrite(self) -> None:
        source = MutableSource(_snapshot(1, value=MATERIAL_ONE))

        def build(value: str) -> tuple[SimpleNamespace, str]:
            if value == MATERIAL_TWO:
                raise RuntimeError("synthetic builder failure")
            return SimpleNamespace(material=value), "frozen-model"

        factory = RotatingSecretClientFactory(source, build, clock=lambda: FIXED_NOW)
        factory.preflight()
        source.current = _snapshot(2, value=MATERIAL_TWO)
        with self.assertRaisesRegex(SecretManagerError, "secret_client_build_failed"):
            factory()

        source.current = _snapshot(1, value=MATERIAL_ONE)
        with self.assertRaisesRegex(SecretManagerError, "secret_generation_rollback"):
            factory()

        source.current = _snapshot(2, value="fixture-corrected-material")
        with self.assertRaisesRegex(SecretManagerError, "secret_generation_conflict"):
            factory()

        source.current = _snapshot(3, value="fixture-material-three")
        recovered, _ = factory()
        self.assertEqual(recovered.material, "fixture-material-three")


class WorkerSecretRotationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()
        self.state = self.root / "state"
        bootstrap = JobStore(self.state)
        self.database_url = bootstrap.database_url
        bootstrap.close()
        self.secret_file = self.root / "runtime-provider.json"

    def environment(self) -> dict[str, str]:
        return {
            "CRAG_STATE_DIR": str(self.state),
            "CRAG_DATABASE_URL": self.database_url,
            "CRAG_REPOSITORIES_JSON": json.dumps({"owner/repo": str(self.repo)}),
            "CRAG_WORKER_ID": "rotation-worker",
            "CRAG_JOB_LEASE_SECONDS": "10",
            "CRAG_JOB_HEARTBEAT_SECONDS": "1",
            "CRAG_WORKER_RUNNER": "real",
            "CRAG_PROVIDER_SECRET_FILE": str(self.secret_file),
            "LLM_PROVIDER": "glm",
            "LLM_MODEL": "glm-frozen",
        }

    def test_worker_observes_atomic_rotation_without_restart_and_redacts_logs(self) -> None:
        now = datetime.now(timezone.utc)
        _write_payload(self.secret_file, _payload(now=now))
        built: list[tuple[str, str, str | None]] = []

        def build(
            value: str,
            *,
            provider: str,
            model: str | None = None,
        ) -> tuple[SimpleNamespace, str]:
            built.append((value, provider, model))
            return SimpleNamespace(material=value), model or "unexpected"

        with (
            patch.dict(os.environ, self.environment(), clear=True),
            patch.object(worker_module, "_client_from_api_key", side_effect=build),
            patch.object(worker_module._LOGGER, "info") as log,
        ):
            worker = worker_module.create_worker_from_env()
            self.addCleanup(worker.store.close)
            self.assertNotIn("CRAG_PROVIDER_SECRET_FILE", os.environ)
            for name in worker_module._PROVIDER_CREDENTIAL_ENV_NAMES:
                self.assertNotIn(name, os.environ)

            factory = worker.runner._client_factory
            initial, initial_model = factory()
            _atomic_replace(
                self.secret_file,
                _payload(
                    generation=2,
                    version="version-2",
                    value=MATERIAL_TWO,
                    now=datetime.now(timezone.utc),
                ),
            )
            rotated, rotated_model = factory()

            self.assertIsInstance(factory, RotatingSecretClientFactory)
            self.assertIsNot(initial, rotated)
            self.assertEqual((initial.material, rotated.material), (MATERIAL_ONE, MATERIAL_TWO))
            self.assertEqual((initial_model, rotated_model), ("glm-frozen", "glm-frozen"))
            self.assertEqual(
                built,
                [
                    (MATERIAL_ONE, "glm", "glm-frozen"),
                    (MATERIAL_TWO, "glm", "glm-frozen"),
                ],
            )
            rendered_logs = "\n".join(
                call.args[0] % call.args[1:] for call in log.call_args_list
            )

        self.assertIn("provider secret rotation", rendered_logs)
        self.assertIn('"status":"rotated"', rendered_logs)
        for forbidden in (MATERIAL_ONE, MATERIAL_TWO, str(self.secret_file)):
            self.assertNotIn(forbidden, rendered_logs)

    def test_secret_manager_mode_rejects_mixed_legacy_configuration(self) -> None:
        environment = self.environment()
        environment["GLM_API_KEY_FILE"] = str(self.root / "legacy")
        with patch.dict(os.environ, environment, clear=True), self.assertRaisesRegex(
            worker_module.InvalidRequest, "cannot be mixed"
        ):
            worker_module._rotating_provider_client_factory()

        environment = self.environment()
        environment["CRAG_PROVIDER_SECRET_FILE"] = "relative-secret.json"
        with patch.dict(os.environ, environment, clear=True), self.assertRaisesRegex(
            worker_module.InvalidRequest, "absolute path"
        ):
            worker_module._rotating_provider_client_factory()

    def test_secret_failure_uses_bounded_retry_category(self) -> None:
        decision = worker_module.classify_failure(
            SecretManagerError("secret_expired"),
            job_id="a" * 32,
            attempt_count=1,
        )

        self.assertEqual(decision.category, "secret_expired")
        self.assertTrue(decision.retryable)


if __name__ == "__main__":
    unittest.main()
