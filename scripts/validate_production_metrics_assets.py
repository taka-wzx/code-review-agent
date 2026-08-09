"""Validate the versioned cross-service Prometheus contract and Grafana dashboard.

The validator is intentionally offline. It checks only repository files and does
not require a Grafana server, a Prometheus server, credentials, or network access.
"""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT = ROOT / "observability" / "metric-contract-v1.json"
DEFAULT_DASHBOARD = ROOT / "observability" / "grafana" / "production-overview-v1.json"
DEFAULT_EXPORTER = ROOT / "src" / "code_review_agent" / "production_metrics.py"
_METRIC_NAME = re.compile(r"^[a-z_:][a-z0-9_:]*$")
_LABEL_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")
_LABEL_MATCHER = re.compile(r"([a-z_][a-z0-9_]*)\s*(?:=~|!~|!=|=)")
_GROUPING = re.compile(r"\b(?:sum|avg|min|max|count)\s+by\s*\(([^)]*)\)")
_METRIC_SELECTOR = re.compile(r"\b([a-zA-Z_:][a-zA-Z0-9_:]*)\s*\{([^{}]*)\}")
_STRING_LITERAL = re.compile(r'"(?:[^"\\]|\\.)*"')
_BRACED_SELECTOR = re.compile(r"\{[^{}]*\}")
_RANGE_SELECTOR = re.compile(r"\[[^\[\]]*\]")
_IDENTIFIER = re.compile(r"\b[a-zA-Z_:][a-zA-Z0-9_:]*\b")
_PROMQL_WORDS = frozenset(
    {
        "abs",
        "absent",
        "absent_over_time",
        "avg",
        "by",
        "clamp",
        "clamp_max",
        "clamp_min",
        "count",
        "histogram_quantile",
        "increase",
        "irate",
        "max",
        "min",
        "quantile",
        "rate",
        "scalar",
        "sum",
        "vector",
    }
)
_SENSITIVE_DASHBOARD_KEY = re.compile(
    r'"(?:authorization|credential|password|secret|token)"\s*:', re.IGNORECASE
)
_HOST_PATH = re.compile(r"[A-Za-z]:\\|/(?:Users|home)/")


class ValidationError(ValueError):
    """Raised when a committed metrics asset violates the versioned contract."""


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot load JSON from {path}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"{path} must contain a JSON object")
    return value


def _strings(value: Any, context: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValidationError(f"{context} must be a list of strings")
    if len(value) != len(set(value)):
        raise ValidationError(f"{context} must not contain duplicate values")
    return value


def _literal_strings(node: ast.AST, context: str) -> set[str]:
    if not isinstance(node, (ast.List, ast.Set, ast.Tuple)):
        raise ValidationError(f"{context} must be a literal string collection")
    values = {
        item.value
        for item in node.elts
        if isinstance(item, ast.Constant) and isinstance(item.value, str)
    }
    if len(values) != len(node.elts):
        raise ValidationError(f"{context} must contain only literal strings")
    return values


def _assignment(tree: ast.Module, name: str) -> ast.AST:
    for statement in tree.body:
        if not isinstance(statement, ast.Assign):
            continue
        if any(isinstance(target, ast.Name) and target.id == name for target in statement.targets):
            return statement.value
    raise ValidationError(f"exporter is missing {name}")


def _exporter_inventory(path: Path) -> tuple[dict[str, str], set[str], set[str], dict[str, str]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        raise ValidationError(f"cannot parse exporter {path}") from exc
    help_node = _assignment(tree, "_HELP")
    try:
        help_values = ast.literal_eval(help_node)
    except ValueError as exc:
        raise ValidationError("exporter _HELP must be a literal mapping") from exc
    if not isinstance(help_values, dict) or not all(
        isinstance(name, str) and isinstance(summary, str)
        for name, summary in help_values.items()
    ):
        raise ValidationError("exporter _HELP must map metric names to summaries")

    def frozen_set(name: str) -> set[str]:
        node = _assignment(tree, name)
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            raise ValidationError(f"exporter {name} must call frozenset")
        if node.func.id != "frozenset" or len(node.args) != 1:
            raise ValidationError(f"exporter {name} must call frozenset once")
        return _literal_strings(node.args[0], f"exporter {name}")

    family_types: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id != "family" or len(node.args) < 2:
            continue
        metric, metric_type = node.args[:2]
        if (
            isinstance(metric, ast.Constant)
            and isinstance(metric.value, str)
            and isinstance(metric_type, ast.Constant)
            and isinstance(metric_type.value, str)
        ):
            family_types[metric.value] = metric_type.value

    for node in ast.walk(tree):
        if not isinstance(node, ast.For) or not isinstance(node.target, ast.Name):
            continue
        dynamic_types: list[str] = []
        for child in node.body:
            if not isinstance(child, ast.Expr) or not isinstance(child.value, ast.Call):
                continue
            call = child.value
            if not isinstance(call.func, ast.Name) or call.func.id != "family" or len(call.args) < 2:
                continue
            metric, metric_type = call.args[:2]
            if (
                isinstance(metric, ast.Name)
                and metric.id == node.target.id
                and isinstance(metric_type, ast.Constant)
                and isinstance(metric_type.value, str)
            ):
                dynamic_types.append(metric_type.value)
        if dynamic_types:
            names = _literal_strings(node.iter, "exporter dynamic metric names")
            for metric_type in dynamic_types:
                family_types.update({name: metric_type for name in names})

    if set(help_values) != set(family_types):
        missing = sorted(set(help_values) - set(family_types))
        extra = sorted(set(family_types) - set(help_values))
        raise ValidationError(f"exporter family inventory is incomplete: missing={missing}, extra={extra}")
    return help_values, frozen_set("_ALLOWED_LABELS"), frozen_set("_PROHIBITED_LABELS"), family_types


def _require_string(mapping: Mapping[str, Any], key: str, context: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{context}.{key} must be a non-empty string")
    return value


def _validate_contract(
    contract: Mapping[str, Any],
    exporter_path: Path,
) -> tuple[dict[str, dict[str, Any]], set[str], str]:
    if contract.get("schema_version") != 1:
        raise ValidationError("contract schema_version must be 1")
    if contract.get("contract_id") != "crag-production-metrics-v1":
        raise ValidationError("contract_id must be crag-production-metrics-v1")
    exporter = contract.get("exporter")
    if not isinstance(exporter, dict):
        raise ValidationError("contract exporter must be an object")
    if exporter.get("endpoint") != "/metrics":
        raise ValidationError("contract exporter endpoint must be /metrics")
    if exporter.get("source") != "src/code_review_agent/production_metrics.py":
        raise ValidationError("contract exporter source must name the production exporter")
    if exporter.get("content_type") != "text/plain; version=0.0.4; charset=utf-8":
        raise ValidationError("contract exporter content type is not the Prometheus text format")

    policy = contract.get("label_policy")
    if not isinstance(policy, dict):
        raise ValidationError("contract label_policy must be an object")
    allowed = set(_strings(policy.get("allowed"), "label_policy.allowed"))
    prohibited = set(_strings(policy.get("prohibited"), "label_policy.prohibited"))
    if allowed & prohibited:
        raise ValidationError("label policy cannot allow and prohibit the same key")
    max_value_length = policy.get("max_value_length")
    if not isinstance(max_value_length, int) or isinstance(max_value_length, bool) or max_value_length < 1:
        raise ValidationError("label_policy.max_value_length must be a positive integer")

    exporter_help, exporter_allowed, exporter_prohibited, exporter_types = _exporter_inventory(
        exporter_path
    )
    if allowed != exporter_allowed or prohibited != exporter_prohibited:
        raise ValidationError("contract label policy drifted from the exporter")

    metrics = contract.get("metrics")
    if not isinstance(metrics, list) or not metrics:
        raise ValidationError("contract metrics must be a non-empty list")
    by_name: dict[str, dict[str, Any]] = {}
    for metric in metrics:
        if not isinstance(metric, dict):
            raise ValidationError("every contract metric must be an object")
        name = _require_string(metric, "name", "metric")
        if _METRIC_NAME.fullmatch(name) is None:
            raise ValidationError(f"metric name is invalid: {name}")
        if name in by_name:
            raise ValidationError(f"contract metric is duplicated: {name}")
        metric_type = _require_string(metric, "prometheus_type", name)
        if metric_type not in {"counter", "gauge", "histogram"}:
            raise ValidationError(f"{name}.prometheus_type is invalid")
        if exporter_types.get(name) != metric_type:
            raise ValidationError(f"{name}.prometheus_type drifted from the exporter")
        service_path = _require_string(metric, "service_path", name)
        if not re.fullmatch(r"[a-z][a-z-]*", service_path):
            raise ValidationError(f"{name}.service_path is invalid")
        if _require_string(metric, "summary", name) != exporter_help[name]:
            raise ValidationError(f"{name}.summary drifted from the exporter")
        label_values = metric.get("label_values")
        if not isinstance(label_values, dict):
            raise ValidationError(f"{name}.label_values must be an object")
        cardinality = 1
        for label, values in label_values.items():
            if not isinstance(label, str) or _LABEL_NAME.fullmatch(label) is None:
                raise ValidationError(f"{name} has an invalid label key")
            if label not in allowed or label in prohibited:
                raise ValidationError(f"{name} uses an unbounded or prohibited label: {label}")
            bounded_values = _strings(values, f"{name}.{label}")
            if not bounded_values:
                raise ValidationError(f"{name}.{label} must list bounded values")
            if any(len(value) > max_value_length for value in bounded_values):
                raise ValidationError(f"{name}.{label} exceeds the label value limit")
            cardinality *= len(bounded_values)
        expected_max_series = metric.get("max_series")
        if (
            not isinstance(expected_max_series, int)
            or isinstance(expected_max_series, bool)
            or expected_max_series != cardinality
        ):
            raise ValidationError(f"{name}.max_series must equal bounded label cardinality")
        by_name[name] = metric

    if set(by_name) != set(exporter_help):
        missing = sorted(set(exporter_help) - set(by_name))
        extra = sorted(set(by_name) - set(exporter_help))
        raise ValidationError(f"metric contract drift: missing={missing}, extra={extra}")

    dashboard = contract.get("dashboard")
    if not isinstance(dashboard, dict):
        raise ValidationError("contract dashboard must be an object")
    if dashboard.get("path") != "observability/grafana/production-overview-v1.json":
        raise ValidationError("contract dashboard path is invalid")
    uid = _require_string(dashboard, "uid", "dashboard")
    required_paths = set(
        _strings(dashboard.get("required_service_paths"), "dashboard.required_service_paths")
    )
    if required_paths != {"review", "queue", "provider", "approval", "publication"}:
        raise ValidationError("dashboard required service paths are incomplete")
    if not required_paths <= {str(metric["service_path"]) for metric in by_name.values()}:
        raise ValidationError("contract metrics do not cover every required service path")
    return by_name, required_paths, uid


def _metric_query_names(metrics: Mapping[str, Mapping[str, Any]]) -> tuple[dict[str, str], set[str]]:
    aliases: dict[str, str] = {}
    for name, metric in metrics.items():
        aliases[name] = name
        if metric["prometheus_type"] == "histogram":
            for suffix in ("_bucket", "_count", "_sum"):
                aliases[f"{name}{suffix}"] = name
    return aliases, set(aliases)


def _query_labels(expression: str) -> set[str]:
    labels: set[str] = set()
    for selector in _BRACED_SELECTOR.findall(expression):
        labels.update(_LABEL_MATCHER.findall(selector))
    for group in _GROUPING.findall(expression):
        labels.update(label.strip() for label in group.split(",") if label.strip())
    return labels


def _validate_selector_labels(
    expression: str,
    aliases: Mapping[str, str],
    metrics: Mapping[str, Mapping[str, Any]],
) -> None:
    for match in _METRIC_SELECTOR.finditer(expression):
        query_name, selector = match.groups()
        metric_name = aliases.get(query_name)
        if metric_name is None:
            continue
        label_values = metrics[metric_name]["label_values"]
        assert isinstance(label_values, dict)
        for label in _LABEL_MATCHER.findall(selector):
            if label not in label_values:
                raise ValidationError(
                    f"dashboard selector uses {label} for {query_name}, but that label is undefined"
                )


def _query_metric_tokens(expression: str, known_names: set[str]) -> set[str]:
    stripped = _STRING_LITERAL.sub("", expression)
    stripped = _BRACED_SELECTOR.sub("", stripped)
    stripped = _RANGE_SELECTOR.sub("", stripped)
    stripped = _GROUPING.sub("", stripped)
    identifiers = set(_IDENTIFIER.findall(stripped)) - _PROMQL_WORDS
    unknown = sorted(identifiers - known_names)
    if unknown:
        raise ValidationError(f"dashboard query references unknown metric(s): {unknown}")
    if not identifiers:
        raise ValidationError("dashboard query must reference at least one metric")
    return identifiers


def _validate_dashboard(
    dashboard: Mapping[str, Any],
    metrics: Mapping[str, Mapping[str, Any]],
    required_paths: set[str],
    expected_uid: str,
    allowed_labels: set[str],
    prohibited_labels: set[str],
) -> tuple[set[str], int]:
    if dashboard.get("id") is not None:
        raise ValidationError("dashboard id must be null for import")
    if dashboard.get("uid") != expected_uid:
        raise ValidationError("dashboard UID does not match the contract")
    if dashboard.get("schemaVersion") != 39 or dashboard.get("version") != 1:
        raise ValidationError("dashboard must use the declared versioned Grafana schema")
    if dashboard.get("timezone") != "utc" or dashboard.get("editable") is not False:
        raise ValidationError("dashboard must be immutable and UTC based")
    templating = dashboard.get("templating")
    if not isinstance(templating, dict) or not isinstance(templating.get("list"), list):
        raise ValidationError("dashboard must define template variables")
    datasource = [
        variable
        for variable in templating["list"]
        if isinstance(variable, dict)
        and variable.get("name") == "DS_PROMETHEUS"
        and variable.get("type") == "datasource"
        and variable.get("query") == "prometheus"
    ]
    if len(datasource) != 1:
        raise ValidationError("dashboard must include one Prometheus datasource variable")

    serialized = json.dumps(dashboard, sort_keys=True)
    if _SENSITIVE_DASHBOARD_KEY.search(serialized) or _HOST_PATH.search(serialized):
        raise ValidationError("dashboard must not contain sensitive settings or host paths")

    panels = dashboard.get("panels")
    if not isinstance(panels, list) or not panels:
        raise ValidationError("dashboard panels must be a non-empty list")
    aliases, query_names = _metric_query_names(metrics)
    panel_ids: set[int] = set()
    observed_paths: set[str] = set()
    target_panels = 0
    for panel in panels:
        if not isinstance(panel, dict):
            raise ValidationError("dashboard panel must be an object")
        panel_id = panel.get("id")
        if not isinstance(panel_id, int) or isinstance(panel_id, bool) or panel_id < 1:
            raise ValidationError("dashboard panel id must be a positive integer")
        if panel_id in panel_ids:
            raise ValidationError(f"dashboard panel id is duplicated: {panel_id}")
        panel_ids.add(panel_id)
        _require_string(panel, "type", f"panel {panel_id}")
        _require_string(panel, "title", f"panel {panel_id}")
        grid_pos = panel.get("gridPos")
        if not isinstance(grid_pos, dict):
            raise ValidationError(f"panel {panel_id} must have a grid position")
        positive = ("h", "w")
        non_negative = ("x", "y")
        if any(
            not isinstance(grid_pos.get(key), int)
            or isinstance(grid_pos.get(key), bool)
            or grid_pos[key] < 1
            for key in positive
        ) or any(
            not isinstance(grid_pos.get(key), int)
            or isinstance(grid_pos.get(key), bool)
            or grid_pos[key] < 0
            for key in non_negative
        ):
            raise ValidationError(f"panel {panel_id} has an invalid grid position")
        if panel["type"] == "row":
            continue
        targets = panel.get("targets")
        if not isinstance(targets, list) or not targets:
            raise ValidationError(f"panel {panel_id} must have at least one target")
        target_panels += 1
        ref_ids: set[str] = set()
        for target in targets:
            if not isinstance(target, dict):
                raise ValidationError(f"panel {panel_id} target must be an object")
            ref_id = _require_string(target, "refId", f"panel {panel_id} target")
            if ref_id in ref_ids:
                raise ValidationError(f"panel {panel_id} has duplicate refId {ref_id}")
            ref_ids.add(ref_id)
            expression = _require_string(target, "expr", f"panel {panel_id} target")
            labels = _query_labels(expression)
            invalid_labels = labels - allowed_labels
            if invalid_labels or labels & prohibited_labels:
                raise ValidationError(
                    f"panel {panel_id} query uses prohibited labels: {sorted(invalid_labels)}"
                )
            _validate_selector_labels(expression, aliases, metrics)
            for query_name in _query_metric_tokens(expression, query_names):
                metric = aliases[query_name]
                observed_paths.add(str(metrics[metric]["service_path"]))
    if target_panels == 0:
        raise ValidationError("dashboard must contain data panels")
    missing_paths = sorted(required_paths - observed_paths)
    if missing_paths:
        raise ValidationError(f"dashboard is missing service-path coverage: {missing_paths}")
    return observed_paths, len(panel_ids)


def validate_assets(
    *,
    contract_path: Path = DEFAULT_CONTRACT,
    dashboard_path: Path = DEFAULT_DASHBOARD,
    exporter_path: Path = DEFAULT_EXPORTER,
) -> dict[str, int]:
    """Validate all committed Issue #38 assets and return a concise inventory."""
    contract = _load_json(contract_path)
    dashboard = _load_json(dashboard_path)
    metrics, required_paths, uid = _validate_contract(contract, exporter_path)
    policy = contract["label_policy"]
    observed_paths, panel_count = _validate_dashboard(
        dashboard,
        metrics,
        required_paths,
        uid,
        set(policy["allowed"]),
        set(policy["prohibited"]),
    )
    return {
        "dashboard_panels": panel_count,
        "metric_families": len(metrics),
        "service_paths": len(observed_paths),
    }


def main(argv: Iterable[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--dashboard", type=Path, default=DEFAULT_DASHBOARD)
    parser.add_argument("--exporter", type=Path, default=DEFAULT_EXPORTER)
    args = parser.parse_args(argv)
    result = validate_assets(
        contract_path=args.contract,
        dashboard_path=args.dashboard,
        exporter_path=args.exporter,
    )
    print(
        "Validated "
        f"{result['metric_families']} metric families, "
        f"{result['service_paths']} service paths, and "
        f"{result['dashboard_panels']} dashboard panels."
    )


if __name__ == "__main__":
    main()
