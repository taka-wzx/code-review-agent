from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile
import tempfile
import unittest

import phase11c_gateb_freeze as freeze
import phase11c_gateb_headline_cohort_executor as protocol


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
SEAL_NOW = NOW - timedelta(minutes=2)


def _sha(character: str) -> str:
    return character * 64


def _materials() -> freeze.FreezeMaterials:
    return freeze.FreezeMaterials(
        executable_source_sha256=protocol.source_sha256(),
        executable_commit_sha="a" * 40,
        source_tree_sha256=_sha("1"),
        source_archive_sha256=_sha("2"),
        dockerfile_sha256=_sha("3"),
        compose_sha256=_sha("4"),
        image_sha256=_sha("5"),
        deployment_sha256=_sha("6"),
        runtime_identity_sha256=_sha("7"),
        provider_policy_evidence_sha256=_sha("8"),
        provider_tariff_evidence_sha256=_sha("9"),
        credential_fingerprint_sha256=hashlib.sha256(b"fake-key").hexdigest(),
    )


def _diagnostic_freeze() -> dict[str, object]:
    return freeze.freeze_diagnostic(
        materials=_materials(),
        window_start_utc=(NOW - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        window_end_utc=(NOW + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        policy_url="https://docs.bigmodel.cn/cn/terms/service-agreement",
        retention_policy_url="https://docs.bigmodel.cn/cn/terms/privacy-policy",
        policy_reviewed_at_utc="2030-01-02T03:00:00Z",
        tariff_observed_at_utc="2030-01-02T03:00:00Z",
        tariff_effective_date="2030-01-02",
        now_utc=SEAL_NOW,
    )


def _rendered_compose(image_sha256: str) -> str:
    source = (ROOT / "compose.phase11c-gateb-headline.yml").read_text(encoding="utf-8")
    return re.sub(
        r"\$\{PHASE11C_GATEB_HEADLINE_IMAGE[^}]*\}",
        "sha256:" + image_sha256,
        source,
    )


class FakeCredential:
    def read(self, expected_fingerprint: str, on_opened: object) -> str:
        assert expected_fingerprint == hashlib.sha256(b"fake-key").hexdigest()
        assert callable(on_opened)
        on_opened()
        return "fake-key"


class FakeTransport:
    def dispatch(self, api_key: str, request_body: bytes) -> protocol.HttpResult:
        assert api_key == "fake-key"
        payload = {
            "choices": [{"message": {"content": protocol.DIAGNOSTIC_TERMINAL_TOKEN}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 7},
        }
        return protocol.HttpResult(200, json.dumps(payload, separators=(",", ":")).encode("utf-8"))


class FreezeChainTests(unittest.TestCase):
    def test_diagnostic_freeze_is_deterministic_and_acyclic(self) -> None:
        first = _diagnostic_freeze()
        second = _diagnostic_freeze()
        self.assertEqual(first, second)
        authorization = first["authorization"]
        preflight = first["preflight"]
        execution_freeze = first["execution_freeze"]
        self.assertEqual(authorization["execution_freeze_sha256"], execution_freeze["execution_freeze_sha256"])
        self.assertEqual(preflight["execution_freeze_sha256"], execution_freeze["execution_freeze_sha256"])
        self.assertNotIn("authorization_sha256", preflight)
        self.assertNotIn("approval_binding_sha256", preflight)
        self.assertEqual(
            first["approval_binding_sha256"],
            protocol.diagnostic_approval_binding_sha256(authorization),
        )
        self.assertEqual(protocol.validate_diagnostic_authorization(authorization, now_utc=NOW), authorization)
        self.assertEqual(freeze.validate_preflight(preflight), preflight)
        self.assertEqual(freeze.validate_execution_freeze(execution_freeze), execution_freeze)

    def test_any_frozen_material_change_changes_authorization_id(self) -> None:
        original = _diagnostic_freeze()
        materials = _materials()
        changed = freeze.FreezeMaterials(
            **{**materials.__dict__, "image_sha256": _sha("f")}
        )
        updated = freeze.freeze_diagnostic(
            materials=changed,
            window_start_utc=(NOW - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            window_end_utc=(NOW + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            policy_url="https://docs.bigmodel.cn/cn/terms/service-agreement",
            retention_policy_url="https://docs.bigmodel.cn/cn/terms/privacy-policy",
            policy_reviewed_at_utc="2030-01-02T03:00:00Z",
            tariff_observed_at_utc="2030-01-02T03:00:00Z",
            tariff_effective_date="2030-01-02",
            now_utc=SEAL_NOW,
        )
        self.assertNotEqual(original["authorization"]["authorization_id"], updated["authorization"]["authorization_id"])
        self.assertNotEqual(original["execution_freeze"]["execution_freeze_sha256"], updated["execution_freeze"]["execution_freeze_sha256"])

    def test_headline_requires_same_image_diagnostic_lineage(self) -> None:
        diagnostic_freeze = _diagnostic_freeze()
        diagnostic_authorization = diagnostic_freeze["authorization"]
        binding = protocol.diagnostic_approval_binding_sha256(diagnostic_authorization)
        receipt = protocol.execute_diagnostic(
            diagnostic_authorization,
            protocol.expected_diagnostic_approval_text(binding),
            store=protocol.InMemoryDiagnosticStateStore(),
            credential_reader=FakeCredential(),
            transport=FakeTransport(),
            now_utc=NOW,
        )
        result = freeze.freeze_headline(
            materials=_materials(),
            window_start_utc=(NOW + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            window_end_utc=(NOW + timedelta(minutes=11)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            policy_url="https://docs.bigmodel.cn/cn/terms/service-agreement",
            retention_policy_url="https://docs.bigmodel.cn/cn/terms/privacy-policy",
            policy_reviewed_at_utc="2030-01-02T03:00:00Z",
            tariff_observed_at_utc="2030-01-02T03:00:00Z",
            tariff_effective_date="2030-01-02",
            diagnostic_authorization=diagnostic_authorization,
            diagnostic_receipt=receipt,
            diagnostic_execution_freeze=diagnostic_freeze["execution_freeze"],
            diagnostic_preflight=diagnostic_freeze["preflight"],
            now_utc=NOW,
        )
        self.assertEqual(result["authorization"]["diagnostic_receipt_sha256"], receipt["receipt_sha256"])
        self.assertNotEqual(result["authorization"]["execution_freeze_sha256"], diagnostic_authorization["execution_freeze_sha256"])
        with self.assertRaisesRegex(protocol.HeadlineCohortError, "diagnostic_freeze_binding_mismatch"):
            changed = freeze.FreezeMaterials(**{**_materials().__dict__, "image_sha256": _sha("f")})
            freeze.freeze_headline(
                materials=changed,
                window_start_utc=(NOW + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                window_end_utc=(NOW + timedelta(minutes=11)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                policy_url="https://docs.bigmodel.cn/cn/terms/service-agreement",
                retention_policy_url="https://docs.bigmodel.cn/cn/terms/privacy-policy",
                policy_reviewed_at_utc="2030-01-02T03:00:00Z",
                tariff_observed_at_utc="2030-01-02T03:00:00Z",
                tariff_effective_date="2030-01-02",
                diagnostic_authorization=diagnostic_authorization,
                diagnostic_receipt=receipt,
                diagnostic_execution_freeze=diagnostic_freeze["execution_freeze"],
                diagnostic_preflight=diagnostic_freeze["preflight"],
                now_utc=NOW,
            )

    def test_runtime_evidence_is_hashed_and_scoped(self) -> None:
        evidence = freeze.build_runtime_evidence(
            image_sha256="sha256:" + _sha("a"),
            instance_id=freeze.ALIYUN_INSTANCE_ID,
            region=freeze.ALIYUN_REGION,
            os_release_bytes=b"NAME=test\n",
            docker_server_bytes=b"{}",
            kernel_release="test-kernel",
        )
        self.assertEqual(freeze.validate_runtime_evidence(evidence, image_sha256=_sha("a")), evidence)
        with self.assertRaisesRegex(freeze.FreezeError, "runtime_identity_scope_mismatch"):
            freeze.build_runtime_evidence(
                image_sha256=_sha("a"),
                instance_id="wrong",
                region=freeze.ALIYUN_REGION,
                os_release_bytes=b"NAME=test\n",
                docker_server_bytes=b"{}",
                kernel_release="test-kernel",
            )
        wrong_instance = dict(evidence)
        wrong_instance["instance_id_sha256"] = _sha("b")
        with self.assertRaisesRegex(freeze.FreezeError, "runtime_evidence_instance_mismatch"):
            freeze.validate_runtime_evidence(wrong_instance, image_sha256=_sha("a"))
        wrong_region = dict(evidence)
        wrong_region["region_sha256"] = _sha("c")
        with self.assertRaisesRegex(freeze.FreezeError, "runtime_evidence_region_mismatch"):
            freeze.validate_runtime_evidence(wrong_region, image_sha256=_sha("a"))


class ComposeValidationTests(unittest.TestCase):
    def test_rendered_compose_binds_digest_on_every_actual_service(self) -> None:
        image = _sha("a")
        parsed = freeze._validate_rendered_compose(
            _rendered_compose(image).encode("utf-8"), image_sha256=image
        )
        self.assertEqual(set(parsed["services"]), {
            "gateb-protocol-diagnostic",
            "gateb-protocol-headline",
            "gateb-protocol-recovery",
            "gateb-protocol-diagnostic-recovery",
        })

        tampered = _rendered_compose(image).replace(
            'image: "sha256:' + image + '"',
            'image: "sha256:' + _sha("b") + '" # sha256:' + image,
            1,
        )
        with self.assertRaisesRegex(freeze.FreezeError, "rendered_deployment_image_mismatch"):
            freeze._validate_rendered_compose(tampered.encode("utf-8"), image_sha256=image)

        comment_only = _rendered_compose(image).replace(
            'image: "sha256:' + image + '"',
            "image: latest # sha256:" + image,
            1,
        )
        with self.assertRaisesRegex(freeze.FreezeError, "rendered_deployment_image_invalid"):
            freeze._validate_rendered_compose(comment_only.encode("utf-8"), image_sha256=image)

    def test_rendered_compose_rejects_network_mount_and_extension_drift(self) -> None:
        image = _sha("a")
        wrong_network = _rendered_compose(image).replace(
            "network_mode: bridge", "network_mode: none", 1
        )
        with self.assertRaisesRegex(freeze.FreezeError, "rendered_deployment_network_invalid"):
            freeze._validate_rendered_compose(wrong_network.encode("utf-8"), image_sha256=image)

        wrong_mount = _rendered_compose(image).replace(
            "target: /run/crag-gateb-diagnostic/approval.txt\n        read_only: true",
            "target: /run/crag-gateb-diagnostic/approval.txt",
            1,
        )
        with self.assertRaisesRegex(freeze.FreezeError, "rendered_deployment_mounts_invalid"):
            freeze._validate_rendered_compose(wrong_mount.encode("utf-8"), image_sha256=image)

        extra_field = _rendered_compose(image).replace(
            "services:\n", "services:\n  x-unrelated:\n    image: sha256:" + image + "\n", 1
        )
        with self.assertRaisesRegex(freeze.FreezeError, "rendered_deployment_services_invalid"):
            freeze._validate_rendered_compose(extra_field.encode("utf-8"), image_sha256=image)

    def test_rendered_compose_rejects_cross_stage_credentials_and_duplicate_keys(self) -> None:
        image = _sha("a")
        cross_stage = _rendered_compose(image).replace(
            "source: /run/crag-gateb-diagnostic/approval.txt\n        target: /run/crag-gateb-diagnostic/approval.txt\n        read_only: true",
            "source: /run/crag-gateb-headline/approval.txt\n        target: /run/crag-gateb-headline/approval.txt\n        read_only: true",
            1,
        )
        with self.assertRaisesRegex(freeze.FreezeError, "rendered_deployment_mounts_invalid"):
            freeze._validate_rendered_compose(cross_stage.encode("utf-8"), image_sha256=image)

        duplicate = _rendered_compose(image).replace(
            "    pull_policy: never\n", "    pull_policy: never\n    pull_policy: never\n", 1
        )
        with self.assertRaisesRegex(freeze.FreezeError, "rendered_deployment_duplicate_key"):
            freeze._validate_rendered_compose(duplicate.encode("utf-8"), image_sha256=image)


class ArtifactTests(unittest.TestCase):
    def test_schemas_are_strict_and_source_tree_is_fixed(self) -> None:
        for name in ("phase11c-gateb-execution-freeze.schema.json", "phase11c-gateb-preflight.schema.json"):
            document = json.loads((ROOT / "schemas" / name).read_text(encoding="utf-8"))
            self.assertFalse(document["additionalProperties"])
            self.assertIn("required", document)
        execution_schema = json.loads((ROOT / "schemas" / "phase11c-gateb-execution-freeze.schema.json").read_text(encoding="utf-8"))
        preflight_schema = json.loads((ROOT / "schemas" / "phase11c-gateb-preflight.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(set(execution_schema["required"]), set(freeze.EXECUTION_FREEZE_FIELDS))
        self.assertEqual(set(preflight_schema["required"]), set(freeze.PREFLIGHT_FIELDS))
        manifest = freeze.source_tree_manifest(ROOT)
        self.assertEqual(manifest["source_tree_sha256"], freeze.source_tree_manifest(ROOT)["source_tree_sha256"])
        self.assertEqual([item["path"] for item in manifest["files"]], list(freeze.EXECUTION_SOURCE_FILES))
        self.assertEqual(
            freeze.normalized_utf8_sha256(ROOT / "phase11c_gateb_headline_cohort_executor.py", code="test"),
            protocol.source_sha256(),
        )

    def test_source_archive_must_match_the_executable_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "source.tar.gz"
            with tarfile.open(archive, "w:gz") as output:
                for relative in freeze.EXECUTION_SOURCE_FILES:
                    output.add(ROOT / relative, arcname=relative, recursive=False)
            self.assertRegex(freeze.validate_source_archive(archive, source_root=ROOT), "^[0-9a-f]{64}$")
            extra = Path(directory) / "unrelated.txt"
            extra.write_text("unexpected", encoding="utf-8")
            with tarfile.open(archive, "w:gz") as output:
                for relative in freeze.EXECUTION_SOURCE_FILES:
                    output.add(ROOT / relative, arcname=relative, recursive=False)
                output.add(extra, arcname="unrelated.txt", recursive=False)
            with self.assertRaisesRegex(freeze.FreezeError, "source_archive_member_unexpected"):
                freeze.validate_source_archive(archive, source_root=ROOT)

    def test_source_archive_normalizes_cross_platform_line_endings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "normalized-source.tar.gz"
            with tarfile.open(archive, "w:gz") as output:
                for relative in freeze.EXECUTION_SOURCE_FILES:
                    content = (ROOT / relative).read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
                    member = tarfile.TarInfo(relative)
                    member.size = len(content)
                    output.addfile(member, io.BytesIO(content))
            self.assertRegex(freeze.validate_source_archive(archive, source_root=ROOT), "^[0-9a-f]{64}$")

    def test_freeze_output_matches_the_compose_control_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "diagnostic"
            freeze.write_freeze_output(output, _diagnostic_freeze(), stage="DIAGNOSTIC")
            self.assertEqual(
                {path.name for path in output.iterdir()},
                {
                    "authorization.json",
                    "runtime-config.json",
                    "tariff-manifest.json",
                    "preflight.json",
                    "execution-freeze.json",
                },
            )
            with self.assertRaisesRegex(freeze.FreezeError, "freeze_output_stage_invalid"):
                freeze.write_freeze_output(Path(directory) / "invalid", _diagnostic_freeze(), stage="invalid")

    def test_freeze_output_never_overwrites_a_sealed_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "sealed.json"
            freeze._write_document(destination, {"safe": True}, 0o600)
            with self.assertRaisesRegex(freeze.FreezeError, "freeze_output_already_exists"):
                freeze._write_document(destination, {"safe": True}, 0o600)

    def test_freeze_tool_has_no_provider_transport_or_shell_client(self) -> None:
        source = Path(freeze.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        self.assertTrue({"http", "socket", "ssl", "subprocess", "openai", "requests"}.isdisjoint(imported))
        self.assertNotIn("/run/crag-gateb-protocol/glm_api_key", source)


if __name__ == "__main__":
    unittest.main()
