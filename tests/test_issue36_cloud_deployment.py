from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "render_kubernetes_manifests.py"
IMAGE = "registry.example.invalid/code-review-agent@sha256:" + "a" * 64


def resource(document: dict[str, object], kind: str, name: str) -> dict[str, object]:
    for item in document["items"]:
        if item["kind"] == kind and item.get("metadata", {}).get("name") == name:
            return item
    raise AssertionError(f"missing {kind}/{name}")


class Issue36CloudDeploymentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.output = Path(self.temp.name) / "production.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def command(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def render(self, *, image: str = IMAGE) -> subprocess.CompletedProcess[str]:
        return self.command(
            "render",
            "--image",
            image,
            "--namespace",
            "crag-production",
            "--ingress-host",
            "api.example.test",
            "--runtime-config",
            "crag-runtime-config",
            "--runtime-secret",
            "crag-runtime-secrets",
            "--tls-secret",
            "api-example-test-tls",
            "--artifact-storage-class",
            "rwx-storage",
            "--output",
            str(self.output),
        )

    def lint(self, path: Path | None = None) -> subprocess.CompletedProcess[str]:
        return self.command("lint", "--input", str(path or self.output))

    def test_rendered_bundle_has_tls_probes_resources_and_external_secrets(self) -> None:
        rendered = self.render()
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        linted = self.lint()
        self.assertEqual(linted.returncode, 0, linted.stderr)

        document = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual((document["apiVersion"], document["kind"]), ("v1", "List"))
        self.assertNotIn("Secret", [item["kind"] for item in document["items"]])
        for kind, name in (("Deployment", "crag-api"), ("Deployment", "crag-worker")):
            workload = resource(document, kind, name)
            pod_spec = workload["spec"]["template"]["spec"]
            container = pod_spec["containers"][0]
            self.assertIn("requests", container["resources"])
            self.assertIn("limits", container["resources"])
            self.assertIn("livenessProbe", container)
            self.assertIn("readinessProbe", container)
            self.assertTrue(container["image"].endswith("a" * 64))
            config_names = [item["configMapRef"]["name"] for item in container["envFrom"]]
            self.assertIn("crag-service-defaults", config_names)
            self.assertIn("crag-runtime-config", config_names)
            secret_volume = next(volume for volume in pod_spec["volumes"] if volume["name"] == "runtime-secrets")
            self.assertEqual(secret_volume["secret"]["secretName"], "crag-runtime-secrets")
            self.assertEqual(
                {item["key"] for item in secret_volume["secret"]["items"]},
                {"database_password", "webhook_secret", "service_token"},
            )
        ingress = resource(document, "Ingress", "crag-api")
        self.assertEqual(ingress["spec"]["rules"][0]["host"], "api.example.test")
        self.assertEqual(ingress["spec"]["tls"][0]["secretName"], "api-example-test-tls")
        claim = resource(document, "PersistentVolumeClaim", "crag-artifacts")
        self.assertIn("ReadWriteMany", claim["spec"]["accessModes"])

    def test_renderer_rejects_mutable_image_without_writing_output(self) -> None:
        rendered = self.render(image="registry.example.invalid/code-review-agent:latest")
        self.assertNotEqual(rendered.returncode, 0)
        self.assertIn("immutable sha256 digest", rendered.stderr)
        self.assertFalse(self.output.exists())

    def test_linter_rejects_tampered_image(self) -> None:
        self.assertEqual(self.render().returncode, 0)
        document = json.loads(self.output.read_text(encoding="utf-8"))
        resource(document, "Deployment", "crag-api")["spec"]["template"]["spec"]["containers"][0][
            "image"
        ] = "registry.example.invalid/code-review-agent:latest"
        self.output.write_text(json.dumps(document), encoding="utf-8")

        linted = self.lint()
        self.assertEqual(linted.returncode, 1)
        self.assertIn("immutable sha256 digests", linted.stderr)

    def test_linter_rejects_missing_tls_and_api_readiness_probe(self) -> None:
        self.assertEqual(self.render().returncode, 0)
        document = json.loads(self.output.read_text(encoding="utf-8"))
        resource(document, "Ingress", "crag-api")["spec"]["tls"] = []
        del resource(document, "Deployment", "crag-api")["spec"]["template"]["spec"][
            "containers"
        ][0]["readinessProbe"]
        self.output.write_text(json.dumps(document), encoding="utf-8")

        linted = self.lint()
        self.assertEqual(linted.returncode, 1)
        self.assertIn("Ingress must define TLS", linted.stderr)
        self.assertIn("crag-api readiness probe", linted.stderr)


if __name__ == "__main__":
    unittest.main()
