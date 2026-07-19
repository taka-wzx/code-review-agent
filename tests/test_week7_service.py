from contextlib import redirect_stdout
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import anyio
from fastapi.testclient import TestClient
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from code_review_agent.service import MAX_WEBHOOK_BYTES, HttpSettings, build_parser, create_app
from code_review_agent.service_core import (
    MAX_DIFF_BYTES,
    JobStore,
    RepositoryRegistry,
    ReviewService,
)

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
        self.registry = RepositoryRegistry.from_json(
            json.dumps({"owner/repo": str(self.repo.resolve())})
        )
        self.runner = FakeRunner()
        self.service = ReviewService(
            self.registry, JobStore(self.root / "state"), runner=self.runner
        )
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

    def test_validation_error_never_echoes_the_submitted_diff(self):
        marker = "SENSITIVE-DIFF-CONTENT"
        submitted = marker + "x" * MAX_DIFF_BYTES
        response = self.client.post(
            "/v1/reviews/diff",
            headers=self.auth,
            json={"repository": "owner/repo", "diff": submitted},
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "validation_error")
        self.assertNotIn(marker, response.text)
        self.assertLess(len(response.content), 1024)

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

    def test_webhook_requires_delivery_id_for_pull_requests(self):
        payload = {
            "action": "opened",
            "repository": {"full_name": "owner/repo"},
            "pull_request": {"number": 8},
        }
        body = json.dumps(payload).encode()
        headers = self.signed_headers(body)
        headers.pop("X-GitHub-Delivery")
        response = self.client.post("/webhooks/github", content=body, headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.runner.calls, [])

    def test_chunked_oversized_webhook_is_rejected_while_streaming(self):
        chunks = [b"x" * (MAX_WEBHOOK_BYTES // 2), b"y" * (MAX_WEBHOOK_BYTES // 2 + 1)]
        headers = {
            "X-Hub-Signature-256": "sha256=" + "0" * 64,
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "oversized-delivery",
            "Content-Type": "application/json",
        }
        response = self.client.post(
            "/webhooks/github", content=iter(chunks), headers=headers
        )
        self.assertEqual(response.status_code, 413, response.text)
        self.assertEqual(response.json()["error"]["code"], "payload_too_large")
        self.assertEqual(self.runner.calls, [])

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

        bad_host = self.client.post(
            "/mcp/",
            headers={**self.auth, "Origin": "http://localhost", "Host": "evil.example"},
            json={},
        )
        self.assertEqual(bad_host.status_code, 421)

    def test_official_client_connects_over_mounted_streamable_http(self):
        runner = FakeRunner()
        service = ReviewService(
            self.registry, JobStore(self.root / "mcp-http-state"), runner=runner
        )
        settings = HttpSettings(
            service_token=TOKEN,
            webhook_secret=SECRET,
            allowed_origins=frozenset({"http://localhost"}),
            allowed_hosts=frozenset({"testserver"}),
        )
        app = create_app(settings=settings, review_service=service)

        async def exercise():
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://testserver",
                    headers={
                        "Authorization": f"Bearer {TOKEN}",
                        "Origin": "http://localhost",
                    },
                ) as client:
                    async with streamable_http_client(
                        "http://testserver/mcp/", http_client=client
                    ) as (read, write, _):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            tools = await session.list_tools()
                            self.assertEqual(
                                {tool.name for tool in tools.tools},
                                {"review_diff", "review_pr", "get_review_status"},
                            )

        anyio.run(exercise)

    def test_http_settings_enforce_secret_lengths_and_origins(self):
        for kwargs in (
            {"service_token": "short", "webhook_secret": SECRET},
            {"service_token": TOKEN, "webhook_secret": "short"},
            {"service_token": TOKEN, "webhook_secret": SECRET, "allowed_origins": frozenset()},
            {"service_token": TOKEN, "webhook_secret": SECRET, "allowed_hosts": frozenset()},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(Exception):
                HttpSettings(**kwargs)

    def test_invalid_port_environment_does_not_break_help(self):
        output = io.StringIO()
        with patch.dict(os.environ, {"CRAG_SERVICE_PORT": "abc"}):
            with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
                build_parser().parse_args(["--help"])
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("--port", output.getvalue())


if __name__ == "__main__":
    unittest.main()
