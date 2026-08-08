from __future__ import annotations

import base64
import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import verify_image_provenance as verifier  # noqa: E402


DIGEST = "a" * 64
IMAGE = f"ghcr.io/taka-wzx/code-review-agent@sha256:{DIGEST}"
IDENTITY = (
    "https://github.com/taka-wzx/code-review-agent/"
    ".github/workflows/supply-chain.yml@refs/tags/v1.2.3"
)


class RecordingRunner:
    def __init__(self, signature: object, provenance: object) -> None:
        self.signature = signature
        self.provenance = provenance
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, command: tuple[str, ...]) -> verifier.CommandResult:
        self.calls.append(command)
        value = self.signature if command[1] == "verify" else self.provenance
        return verifier.CommandResult(returncode=0, stdout=json.dumps(value))


def signature_record(*, digest: str = DIGEST, issuer: str = "https://token.actions.githubusercontent.com") -> dict:
    return {
        "critical": {
            "identity": {"docker-reference": "ghcr.io/taka-wzx/code-review-agent"},
            "image": {"docker-manifest-digest": f"sha256:{digest}"},
        },
        "optional": {"Issuer": issuer, "Subject": IDENTITY},
    }


def provenance_record(*, digest: str = DIGEST) -> dict:
    statement = {
        "predicateType": "https://slsa.dev/provenance/v1",
        "subject": [
            {
                "name": "ghcr.io/taka-wzx/code-review-agent",
                "digest": {"sha256": digest},
            }
        ],
    }
    return {"payload": base64.b64encode(json.dumps(statement).encode("utf-8")).decode("ascii")}


class Issue37ImageProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = verifier.load_policy()

    def test_valid_keyless_signature_and_provenance_are_accepted(self) -> None:
        runner = RecordingRunner([signature_record()], [provenance_record()])

        result = verifier.verify_image(IMAGE, policy=self.policy, runner=runner)

        self.assertEqual(result["image_digest"], f"sha256:{DIGEST}")
        self.assertTrue(result["signature_verified"])
        self.assertTrue(result["provenance_verified"])
        self.assertEqual([call[1] for call in runner.calls], ["verify", "verify-attestation"])
        self.assertTrue(all("--certificate-oidc-issuer" in call for call in runner.calls))
        self.assertTrue(all("--certificate-identity-regexp" in call for call in runner.calls))

    def test_mutable_or_foreign_image_is_rejected_before_cosign(self) -> None:
        runner = RecordingRunner([signature_record()], [provenance_record()])
        for image in (
            "ghcr.io/taka-wzx/code-review-agent:v1.2.3",
            f"ghcr.io/other/image@sha256:{DIGEST}",
            "ghcr.io/taka-wzx/code-review-agent@sha256:short",
        ):
            with self.subTest(image=image), self.assertRaisesRegex(verifier.VerificationError, "image"):
                verifier.verify_image(image, policy=self.policy, runner=runner)
        self.assertEqual(runner.calls, [])

    def test_tampered_signature_digest_and_bad_issuer_are_rejected(self) -> None:
        for record in (
            signature_record(digest="b" * 64),
            signature_record(issuer="https://issuer.example.invalid"),
        ):
            runner = RecordingRunner([record], [provenance_record()])
            with self.subTest(record=record), self.assertRaisesRegex(
                verifier.VerificationError, "signature"
            ):
                verifier.verify_image(IMAGE, policy=self.policy, runner=runner)

    def test_tampered_or_missing_provenance_is_rejected(self) -> None:
        for records in ([provenance_record(digest="b" * 64)], [{}]):
            runner = RecordingRunner([signature_record()], records)
            with self.subTest(records=records), self.assertRaisesRegex(
                verifier.VerificationError, "provenance"
            ):
                verifier.verify_image(IMAGE, policy=self.policy, runner=runner)

    def test_policy_and_workflow_are_digest_bound_and_sha_pinned(self) -> None:
        workflow = (ROOT / ".github/workflows/supply-chain.yml").read_text(encoding="utf-8")

        self.assertNotIn("pull_request", workflow)
        self.assertIn('tags:\n      - "v*"', workflow)
        self.assertIn("if: startsWith(github.ref, 'refs/tags/v')", workflow)
        self.assertIn("id-token: write", workflow)
        self.assertIn("attestations: write", workflow)
        self.assertIn("Dockerfile.service", workflow)
        self.assertIn('cosign sign --yes "${IMAGE_NAME}@${IMAGE_DIGEST}"', workflow)
        self.assertIn("subject-digest: ${{ steps.build.outputs.digest }}", workflow)
        expected_actions = {
            "actions/checkout",
            "docker/login-action",
            "docker/setup-buildx-action",
            "docker/build-push-action",
            "sigstore/cosign-installer",
            "actions/attest-build-provenance",
        }
        found_actions = set()
        for line in workflow.splitlines():
            stripped = line.strip().removeprefix("- ")
            if not stripped.startswith("uses: "):
                continue
            action, revision = stripped.removeprefix("uses: ").split("@", 1)
            found_actions.add(action)
            self.assertRegex(revision.split()[0], r"^[0-9a-f]{40}$")
        self.assertEqual(found_actions, expected_actions)
        self.assertTrue(
            self.policy.certificate_identity_regexp.startswith(
                "^https://github\\.com/taka-wzx/code-review-agent/"
            )
        )


if __name__ == "__main__":
    unittest.main()
