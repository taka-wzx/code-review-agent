import hashlib
import hmac
import json
from pathlib import Path
import tempfile
import time
import unittest

from fastapi.testclient import TestClient

from code_review_agent.service import HttpSettings, create_app
from code_review_agent.service_core import JobStore, RepositoryRegistry, ReviewService

from tests.test_week7_service_core import DIFF, FakeRunner


TOKEN = "service-token-that-is-at-least-32-bytes"
SECRET = "webhook-secret-at-least-16"


class Week7HttpServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()
        registry = RepositoryRegistry.from_json(
            json.dumps({"owner/repo": str(self.repo.resolve())})
        )
        self.runner = FakeRunner()
        self.service = ReviewService(registry, JobStore(self.root / "state"), runner=self.runner)
        settings = HttpSettings(
            service_token=TOKEN,
            webhook_secret=SECRET,
            allowed_origins=frozenset({"http://localhost"}),
            allowed_hosts=frozenset({"testserver"}),
        )
        self.app = create_app(settings=settings, review_service=self.service)
        self.client_context = TestClient(self.app)
        self.client = self.client_context.__enter__()
        self.auth = {"Authorization": f"Bearer {TOKEN}"}

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.temp.cleanup()

    def signed_headers(self, body: bytes, **extra):
        signature = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        return {
            "X-Hub-Signature-256": signature,
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "delivery-1",
            "Content-Type": "application/json",
            **extra,
        }

    def wait_terminal(self, job_id: str):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            response = self.client.get(f"/v1/reviews/{job_id}", headers=self.auth)
            if response.json()["state"] in {"succeeded", "failed"}:
                return response
            time.sleep(0.01)
        self.fail("job did not become terminal")

    def test_health_and_openapi_do_not_disclose_configuration(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertNotIn(TOKEN, response.text)
        schema = self.client.get("/openapi.json").json()
        self.assertIn("/v1/reviews/diff", schema["paths"])
        self.assertIn("/webhooks/github", schema["paths"])

    def test_bearer_auth_is_required_and_constant_shape(self):
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}):
            with self.subTest(headers=headers):
                response = self.client.get("/v1/reviews/" + "0" * 32, headers=headers)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["error"]["code"], "service_error")
                self.assertEqual(response.headers["www-authenticate"], "Bearer")
        missing = self.client.get("/v1/reviews/" + "0" * 32, headers=self.auth)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["error"]["code"], "job_not_found")

    def test_submit_diff_status_and_trace(self):
        response = self.client.post(
            "/v1/reviews/diff",
            headers=self.auth,
            json={"repository": "owner/repo", "diff": DIFF},
        )
        self.assertEqual(response.status_code, 202, response.text)
        job_id = response.json()["review_id"]
        done = self.wait_terminal(job_id)
        self.assertEqual(done.json()["review"]["summary"], "ok")
        trace = self.client.get(f"/v1/reviews/{job_id}/trace", headers=self.auth)
        self.assertEqual(trace.status_code, 200)
        self.assertEqual(trace.headers["content-type"].split(";")[0], "application/x-ndjson")
        self.assertIn("redacted", trace.text)

    def test_submission_validation_rejects_extra_fields_and_paths(self):
        cases = [
            {"repository": "owner/repo", "diff": "not a diff"},
            {"repository": "../repo", "diff": DIFF},
            {"repository": "owner/repo", "diff": DIFF, "path": "C:/host"},
        ]
        for body in cases:
            with self.subTest(body=body):
                response = self.client.post("/v1/reviews/diff", headers=self.auth, json=body)
                self.assertIn(response.status_code, {400, 422})
        self.assertEqual(self.runner.calls, [])

    def test_github_webhook_signature_idempotency_and_ignored_event(self):
        payload = {
            "action": "opened",
            "repository": {"full_name": "owner/repo"},
            "pull_request": {"number": 8},
        }
        body = json.dumps(payload, separators=(",", ":")).encode()
        headers = self.signed_headers(body)
        first = self.client.post("/webhooks/github", content=body, headers=headers)
        second = self.client.post("/webhooks/github", content=body, headers=headers)
        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(second.status_code, 202, second.text)
        self.assertFalse(first.json()["duplicate"])
        self.assertTrue(second.json()["duplicate"])
        self.assertEqual(first.json()["review_id"], second.json()["review_id"])
        self.wait_terminal(first.json()["review_id"])
        self.assertEqual(len(self.runner.calls), 1)

        ignored_headers = self.signed_headers(body, **{"X-GitHub-Event": "issues"})
        ignored = self.client.post("/webhooks/github", content=body, headers=ignored_headers)
        self.assertEqual(ignored.json()["status"], "ignored")

    def test_github_ping_and_bad_signature(self):
        body = b'{"zen":"safe"}'
        ping = self.client.post(
            "/webhooks/github",
            content=body,
            headers=self.signed_headers(body, **{"X-GitHub-Event": "ping"}),
        )
        self.assertEqual(ping.status_code, 200)
        self.assertEqual(ping.json()["status"], "pong")
        bad = self.client.post(
            "/webhooks/github",
            content=body,
            headers={
                "X-Hub-Signature-256": "sha256=" + "0" * 64,
                "X-GitHub-Event": "ping",
            },
        )
        self.assertEqual(bad.status_code, 401)
        malformed = b"not json"
        malformed_response = self.client.post(
            "/webhooks/github",
            content=malformed,
            headers=self.signed_headers(malformed),
        )
        self.assertEqual(malformed_response.status_code, 400)

    def test_webhook_rejects_bad_action_delivery_and_repository(self):
        cases = [
            ({"action": "closed", "repository": {"full_name": "owner/repo"}, "pull_request": {"number": 1}}, "d-1"),
            ({"action": "opened", "repository": {"full_name": "other/repo"}, "pull_request": {"number": 1}}, "d-2"),
            ({"action": "opened", "repository": {"full_name": "owner/repo"}, "pull_request": {"number": True}}, "d-3"),
        ]
        for payload, delivery in cases:
            body = json.dumps(payload).encode()
            headers = self.signed_headers(body, **{"X-GitHub-Delivery": delivery})
            with self.subTest(payload=payload):
                response = self.client.post("/webhooks/github", content=body, headers=headers)
                self.assertEqual(response.status_code, 400)

    def test_mcp_http_boundary_checks_auth_and_origin_before_protocol(self):
        no_auth = self.client.post("/mcp", json={})
        self.assertEqual(no_auth.status_code, 401)
        bad_origin = self.client.post(
            "/mcp",
            headers={**self.auth, "Origin": "https://evil.example"},
            json={},
        )
        self.assertEqual(bad_origin.status_code, 403)
        allowed = self.client.post(
            "/mcp",
            headers={**self.auth, "Origin": "http://localhost"},
            json={},
        )
        self.assertNotIn(allowed.status_code, {401, 403, 421})

    def test_http_settings_enforce_secret_lengths_and_origins(self):
        for kwargs in (
            {"service_token": "short", "webhook_secret": SECRET},
            {"service_token": TOKEN, "webhook_secret": "short"},
            {"service_token": TOKEN, "webhook_secret": SECRET, "allowed_origins": frozenset()},
            {"service_token": TOKEN, "webhook_secret": SECRET, "allowed_hosts": frozenset()},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(Exception):
                HttpSettings(**kwargs)


if __name__ == "__main__":
    unittest.main()
