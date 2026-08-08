import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from code_review_agent.identity import FakeAuthBackend, Principal, Role
from code_review_agent.service import HttpSettings, create_app
from code_review_agent.service_core import JobStore, RepositoryRegistry, ReviewService


class Issue39AdminUITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        checkout = root / "repo"
        checkout.mkdir()
        (checkout / ".git").mkdir()
        registry = RepositoryRegistry.from_json(
            json.dumps({"owner/repo": str(checkout.resolve())})
        )
        store = JobStore(root / "state")
        database = store.database
        organization = database.create_organization("org-a", "Organization A")
        repository = database.register_repository(
            organization["id"], "owner/repo", policy_version="policy/a"
        )
        admin_membership = database.create_membership(
            organization["id"],
            subject="admin",
            display_name="Admin",
            role=Role.ORG_ADMIN,
            repository_ids=(),
        )
        viewer_membership = database.create_membership(
            organization["id"],
            subject="viewer",
            display_name="Viewer",
            role=Role.VIEWER,
            repository_ids=(repository["id"],),
        )
        admin = database.principal_for_user(organization["id"], admin_membership["user_id"])
        viewer = database.principal_for_user(organization["id"], viewer_membership["user_id"])
        assert isinstance(admin, Principal)
        assert isinstance(viewer, Principal)
        self.organization_id = organization["id"]
        self.service = ReviewService(registry, store, runner=None, local_mode=False)
        self.client_context = TestClient(
            create_app(
                settings=HttpSettings(
                    service_token="",
                    webhook_secret="issue39-webhook-secret",
                    allowed_origins=frozenset({"http://localhost"}),
                    allowed_hosts=frozenset({"testserver"}),
                    local_token_enabled=False,
                ),
                review_service=self.service,
                auth_backend=FakeAuthBackend(
                    {"admin-token": admin, "viewer-token": viewer}
                ),
            )
        )
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.temp.cleanup()

    @staticmethod
    def auth(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def test_admin_shell_and_assets_are_same_origin_and_non_cacheable(self) -> None:
        page = self.client.get("/admin")
        css = self.client.get("/admin/assets/app.css")
        script = self.client.get("/admin/assets/app.js")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(css.status_code, 200)
        self.assertEqual(script.status_code, 200)
        self.assertEqual(page.headers["cache-control"], "no-store")
        self.assertIn("default-src 'self'", page.headers["content-security-policy"])
        self.assertIn("/v1/principal", script.text)
        self.assertIn('data-role="org_admin"', page.text)
        self.assertIn("sessionStorage", script.text)
        self.assertNotIn("localStorage", script.text)
        self.assertNotIn('byId("token-input").value = state.token', script.text)
        self.assertIn("window.confirm", script.text)
        self.assertIn("payload_sha256: record.payload_sha256", script.text)
        self.assertIn("nonce: record.nonce", script.text)
        self.assertIn("if (!target || !tab || tab.hidden) return", script.text)
        self.assertIn("if (!saved) return", script.text)
        self.assertNotIn("https://", script.text)
        self.assertNotIn("Bearer ", page.text)

    def test_console_login_endpoint_remains_authenticated(self) -> None:
        self.assertEqual(self.client.get("/v1/principal").status_code, 401)
        principal = self.client.get("/v1/principal", headers=self.auth("admin-token"))
        self.assertEqual(principal.status_code, 200)
        self.assertEqual(principal.json()["role"], "org_admin")

    def test_server_side_rbac_rejects_viewer_configuration_write(self) -> None:
        path = f"/v1/organizations/{self.organization_id}/repositories"
        response = self.client.post(
            path,
            headers=self.auth("viewer-token"),
            json={"repository": "owner/repo", "mode": "shadow", "policy_version": "v1"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    def test_admin_can_reach_configuration_view_but_cross_org_is_not_found(self) -> None:
        path = f"/v1/organizations/{self.organization_id}/repositories"
        response = self.client.get(path, headers=self.auth("admin-token"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["repositories"]), 1)
        cross_org = self.client.get(
            "/v1/organizations/org-other/repositories",
            headers=self.auth("admin-token"),
        )
        self.assertEqual(cross_org.status_code, 404)


if __name__ == "__main__":
    unittest.main()
