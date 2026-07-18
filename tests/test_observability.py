from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from code_review_agent.agent import run_review
from code_review_agent.observability import (
    FailingExporter,
    InMemoryExporter,
    JsonlFileExporter,
    SpanLifecycleError,
    TelemetryValidationError,
    TelemetryWriteError,
    Tracer,
    aggregate_trace,
    error_category_for_exception,
    load_span_records,
    validate_span_record,
    validate_trace,
)
from code_review_agent.tracelog import Trace, iter_events
from tests.fakes import FakeClient, response, tool_call


SOURCE_COMMIT = "1" * 40


class TestCanonicalTracer(unittest.TestCase):
    def test_error_categories_are_type_only_and_complete(self):
        expected = {
            "AuthenticationError": "auth",
            "PermissionError": "auth",
            "RateLimitError": "rate_limit",
            "RequestTimeout": "timeout",
            "ConnectionError": "connection",
            "BudgetExceeded": "budget_exhausted",
            "ApprovalRejected": "approval_rejected",
            "SandboxViolation": "sandbox_violation",
            "TelemetryWriteFailure": "telemetry_write",
            "TelemetryExportFailure": "telemetry_export",
            "APIError": "provider",
            "UnexpectedFailure": "internal",
        }
        for name, category in expected.items():
            exception_type = type(name, (Exception,), {})
            with self.subTest(name=name):
                self.assertEqual(
                    error_category_for_exception(exception_type("secret")),
                    category,
                )

    def test_hierarchy_usage_and_tool_aggregation(self):
        exporter = InMemoryExporter()
        tracer = Tracer(
            exporter,
            run_id="run-1",
            source_commit=SOURCE_COMMIT,
            root_attributes={"gen_ai.provider.name": "deepseek"},
        )
        with tracer.start_span(
            "crag.stage finder",
            operation="agent.stage",
            attributes={"crag.stage.name": "finder"},
        ):
            with tracer.start_span(
                "chat model-v1",
                operation="llm.request",
                kind="CLIENT",
                attributes={
                    "gen_ai.operation.name": "chat",
                    "gen_ai.provider.name": "deepseek",
                    "gen_ai.request.model": "model-v1",
                    "gen_ai.usage.input_tokens": 100,
                    "gen_ai.usage.output_tokens": 20,
                    "gen_ai.usage.cache_read.input_tokens": 40,
                    "crag.cost.micro_usd": 125,
                },
            ):
                pass
            with tracer.start_span(
                "execute_tool read_file",
                operation="tool.execute",
                attributes={
                    "gen_ai.operation.name": "execute_tool",
                    "gen_ai.tool.name": "read_file",
                },
            ):
                pass
            with tracer.start_span(
                "crag.sandbox git",
                operation="sandbox.command",
                attributes={"crag.sandbox.command": "git"},
            ):
                pass
            with tracer.start_span(
                "crag.policy write",
                operation="policy.decision",
                attributes={
                    "crag.policy.operation": "write",
                    "crag.policy.decision": "rejected",
                },
            ):
                pass
            with tracer.start_span(
                "crag.checkpoint save",
                operation="checkpoint",
                attributes={"crag.checkpoint.operation": "save"},
            ):
                pass
        tracer.close()

        records = validate_trace(exporter.records)
        self.assertEqual(len(records), 7)
        root = next(record for record in records if record["parent_span_id"] is None)
        stage = next(record for record in records if record["operation"] == "agent.stage")
        children = [
            record
            for record in records
            if record["operation"] in {"llm.request", "tool.execute"}
        ]
        self.assertEqual(stage["parent_span_id"], root["span_id"])
        self.assertTrue(all(record["parent_span_id"] == stage["span_id"] for record in children))
        aggregate = aggregate_trace(records)
        self.assertEqual(aggregate["input_tokens"], 100)
        self.assertEqual(aggregate["output_tokens"], 20)
        self.assertEqual(aggregate["cache_read_tokens"], 40)
        self.assertIsNone(aggregate["cache_creation_tokens"])
        self.assertEqual(aggregate["llm_calls"], 1)
        self.assertEqual(aggregate["tool_calls"], 1)
        self.assertEqual(aggregate["total_tokens"], 120)
        self.assertEqual(aggregate["cost_microusd"], 125)
        self.assertEqual(aggregate["sandbox_commands"], 1)
        self.assertEqual(aggregate["policy_decisions"], 1)
        self.assertEqual(aggregate["policy_rejections"], 1)
        self.assertEqual(aggregate["checkpoint_operations"], 1)
        self.assertEqual(aggregate["stage_spans"], 1)
        self.assertEqual(aggregate["run_status"], "ok")
        self.assertEqual(aggregate["telemetry_mode"], "normal")

    def test_exception_records_only_type_and_category(self):
        exporter = InMemoryExporter()
        tracer = Tracer(exporter, run_id="run-error", source_commit=SOURCE_COMMIT)
        with self.assertRaisesRegex(ValueError, "W6_CANARY"):
            with tracer.start_span(
                "crag.policy write",
                operation="policy.decision",
                attributes={"crag.policy.operation": "write"},
            ):
                raise ValueError("W6_CANARY_must-not-be-serialized")
        tracer.close()
        error = next(record for record in exporter.records if record["status"] == "error")
        encoded = json.dumps(error)
        self.assertNotIn("must-not-be-serialized", encoded)
        self.assertEqual(error["attributes"]["error.type"], "ValueError")
        self.assertEqual(error["attributes"]["crag.error.category"], "internal")

    def test_lifecycle_rejects_mutation_and_duplicate_end(self):
        exporter = InMemoryExporter()
        tracer = Tracer(exporter, run_id="run-life", source_commit=SOURCE_COMMIT)
        span = tracer.start_span("crag.stage test", operation="agent.stage")
        with span:
            pass
        with self.assertRaises(SpanLifecycleError):
            span.set_attribute("late", True)
        with self.assertRaises(SpanLifecycleError):
            span.add_event("late")
        with self.assertRaises(SpanLifecycleError):
            span.end()
        tracer.close()

    def test_terminal_error_metadata_survives_a_full_attribute_budget(self):
        exporter = InMemoryExporter()
        tracer = Tracer(exporter, run_id="run-full-error", source_commit=SOURCE_COMMIT)
        span = tracer.start_span("full error", operation="agent.stage")
        for index in range(70):
            span.set_attribute(f"crag.test.attribute.{index}", index)

        record = span.end(
            status="error",
            error_type="SyntheticFailure",
            error_category="internal",
        )
        tracer.close(status="error", error_type="SyntheticFailure")

        self.assertEqual(record["attributes"]["error.type"], "SyntheticFailure")
        self.assertEqual(record["attributes"]["crag.error.category"], "internal")
        self.assertTrue(record["attributes"]["crag.redaction.truncated"])
        validate_trace(exporter.records)

    def test_invalid_snapshot_is_unregistered_and_original_error_survives(self):
        exporter = InMemoryExporter()
        tracer = Tracer(exporter, run_id="run-fallback", source_commit=SOURCE_COMMIT)
        span = tracer.start_span(
            "chat missing-provider",
            kind="CLIENT",
            operation="llm.request",
            attributes={
                "gen_ai.operation.name": "chat",
                "gen_ai.request.model": "model-v1",
            },
        )

        with self.assertRaisesRegex(RuntimeError, "original failure"):
            with span:
                raise RuntimeError("original failure")
        tracer.close(status="error", error_type="RuntimeError")

        fallback = next(record for record in exporter.records if record["span_id"] == span.span_id)
        self.assertEqual(fallback["status"], "error")
        self.assertEqual(fallback["attributes"]["gen_ai.provider.name"], "unknown")
        self.assertEqual(
            fallback["attributes"]["crag.telemetry.degraded_reason"],
            "span_validation_failed",
        )
        self.assertEqual(fallback["attributes"]["crag.telemetry.mode"], "degraded")
        validate_trace(exporter.records)

    def test_unknown_source_commit_is_explicitly_degraded(self):
        exporter = InMemoryExporter()
        tracer = Tracer(exporter, run_id="run-unknown", source_commit="unknown")
        tracer.close()
        root = exporter.records[0]
        self.assertEqual(root["source_commit"], "unknown")
        self.assertEqual(root["attributes"]["crag.telemetry.mode"], "degraded")
        self.assertTrue(
            any(event["name"] == "crag.telemetry.degraded" for event in root["events"])
        )

    def test_constructor_lifecycle_and_caps_fail_closed(self):
        for run_id, source_commit in (("bad id", SOURCE_COMMIT), ("valid", "not-a-commit")):
            with self.subTest(run_id=run_id, source_commit=source_commit):
                with self.assertRaises(TelemetryValidationError):
                    Tracer(
                        InMemoryExporter(),
                        run_id=run_id,
                        source_commit=source_commit,
                    )

        exporter = InMemoryExporter()
        tracer = Tracer(exporter, run_id="lifecycle", source_commit=SOURCE_COMMIT)
        self.assertIs(tracer.current_span(), tracer.root_span)
        invalid_spans = (
            {"name": "\n", "operation": "agent.stage"},
            {"name": "valid", "operation": "\n"},
            {"name": "valid", "operation": "agent.stage", "kind": "INVALID"},
            {
                "name": "valid",
                "operation": "agent.stage",
                "parent_span_id": "0" * 16,
            },
        )
        for arguments in invalid_spans:
            with self.subTest(arguments=arguments):
                with self.assertRaises(TelemetryValidationError):
                    tracer.start_span(**arguments)

        child = tracer.start_span("crag.stage caps", operation="agent.stage")
        with self.assertRaises(SpanLifecycleError):
            child._snapshot()
        child.__enter__()
        with self.assertRaises(SpanLifecycleError):
            child.__enter__()
        for index in range(70):
            child.set_attribute(f"crag.test.attribute.{index}", index)
        for index in range(129):
            child.add_event(f"crag.test.event.{index}")
        with self.assertRaises(TelemetryValidationError):
            child.add_event("\n")
        with self.assertRaises(SpanLifecycleError):
            tracer.close()
        child.__exit__(None, None, None)
        record = next(item for item in exporter.records if item["span_id"] == child.span_id)
        self.assertTrue(record["attributes"]["crag.redaction.truncated"])
        self.assertGreater(record["attributes"]["crag.redaction.omitted_count"], 0)
        self.assertEqual(len(record["events"]), 128)
        tracer.close()
        tracer.close()
        with self.assertRaises(SpanLifecycleError):
            tracer.start_span("late", operation="agent.stage")

        for index, arguments in enumerate(
            (
                {"status": "invalid"},
                {"status": "error"},
                {
                    "status": "error",
                    "error_type": "Failure",
                    "error_category": "invalid",
                },
                {"status": "ok", "error_type": "Failure"},
            )
        ):
            isolated = Tracer(
                InMemoryExporter(),
                run_id=f"end-{index}",
                source_commit=SOURCE_COMMIT,
            )
            span = isolated.start_span("end checks", operation="agent.stage")
            with self.subTest(arguments=arguments):
                with self.assertRaises(TelemetryValidationError):
                    span.end(**arguments)


class TestTraceCompatibility(unittest.TestCase):
    def test_file_and_memory_exporter_bounds(self):
        for value in (True, 0, -1, 10**12):
            with tempfile.TemporaryDirectory() as tmp:
                with self.subTest(value=value):
                    with self.assertRaises(ValueError):
                        JsonlFileExporter(Path(tmp) / "trace.jsonl", max_file_bytes=value)

        base = TestValidation()._valid_records()[0]
        memory = InMemoryExporter()
        memory.close()
        with self.assertRaises(TelemetryWriteError):
            memory.export(base)

        with tempfile.TemporaryDirectory() as tmp:
            bounded = JsonlFileExporter(Path(tmp) / "bounded.jsonl", max_file_bytes=1)
            with self.assertRaises(TelemetryWriteError):
                bounded.export(base)
            bounded.close()
            bounded.close()

            closed = JsonlFileExporter(Path(tmp) / "closed.jsonl")
            closed.close()
            with self.assertRaises(TelemetryWriteError):
                closed.export(base)

    def test_existing_audit_file_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.jsonl"
            path.write_text("prior-evidence\n", encoding="utf-8")
            with self.assertRaises(TelemetryWriteError):
                Trace(path, run_id="must-not-truncate", source_commit=SOURCE_COMMIT)
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                "prior-evidence\n",
            )

    def test_canonical_file_has_legacy_event_projection(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.jsonl"
            trace = Trace(path, run_id="legacy-roundtrip", source_commit=SOURCE_COMMIT)
            trace.event("meta", provider="deepseek", model="model-v1")
            trace.event("start", value=Path("x"))
            trace.event("done", count=2)
            trace.close()
            raw = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            events = list(iter_events(path))
            records = load_span_records(path)

        self.assertTrue(all(record["schema_version"] == "crag.observability/v1alpha1" for record in raw))
        self.assertEqual([event["kind"] for event in events], ["meta", "start", "done"])
        self.assertEqual(events[1]["value"], "x")
        self.assertIsInstance(events[1]["t"], float)
        self.assertEqual(len(records), 4)

    def test_optional_export_failure_preserves_local_degraded_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.jsonl"
            failing = FailingExporter()
            trace = Trace(
                path,
                run_id="export-failure",
                source_commit=SOURCE_COMMIT,
                optional_exporters=[failing],
            )
            trace.event("tool", tool="read_file", result_chars=12, args={"path": ".env"})
            trace.close()
            records = load_span_records(path)
            serialized = path.read_text(encoding="utf-8")

        self.assertEqual(failing.attempts, 1)
        self.assertNotIn(".env", serialized)
        root = next(record for record in records if record["parent_span_id"] is None)
        self.assertEqual(root["attributes"]["crag.telemetry.mode"], "degraded")
        self.assertTrue(
            any(event["name"] == "crag.telemetry.export_failed" for event in root["events"])
        )
        export_failure = next(
            record for record in records if record["operation"] == "telemetry.export"
        )
        self.assertEqual(export_failure["status"], "error")
        self.assertEqual(
            export_failure["attributes"]["crag.error.category"],
            "telemetry_export",
        )

    def test_root_only_optional_failure_is_persisted_before_local_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.jsonl"
            trace = Trace(
                path,
                run_id="root-export-failure",
                source_commit=SOURCE_COMMIT,
                optional_exporters=[FailingExporter()],
            )
            trace.close()
            records = load_span_records(path)

        self.assertEqual(len(records), 1)
        root = records[0]
        self.assertEqual(root["attributes"]["crag.telemetry.mode"], "degraded")
        self.assertTrue(
            any(event["name"] == "crag.telemetry.export_failed" for event in root["events"])
        )

    def test_fail_open_is_a_cross_checkable_policy_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.jsonl"
            trace = Trace(path, run_id="fail-open", source_commit=SOURCE_COMMIT)
            trace.event("verifier_fail_open", n_findings=2)
            trace.close()
            aggregate = aggregate_trace(load_span_records(path))

        self.assertEqual(aggregate["policy_decisions"], 1)
        self.assertEqual(aggregate["fail_open_decisions"], 1)
        self.assertEqual(aggregate["degraded_decisions"], 0)

    def test_concurrent_events_remain_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.jsonl"
            trace = Trace(path, run_id="concurrent", source_commit=SOURCE_COMMIT)

            def write(worker):
                for index in range(50):
                    trace.event("worker", worker=worker, index=index)

            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(write, range(4)))
            trace.close()
            events = list(iter_events(path))
            load_span_records(path)

        self.assertEqual(len(events), 200)
        self.assertEqual(
            {(event["worker"], event["index"]) for event in events},
            {(worker, index) for worker in range(4) for index in range(50)},
        )

    def test_review_vertical_path_has_parallel_stage_and_llm_children(self):
        review_payload = {
            "summary": "ok",
            "findings": [
                {
                    "file": "mod.py",
                    "line": 1,
                    "severity": "low",
                    "issue": "issue",
                    "suggestion": "fix",
                }
            ],
        }
        diff = (
            "diff --git a/mod.py b/mod.py\n"
            "--- a/mod.py\n"
            "+++ b/mod.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )
        client = FakeClient(
            [
                response([tool_call("one", "submit_review", review_payload)]),
                response([tool_call("two", "submit_review", review_payload)]),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "review.jsonl"
            trace = Trace(path, run_id="review-vertical", source_commit=SOURCE_COMMIT)
            run_review(
                client,
                diff,
                Path(tmp),
                "model-v1",
                use_context=False,
                use_verify=False,
                trace=trace,
            )
            trace.close()
            records = load_span_records(path)

        finder_stage = next(
            record
            for record in records
            if record["operation"] == "agent.stage"
            and record["attributes"].get("crag.stage.name") == "finder"
        )
        llm_spans = [
            record for record in records if record["operation"] == "llm.request"
        ]
        self.assertEqual(len(llm_spans), 2)
        self.assertTrue(
            all(record["parent_span_id"] == finder_stage["span_id"] for record in llm_spans)
        )
        self.assertEqual(aggregate_trace(records)["input_tokens"], 200)


class TestValidation(unittest.TestCase):
    def _valid_records(self):
        exporter = InMemoryExporter()
        tracer = Tracer(exporter, run_id="validate", source_commit=SOURCE_COMMIT)
        with tracer.start_span("crag.stage validate", operation="agent.stage"):
            pass
        tracer.close()
        return exporter.records

    def test_record_rejects_bad_ids_times_status_content_and_size(self):
        base = self._valid_records()[0]
        mutations = []
        bad_id = deepcopy(base)
        bad_id["trace_id"] = "0" * 32
        mutations.append(bad_id)
        bad_parent = deepcopy(base)
        bad_parent["parent_span_id"] = bad_parent["span_id"]
        mutations.append(bad_parent)
        bad_time = deepcopy(base)
        bad_time["end_time"] = "2000-01-01T00:00:00.000000Z"
        mutations.append(bad_time)
        bad_status = deepcopy(base)
        bad_status["status"] = "error"
        mutations.append(bad_status)
        bad_content = deepcopy(base)
        bad_content["attributes"]["gen_ai.input.messages"] = ["secret"]
        mutations.append(bad_content)
        bad_envelope = deepcopy(base)
        bad_envelope["attributes"]["crag.run.id"] = "another-run"
        mutations.append(bad_envelope)
        bad_event_time = deepcopy(base)
        bad_event_time["events"].append(
            {
                "name": "crag.test",
                "time": "2000-01-01T00:00:00.000000Z",
                "attributes": {},
            }
        )
        mutations.append(bad_event_time)
        for record in mutations:
            with self.subTest(record=record):
                with self.assertRaises(TelemetryValidationError):
                    validate_span_record(record)

    def test_trace_rejects_duplicate_unknown_parent_cycle_and_multiple_roots(self):
        records = self._valid_records()
        root = next(record for record in records if record["parent_span_id"] is None)
        child = next(record for record in records if record["parent_span_id"] is not None)

        duplicate = records + [deepcopy(child)]
        unknown = deepcopy(records)
        next(record for record in unknown if record["parent_span_id"] is not None)[
            "parent_span_id"
        ] = "2" * 16
        multiple_roots = deepcopy(records)
        next(record for record in multiple_roots if record["parent_span_id"] is not None)[
            "parent_span_id"
        ] = None
        cycle = deepcopy(records)
        cycle_root = next(record for record in cycle if record["span_id"] == root["span_id"])
        cycle_child = next(record for record in cycle if record["span_id"] == child["span_id"])
        cycle_root["parent_span_id"] = cycle_child["span_id"]
        child_outside_parent = deepcopy(records)
        next(
            record
            for record in child_outside_parent
            if record["parent_span_id"] is not None
        )["end_time"] = "2999-01-01T00:00:00.000000Z"
        mixed_commit = deepcopy(records)
        mixed_child = next(
            record for record in mixed_commit if record["parent_span_id"] is not None
        )
        mixed_child["source_commit"] = "2" * 40
        mixed_child["attributes"]["crag.source.commit"] = "2" * 40

        for candidate in (
            duplicate,
            unknown,
            multiple_roots,
            cycle,
            child_outside_parent,
            mixed_commit,
        ):
            with self.subTest(candidate=candidate):
                with self.assertRaises(TelemetryValidationError):
                    validate_trace(candidate)

    def test_record_validation_rejects_every_bounded_envelope_violation(self):
        base = next(
            record
            for record in self._valid_records()
            if record["parent_span_id"] is None
        )
        candidates = []

        def mutate(callback):
            candidate = deepcopy(base)
            callback(candidate)
            candidates.append(candidate)

        mutate(lambda record: record.pop("links"))
        mutate(lambda record: record.update(extra=True))
        mutate(lambda record: record.update(schema_version="other"))
        mutate(lambda record: record.update(span_id="0" * 16))
        mutate(lambda record: record.update(run_id="bad id"))
        mutate(lambda record: record.update(name="\n"))
        mutate(lambda record: record.update(operation="\n"))
        mutate(lambda record: record.update(kind="INVALID"))
        mutate(lambda record: record.update(start_time="not-utc"))
        mutate(lambda record: record.update(start_time="not-a-timeZ"))
        mutate(lambda record: record.update(duration_ms=True))
        mutate(lambda record: record.update(duration_ms=float("nan")))
        mutate(lambda record: record.update(duration_ms=-1))
        mutate(lambda record: record.update(source_commit="bad"))
        mutate(lambda record: record.update(redaction_policy_version="other"))
        mutate(lambda record: record.update(runtime_version="\n"))
        mutate(lambda record: record.update(attributes=[]))
        mutate(
            lambda record: record["attributes"].update(
                {f"crag.extra.{index}": index for index in range(70)}
            )
        )
        mutate(
            lambda record: record["attributes"].update(
                {"crag.telemetry.mode": "other"}
            )
        )
        mutate(lambda record: record["attributes"].pop("gen_ai.agent.name"))

        def llm_without_required_fields(record):
            record["operation"] = "llm.request"

        mutate(llm_without_required_fields)

        def tool_without_required_fields(record):
            record["operation"] = "tool.execute"

        mutate(tool_without_required_fields)

        def unknown_without_degradation(record):
            record["source_commit"] = "unknown"
            record["attributes"]["crag.source.commit"] = "unknown"

        mutate(unknown_without_degradation)
        mutate(
            lambda record: record["attributes"].update(
                {"crag.redaction.count": True}
            )
        )
        mutate(
            lambda record: record["attributes"].update(
                {"crag.redaction.truncated": 1}
            )
        )

        def error_without_category(record):
            record["status"] = "error"
            record["attributes"]["error.type"] = "Failure"

        mutate(error_without_category)
        mutate(
            lambda record: record["attributes"].update({"error.type": "Failure"})
        )
        mutate(lambda record: record.update(events={}))
        mutate(
            lambda record: record["events"].append(
                {"name": "event", "time": record["end_time"]}
            )
        )
        mutate(
            lambda record: record["events"].append(
                {
                    "name": "\n",
                    "time": record["end_time"],
                    "attributes": {},
                }
            )
        )
        mutate(
            lambda record: record["events"].append(
                {
                    "name": "event",
                    "time": record["end_time"],
                    "attributes": {
                        f"crag.extra.{index}": index for index in range(70)
                    },
                }
            )
        )
        mutate(
            lambda record: record.update(
                events=[
                    {
                        "name": f"event-{index}",
                        "time": record["end_time"],
                        "attributes": {},
                    }
                    for index in range(129)
                ]
            )
        )
        mutate(lambda record: record.update(links={}))
        mutate(lambda record: record.update(links=[{}] * 33))
        mutate(
            lambda record: record["attributes"].update(
                {f"crag.large.{index}": "x" * 2_000 for index in range(55)}
            )
        )

        for index, candidate in enumerate(candidates):
            with self.subTest(index=index):
                with self.assertRaises(TelemetryValidationError):
                    validate_span_record(candidate)

    def test_trace_loading_and_aggregate_boundaries(self):
        records = self._valid_records()
        child = next(record for record in records if record["parent_span_id"] is not None)

        candidates = []
        mixed_trace = deepcopy(records)
        mixed_trace[0]["trace_id"] = "3" * 32
        candidates.append(mixed_trace)
        mixed_run = deepcopy(records)
        mixed_run[0]["run_id"] = "another-run"
        mixed_run[0]["attributes"]["crag.run.id"] = "another-run"
        candidates.append(mixed_run)
        mixed_runtime = deepcopy(records)
        mixed_runtime[0]["runtime_version"] = "other"
        candidates.append(mixed_runtime)
        wrong_root = deepcopy(records)
        next(record for record in wrong_root if record["parent_span_id"] is None)[
            "operation"
        ] = "agent.stage"
        candidates.append(wrong_root)
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                with self.assertRaises(TelemetryValidationError):
                    validate_trace(candidate)
        with self.assertRaises(TelemetryValidationError):
            validate_trace([])

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            inputs = {
                "duplicate.jsonl": '{"a":1,"a":2}\n',
                "malformed.jsonl": '{"a":\n',
                "non-object.jsonl": "[]\n",
                "empty.jsonl": "",
            }
            for name, payload in inputs.items():
                candidate = path / name
                candidate.write_text(payload, encoding="utf-8")
                with self.subTest(name=name):
                    with self.assertRaises(TelemetryValidationError):
                        load_span_records(candidate)

        for attribute, value in (
            ("crag.cost.micro_usd", True),
            ("crag.cost.micro_usd", -1),
            ("crag.usage.total_tokens", -1),
            ("gen_ai.usage.input_tokens", -1),
        ):
            candidate = deepcopy(records)
            target = next(
                record for record in candidate if record["span_id"] == child["span_id"]
            )
            target["attributes"][attribute] = value
            with self.subTest(attribute=attribute, value=value):
                with self.assertRaises(TelemetryValidationError):
                    aggregate_trace(candidate)

        exporter = InMemoryExporter()
        tracer = Tracer(exporter, run_id="aggregate", source_commit=SOURCE_COMMIT)
        tracer.root_span.add_event("crag.retry.model")
        for decision in ("degraded", "fail_open"):
            with tracer.start_span(
                f"policy {decision}",
                operation="policy.decision",
                attributes={"crag.policy.decision": decision},
            ):
                pass
        with tracer.start_span(
            "extension usage",
            operation="llm.request",
            attributes={
                "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": "fake",
                "gen_ai.request.model": "fake-model",
                "crag.usage.total_tokens": 9,
                "gen_ai.usage.cache_creation.input_tokens": 3,
                "gen_ai.usage.reasoning_tokens": 4,
            },
        ):
            pass
        failure = tracer.start_span(
            "failed tool",
            operation="tool.execute",
            attributes={
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": "synthetic",
            },
        )
        failure.end(
            status="error",
            error_type="SyntheticFailure",
            error_category="internal",
        )
        tracer.close()
        aggregate = aggregate_trace(exporter.records)
        self.assertEqual(aggregate["degraded_decisions"], 1)
        self.assertEqual(aggregate["fail_open_decisions"], 1)
        self.assertEqual(aggregate["retry_events"], 1)
        self.assertEqual(aggregate["error_spans"], 1)
        self.assertEqual(aggregate["cache_creation_tokens"], 3)
        self.assertEqual(aggregate["reasoning_tokens"], 4)
        self.assertEqual(aggregate["total_tokens"], 9)
        self.assertIsNone(aggregate["cost_microusd"])


if __name__ == "__main__":
    unittest.main()
