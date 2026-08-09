from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
import tempfile
import threading
import unittest

from fastapi.testclient import TestClient

from code_review_agent.service import HttpSettings, create_app
from code_review_agent.service_core import JobStore, RepositoryRegistry, ReviewService


SECRET = "issue33-webhook-secret-at-least-16-bytes"


class Issue33GitHubWebhookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repository = self.root / "repo"
        self.repository.mkdir()
        (self.repository / ".git").mkdir()
        self.registry = RepositoryRegistry.from_json(
            json.dumps({"owner/repo": str(self.repository.resolve())})
        )
        self.state = self.root / "state"
        self.client_context: TestClient | None = None
        self._start_service()

    def tearDown(self) -> None:
        self._stop_service()
        self.temp.cleanup()

    def _start_service(self) -> None:
        self.service = ReviewService(
            self.registry,
            JobStore(self.state),
            runner=None,
        )
        settings = HttpSettings(
            service_token="service-token-that-is-at-least-32-bytes",
            webhook_secret=SECRET,
            allowed_origins=frozenset({"http://localhost"}),
            allowed_hosts=frozenset({"testserver"}),
        )
        self.app = create_app(settings=settings, review_service=self.service)
        self.client_context = TestClient(self.app)
        self.client = self.client_context.__enter__()

    def _stop_service(self) -> None:
        if self.client_context is not None:
            self.client_context.__exit__(None, None, None)
            self.client_context = None

    def _restart_service(self) -> None:
        self._stop_service()
        self._start_service()

    @staticmethod
    def installation_payload(action: str, *, account_id: int = 20105) -> dict[str, object]:
        return {
            "action": action,
            "installation": {
                "id": 149747930,
                "app_id": 4421400,
                "account": {"id": account_id},
            },
        }

    @staticmethod
    def pull_request_payload(
        *,
        number: int = 12,
        head_sha: str = "a" * 40,
        installation_id: int | None = 149747930,
        owner_id: int = 20105,
    ) -> dict[str, object]:
        repository: dict[str, object] = {"full_name": "owner/repo"}
        if installation_id is not None:
            repository["owner"] = {"id": owner_id}
        payload: dict[str, object] = {
            "action": "opened",
            "repository": repository,
            "pull_request": {"number": number, "head": {"sha": head_sha}},
        }
        if installation_id is not None:
            payload["installation"] = {"id": installation_id}
        return payload

    @staticmethod
    def signed_headers(body: bytes, *, event: str, delivery: str) -> dict[str, str]:
        signature = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        return {
            "X-Hub-Signature-256": signature,
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery,
            "Content-Type": "application/json",
        }

    def post(self, payload: dict[str, object], *, event: str, delivery: str):
        body = json.dumps(payload, separators=(",", ":")).encode()
        return self.client.post(
            "/webhooks/github",
            content=body,
            headers=self.signed_headers(body, event=event, delivery=delivery),
        )

    def test_installation_transitions_and_delivery_conflicts_are_durable(self) -> None:
        created_payload = self.installation_payload("created")
        created = self.post(created_payload, event="installation", delivery="install-created")
        self.assertEqual(created.status_code, 202, created.text)
        self.assertEqual(created.json()["status"], "installation_active")
        self.assertFalse(created.json()["duplicate"])
        self.assertEqual(
            self.service.store.database.github_app_installation(149747930)["state"], "active"
        )

        duplicate = self.post(created_payload, event="installation", delivery="install-created")
        self.assertEqual(duplicate.status_code, 202, duplicate.text)
        self.assertTrue(duplicate.json()["duplicate"])

        conflict = self.post(
            self.installation_payload("suspend"),
            event="installation",
            delivery="install-created",
        )
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertEqual(conflict.json()["error"]["code"], "github_webhook_delivery_conflict")

        suspended = self.post(
            self.installation_payload("suspend"),
            event="installation",
            delivery="install-suspend",
        )
        self.assertEqual(suspended.status_code, 202, suspended.text)
        self.assertEqual(suspended.json()["status"], "installation_suspended")

        restored = self.post(
            self.installation_payload("unsuspend"),
            event="installation",
            delivery="install-unsuspend",
        )
        self.assertEqual(restored.status_code, 202, restored.text)
        self.assertEqual(restored.json()["status"], "installation_active")

        deleted = self.post(
            self.installation_payload("deleted"),
            event="installation",
            delivery="install-delete",
        )
        self.assertEqual(deleted.status_code, 202, deleted.text)
        self.assertEqual(deleted.json()["status"], "installation_deleted")
        denied = self.post(
            self.installation_payload("unsuspend"),
            event="installation",
            delivery="install-after-delete",
        )
        self.assertEqual(denied.status_code, 202, denied.text)
        self.assertEqual(denied.json()["status"], "ignored")
        self.assertEqual(denied.json()["reason"], "installation_transition_denied")

    def test_bad_signature_leaves_no_durable_state(self) -> None:
        payload = self.installation_payload("created")
        body = json.dumps(payload, separators=(",", ":")).encode()
        response = self.client.post(
            "/webhooks/github",
            content=body,
            headers={
                "X-Hub-Signature-256": "sha256=" + "0" * 64,
                "X-GitHub-Event": "installation",
                "X-GitHub-Delivery": "untrusted-installation",
                "Content-Type": "application/json",
            },
        )
        self.assertEqual(response.status_code, 401, response.text)
        self.assertIsNone(self.service.store.database.github_app_installation(149747930))
        self.assertIsNone(
            self.service.store.database.github_webhook_delivery(
                "untrusted-installation",
                event="installation",
                payload_sha256=hashlib.sha256(body).hexdigest(),
            )
        )

    def test_concurrent_installation_delivery_transitions_once(self) -> None:
        payload = self.installation_payload("created")
        body = json.dumps(payload, separators=(",", ":")).encode()
        processor = self.app.state.github_webhooks
        start = threading.Barrier(3)
        acknowledgements = []

        def acknowledge() -> None:
            start.wait()
            acknowledgements.append(
                processor.acknowledge(
                    event="installation",
                    delivery_id="concurrent-installation",
                    body=body,
                    payload=payload,
                )
            )

        threads = [threading.Thread(target=acknowledge) for _ in range(2)]
        for thread in threads:
            thread.start()
        start.wait()
        for thread in threads:
            thread.join(5)
        self.assertEqual(len(acknowledgements), 2)
        self.assertEqual(
            sorted(item.body["duplicate"] for item in acknowledgements), [False, True]
        )
        self.assertEqual(
            self.service.store.database.github_app_installation(149747930)["state"], "active"
        )

    def test_active_app_pr_is_queued_once_and_inactive_or_mismatched_prs_are_ignored(self) -> None:
        self.post(
            self.installation_payload("created"),
            event="installation",
            delivery="install-for-pr",
        )
        accepted_payload = self.pull_request_payload()
        accepted = self.post(accepted_payload, event="pull_request", delivery="pr-active")
        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.assertFalse(accepted.json()["duplicate"])
        review_id = accepted.json()["review_id"]
        self.assertEqual(self.service.store.get(review_id)["state"], "queued")

        replay = self.post(accepted_payload, event="pull_request", delivery="pr-active")
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertTrue(replay.json()["duplicate"])
        self.assertEqual(replay.json()["review_id"], review_id)

        mismatch = self.post(
            self.pull_request_payload(number=13, head_sha="b" * 40, owner_id=999),
            event="pull_request",
            delivery="pr-account-mismatch",
        )
        self.assertEqual(mismatch.status_code, 202, mismatch.text)
        self.assertEqual(mismatch.json()["status"], "ignored")
        self.assertEqual(mismatch.json()["reason"], "installation_account_mismatch")

        self.post(
            self.installation_payload("suspend"),
            event="installation",
            delivery="install-for-pr-suspend",
        )
        inactive = self.post(
            self.pull_request_payload(number=14, head_sha="c" * 40),
            event="pull_request",
            delivery="pr-inactive",
        )
        self.assertEqual(inactive.status_code, 202, inactive.text)
        self.assertEqual(inactive.json()["status"], "ignored")
        self.assertEqual(inactive.json()["reason"], "installation_inactive")

    def test_delivery_receipts_survive_service_restart(self) -> None:
        payload = self.installation_payload("created")
        first = self.post(payload, event="installation", delivery="durable-installation")
        self.assertEqual(first.status_code, 202, first.text)
        self._restart_service()

        replay = self.post(payload, event="installation", delivery="durable-installation")
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertTrue(replay.json()["duplicate"])
        self.assertEqual(replay.json()["status"], "installation_active")
        self.assertEqual(
            self.service.store.database.github_app_installation(149747930)["state"], "active"
        )

    def test_legacy_hmac_webhook_remains_compatible(self) -> None:
        response = self.post(
            self.pull_request_payload(installation_id=None),
            event="pull_request",
            delivery="legacy-pr",
        )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertIn("review_id", response.json())
        self.assertFalse(response.json()["duplicate"])


if __name__ == "__main__":
    unittest.main()
