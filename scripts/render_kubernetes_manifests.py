"""Render and lint the offline Kubernetes production deployment bundle."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Mapping


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEMPLATE = ROOT / "deploy" / "kubernetes" / "production.template.json"
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_IMAGE = re.compile(r"[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}\Z")
_PLACEHOLDER = re.compile(r"__[A-Z_]+__")


class ManifestError(ValueError):
    """A stable render or lint error that contains no secret material."""


def _dns_label(value: str, field: str) -> str:
    if not isinstance(value, str) or _DNS_LABEL.fullmatch(value) is None:
        raise ManifestError(f"{field} must be a lowercase DNS label")
    return value


def _hostname(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 253:
        raise ManifestError("ingress host is invalid")
    labels = value.split(".")
    if len(labels) < 2 or any(_DNS_LABEL.fullmatch(label) is None for label in labels):
        raise ManifestError("ingress host must be a DNS hostname")
    return value


def _image_digest(value: str) -> str:
    if not isinstance(value, str) or _IMAGE.fullmatch(value) is None:
        raise ManifestError("image must be a lowercase immutable sha256 digest reference")
    return value


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError("manifest JSON is unreadable") from exc
    if not isinstance(value, dict):
        raise ManifestError("manifest root must be an object")
    return value


def _contains_placeholder(value: object) -> bool:
    if isinstance(value, str):
        return _PLACEHOLDER.search(value) is not None
    if isinstance(value, Mapping):
        return any(_contains_placeholder(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_placeholder(item) for item in value)
    return False


def render_manifest(
    *,
    template: Path,
    image: str,
    namespace: str,
    ingress_host: str,
    runtime_config: str,
    runtime_secret: str,
    tls_secret: str,
    artifact_storage_class: str,
) -> dict[str, Any]:
    values = {
        "__IMAGE__": _image_digest(image),
        "__NAMESPACE__": _dns_label(namespace, "namespace"),
        "__INGRESS_HOST__": _hostname(ingress_host),
        "__RUNTIME_CONFIG__": _dns_label(runtime_config, "runtime config name"),
        "__RUNTIME_SECRET__": _dns_label(runtime_secret, "runtime secret name"),
        "__TLS_SECRET__": _dns_label(tls_secret, "TLS secret name"),
        "__ARTIFACT_STORAGE_CLASS__": _dns_label(
            artifact_storage_class, "artifact storage class"
        ),
    }
    try:
        rendered = template.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ManifestError("template is unreadable") from exc
    for placeholder, value in values.items():
        if placeholder not in rendered:
            raise ManifestError("template is missing a required placeholder")
        rendered = rendered.replace(placeholder, value)
    if _PLACEHOLDER.search(rendered) is not None:
        raise ManifestError("template contains an unresolved placeholder")
    try:
        document = json.loads(rendered)
    except json.JSONDecodeError as exc:
        raise ManifestError("rendered template is not valid JSON") from exc
    if not isinstance(document, dict):
        raise ManifestError("rendered manifest root must be an object")
    errors = lint_manifest(document)
    if errors:
        raise ManifestError("rendered manifest failed lint: " + "; ".join(errors))
    return document


def _mapping(value: object, location: str, errors: list[str]) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    errors.append(f"{location} must be an object")
    return {}


def _items(document: Mapping[str, Any], errors: list[str]) -> list[dict[str, Any]]:
    if document.get("apiVersion") != "v1" or document.get("kind") != "List":
        errors.append("root must be a v1 List")
    raw_items = document.get("items")
    if not isinstance(raw_items, list):
        errors.append("root items must be a list")
        return []
    items: list[dict[str, Any]] = []
    for index, item in enumerate(raw_items):
        items.append(_mapping(item, f"items[{index}]", errors))
    return items


def _resource_index(items: Iterable[Mapping[str, Any]]) -> dict[tuple[str, str], Mapping[str, Any]]:
    resources: dict[tuple[str, str], Mapping[str, Any]] = {}
    for item in items:
        metadata = item.get("metadata")
        if not isinstance(metadata, Mapping):
            continue
        kind = item.get("kind")
        name = metadata.get("name")
        if isinstance(kind, str) and isinstance(name, str):
            resources[(kind, name)] = item
    return resources


def _container(
    deployment: Mapping[str, Any], deployment_name: str, errors: list[str]
) -> Mapping[str, Any]:
    pod_spec = _pod_spec(deployment, deployment_name, errors)
    containers = pod_spec.get("containers")
    if not isinstance(containers, list) or len(containers) != 1:
        errors.append(f"{deployment_name} must have exactly one container")
        return {}
    return _mapping(containers[0], f"{deployment_name}.container", errors)


def _pod_spec(
    deployment: Mapping[str, Any], deployment_name: str, errors: list[str]
) -> Mapping[str, Any]:
    spec = _mapping(deployment.get("spec"), f"{deployment_name}.spec", errors)
    template = _mapping(spec.get("template"), f"{deployment_name}.template", errors)
    return _mapping(template.get("spec"), f"{deployment_name}.pod spec", errors)


def _probe_path(container: Mapping[str, Any], probe: str) -> str | None:
    value = container.get(probe)
    if not isinstance(value, Mapping):
        return None
    http_get = value.get("httpGet")
    if not isinstance(http_get, Mapping):
        return None
    path = http_get.get("path")
    return path if isinstance(path, str) else None


def _worker_probe(container: Mapping[str, Any], probe: str) -> bool:
    value = container.get(probe)
    if not isinstance(value, Mapping):
        return False
    command = value.get("exec", {}).get("command") if isinstance(value.get("exec"), Mapping) else None
    return isinstance(command, list) and command[-1:] == ["--check"]


def _images(value: object) -> list[str]:
    result: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key == "image" and isinstance(child, str):
                result.append(child)
            result.extend(_images(child))
    elif isinstance(value, list):
        for child in value:
            result.extend(_images(child))
    return result


def _has_secret_reference(container: Mapping[str, Any]) -> bool:
    mounts = container.get("volumeMounts")
    return isinstance(mounts, list) and any(
        isinstance(mount, Mapping) and mount.get("name") == "runtime-secrets"
        for mount in mounts
    )


def _has_runtime_config(container: Mapping[str, Any]) -> bool:
    env_from = container.get("envFrom")
    if not isinstance(env_from, list):
        return False
    names = {
        item.get("configMapRef", {}).get("name")
        for item in env_from
        if isinstance(item, Mapping) and isinstance(item.get("configMapRef"), Mapping)
    }
    return "crag-service-defaults" in names and len(names - {"crag-service-defaults"}) == 1


def _has_runtime_secret_volume(pod_spec: Mapping[str, Any]) -> bool:
    volumes = pod_spec.get("volumes")
    if not isinstance(volumes, list):
        return False
    for volume in volumes:
        if not isinstance(volume, Mapping) or volume.get("name") != "runtime-secrets":
            continue
        secret = volume.get("secret")
        if not isinstance(secret, Mapping) or not isinstance(secret.get("secretName"), str):
            continue
        items = secret.get("items")
        if not isinstance(items, list):
            continue
        keys = {
            item.get("key")
            for item in items
            if isinstance(item, Mapping) and isinstance(item.get("key"), str)
        }
        if {"database_password", "webhook_secret", "service_token"}.issubset(keys):
            return bool(secret["secretName"])
    return False


def _validate_security(container: Mapping[str, Any], name: str, errors: list[str]) -> None:
    security = container.get("securityContext")
    if not isinstance(security, Mapping):
        errors.append(f"{name} lacks container securityContext")
        return
    if security.get("allowPrivilegeEscalation") is not False:
        errors.append(f"{name} must disable privilege escalation")
    if security.get("readOnlyRootFilesystem") is not True:
        errors.append(f"{name} must use a read-only root filesystem")
    capabilities = security.get("capabilities")
    dropped = capabilities.get("drop") if isinstance(capabilities, Mapping) else None
    if not isinstance(dropped, list) or "ALL" not in dropped:
        errors.append(f"{name} must drop all Linux capabilities")


def _validate_resources(container: Mapping[str, Any], name: str, errors: list[str]) -> None:
    resources = container.get("resources")
    if not isinstance(resources, Mapping):
        errors.append(f"{name} lacks resource requests and limits")
        return
    for section in ("requests", "limits"):
        values = resources.get(section)
        if not isinstance(values, Mapping) or not values.get("cpu") or not values.get("memory"):
            errors.append(f"{name} lacks {section} CPU or memory")


def lint_manifest(document: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if _contains_placeholder(document):
        errors.append("manifest contains an unresolved placeholder")
    items = _items(document, errors)
    resources = _resource_index(items)
    required = {
        ("Namespace", None),
        ("ServiceAccount", "crag-service"),
        ("ConfigMap", "crag-service-defaults"),
        ("PersistentVolumeClaim", "crag-artifacts"),
        ("Deployment", "crag-api"),
        ("Service", "crag-api"),
        ("Deployment", "crag-worker"),
        ("Job", "crag-migrate"),
        ("NetworkPolicy", "crag-api-ingress"),
        ("PodDisruptionBudget", "crag-api"),
        ("Ingress", "crag-api"),
    }
    namespaced: list[Mapping[str, Any]] = []
    namespace = ""
    for item in items:
        kind = item.get("kind")
        metadata = item.get("metadata")
        if kind == "Secret":
            errors.append("manifest must not commit a Secret resource")
        if kind == "Namespace" and isinstance(metadata, Mapping):
            value = metadata.get("name")
            if isinstance(value, str):
                namespace = value
        if kind != "Namespace":
            namespaced.append(item)
    if not namespace:
        errors.append("manifest lacks a Namespace")
    for kind, name in required:
        if kind == "Namespace":
            if not namespace:
                errors.append("Namespace resource is required")
        elif (kind, name or "") not in resources:
            errors.append(f"missing {kind}/{name}")
    for resource_item in namespaced:
        metadata = resource_item.get("metadata")
        if isinstance(metadata, Mapping) and metadata.get("namespace") != namespace:
            errors.append("all resources must use the declared namespace")

    for image in _images(document):
        if _IMAGE.fullmatch(image) is None:
            errors.append("all workload images must be immutable sha256 digests")
            break

    api = resources.get(("Deployment", "crag-api"))
    worker = resources.get(("Deployment", "crag-worker"))
    if api is not None:
        api_container = _container(api, "crag-api", errors)
        api_pod_spec = _pod_spec(api, "crag-api", errors)
        _validate_resources(api_container, "crag-api", errors)
        _validate_security(api_container, "crag-api", errors)
        if _probe_path(api_container, "livenessProbe") != "/healthz":
            errors.append("crag-api liveness probe must target /healthz")
        if _probe_path(api_container, "readinessProbe") != "/readyz":
            errors.append("crag-api readiness probe must target /readyz")
        if not _has_secret_reference(api_container):
            errors.append("crag-api must mount a runtime Secret")
        if not _has_runtime_secret_volume(api_pod_spec):
            errors.append("crag-api must define a runtime Secret volume")
        if not _has_runtime_config(api_container):
            errors.append("crag-api must reference one external runtime ConfigMap")
    if worker is not None:
        worker_container = _container(worker, "crag-worker", errors)
        worker_pod_spec = _pod_spec(worker, "crag-worker", errors)
        _validate_resources(worker_container, "crag-worker", errors)
        _validate_security(worker_container, "crag-worker", errors)
        for probe in ("livenessProbe", "readinessProbe"):
            if not _worker_probe(worker_container, probe):
                errors.append(f"crag-worker {probe} must run crag-worker --check")
        if not _has_secret_reference(worker_container):
            errors.append("crag-worker must mount a runtime Secret")
        if not _has_runtime_secret_volume(worker_pod_spec):
            errors.append("crag-worker must define a runtime Secret volume")
        if not _has_runtime_config(worker_container):
            errors.append("crag-worker must reference one external runtime ConfigMap")

    ingress = resources.get(("Ingress", "crag-api"))
    if ingress is not None:
        spec = ingress.get("spec")
        tls = spec.get("tls") if isinstance(spec, Mapping) else None
        rules = spec.get("rules") if isinstance(spec, Mapping) else None
        if not isinstance(tls, list) or not tls or not isinstance(tls[0], Mapping):
            errors.append("Ingress must define TLS")
        elif not tls[0].get("secretName"):
            errors.append("Ingress TLS must reference a secret")
        if not isinstance(rules, list) or not rules or not isinstance(rules[0], Mapping):
            errors.append("Ingress must define a host rule")
        elif not isinstance(rules[0].get("host"), str):
            errors.append("Ingress host is invalid")
        annotations = ingress.get("metadata", {}).get("annotations") if isinstance(ingress.get("metadata"), Mapping) else None
        if not isinstance(annotations, Mapping) or annotations.get(
            "nginx.ingress.kubernetes.io/force-ssl-redirect"
        ) != "true":
            errors.append("Ingress must force TLS redirects")

    claim = resources.get(("PersistentVolumeClaim", "crag-artifacts"))
    if claim is not None:
        spec = claim.get("spec")
        access_modes = spec.get("accessModes") if isinstance(spec, Mapping) else None
        if not isinstance(access_modes, list) or "ReadWriteMany" not in access_modes:
            errors.append("artifact PVC must require ReadWriteMany")
        if not isinstance(spec, Mapping) or not spec.get("storageClassName"):
            errors.append("artifact PVC must declare a storage class")
    return errors


def _render_command(args: argparse.Namespace) -> int:
    output = Path(args.output)
    template = Path(args.template)
    if output.resolve() == template.resolve():
        raise ManifestError("output must not overwrite the template")
    document = render_manifest(
        template=template,
        image=args.image,
        namespace=args.namespace,
        ingress_host=args.ingress_host,
        runtime_config=args.runtime_config,
        runtime_secret=args.runtime_secret,
        tls_secret=args.tls_secret,
        artifact_storage_class=args.artifact_storage_class,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


def _lint_command(args: argparse.Namespace) -> int:
    errors = lint_manifest(_load_json(Path(args.input)))
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render or lint CRAG Kubernetes manifests")
    commands = parser.add_subparsers(dest="command", required=True)
    render = commands.add_parser("render", help="render a deployment bundle without cluster access")
    render.add_argument("--template", default=DEFAULT_TEMPLATE, type=Path)
    render.add_argument("--image", required=True)
    render.add_argument("--namespace", default="code-review-agent")
    render.add_argument("--ingress-host", required=True)
    render.add_argument("--runtime-config", required=True)
    render.add_argument("--runtime-secret", required=True)
    render.add_argument("--tls-secret", required=True)
    render.add_argument("--artifact-storage-class", required=True)
    render.add_argument("--output", required=True)
    lint = commands.add_parser("lint", help="lint a rendered deployment bundle")
    lint.add_argument("--input", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "render":
            return _render_command(args)
        return _lint_command(args)
    except ManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
