import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch


APP_DIR = Path(__file__).resolve().parents[1] / "app"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
AGENT_BASELINE_PATH = PROJECT_ROOT / "evaluation" / "baseline" / "agent-evolution-baseline.json"
API_CONTRACT_PATH = (
    PROJECT_ROOT / "backend" / "tests" / "fixtures" / "api_contracts" / "current_http_server.json"
)
sys.path.insert(0, str(APP_DIR))

from models.evaluation import AnnotationBundle
from services.evaluation_service import (
    EFFECT_NO_PRODUCTION_CLAIM,
    _clause_metrics,
    _evidence_metric,
    _load_annotations,
    _load_effect_config,
    _retrieval_repair_checks,
    _runtime_summary,
    run_effect_evaluation,
)
from services.memory_service import evaluate_memory_comparison


class EffectEvaluationTest(unittest.TestCase):
    def test_agent_evolution_baseline_is_complete_and_keeps_known_failure(self):
        baseline = json.loads(AGENT_BASELINE_PATH.read_text(encoding="utf-8"))
        api_contract = json.loads(API_CONTRACT_PATH.read_text(encoding="utf-8"))

        self.assertEqual(baseline["status"], "captured_with_known_failure")
        self.assertFalse(baseline["versions"]["code"]["git_dirty_at_capture"])
        self.assertEqual(baseline["versions"]["playbook"]["version"], "nda-v1")
        self.assertEqual(baseline["versions"]["annotations"]["version"], "effect-v1")
        self.assertIsNone(baseline["versions"]["prompt"]["explicit_version"])
        self.assertEqual(baseline["versions"]["llm"]["mode"], "local_structured")
        self.assertFalse(baseline["versions"]["llm"]["deepseek_real_call_verified"])
        self.assertEqual(baseline["versions"]["embedding"]["mode"], "local_sparse")

        flow = baseline["flow_evaluation"]
        self.assertEqual(flow["sample_count"], 10)
        self.assertEqual(flow["passed_count"], 10)
        self.assertEqual(len(flow["samples"]), 10)
        for sample in flow["samples"]:
            self.assertTrue(
                all(sample[check_name] for check_name in flow["required_checks_per_sample"]),
                sample["sample_name"],
            )
            self.assertEqual(sample["failure_reason"], "")

        effect = baseline["effect_evaluation"]
        metrics = {item["metric"]: item for item in effect["metrics"]}
        self.assertEqual(effect["sample_count"], 6)
        self.assertEqual(effect["metric_count"], 14)
        self.assertEqual(len(metrics), 14)
        related = metrics["related_clause_recall_at_k"]
        self.assertEqual(related["score"], 0.3333)
        self.assertEqual(related["threshold"], 0.8)
        self.assertEqual(related["passed_count"], 1)
        self.assertEqual(related["failed_count"], 2)
        self.assertFalse(related["threshold_met"])

        expected_success_fields = {
            "health": list(api_contract["success"]["health"]["body"]),
            "task": api_contract["success"]["task_create"]["required_keys"],
            "local_review": api_contract["success"]["local_review"]["required_keys"],
            "feedback": api_contract["success"]["feedback"]["required_keys"],
            "report": api_contract["success"]["report"]["required_keys"],
            "flow_evaluation": api_contract["success"]["evaluation"]["required_keys"],
            "effect_evaluation": api_contract["success"]["effect_evaluation"]["required_keys"],
        }
        historical_success_fields = baseline["api_contract"]["success_fields"]
        self.assertEqual(set(historical_success_fields), set(expected_success_fields))
        for name, fields in historical_success_fields.items():
            if name == "effect_evaluation":
                continue
            self.assertEqual(fields, expected_success_fields[name], name)
        self.assertEqual(
            set(expected_success_fields["effect_evaluation"])
            - set(historical_success_fields["effect_evaluation"]),
            {"runtime"},
        )
        self.assertEqual(
            set(historical_success_fields["effect_evaluation"]),
            set(expected_success_fields["effect_evaluation"]) - {"runtime"},
        )
        self.assertEqual(
            len(expected_success_fields["effect_evaluation"]),
            len(historical_success_fields["effect_evaluation"]) + 1,
        )
        self.assertEqual(
            baseline["api_contract"]["http_errors"],
            {
                name: contract["status"]
                for name, contract in api_contract["errors"].items()
            },
        )
        self.assertEqual(
            baseline["api_contract"]["sse"]["content_type"],
            api_contract["success"]["events"]["content_type"],
        )
        self.assertEqual(
            baseline["api_contract"]["sse"]["event_name"],
            api_contract["success"]["events"]["event_name"],
        )
        self.assertEqual(
            baseline["api_contract"]["sse"]["required_keys"],
            api_contract["success"]["events"]["event_required_keys"],
        )
        self.assertEqual(len(baseline["deepseek_contract"]["case_ids"]), 8)
        self.assertTrue(baseline["deepseek_contract"]["current_schema_error_retryable"])
        self.assertFalse(
            baseline["deepseek_contract"]["task_2_target_schema_error_retryable"]
        )
        self.assertFalse(baseline["deepseek_contract"]["contains_real_key"])
        self.assertFalse(baseline["deepseek_contract"]["real_call_verified"])

    def test_annotation_schema_and_source_policy_are_valid(self):
        samples_dir = PROJECT_ROOT / "samples"
        config = _load_effect_config(samples_dir)
        bundle = _load_annotations(samples_dir, config)
        schema = json.loads(
            (samples_dir / "annotations" / "schema.json").read_text(encoding="utf-8")
        )
        generated_schema = AnnotationBundle.model_json_schema()

        for key, value in generated_schema.items():
            self.assertEqual(schema.get(key), value)
        risk_clause_keys = {(item.contract_id, item.clause_id) for item in bundle.risks}
        clause_keys = {(item.contract_id, item.clause_id) for item in bundle.clauses}
        self.assertEqual(len(bundle.contracts), 23)
        self.assertEqual(len(bundle.clauses), 174)
        self.assertEqual(len(risk_clause_keys), 65)
        self.assertEqual(len(clause_keys - risk_clause_keys), 109)
        self.assertEqual(len(bundle.related_clauses), 20)
        self.assertEqual(len(bundle.memory), 20)
        self.assertEqual(len(bundle.prompt_injection), 10)
        self.assertEqual(config["related_clause_dataset"]["revision"], "hybrid-retrieval-v2")
        self.assertEqual(config["related_clause_dataset"]["case_count"], 20)
        self.assertEqual(config["related_clause_dataset"]["risk_type_count"], 8)
        self.assertEqual(len({item.risk_type for item in bundle.related_clauses}), 8)
        self.assertTrue(
            all(item.source.source_type == "synthetic" for item in bundle.contracts)
        )
        self.assertTrue(all(item.source.source_type == "synthetic" for item in bundle.memory))
        self.assertTrue(all("客户合同" in item.source.note or "测试夹具" in item.source.note for item in bundle.contracts))

        comparison = evaluate_memory_comparison(bundle.memory)
        self.assertEqual(comparison["sample_count"], 20)
        self.assertEqual(comparison["without_memory_consistent_count"], 11)
        self.assertEqual(comparison["with_memory_consistent_count"], 11)
        self.assertEqual(comparison["consistency_delta"], 0)
        self.assertFalse(comparison["improved"])
        self.assertEqual(comparison["conclusion"], "not_improved")

    def test_dataset_minimums_are_enforced(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            samples_dir = Path(temp_dir) / "samples"
            shutil.copytree(PROJECT_ROOT / "samples", samples_dir)
            contracts_path = samples_dir / "annotations" / "contracts.json"
            clauses_path = samples_dir / "annotations" / "clauses.json"
            risks_path = samples_dir / "annotations" / "risks.json"
            contracts = json.loads(contracts_path.read_text(encoding="utf-8"))
            clauses = json.loads(clauses_path.read_text(encoding="utf-8"))
            risks = json.loads(risks_path.read_text(encoding="utf-8"))
            removed_ids = {item["contract_id"] for item in contracts["items"][-4:]}
            contracts["items"] = contracts["items"][:-4]
            clauses["items"] = [
                item for item in clauses["items"] if item["contract_id"] not in removed_ids
            ]
            risks["items"] = [
                item for item in risks["items"] if item["contract_id"] not in removed_ids
            ]
            contracts_path.write_text(json.dumps(contracts, ensure_ascii=False), encoding="utf-8")
            clauses_path.write_text(json.dumps(clauses, ensure_ascii=False), encoding="utf-8")
            risks_path.write_text(json.dumps(risks, ensure_ascii=False), encoding="utf-8")

            summary = run_effect_evaluation(
                {"samples_dir": str(samples_dir), "output_dir": temp_dir}
            )

        self.assertEqual(summary["status"], "annotation_failed")
        self.assertIn("contract annotations require at least 20", summary["evaluation_failures"][0]["reason"])

    def test_manifest_dataset_minimums_cannot_drift_from_task_thresholds(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            samples_dir = Path(temp_dir) / "samples"
            shutil.copytree(PROJECT_ROOT / "samples", samples_dir)
            manifest_path = samples_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["effect_evaluation"]["dataset_minimums"]["contracts"] = 1
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False),
                encoding="utf-8",
            )

            summary = run_effect_evaluation(
                {"samples_dir": str(samples_dir), "output_dir": temp_dir}
            )

        self.assertEqual(summary["status"], "annotation_failed")
        self.assertIn(
            "dataset minimums do not match",
            summary["evaluation_failures"][0]["reason"],
        )

    def test_runtime_summary_hashes_request_ids_and_records_usage(self):
        runs = [
            {
                "task": {
                    "logs": [
                        {
                            "trace_summary": {
                                "versions": {"prompt": "prompt-v-test"},
                                "provider": {
                                    "mode": "openai_compatible",
                                    "calls": [
                                        {
                                            "provider_request_id": "req-sensitive-1",
                                            "prompt_tokens": 100,
                                            "completion_tokens": 20,
                                            "latency_ms": 10,
                                            "estimated_cost": "0.1",
                                            "cost_status": "calculated",
                                            "error_type": "",
                                        },
                                        {
                                            "provider_request_id": "req-sensitive-2",
                                            "prompt_tokens": 50,
                                            "completion_tokens": 0,
                                            "latency_ms": 20,
                                            "estimated_cost": None,
                                            "cost_status": "未配置",
                                            "error_type": "schema_error",
                                        },
                                    ],
                                },
                            }
                        }
                    ]
                }
            }
        ]

        runtime = _runtime_summary(runs, [], "stability-1").model_dump(mode="json")
        serialized = json.dumps(runtime, ensure_ascii=False)
        measurements = {
            item["measurement"]: item["value"] for item in runtime["measurements"]
        }

        self.assertEqual(runtime["provider_call_count"], 2)
        self.assertEqual(runtime["provider_success_count"], 1)
        self.assertEqual(len(runtime["request_id_hashes"]), 2)
        self.assertNotIn("req-sensitive", serialized)
        self.assertEqual(measurements["total_tokens"], 170)
        self.assertIsNone(measurements["estimated_cost"])
        self.assertEqual(
            measurements["cost_status"],
            "partial_unavailable:calculated,未配置",
        )
        self.assertEqual(measurements["provider_latency_p95"], 20)
        self.assertEqual(measurements["prompt_versions"], "prompt-v-test")

        calls = runs[0]["task"]["logs"][0]["trace_summary"]["provider"]["calls"]
        calls[1]["estimated_cost"] = "0.2"
        calls[1]["cost_status"] = "calculated"
        complete_runtime = _runtime_summary(runs, [], "stability-1").model_dump(
            mode="json"
        )
        complete_measurements = {
            item["measurement"]: item["value"]
            for item in complete_runtime["measurements"]
        }
        self.assertEqual(complete_measurements["estimated_cost"], 0.3)
        self.assertEqual(complete_measurements["cost_status"], "calculated")

    def test_retrieval_repair_requires_candidates_and_evidence_recovery(self):
        def log(tool_name, decision, status="success"):
            return {
                "tool_name": tool_name,
                "status": status,
                "trace_summary": {"decision": decision},
            }

        runs = [
            {
                "contract_id": "retrieval-only",
                "task": {
                    "logs": [
                        log(
                            "plan_review_action",
                            {
                                "type": "planner",
                                "action": "RETRIEVE_AGAIN",
                                "reason_code": "RETRIEVAL_INSUFFICIENT",
                            },
                        ),
                        log(
                            "retrieve_related_clauses",
                            {"type": "retrieval", "candidate_count": 1},
                        ),
                    ]
                },
            },
            {
                "contract_id": "evidence-recovered",
                "task": {
                    "logs": [
                        log(
                            "plan_review_action",
                            {
                                "type": "planner",
                                "action": "RETRIEVE_AGAIN",
                                "reason_code": "EVIDENCE_MISSING",
                            },
                        ),
                        log(
                            "retrieve_related_clauses",
                            {"type": "retrieval", "candidate_count": 1},
                        ),
                        log(
                            "verify_evidence",
                            {"type": "evidence_verifier", "is_valid": True},
                        ),
                    ]
                },
            },
            {
                "contract_id": "empty-repair",
                "task": {
                    "logs": [
                        log(
                            "plan_review_action",
                            {
                                "type": "planner",
                                "action": "RETRIEVE_AGAIN",
                                "reason_code": "RETRIEVAL_INSUFFICIENT",
                            },
                        ),
                        log(
                            "retrieve_related_clauses",
                            {"type": "retrieval", "candidate_count": 0},
                        ),
                    ]
                },
            },
            {
                "contract_id": "evidence-still-invalid",
                "task": {
                    "logs": [
                        log(
                            "plan_review_action",
                            {
                                "type": "planner",
                                "action": "RETRIEVE_AGAIN",
                                "reason_code": "EVIDENCE_MISSING",
                            },
                        ),
                        log(
                            "retrieve_related_clauses",
                            {"type": "retrieval", "candidate_count": 1},
                        ),
                        log(
                            "verify_evidence",
                            {"type": "evidence_verifier", "is_valid": False},
                        ),
                    ]
                },
            },
        ]

        checks = _retrieval_repair_checks(runs)

        self.assertEqual([passed for passed, _failure in checks], [True, True, False, False])
        self.assertEqual(
            [failure.sample_id for _passed, failure in checks],
            [
                "retrieval-only:1",
                "evidence-recovered:1",
                "empty-repair:1",
                "evidence-still-invalid:1",
            ],
        )

    def test_schema_drift_is_recorded_as_invalid_annotation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            samples_dir = Path(temp_dir) / "samples"
            shutil.copytree(PROJECT_ROOT / "samples", samples_dir)
            schema_path = samples_dir / "annotations" / "schema.json"
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            schema["properties"]["risks"]["minItems"] = 2
            schema_path.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")

            summary = run_effect_evaluation(
                {"samples_dir": str(samples_dir), "output_dir": str(Path(temp_dir) / "out")}
            )

        self.assertEqual(summary["status"], "annotation_failed")
        self.assertEqual(summary["evaluation_failures"][0]["sample_id"], "schema.json")
        self.assertEqual(summary["evaluation_failures"][0]["failure_type"], "invalid_annotation")

    def test_duplicate_rule_labels_are_recorded_as_invalid_annotation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            samples_dir = Path(temp_dir) / "samples"
            shutil.copytree(PROJECT_ROOT / "samples", samples_dir)
            clauses_path = samples_dir / "annotations" / "clauses.json"
            clauses = json.loads(clauses_path.read_text(encoding="utf-8"))
            clauses["items"][0]["expected_rule_ids"].append("NDA-R001")
            clauses_path.write_text(json.dumps(clauses, ensure_ascii=False), encoding="utf-8")

            summary = run_effect_evaluation(
                {"samples_dir": str(samples_dir), "output_dir": str(Path(temp_dir) / "out")}
            )

        self.assertEqual(summary["status"], "annotation_failed")
        self.assertEqual(summary["evaluation_failures"][0]["failure_type"], "invalid_annotation")

    def test_evidence_span_outside_annotated_clause_is_invalid(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            samples_dir = Path(temp_dir) / "samples"
            shutil.copytree(PROJECT_ROOT / "samples", samples_dir)
            risks_path = samples_dir / "annotations" / "risks.json"
            risks = json.loads(risks_path.read_text(encoding="utf-8"))
            risks["items"][0]["evidence_spans"] = ["不存在于对应条款的证据"]
            risks_path.write_text(json.dumps(risks, ensure_ascii=False), encoding="utf-8")

            summary = run_effect_evaluation(
                {"samples_dir": str(samples_dir), "output_dir": str(Path(temp_dir) / "out")}
            )

        self.assertEqual(summary["status"], "annotation_failed")
        self.assertIn("evidence span", summary["evaluation_failures"][0]["reason"])

    def test_effect_evaluation_reports_metrics_failures_and_versions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            summary = run_effect_evaluation(
                {
                    "samples_dir": str(PROJECT_ROOT / "samples"),
                    "output_dir": temp_dir,
                }
            )
            summary_path = Path(summary["summary_path"])
            markdown_path = Path(summary["markdown_path"])
            persisted = json.loads(summary_path.read_text(encoding="utf-8"))
            markdown = markdown_path.read_text(encoding="utf-8")
            memory_paths = list(
                (Path(temp_dir) / "effect").glob("*_memory_comparison.json")
            )
            self.assertEqual(len(memory_paths), 1)
            memory_comparison = json.loads(memory_paths[0].read_text(encoding="utf-8"))

        metrics = {item["metric"]: item for item in summary["metrics"]}
        expected_metrics = {
            "nda_classification_accuracy",
            "non_nda_rejection_rate",
            "clause_boundary_accuracy",
            "clause_type_accuracy",
            "playbook_recall_at_k",
            "related_clause_recall_at_k",
            "risk_precision",
            "risk_recall",
            "risk_f1",
            "evidence_span_hit_rate",
            "manual_review_trigger_rate",
            "report_filter_accuracy",
            "tool_call_success_rate",
            "end_to_end_success_rate",
            "planner_action_accuracy",
            "invalid_tool_action_rate",
            "llm_json_valid_rate",
            "llm_schema_repair_rate",
            "retrieval_repair_success_rate",
            "retry_recovery_rate",
            "unsupported_finding_rate",
            "memory_preference_consistency",
            "prompt_injection_block_rate",
        }
        self.assertEqual(set(metrics), expected_metrics)
        self.assertEqual(summary["evaluation_type"], "effect")
        self.assertEqual(summary["sample_count"], 23)
        self.assertEqual(summary["claim"], EFFECT_NO_PRODUCTION_CLAIM)
        self.assertEqual(summary["status"], "completed_with_failures")
        self.assertEqual(memory_comparison["consistency_delta"], 0)
        self.assertFalse(memory_comparison["improved"])
        self.assertEqual(memory_comparison["conclusion"], "not_improved")
        self.assertEqual(metrics["related_clause_recall_at_k"]["sample_count"], 20)
        self.assertEqual(metrics["related_clause_recall_at_k"]["passed_count"], 20)
        self.assertEqual(metrics["related_clause_recall_at_k"]["failed_count"], 0)
        self.assertEqual(metrics["related_clause_recall_at_k"]["score"], 1.0)
        self.assertTrue(metrics["related_clause_recall_at_k"]["threshold_met"])
        self.assertEqual(metrics["related_clause_recall_at_k"]["failure_samples"], [])
        for metric_name in [
            "playbook_recall_at_k",
            "prompt_injection_block_rate",
        ]:
            self.assertTrue(metrics[metric_name]["threshold_met"], metric_name)
            self.assertGreaterEqual(
                metrics[metric_name]["score"],
                metrics[metric_name]["threshold"],
                metric_name,
            )
        self.assertTrue(
            all(
                set(["sample_count", "passed_count", "failed_count", "failure_samples"]).issubset(metric)
                for metric in summary["metrics"]
            )
        )
        self.assertEqual(summary["versions"]["annotation_version"], "effect-v2")
        self.assertEqual(summary["versions"]["playbook_version"], "nda-v1")
        self.assertTrue(summary["versions"]["code_version"])
        self.assertIn("llm_mode", summary["versions"])
        self.assertIn("embedding_mode", summary["versions"])
        self.assertIn("thresholds", summary["versions"]["parameters"])
        self.assertEqual(summary["runtime"]["provider_call_count"], 0)
        self.assertEqual(summary["runtime"]["request_id_hashes"], [])
        measurements = {
            item["measurement"]: item for item in summary["runtime"]["measurements"]
        }
        self.assertEqual(measurements["total_tokens"]["value"], 0)
        self.assertIsNone(measurements["estimated_cost"]["value"])
        self.assertIn("provider_latency_p95", measurements)
        self.assertEqual(summary_path.stem, summary["evaluation_id"])
        self.assertEqual(markdown_path.stem, summary["evaluation_id"])
        self.assertEqual(persisted, summary)
        self.assertIn("- 标注版本：effect-v2", markdown)
        self.assertIn("- Playbook Recall@K：3", markdown)
        self.assertIn("- 相关条款 Recall@K：1", markdown)
        self.assertIn('"risk_f1": 0.8', markdown)

    def test_missing_annotation_file_is_recorded_without_fake_metrics(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            samples_dir = Path(temp_dir) / "samples"
            shutil.copytree(PROJECT_ROOT / "samples", samples_dir)
            (samples_dir / "annotations" / "contracts.json").unlink()

            summary = run_effect_evaluation(
                {"samples_dir": str(samples_dir), "output_dir": str(Path(temp_dir) / "out")}
            )

        self.assertEqual(summary["status"], "annotation_failed")
        self.assertEqual(summary["sample_count"], 0)
        self.assertEqual(summary["metrics"], [])
        self.assertEqual(summary["evaluation_failures"][0]["failure_type"], "missing_annotation")

    def test_invalid_related_source_is_recorded_as_invalid_annotation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            samples_dir = Path(temp_dir) / "samples"
            shutil.copytree(PROJECT_ROOT / "samples", samples_dir)
            related_path = samples_dir / "annotations" / "related_clauses.json"
            related = json.loads(related_path.read_text(encoding="utf-8"))
            related["source"] = {"source_type": "customer_contract", "note": "unauthorized"}
            related_path.write_text(json.dumps(related, ensure_ascii=False), encoding="utf-8")

            summary = run_effect_evaluation(
                {"samples_dir": str(samples_dir), "output_dir": str(Path(temp_dir) / "out")}
            )

        self.assertEqual(summary["status"], "annotation_failed")
        self.assertEqual(summary["evaluation_failures"][0]["failure_type"], "invalid_annotation")

    def test_incomplete_thresholds_are_recorded_as_invalid_annotation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            samples_dir = Path(temp_dir) / "samples"
            shutil.copytree(PROJECT_ROOT / "samples", samples_dir)
            manifest_path = samples_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            del manifest["effect_evaluation"]["parameters"]["thresholds"]["risk_f1"]
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

            summary = run_effect_evaluation(
                {"samples_dir": str(samples_dir), "output_dir": str(Path(temp_dir) / "out")}
            )

        self.assertEqual(summary["status"], "annotation_failed")
        self.assertEqual(summary["evaluation_failures"][0]["failure_type"], "invalid_annotation")

    def test_missing_sample_text_is_recorded_as_parse_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            samples_dir = Path(temp_dir) / "samples"
            shutil.copytree(PROJECT_ROOT / "samples", samples_dir)
            (samples_dir / "nda_sample_01.txt").unlink()

            summary = run_effect_evaluation(
                {"samples_dir": str(samples_dir), "output_dir": str(Path(temp_dir) / "out")}
            )

        failures = {item["sample_id"]: item for item in summary["evaluation_failures"]}
        self.assertEqual(failures["nda-01"]["failure_type"], "parse_failed")
        e2e = next(item for item in summary["metrics"] if item["metric"] == "end_to_end_success_rate")
        self.assertTrue(any(item["sample_id"] == "nda-01" for item in e2e["failure_samples"]))

    def test_extra_extracted_clause_reduces_boundary_and_type_accuracy(self):
        samples_dir = PROJECT_ROOT / "samples"
        config = _load_effect_config(samples_dir)
        bundle = _load_annotations(samples_dir, config)
        runs = []
        for contract in bundle.contracts:
            clauses = [
                {
                    "clause_id": item.clause_id,
                    "text": item.exact_text,
                    "clause_type": item.clause_type,
                }
                for item in bundle.clauses
                if item.contract_id == contract.contract_id
            ]
            if contract.contract_id == "nda-01":
                clauses.append(
                    {
                        "clause_id": "CL-999",
                        "text": "额外生成但没有人工标注的条款。",
                        "clause_type": "其他",
                    }
                )
            runs.append({"contract_id": contract.contract_id, "task": {"clauses": clauses}})

        metrics = {
            item.metric: item
            for item in _clause_metrics(
                bundle,
                runs,
                config["parameters"]["thresholds"],
            )
        }

        for metric_name in ("clause_boundary_accuracy", "clause_type_accuracy"):
            metric = metrics[metric_name]
            self.assertEqual(metric.sample_count, len(bundle.clauses) + 1)
            self.assertEqual(metric.failed_count, 1)
            self.assertEqual(metric.failure_samples[0].sample_id, "nda-01:unexpected:CL-999")

    def test_short_evidence_fragment_does_not_count_as_span_hit(self):
        samples_dir = PROJECT_ROOT / "samples"
        config = _load_effect_config(samples_dir)
        bundle = _load_annotations(samples_dir, config)
        runs = []
        for contract in bundle.contracts:
            risks = []
            for annotation in bundle.risks:
                if annotation.contract_id != contract.contract_id:
                    continue
                evidence = annotation.evidence_spans[0]
                if annotation.contract_id == "nda-01":
                    evidence = "保"
                risks.append(
                    {
                        "risk_id": annotation.annotation_id,
                        "clause_id": annotation.clause_id,
                        "risk_type": annotation.risk_type,
                        "evidence_text": evidence,
                    }
                )
            runs.append(
                {"contract_id": contract.contract_id, "task": {"risk_findings": risks}}
            )

        metric = _evidence_metric(
            bundle,
            runs,
            config["parameters"]["thresholds"],
        )

        self.assertEqual(metric.sample_count, len(bundle.risks))
        self.assertEqual(metric.failed_count, 1)
        self.assertEqual(metric.failure_samples[0].sample_id, "nda-01-risk-01")

    def test_model_failure_status_is_recorded_separately(self):
        class FailedState:
            def to_dict(self):
                return {
                    "task_id": "task-model-failed",
                    "status": "LLM_OUTPUT_INVALID",
                    "message": "controlled model failure",
                    "contract_classification": {},
                    "clauses": [],
                    "matched_rules": [],
                    "risk_findings": [],
                    "logs": [],
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch(
                "services.evaluation_service.ReviewOrchestratorAgent.run_sync",
                return_value=FailedState(),
            ):
                summary = run_effect_evaluation(
                    {
                        "samples_dir": str(PROJECT_ROOT / "samples"),
                        "output_dir": temp_dir,
                    }
                )

        self.assertEqual(len(summary["evaluation_failures"]), 23)
        self.assertTrue(
            all(item["failure_type"] == "model_call_failed" for item in summary["evaluation_failures"])
        )

    def test_retrieval_failure_status_is_not_reported_as_model_failure(self):
        class FailedState:
            def to_dict(self):
                return {
                    "task_id": "task-retrieval-failed",
                    "status": "RETRIEVAL_FAILED",
                    "message": "controlled retrieval failure",
                    "contract_classification": {},
                    "clauses": [],
                    "matched_rules": [],
                    "risk_findings": [],
                    "logs": [],
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch(
                "services.evaluation_service.ReviewOrchestratorAgent.run_sync",
                return_value=FailedState(),
            ):
                summary = run_effect_evaluation(
                    {
                        "samples_dir": str(PROJECT_ROOT / "samples"),
                        "output_dir": temp_dir,
                    }
                )

        self.assertEqual(len(summary["evaluation_failures"]), 23)
        self.assertTrue(
            all(item["failure_type"] == "retrieval_failed" for item in summary["evaluation_failures"])
        )


if __name__ == "__main__":
    unittest.main()
