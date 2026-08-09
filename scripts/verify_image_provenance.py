"""Verify a deployed image digest has the expected Cosign signature and provenance."""
from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Callable, Iterable, Mapping


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "supply_chain" / "image-verification-policy.json"
_DIGEST_REFERENCE = re.compile(
    r"^(?P<repository>[a-z0-9][a-z0-9._/-]*)@sha256:(?P<digest>[a-f0-9]{64})$"
)
_REQUIRED_POLICY_FIELDS = frozenset(
    {
        "schema_version",
        "image_repository",
        "certificate_oidc_issuer",
        "certificate_identity_regexp",
        "provenance_predicate_type",
    }
)


class VerificationError(RuntimeError):
    """Raised for a safe, non-sensitive deploy-time verification failure."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str


@dataclass(frozen=True)
class VerificationPolicy:
    image_repository: str
    certificate_oidc_issuer: str
    certificate_identity_regexp: str
    provenance_predicate_type: str


CommandRunner = Callable[[tuple[str, ...]], CommandResult]


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerificationError("verification policy could not be loaded") from exc
    if not isinstance(value, dict):
        raise VerificationError("verification policy must be a JSON object")
    return value


def load_policy(path: Path = DEFAULT_POLICY) -> VerificationPolicy:
    value = _load_json(path)
    if set(value) != _REQUIRED_POLICY_FIELDS:
        raise VerificationError("verification policy fields are invalid")
    if value.get("schema_version") != "crag.supply-chain-policy/v1":
        raise VerificationError("verification policy version is unsupported")
    image_repository = value.get("image_repository")
    issuer = value.get("certificate_oidc_issuer")
    identity_regexp = value.get("certificate_identity_regexp")
    predicate_type = value.get("provenance_predicate_type")
    if not all(isinstance(item, str) and item for item in (
        image_repository,
        issuer,
        identity_regexp,
        predicate_type,
    )):
        raise VerificationError("verification policy values are invalid")
    if _DIGEST_REFERENCE.fullmatch(f"{image_repository}@sha256:{'a' * 64}") is None:
        raise VerificationError("verification policy image repository is invalid")
    try:
        re.compile(identity_regexp)
    except re.error as exc:
        raise VerificationError("verification policy identity expression is invalid") from exc
    if issuer != "https://token.actions.githubusercontent.com":
        raise VerificationError("verification policy issuer is invalid")
    if predicate_type != "https://slsa.dev/provenance/v1":
        raise VerificationError("verification policy provenance type is invalid")
    return VerificationPolicy(
        image_repository=image_repository,
        certificate_oidc_issuer=issuer,
        certificate_identity_regexp=identity_regexp,
        provenance_predicate_type=predicate_type,
    )


def parse_image_reference(image: str, policy: VerificationPolicy) -> tuple[str, str]:
    match = _DIGEST_REFERENCE.fullmatch(image)
    if match is None:
        raise VerificationError("image must be an immutable sha256 digest reference")
    repository = match.group("repository")
    digest = match.group("digest")
    if repository != policy.image_repository:
        raise VerificationError("image repository is not allowed by the verification policy")
    return repository, digest


def _run_cosign(command: tuple[str, ...]) -> CommandResult:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VerificationError("cosign verification command could not complete") from exc
    if completed.returncode != 0:
        raise VerificationError("cosign verification failed")
    return CommandResult(returncode=completed.returncode, stdout=completed.stdout)


def _records(result: CommandResult) -> list[dict[str, Any]]:
    if result.returncode != 0:
        raise VerificationError("cosign verification failed")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise VerificationError("cosign verification output is malformed") from exc
    raw_records = value if isinstance(value, list) else [value]
    if not raw_records or not all(isinstance(record, dict) for record in raw_records):
        raise VerificationError("cosign verification output is empty")
    return raw_records


def _verify_signature(
    records: Iterable[Mapping[str, Any]],
    *,
    repository: str,
    digest: str,
    policy: VerificationPolicy,
) -> None:
    expected_digest = f"sha256:{digest}"
    for record in records:
        critical = record.get("critical")
        optional = record.get("optional")
        if not isinstance(critical, Mapping) or not isinstance(optional, Mapping):
            continue
        identity = critical.get("identity")
        image = critical.get("image")
        if not isinstance(identity, Mapping) or not isinstance(image, Mapping):
            continue
        if identity.get("docker-reference") != repository:
            continue
        if image.get("docker-manifest-digest") != expected_digest:
            continue
        issuer = optional.get("Issuer")
        subject = optional.get("Subject")
        if issuer != policy.certificate_oidc_issuer or not isinstance(subject, str):
            continue
        if re.fullmatch(policy.certificate_identity_regexp, subject) is None:
            continue
        return
    raise VerificationError("image signature does not satisfy the verification policy")


def _statement(record: Mapping[str, Any]) -> Mapping[str, Any]:
    if "predicateType" in record:
        return record
    payload = record.get("payload")
    if not isinstance(payload, str):
        raise VerificationError("provenance statement is missing")
    try:
        decoded = base64.b64decode(payload, validate=True)
        statement = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerificationError("provenance statement is malformed") from exc
    if not isinstance(statement, Mapping):
        raise VerificationError("provenance statement is malformed")
    return statement


def _verify_provenance(
    records: Iterable[Mapping[str, Any]],
    *,
    repository: str,
    digest: str,
    policy: VerificationPolicy,
) -> None:
    for record in records:
        statement = _statement(record)
        if statement.get("predicateType") != policy.provenance_predicate_type:
            continue
        subjects = statement.get("subject")
        if not isinstance(subjects, list):
            continue
        for subject in subjects:
            if not isinstance(subject, Mapping):
                continue
            if subject.get("name") != repository:
                continue
            subject_digest = subject.get("digest")
            if isinstance(subject_digest, Mapping) and subject_digest.get("sha256") == digest:
                return
    raise VerificationError("image provenance does not bind the deployed digest")


def verify_image(
    image: str,
    *,
    policy: VerificationPolicy,
    runner: CommandRunner = _run_cosign,
) -> dict[str, Any]:
    """Verify an immutable image with Cosign outputs supplied by ``runner``."""
    repository, digest = parse_image_reference(image, policy)
    common = (
        "--output",
        "json",
        "--certificate-oidc-issuer",
        policy.certificate_oidc_issuer,
        "--certificate-identity-regexp",
        policy.certificate_identity_regexp,
        image,
    )
    signature = runner(("cosign", "verify", *common))
    _verify_signature(
        _records(signature),
        repository=repository,
        digest=digest,
        policy=policy,
    )
    provenance = runner(("cosign", "verify-attestation", "--type", "slsaprovenance", *common))
    _verify_provenance(
        _records(provenance),
        repository=repository,
        digest=digest,
        policy=policy,
    )
    return {
        "schema_version": "crag.supply-chain-verification/v1",
        "image_digest": f"sha256:{digest}",
        "signature_verified": True,
        "provenance_verified": True,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="immutable image reference using @sha256:<digest>")
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    result = verify_image(args.image, policy=load_policy(args.policy))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
