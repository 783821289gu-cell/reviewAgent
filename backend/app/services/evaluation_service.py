from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from collections import Counter
from hashlib import sha256
from math import ceil
import json
import os
import re
import subprocess
from io import BytesIO

from docx import Document as WordDocument
from pydantic import ValidationError

from config import settings
from models.evaluation import (
    AnnotationBundle,
    AnnotationSource,
    EffectEvaluationSummary,
    EvaluationMeasurement,
    EvaluationMetric,
    EvaluationRuntime,
    EvaluationVersion,
    FailureSample,
)
from models.log import StepLog
from models.memory import build_semantic_preference
from models.review import ReviewPosition, ReviewStatus
from providers.embedding_provider import create_embedding_provider
from services.event_service import ReviewEventStore
from services.feedback_service import apply_feedback_to_task
from services.context_builder import ContextBudgetExceededError, build_review_context
from services.log_service import invoke_tool
from services.memory_service import evaluate_memory_comparison, write_memory
from services.review_service import ReviewOrchestratorAgent
from tools.contracts import tool_contracts
from tools.registry import tool_registry


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SAMPLES_DIR = PROJECT_ROOT / "samples"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "evaluation"
NO_PRODUCTION_CLAIM = "基础评测仅验证流程跑通，不代表生产级准确率。"
EFFECT_NO_PRODUCTION_CLAIM = "效果评测仅报告当前人工标注集上的实际指标和失败样本，不代表生产级准确率。"
ANNOTATION_SCHEMA_VERSION = "effect-v3"
MODEL_FAILURE_STATUSES = {"LLM_OUTPUT_INVALID"}
EFFECT_METRIC_NAMES = {
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
    "memory_retrieval_recall_at_k",
    "memory_retrieval_mrr",
    "memory_embedding_coverage",
    "embedding_call_success_rate",
    "prompt_injection_block_rate",
}
ALLOWED_PLANNER_ACTIONS = {
    "RETRIEVE_AGAIN",
    "ANALYZE_AGAIN",
    "REQUEST_HUMAN_REVIEW",
    "TERMINATE",
}
MIN_CONTRACT_ANNOTATIONS = 20
MIN_RISK_CLAUSE_ANNOTATIONS = 50
MIN_NORMAL_CLAUSE_ANNOTATIONS = 100
MIN_RELATED_CLAUSE_ANNOTATIONS = 20
MIN_MEMORY_ANNOTATIONS = 20
MIN_PROMPT_INJECTION_ANNOTATIONS = 10
DATASET_MINIMUMS = {
    "contracts": MIN_CONTRACT_ANNOTATIONS,
    "risk_clauses": MIN_RISK_CLAUSE_ANNOTATIONS,
    "normal_clauses": MIN_NORMAL_CLAUSE_ANNOTATIONS,
    "related_clause_cases": MIN_RELATED_CLAUSE_ANNOTATIONS,
    "memory_cases": MIN_MEMORY_ANNOTATIONS,
    "prompt_injection_cases": MIN_PROMPT_INJECTION_ANNOTATIONS,
}
SUMMARY_FIELDS = (
    "task_ran",
    "document_parsed",
    "clauses_structured",
    "playbook_hit",
    "risk_has_evidence",
    "feedback_memory_written",
    "report_exported",
)


class AnnotationLoadError(ValueError):
    def __init__(self, failure_type: str, sample_id: str, message: str):
        super().__init__(message)
        self.failure_type = failure_type
        self.sample_id = sample_id


def run_basic_evaluation(tool_input: dict | None = None) -> dict:
    tool_input = tool_input or {}
    samples_dir = Path(str(tool_input.get("samples_dir") or DEFAULT_SAMPLES_DIR))
    output_dir = Path(
        str(
            tool_input.get("output_dir")
            or os.getenv("REVIEW_AGENT_EVALUATION_OUTPUT_DIR")
            or DEFAULT_OUTPUT_DIR
        )
    )
    report_dir = Path(str(tool_input.get("report_dir") or output_dir / "reports"))
    memory_db_path = str(tool_input.get("memory_db_path") or output_dir / "evaluation_memory.sqlite3")

    manifest = _load_manifest(samples_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)

    results = [
        _run_sample(sample, samples_dir, report_dir, memory_db_path)
        for sample in manifest
    ]
    summary = {
        "evaluation_id": f"eval_{uuid4().hex[:12]}",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "sample_count": len(results),
        "passed_count": sum(1 for result in results if _sample_passed(result)),
        "claim": NO_PRODUCTION_CLAIM,
        "results": results,
    }

    summary_path = output_dir / "evaluation_summary.json"
    markdown_path = output_dir / "evaluation_summary.md"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(_summary_markdown(summary), encoding="utf-8")
    summary["summary_path"] = str(summary_path)
    summary["markdown_path"] = str(markdown_path)
    return summary


def run_effect_evaluation(tool_input: dict | None = None) -> dict:
    tool_input = tool_input or {}
    run_label = str(tool_input.get("run_label") or "").strip()[:80]
    samples_dir = Path(str(tool_input.get("samples_dir") or DEFAULT_SAMPLES_DIR))
    output_dir = (
        Path(
            str(
                tool_input.get("output_dir")
                or os.getenv("REVIEW_AGENT_EVALUATION_OUTPUT_DIR")
                or DEFAULT_OUTPUT_DIR
            )
        )
        / "effect"
    )
    evaluation_id = f"effect_{uuid4().hex[:12]}"
    created_at = datetime.now(timezone.utc).isoformat()

    try:
        effect_config = _load_effect_config(samples_dir)
    except AnnotationLoadError as exc:
        versions = _collect_versions(ANNOTATION_SCHEMA_VERSION, {})
        return _write_effect_summary(
            EffectEvaluationSummary(
                evaluation_id=evaluation_id,
                evaluation_type="effect",
                created_at=created_at,
                status="annotation_failed",
                claim=EFFECT_NO_PRODUCTION_CLAIM,
                sample_count=0,
                metrics=[],
                versions=versions,
                runtime=_empty_runtime(run_label),
                evaluation_failures=[_load_failure(exc)],
            ),
            output_dir,
        )

    versions = _collect_versions(
        str(effect_config["annotation_version"]),
        dict(effect_config["parameters"]),
    )
    try:
        annotations = _load_annotations(samples_dir, effect_config)
        annotations = _select_contract_annotations(
            annotations,
            tool_input.get("contract_ids"),
        )
    except AnnotationLoadError as exc:
        return _write_effect_summary(
            EffectEvaluationSummary(
                evaluation_id=evaluation_id,
                evaluation_type="effect",
                created_at=created_at,
                status="annotation_failed",
                claim=EFFECT_NO_PRODUCTION_CLAIM,
                sample_count=0,
                metrics=[],
                versions=versions,
                runtime=_empty_runtime(run_label),
                evaluation_failures=[_load_failure(exc)],
            ),
            output_dir,
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    memory_comparison = evaluate_memory_comparison(annotations.memory)
    _write_memory_comparison(
        output_dir,
        evaluation_id,
        created_at,
        memory_comparison,
    )
    report_dir = output_dir / "reports" / evaluation_id
    memory_db_path = str(output_dir / f"{evaluation_id}_memory.sqlite3")
    runs = [
        _run_effect_contract(annotation, samples_dir)
        for annotation in annotations.contracts
    ]
    metrics, additional_logs = _calculate_effect_metrics(
        annotations,
        runs,
        dict(effect_config["parameters"]),
        report_dir,
        memory_db_path,
    )
    planner_metrics, planner_logs = _planner_metrics(
        annotations,
        dict(effect_config["parameters"]["thresholds"]),
    )
    additional_logs.extend(planner_logs)
    metrics.extend(
        _operational_metrics(
            annotations,
            runs,
            additional_logs,
            dict(effect_config["parameters"]["thresholds"]),
        )
    )
    metrics.extend(planner_metrics)
    metrics.extend(
        _agent_metrics(
            annotations,
            runs,
            memory_comparison,
            dict(effect_config["parameters"]["thresholds"]),
        )
    )
    metrics.append(
        _embedding_call_success_metric(
            additional_logs,
            dict(effect_config["parameters"]["thresholds"]),
        )
    )
    runtime = _runtime_summary(runs, additional_logs, run_label)
    evaluation_failures = [
        FailureSample(
            sample_id=run["contract_id"],
            failure_type=run["failure_type"],
            reason=run["failure_reason"],
        )
        for run in runs
        if run["failure_type"]
    ]
    status = (
        "completed"
        if not evaluation_failures and all(metric.threshold_met for metric in metrics)
        else "completed_with_failures"
    )
    summary = EffectEvaluationSummary(
        evaluation_id=evaluation_id,
        evaluation_type="effect",
        created_at=created_at,
        status=status,
        claim=EFFECT_NO_PRODUCTION_CLAIM,
        sample_count=len(annotations.contracts),
        metrics=metrics,
        versions=versions,
        runtime=runtime,
        evaluation_failures=evaluation_failures,
    )
    return _write_effect_summary(summary, output_dir)


def _load_effect_config(samples_dir: Path) -> dict:
    manifest_path = samples_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AnnotationLoadError("missing_annotation", "manifest.json", "samples manifest not found") from exc
    except json.JSONDecodeError as exc:
        raise AnnotationLoadError("invalid_annotation", "manifest.json", "samples manifest is invalid JSON") from exc
    config = manifest.get("effect_evaluation")
    if not isinstance(config, dict):
        raise AnnotationLoadError(
            "missing_annotation",
            "manifest.json",
            "effect_evaluation config is missing",
        )
    required = {
        "annotation_version",
        "schema",
        "contracts",
        "clauses",
        "risks",
        "related_clauses",
        "memory",
        "prompt_injection",
        "parameters",
    }
    missing = sorted(required - set(config))
    if missing:
        raise AnnotationLoadError(
            "missing_annotation",
            "manifest.json",
            f"effect_evaluation config is missing: {', '.join(missing)}",
        )
    parameters = config.get("parameters")
    if not isinstance(parameters, dict) or not isinstance(parameters.get("thresholds"), dict):
        raise AnnotationLoadError("invalid_annotation", "manifest.json", "effect evaluation parameters are invalid")
    for name in (
        "playbook_recall_k",
        "related_clause_recall_k",
        "memory_retrieval_k",
    ):
        value = parameters.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise AnnotationLoadError("invalid_annotation", "manifest.json", f"{name} must be a positive integer")
    thresholds = parameters["thresholds"]
    if set(thresholds) != EFFECT_METRIC_NAMES:
        raise AnnotationLoadError("invalid_annotation", "manifest.json", "effect metric thresholds are incomplete")
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 <= float(value) <= 1
        for value in thresholds.values()
    ):
        raise AnnotationLoadError("invalid_annotation", "manifest.json", "effect metric thresholds must be between 0 and 1")
    if config.get("dataset_minimums") != DATASET_MINIMUMS:
        raise AnnotationLoadError(
            "invalid_annotation",
            "manifest.json",
            "effect evaluation dataset minimums do not match the required task thresholds",
        )
    return config


def _load_annotations(samples_dir: Path, config: dict) -> AnnotationBundle:
    schema_path = _safe_annotation_path(samples_dir, str(config["schema"]))
    schema = _read_annotation_json(schema_path, "schema.json")
    _validate_annotation_schema(schema)

    payloads = {}
    for key in (
        "contracts",
        "clauses",
        "risks",
        "related_clauses",
        "memory",
        "prompt_injection",
    ):
        path = _safe_annotation_path(samples_dir, str(config[key]))
        payload = _read_annotation_json(path, path.name)
        if payload.get("annotation_version") != config["annotation_version"]:
            raise AnnotationLoadError(
                "invalid_annotation",
                path.name,
                "annotation version does not match manifest",
            )
        if key in {"related_clauses", "prompt_injection"}:
            try:
                AnnotationSource.model_validate(payload.get("source"))
            except ValidationError as exc:
                raise AnnotationLoadError(
                    "invalid_annotation",
                    path.name,
                    f"{key} source metadata is invalid",
                ) from exc
        items = (
            payload.get("cases")
            if key in {"related_clauses", "prompt_injection"}
            else payload.get("items")
        )
        if not isinstance(items, list):
            raise AnnotationLoadError("missing_annotation", path.name, "annotation items are missing")
        payloads[key] = items

    try:
        bundle = AnnotationBundle.model_validate(
            {
                "schema_version": config["annotation_version"],
                **payloads,
            }
        )
    except ValidationError as exc:
        raise AnnotationLoadError("invalid_annotation", "annotation_bundle", str(exc)) from exc
    _validate_annotation_references(bundle)
    return bundle


def _validate_annotation_schema(schema: dict) -> None:
    generated_schema = AnnotationBundle.model_json_schema()
    declared_schema = {key: schema.get(key) for key in generated_schema}
    if declared_schema != generated_schema:
        raise AnnotationLoadError(
            "invalid_annotation",
            "schema.json",
            "annotation schema does not match the runtime annotation model",
        )


def _safe_annotation_path(samples_dir: Path, relative_path: str) -> Path:
    root = samples_dir.resolve()
    path = (samples_dir / relative_path).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise AnnotationLoadError("invalid_annotation", relative_path, "annotation path leaves samples directory") from exc
    return path


def _read_annotation_json(path: Path, sample_id: str) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AnnotationLoadError("missing_annotation", sample_id, f"annotation file not found: {path.name}") from exc
    except json.JSONDecodeError as exc:
        raise AnnotationLoadError("invalid_annotation", sample_id, f"annotation file is invalid JSON: {path.name}") from exc
    if not isinstance(payload, dict):
        raise AnnotationLoadError("invalid_annotation", sample_id, "annotation file must contain an object")
    return payload


def _validate_annotation_references(bundle: AnnotationBundle) -> None:
    contract_ids = [item.contract_id for item in bundle.contracts]
    if len(contract_ids) != len(set(contract_ids)):
        raise AnnotationLoadError("invalid_annotation", "contracts.json", "contract_id values must be unique")
    clause_ids = [item.annotation_id for item in bundle.clauses]
    risk_ids = [item.annotation_id for item in bundle.risks]
    clause_key_list = [(item.contract_id, item.clause_id) for item in bundle.clauses]
    if len(clause_ids) != len(set(clause_ids)):
        raise AnnotationLoadError("invalid_annotation", "clauses.json", "clause annotation_id values must be unique")
    if len(risk_ids) != len(set(risk_ids)):
        raise AnnotationLoadError("invalid_annotation", "risks.json", "risk annotation_id values must be unique")
    if len(clause_key_list) != len(set(clause_key_list)):
        raise AnnotationLoadError("invalid_annotation", "clauses.json", "contract clause labels must be unique")
    clause_keys = set(clause_key_list)
    clauses_by_key = {
        (item.contract_id, item.clause_id): item
        for item in bundle.clauses
    }
    contracts_by_id = {item.contract_id: item for item in bundle.contracts}
    for contract in bundle.contracts:
        planner_keys = [
            (item.reason_code, item.target_clause_id)
            for item in contract.planner_expectations
        ]
        if len(planner_keys) != len(set(planner_keys)):
            raise AnnotationLoadError(
                "invalid_annotation",
                contract.contract_id,
                "planner expectations must be unique by reason and target clause",
            )
    for clause in bundle.clauses:
        if clause.contract_id not in contract_ids:
            raise AnnotationLoadError("missing_annotation", clause.annotation_id, "clause contract annotation is missing")
    for risk in bundle.risks:
        if (risk.contract_id, risk.clause_id) not in clause_keys:
            raise AnnotationLoadError("missing_annotation", risk.annotation_id, "risk clause annotation is missing")
        clause_text = _normalized_span(
            clauses_by_key[(risk.contract_id, risk.clause_id)].exact_text
        )
        if any(_normalized_span(span) not in clause_text for span in risk.evidence_spans):
            raise AnnotationLoadError(
                "invalid_annotation",
                risk.annotation_id,
                "risk evidence span is not contained in the annotated clause",
            )
        if risk.manual_review_expected and not contracts_by_id[risk.contract_id].manual_review_expected:
            raise AnnotationLoadError(
                "invalid_annotation",
                risk.annotation_id,
                "risk manual review expectation conflicts with the contract annotation",
            )
    for case in bundle.related_clauses:
        current_clause_id = case.current_clause.clause_id
        candidate_id_list = [item.clause_id for item in case.candidates]
        if len(candidate_id_list) != len(set(candidate_id_list)) or current_clause_id in candidate_id_list:
            raise AnnotationLoadError("invalid_annotation", case.case_id, "related clause candidate IDs are invalid")
        candidate_ids = set(candidate_id_list)
        missing = set(case.relevant_clause_ids) - candidate_ids
        if missing:
            raise AnnotationLoadError(
                "missing_annotation",
                case.case_id,
                f"related clause candidates are missing labels: {', '.join(sorted(missing))}",
            )
    memory_case_ids = [item.case_id for item in bundle.memory]
    if len(memory_case_ids) != len(set(memory_case_ids)):
        raise AnnotationLoadError(
            "invalid_annotation",
            "memory.json",
            "memory comparison case_id values must be unique",
        )
    injection_ids = [item.id for item in bundle.prompt_injection]
    if len(injection_ids) != len(set(injection_ids)):
        raise AnnotationLoadError(
            "invalid_annotation",
            "prompt_injection/cases.json",
            "prompt injection IDs must be unique",
        )
    for contract in bundle.contracts:
        for expectation in contract.planner_expectations:
            if (contract.contract_id, expectation.target_clause_id) not in clause_keys:
                raise AnnotationLoadError(
                    "missing_annotation",
                    contract.contract_id,
                    "planner target clause annotation is missing",
                )

    risk_clause_keys = {(item.contract_id, item.clause_id) for item in bundle.risks}
    normal_clause_count = len(clause_keys - risk_clause_keys)
    minimums = {
        "contract annotations": (len(bundle.contracts), MIN_CONTRACT_ANNOTATIONS),
        "risk clause annotations": (len(risk_clause_keys), MIN_RISK_CLAUSE_ANNOTATIONS),
        "normal clause annotations": (normal_clause_count, MIN_NORMAL_CLAUSE_ANNOTATIONS),
        "related clause annotations": (
            len(bundle.related_clauses),
            MIN_RELATED_CLAUSE_ANNOTATIONS,
        ),
        "Memory annotations": (len(bundle.memory), MIN_MEMORY_ANNOTATIONS),
        "Prompt Injection annotations": (
            len(bundle.prompt_injection),
            MIN_PROMPT_INJECTION_ANNOTATIONS,
        ),
    }
    for label, (actual, minimum) in minimums.items():
        if actual < minimum:
            raise AnnotationLoadError(
                "invalid_annotation",
                "annotation_bundle",
                f"{label} require at least {minimum} items; found {actual}",
            )


def _select_contract_annotations(
    bundle: AnnotationBundle,
    requested_contract_ids,
) -> AnnotationBundle:
    if requested_contract_ids is None:
        return bundle
    if (
        not isinstance(requested_contract_ids, list)
        or not requested_contract_ids
        or any(
            not isinstance(item, str) or not item.strip()
            for item in requested_contract_ids
        )
    ):
        raise AnnotationLoadError(
            "invalid_annotation",
            "contract_ids",
            "contract_ids must be a non-empty list of strings",
        )
    normalized_ids = [item.strip() for item in requested_contract_ids]
    if len(normalized_ids) != len(set(normalized_ids)):
        raise AnnotationLoadError(
            "invalid_annotation",
            "contract_ids",
            "contract_ids must be unique",
        )
    available_ids = {item.contract_id for item in bundle.contracts}
    missing = sorted(set(normalized_ids) - available_ids)
    if missing:
        raise AnnotationLoadError(
            "missing_annotation",
            "contract_ids",
            f"unknown contract IDs: {', '.join(missing)}",
        )
    selected_ids = set(normalized_ids)
    contracts_by_id = {item.contract_id: item for item in bundle.contracts}
    return bundle.model_copy(
        update={
            "contracts": [contracts_by_id[item] for item in normalized_ids],
            "clauses": [
                item for item in bundle.clauses if item.contract_id in selected_ids
            ],
            "risks": [
                item for item in bundle.risks if item.contract_id in selected_ids
            ],
        }
    )


def _write_memory_comparison(
    output_dir: Path,
    evaluation_id: str,
    created_at: str,
    comparison: dict,
) -> Path:
    path = output_dir / f"{evaluation_id}_memory_comparison.json"
    payload = {
        "evaluation_id": evaluation_id,
        "created_at": created_at,
        "claim": (
            "Memory effect is measured on the declared synthetic comparison cases; "
            "no improvement is claimed unless the measured consistency delta is positive."
        ),
        **comparison,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _collect_versions(annotation_version: str, parameters: dict) -> EvaluationVersion:
    code_version, git_dirty = _git_version()
    playbook_path = Path(__file__).resolve().parents[1] / "playbooks" / "nda.json"
    try:
        playbook_version = str(json.loads(playbook_path.read_text(encoding="utf-8")).get("playbook_version") or "unknown")
    except (OSError, json.JSONDecodeError):
        playbook_version = "unavailable"
    try:
        embedding_provider = create_embedding_provider(settings)
        embedding_model = embedding_provider.model or "unconfigured"
    except Exception:
        embedding_model = "unavailable"
    llm_model = (
        settings.llm_model or "unconfigured"
        if settings.llm_mode == "openai_compatible"
        else "no_external_llm"
    )
    return EvaluationVersion(
        annotation_version=annotation_version,
        code_version=code_version,
        git_dirty=git_dirty,
        playbook_version=playbook_version,
        llm_mode=settings.llm_mode,
        llm_model=llm_model,
        embedding_mode=settings.embedding_mode,
        embedding_model=embedding_model,
        parameters=parameters,
    )


def _git_version() -> tuple[str, bool | None]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=PROJECT_ROOT,
                capture_output=True,
                check=True,
                text=True,
                timeout=5,
            ).stdout.strip()
        )
        return commit or "unavailable", dirty
    except (OSError, subprocess.SubprocessError):
        return "unavailable", None


def _load_failure(exc: AnnotationLoadError) -> FailureSample:
    return FailureSample(
        sample_id=exc.sample_id,
        failure_type=exc.failure_type,
        reason=str(exc),
    )


def _write_effect_summary(summary: EffectEvaluationSummary, output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / f"{summary.evaluation_id}.json"
    markdown_path = output_dir / f"{summary.evaluation_id}.md"
    summary.summary_path = str(summary_path)
    summary.markdown_path = str(markdown_path)
    payload = summary.model_dump(mode="json")
    summary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(_effect_summary_markdown(payload), encoding="utf-8")
    return payload


def _run_effect_contract(annotation, samples_dir: Path) -> dict:
    event_store = ReviewEventStore()
    result = {
        "contract_id": annotation.contract_id,
        "annotation": annotation,
        "event_store": event_store,
        "task": {},
        "failure_type": "",
        "failure_reason": "",
    }
    try:
        sample_path = _safe_annotation_path(samples_dir, annotation.file)
        text = sample_path.read_text(encoding="utf-8")
        state = ReviewOrchestratorAgent(event_store).run_sync(
            file_name=f"{sample_path.stem}.docx",
            file_type="docx",
            content=_docx_bytes_from_text(text),
            review_position=_review_position(annotation.review_position),
        )
        task = state.to_dict()
        result["task"] = task
        if task["status"] == ReviewStatus.PARSE_FAILED.value:
            result["failure_type"] = "parse_failed"
            result["failure_reason"] = str(task.get("message") or "document parsing failed")
        elif task["status"] in MODEL_FAILURE_STATUSES:
            result["failure_type"] = "model_call_failed"
            result["failure_reason"] = str(task.get("message") or "model call failed")
        elif task["status"] == ReviewStatus.RETRIEVAL_FAILED.value:
            result["failure_type"] = "retrieval_failed"
            result["failure_reason"] = str(task.get("message") or "retrieval failed")
    except (OSError, UnicodeError) as exc:
        result["failure_type"] = "parse_failed"
        result["failure_reason"] = f"sample file cannot be read: {exc.__class__.__name__}"
    except Exception as exc:
        result["failure_type"] = "evaluation_execution_failed"
        result["failure_reason"] = str(exc) or exc.__class__.__name__
    return result


def _calculate_effect_metrics(
    annotations: AnnotationBundle,
    runs: list[dict],
    parameters: dict,
    report_dir: Path,
    memory_db_path: str,
) -> tuple[list[EvaluationMetric], list[dict]]:
    thresholds = dict(parameters["thresholds"])
    metrics = [
        _classification_metric(annotations, runs, thresholds, nda=True),
        _classification_metric(annotations, runs, thresholds, nda=False),
    ]
    metrics.extend(_clause_metrics(annotations, runs, thresholds))
    metrics.append(
        _playbook_metric(
            annotations,
            runs,
            int(parameters["playbook_recall_k"]),
            thresholds,
        )
    )
    related_metric, related_logs = _related_clause_metric(
        annotations,
        int(parameters["related_clause_recall_k"]),
        thresholds,
    )
    metrics.append(related_metric)
    memory_metrics, memory_logs = _memory_retrieval_metrics(
        annotations,
        int(parameters["memory_retrieval_k"]),
        thresholds,
        f"{memory_db_path}.vectors",
    )
    metrics.extend(memory_metrics)
    metrics.extend(_risk_metrics(annotations, runs, thresholds))
    metrics.append(_evidence_metric(annotations, runs, thresholds))
    metrics.append(_manual_review_metric(annotations, runs, thresholds))
    report_metric, report_logs = _report_filter_metric(
        annotations,
        runs,
        thresholds,
        report_dir,
        memory_db_path,
    )
    metrics.append(report_metric)
    return metrics, [*related_logs, *memory_logs, *report_logs]


def _classification_metric(
    annotations: AnnotationBundle,
    runs: list[dict],
    thresholds: dict,
    nda: bool,
) -> EvaluationMetric:
    run_by_id = {run["contract_id"]: run for run in runs}
    checks = []
    for annotation in annotations.contracts:
        if nda and annotation.expected_contract_type != "NDA":
            continue
        if not nda and annotation.expected_decision != "UNSUPPORTED_CONTRACT_TYPE":
            continue
        task = run_by_id[annotation.contract_id]["task"]
        classification = task.get("contract_classification") or {}
        passed = (
            classification.get("contract_type") == annotation.expected_contract_type
            and classification.get("decision") == annotation.expected_decision
        )
        checks.append(
            (
                passed,
                FailureSample(
                    sample_id=annotation.contract_id,
                    failure_type="classification_mismatch",
                    reason="contract type or decision does not match annotation",
                    expected=f"{annotation.expected_contract_type}/{annotation.expected_decision}",
                    actual=f"{classification.get('contract_type', '')}/{classification.get('decision', '')}",
                ),
            )
        )
    metric_name = "nda_classification_accuracy" if nda else "non_nda_rejection_rate"
    label = "NDA 分类准确率" if nda else "非 NDA 拒绝率"
    return _checks_metric(metric_name, label, checks, thresholds)


def _clause_metrics(
    annotations: AnnotationBundle,
    runs: list[dict],
    thresholds: dict,
) -> list[EvaluationMetric]:
    run_by_id = {run["contract_id"]: run for run in runs}
    boundary_checks = []
    type_checks = []
    for annotation in annotations.clauses:
        actual_clauses = run_by_id[annotation.contract_id]["task"].get("clauses") or []
        matches = [
            clause
            for clause in actual_clauses
            if _normalized_text(clause.get("text")) == _normalized_text(annotation.exact_text)
        ]
        boundary_passed = (
            len(matches) == 1
            and str(matches[0].get("clause_id", "")) == annotation.clause_id
        )
        boundary_checks.append(
            (
                boundary_passed,
                FailureSample(
                    sample_id=annotation.annotation_id,
                    failure_type="clause_boundary_mismatch",
                    reason="annotated clause text was not recovered as one stable clause",
                    expected=annotation.clause_id,
                    actual=[str(item.get("clause_id", "")) for item in matches],
                ),
            )
        )
        actual_type = str(matches[0].get("clause_type", "")) if len(matches) == 1 else ""
        type_checks.append(
            (
                boundary_passed and actual_type == annotation.clause_type,
                FailureSample(
                    sample_id=annotation.annotation_id,
                    failure_type="clause_type_mismatch",
                    reason="clause type does not match annotation",
                    expected=annotation.clause_type,
                    actual=actual_type,
                ),
            )
        )

    annotations_by_contract = {}
    for annotation in annotations.clauses:
        annotations_by_contract.setdefault(annotation.contract_id, []).append(annotation)
    for contract_id, expected_clauses in annotations_by_contract.items():
        actual_clauses = run_by_id[contract_id]["task"].get("clauses") or []
        expected_counts = Counter(
            (item.clause_id, _normalized_text(item.exact_text))
            for item in expected_clauses
        )
        actual_counts = Counter(
            (str(item.get("clause_id", "")), _normalized_text(item.get("text")))
            for item in actual_clauses
        )
        for (clause_id, clause_text), count in (actual_counts - expected_counts).items():
            for index in range(count):
                sample_id = f"{contract_id}:unexpected:{clause_id or index + 1}"
                boundary_checks.append(
                    (
                        False,
                        FailureSample(
                            sample_id=sample_id,
                            failure_type="unexpected_clause",
                            reason="extracted clause is not present in the exhaustive annotation set",
                            expected="no additional clause",
                            actual=f"{clause_id}/{clause_text}",
                        ),
                    )
                )
                type_checks.append(
                    (
                        False,
                        FailureSample(
                            sample_id=sample_id,
                            failure_type="unexpected_clause_type",
                            reason="clause type cannot be scored because the extracted clause is unannotated",
                            expected="annotated clause",
                            actual=clause_id,
                        ),
                    )
                )
    return [
        _checks_metric(
            "clause_boundary_accuracy",
            "条款切分准确率",
            boundary_checks,
            thresholds,
        ),
        _checks_metric(
            "clause_type_accuracy",
            "条款类型准确率",
            type_checks,
            thresholds,
        ),
    ]


def _playbook_metric(
    annotations: AnnotationBundle,
    runs: list[dict],
    k: int,
    thresholds: dict,
) -> EvaluationMetric:
    run_by_id = {run["contract_id"]: run for run in runs}
    checks = []
    for annotation in annotations.clauses:
        groups = run_by_id[annotation.contract_id]["task"].get("matched_rules") or []
        group = next(
            (item for item in groups if item.get("clause_id") == annotation.clause_id),
            {},
        )
        actual_ids = [
            str(rule.get("rule_id", ""))
            for rule in (group.get("matched_rules") or [])[:k]
        ]
        for expected_rule_id in annotation.expected_rule_ids:
            checks.append(
                (
                    expected_rule_id in actual_ids,
                    FailureSample(
                        sample_id=annotation.annotation_id,
                        failure_type="playbook_rule_missed",
                        reason=f"expected rule was not found in top {k}",
                        expected=expected_rule_id,
                        actual=actual_ids,
                    ),
                )
            )
    return _checks_metric(
        "playbook_recall_at_k",
        f"Playbook Recall@{k}",
        checks,
        thresholds,
    )


def _related_clause_metric(
    annotations: AnnotationBundle,
    k: int,
    thresholds: dict,
) -> tuple[EvaluationMetric, list[dict]]:
    checks = []
    all_logs = []
    for case in annotations.related_clauses:
        logs: list[StepLog] = []
        current_clause = case.current_clause.model_dump(mode="json")
        candidates = [item.model_dump(mode="json") for item in case.candidates]
        try:
            related = invoke_tool(
                f"effect_{case.case_id}",
                tool_registry,
                "retrieve_related_clauses",
                {
                    "contract_type": "NDA",
                    "current_clause": current_clause,
                    "clauses": [current_clause, *candidates],
                    "risk_type": case.risk_type,
                    "playbook_check_point": case.playbook_check_point,
                    "limit": k,
                    "embedding_cache": {},
                },
                logs,
                step_name="effect_related_clause_retrieval",
            )
            actual_ids = [str(item.get("clause_id", "")) for item in related[:k]]
            failure_type = "related_clause_missed"
            reason = f"annotated clause was not found in top {k}"
        except Exception as exc:
            actual_ids = []
            failure_type = "retrieval_failed"
            reason = str(exc) or exc.__class__.__name__
        all_logs.extend(log.to_dict() for log in logs)
        for expected_id in case.relevant_clause_ids:
            checks.append(
                (
                    expected_id in actual_ids,
                    FailureSample(
                        sample_id=case.case_id,
                        failure_type=failure_type,
                        reason=reason,
                        expected=expected_id,
                        actual=actual_ids,
                    ),
                )
            )
    return (
        _checks_metric(
            "related_clause_recall_at_k",
            f"相关条款 Recall@{k}",
            checks,
            thresholds,
        ),
        all_logs,
    )


def _memory_retrieval_metrics(
    annotations: AnnotationBundle,
    k: int,
    thresholds: dict,
    db_path: str,
) -> tuple[list[EvaluationMetric], list[dict]]:
    expected_ids: dict[str, str] = {}
    for annotation in annotations.memory:
        case = annotation.model_dump(mode="json")
        episodes = []
        for episode in case["episodes"]:
            memory_item = {
                "memory_id": episode["memory_id"],
                "contract_type": case["contract_type"],
                "clause_type": case["clause_type"],
                "risk_type": case["risk_type"],
                "review_position": case["review_position"],
                "user_action": episode["user_action"],
                "original_severity": episode["final_severity"],
                "final_severity": episode["final_severity"],
                "original_suggestion": case["baseline_suggestion"],
                "final_suggestion": episode["final_suggestion"],
                "ignore_reason": (
                    "Annotated evaluation opposition."
                    if episode["user_action"] == "ignore"
                    else ""
                ),
                "source_finding_id": f"EVAL-{case['case_id']}",
                "source_clause_id": f"EVAL-CLAUSE-{case['case_id']}",
                "include_in_report": episode["user_action"] != "ignore",
                "created_at": episode["created_at"],
            }
            episodes.append(memory_item)
            write_memory(
                {
                    "db_path": db_path,
                    "idempotency_key": f"effect-memory:{episode['memory_id']}",
                    "human_feedback": memory_item,
                }
            )
        expected_ids[case["case_id"]] = build_semantic_preference(
            episodes
        ).preference_id

    recall_checks = []
    reciprocal_rank_scores = []
    coverage_checks = []
    logs: list[StepLog] = []
    for annotation in annotations.memory:
        case = annotation.model_dump(mode="json")
        expected_id = expected_ids[case["case_id"]]
        execution_failed = False
        try:
            results = invoke_tool(
                f"effect_memory_{case['case_id']}",
                tool_registry,
                "retrieve_memory",
                {
                    "db_path": db_path,
                    "contract_type": case["contract_type"],
                    "clause": {
                        "clause_id": f"EVAL-QUERY-{case['case_id']}",
                        "clause_type": case["clause_type"],
                        "title": case["clause_type"].replace("_", " "),
                        "text": case["query_text"],
                        "key_fields": {},
                    },
                    "risk_type": case["risk_type"],
                    "review_position": case["review_position"],
                    "memory_items": [],
                    "embedding_cache": {},
                    "limit": k,
                    "now": case["evaluation_time"],
                    "stale_after_days": case["stale_after_days"],
                },
                logs,
                step_name="effect_memory_vector_retrieval",
            )
            actual_ids = [str(item.get("memory_id", "")) for item in results[:k]]
            rank = actual_ids.index(expected_id) + 1 if expected_id in actual_ids else None
            expected_item = next(
                (item for item in results[:k] if item.get("memory_id") == expected_id),
                None,
            )
            failure_reason = f"annotated Memory was not found in top {k}"
        except Exception as exc:
            execution_failed = True
            actual_ids = []
            rank = None
            expected_item = None
            failure_reason = str(exc) or exc.__class__.__name__

        recall_checks.append(
            (
                rank is not None,
                FailureSample(
                    sample_id=case["case_id"],
                    failure_type=(
                        "memory_retrieval_failed"
                        if execution_failed
                        else "memory_not_recalled"
                    ),
                    reason=failure_reason,
                    expected=expected_id,
                    actual=actual_ids,
                ),
            )
        )
        reciprocal_rank_scores.append(
            (
                1.0 / rank if rank is not None else 0.0,
                FailureSample(
                    sample_id=case["case_id"],
                    failure_type="memory_reciprocal_rank_zero",
                    reason="annotated Memory has no rank within the retrieval window",
                    expected=f"rank<={k}",
                    actual=rank,
                ),
            )
        )
        coverage_checks.append(
            (
                _memory_embedding_is_covered(expected_item),
                FailureSample(
                    sample_id=case["case_id"],
                    failure_type="memory_embedding_metadata_missing",
                    reason="retrieved Memory lacks complete vector version metadata",
                    expected="model, dimension, similarity, content hash, strategy",
                    actual=(
                        sorted(expected_item.keys())
                        if isinstance(expected_item, dict)
                        else "memory_not_retrieved"
                    ),
                ),
            )
        )

    return (
        [
            _checks_metric(
                "memory_retrieval_recall_at_k",
                f"Memory Recall@{k}",
                recall_checks,
                thresholds,
            ),
            _mean_score_metric(
                "memory_retrieval_mrr",
                "Memory MRR",
                reciprocal_rank_scores,
                thresholds,
            ),
            _checks_metric(
                "memory_embedding_coverage",
                "Memory 向量覆盖率",
                coverage_checks,
                thresholds,
            ),
        ],
        [log.to_dict() for log in logs],
    )


def _memory_embedding_is_covered(item) -> bool:
    if not isinstance(item, dict):
        return False
    dimension = item.get("vector_dimension")
    similarity = item.get("vector_similarity")
    return bool(
        str(item.get("embedding_mode") or "")
        and str(item.get("embedding_model") or "")
        and isinstance(dimension, int)
        and not isinstance(dimension, bool)
        and dimension > 0
        and isinstance(similarity, (int, float))
        and not isinstance(similarity, bool)
        and len(str(item.get("embedding_content_hash") or "")) == 64
        and item.get("retrieval_strategy")
        == "vector_with_structured_safety_filters"
    )


def _risk_metrics(
    annotations: AnnotationBundle,
    runs: list[dict],
    thresholds: dict,
) -> list[EvaluationMetric]:
    expected = {_risk_annotation_key(item): item for item in annotations.risks}
    actual = [
        (run["contract_id"], risk)
        for run in runs
        for risk in (run["task"].get("risk_findings") or [])
    ]
    precision_checks = []
    matched_keys = set()
    for contract_id, risk in actual:
        key = _risk_key(contract_id, risk)
        annotation = expected.get(key)
        severity_ok = annotation is not None and risk.get("severity") in annotation.acceptable_severities
        unique_match = severity_ok and key not in matched_keys
        if unique_match:
            matched_keys.add(key)
        precision_checks.append(
            (
                unique_match,
                FailureSample(
                    sample_id=str(risk.get("risk_id") or contract_id),
                    failure_type="unexpected_risk" if annotation is None else "risk_severity_mismatch",
                    reason="risk label is not annotated or severity is outside the accepted range",
                    expected=(annotation.acceptable_severities if annotation else "no risk"),
                    actual=f"{risk.get('risk_type', '')}/{risk.get('severity', '')}",
                ),
            )
        )

    recall_checks = []
    for key, annotation in expected.items():
        candidates = [risk for contract_id, risk in actual if _risk_key(contract_id, risk) == key]
        passed = any(risk.get("severity") in annotation.acceptable_severities for risk in candidates)
        recall_checks.append(
            (
                passed,
                FailureSample(
                    sample_id=annotation.annotation_id,
                    failure_type="expected_risk_missed",
                    reason="expected risk was not produced with an accepted severity",
                    expected=f"{annotation.risk_type}/{','.join(annotation.acceptable_severities)}",
                    actual=[f"{risk.get('risk_type', '')}/{risk.get('severity', '')}" for risk in candidates],
                ),
            )
        )

    precision = _checks_metric("risk_precision", "风险 Precision", precision_checks, thresholds)
    recall = _checks_metric("risk_recall", "风险 Recall", recall_checks, thresholds)
    f1_score = (
        2 * precision.score * recall.score / (precision.score + recall.score)
        if precision.score + recall.score
        else 0.0
    )
    tp = len(matched_keys)
    fp = precision.failed_count
    fn = recall.failed_count
    f1_failures = [*precision.failure_samples, *recall.failure_samples]
    f1 = EvaluationMetric(
        metric="risk_f1",
        label="风险 F1",
        sample_count=tp + fp + fn,
        passed_count=tp,
        failed_count=fp + fn,
        score=round(f1_score, 4),
        threshold=float(thresholds["risk_f1"]),
        threshold_met=f1_score >= float(thresholds["risk_f1"]),
        failure_samples=f1_failures,
    )
    return [precision, recall, f1]


def _evidence_metric(
    annotations: AnnotationBundle,
    runs: list[dict],
    thresholds: dict,
) -> EvaluationMetric:
    run_by_id = {run["contract_id"]: run for run in runs}
    checks = []
    for annotation in annotations.risks:
        candidates = [
            risk
            for risk in (run_by_id[annotation.contract_id]["task"].get("risk_findings") or [])
            if _risk_key(annotation.contract_id, risk) == _risk_annotation_key(annotation)
        ]
        actual_evidence = str(candidates[0].get("evidence_text", "")) if candidates else ""
        normalized_actual = _normalized_span(actual_evidence)
        passed = bool(normalized_actual) and any(
            _normalized_span(span) in normalized_actual
            for span in annotation.evidence_spans
        )
        checks.append(
            (
                passed,
                FailureSample(
                    sample_id=annotation.annotation_id,
                    failure_type="evidence_span_missed",
                    reason="risk evidence does not overlap an allowed annotated span",
                    expected=annotation.evidence_spans,
                    actual=actual_evidence,
                ),
            )
        )
    return _checks_metric(
        "evidence_span_hit_rate",
        "证据 span 命中率",
        checks,
        thresholds,
    )


def _manual_review_metric(
    annotations: AnnotationBundle,
    runs: list[dict],
    thresholds: dict,
) -> EvaluationMetric:
    run_by_id = {run["contract_id"]: run for run in runs}
    checks = []
    contracts_with_risk_trigger = set()
    for annotation in annotations.risks:
        if not annotation.manual_review_expected:
            continue
        contracts_with_risk_trigger.add(annotation.contract_id)
        risks = run_by_id[annotation.contract_id]["task"].get("risk_findings") or []
        candidate = next(
            (risk for risk in risks if _risk_key(annotation.contract_id, risk) == _risk_annotation_key(annotation)),
            {},
        )
        actual_status = str(candidate.get("review_status", ""))
        checks.append(
            (
                actual_status == "NEED_MANUAL_REVIEW",
                FailureSample(
                    sample_id=annotation.annotation_id,
                    failure_type="manual_review_not_triggered",
                    reason="annotated risk did not enter manual review",
                    expected="NEED_MANUAL_REVIEW",
                    actual=actual_status,
                ),
            )
        )
    for annotation in annotations.contracts:
        if not annotation.manual_review_expected or annotation.contract_id in contracts_with_risk_trigger:
            continue
        actual_status = str(run_by_id[annotation.contract_id]["task"].get("status", ""))
        checks.append(
            (
                actual_status in {"NEED_MANUAL_REVIEW", "HUMAN_REVIEW_PENDING"},
                FailureSample(
                    sample_id=annotation.contract_id,
                    failure_type="manual_review_not_triggered",
                    reason="annotated contract did not enter manual review",
                    expected="manual review",
                    actual=actual_status,
                ),
            )
        )
    return _checks_metric(
        "manual_review_trigger_rate",
        "人工复核触发率",
        checks,
        thresholds,
    )


def _report_filter_metric(
    annotations: AnnotationBundle,
    runs: list[dict],
    thresholds: dict,
    report_dir: Path,
    memory_db_path: str,
) -> tuple[EvaluationMetric, list[dict]]:
    run_by_id = {run["contract_id"]: run for run in runs}
    risks_by_contract = {}
    for annotation in annotations.risks:
        risks_by_contract.setdefault(annotation.contract_id, []).append(annotation)
    checks = []
    extra_logs = []
    for contract_id, expected_risks in risks_by_contract.items():
        run = run_by_id[contract_id]
        task = run["task"]
        expected_by_key = {_risk_annotation_key(item): item for item in expected_risks}
        expected_types = sorted(
            item.risk_type
            for item in expected_risks
            if item.include_in_report and item.feedback_action != "ignore"
        )
        actual_types = []
        original_log_count = len(task.get("logs") or [])
        logs = []
        try:
            updated_task = task
            for risk in list(task.get("risk_findings") or []):
                annotation = expected_by_key.get(_risk_key(contract_id, risk))
                feedback_action = annotation.feedback_action if annotation else "ignore"
                include_in_report = annotation.include_in_report if annotation else False
                feedback = apply_feedback_to_task(
                    task["task_id"],
                    {
                        "risk_id": risk["risk_id"],
                        "action": feedback_action,
                        "include_in_report": include_in_report,
                        "ignore_reason": "效果评测预设反馈" if feedback_action == "ignore" else "",
                    },
                    event_store=run["event_store"],
                    db_path=memory_db_path,
                )
                updated_task = feedback["task"]
            logs = [StepLog(**item) for item in (updated_task.get("logs") or [])]
            report = invoke_tool(
                updated_task["task_id"],
                tool_registry,
                "generate_report",
                {
                    "task_id": updated_task["task_id"],
                    "task": updated_task,
                    "report_dir": str(report_dir),
                },
                logs,
                step_name="effect_report_generation",
            )["report_file"]
            actual_types = sorted(
                match.group(1).strip()
                for match in re.finditer(r"^### \d+\. (.+)$", report["markdown"], re.M)
            )
            passed = actual_types == expected_types and report["risk_count"] == len(expected_types)
            failure_type = "report_filter_mismatch"
            reason = "report risks do not match annotated include/exclude decisions"
        except Exception as exc:
            passed = False
            failure_type = "report_filter_failed"
            reason = str(exc) or exc.__class__.__name__
        finally:
            extra_logs.extend(log.to_dict() for log in logs[original_log_count:])
        checks.append(
            (
                passed,
                FailureSample(
                    sample_id=contract_id,
                    failure_type=failure_type,
                    reason=reason,
                    expected=expected_types,
                    actual=actual_types,
                ),
            )
        )
    return (
        _checks_metric(
            "report_filter_accuracy",
            "报告过滤正确率",
            checks,
            thresholds,
        ),
        extra_logs,
    )


def _planner_metrics(
    annotations: AnnotationBundle,
    thresholds: dict,
) -> tuple[list[EvaluationMetric], list[dict]]:
    clauses_by_contract: dict[str, list[str]] = {}
    for clause in annotations.clauses:
        clauses_by_contract.setdefault(clause.contract_id, []).append(clause.clause_id)
    status_by_reason = {
        "LOW_CONFIDENCE": ReviewStatus.RISK_ANALYZED.value,
        "EVIDENCE_MISSING": ReviewStatus.RISK_ANALYZED.value,
        "RETRIEVAL_INSUFFICIENT": ReviewStatus.CONTEXT_BUILT.value,
        "ANALYZER_VERIFIER_CONFLICT": ReviewStatus.RISK_ANALYZED.value,
        "STRUCTURED_OUTPUT_INVALID": ReviewStatus.CONTEXT_BUILT.value,
    }
    accuracy_checks = []
    invalid_action_violations = []
    logs: list[StepLog] = []
    for contract in annotations.contracts:
        clause_ids = clauses_by_contract.get(contract.contract_id, [])
        for expectation in contract.planner_expectations:
            before_count = len(logs)
            try:
                invoke_tool(
                    f"planner_{contract.contract_id}",
                    tool_registry,
                    "plan_review_action",
                    {
                        "trigger_reason": expectation.reason_code,
                        "current_status": status_by_reason.get(
                            expectation.reason_code,
                            ReviewStatus.RISK_ANALYZED.value,
                        ),
                        "target_clause_id": expectation.target_clause_id,
                        "contract_clause_ids": clause_ids,
                        "retry_count": 0,
                        "failure_reason": "Synthetic annotated Planner evaluation case.",
                    },
                    logs,
                    step_name="effect_planner_decision",
                )
            except Exception:
                pass
            log = logs[-1] if len(logs) > before_count else None
            decision = (
                (log.trace_summary or {}).get("decision")
                if log is not None
                else {}
            ) or {}
            actual_action = str(decision.get("action") or "")
            sample_id = (
                f"{contract.contract_id}:{expectation.reason_code}:"
                f"{expectation.target_clause_id}"
            )
            accuracy_checks.append(
                (
                    actual_action == expectation.expected_action,
                    FailureSample(
                        sample_id=sample_id,
                        failure_type="planner_action_mismatch",
                        reason=(
                            log.error_message
                            if log is not None and log.error_message
                            else "Planner action did not match the annotation"
                        ),
                        expected=expectation.expected_action,
                        actual=actual_action,
                    ),
                )
            )
            invalid_action_violations.append(
                (
                    bool(actual_action) and actual_action not in ALLOWED_PLANNER_ACTIONS,
                    FailureSample(
                        sample_id=sample_id,
                        failure_type="invalid_tool_action_executed",
                        reason="Planner produced an action outside the execution whitelist",
                        expected=sorted(ALLOWED_PLANNER_ACTIONS),
                        actual=actual_action,
                    ),
                )
            )
    return (
        [
            _checks_metric(
                "planner_action_accuracy",
                "Planner 动作准确率",
                accuracy_checks,
                thresholds,
            ),
            _violation_rate_metric(
                "invalid_tool_action_rate",
                "非法工具动作执行率",
                invalid_action_violations,
                thresholds,
            ),
        ],
        [log.to_dict() for log in logs],
    )


def _agent_metrics(
    annotations: AnnotationBundle,
    runs: list[dict],
    memory_comparison: dict,
    thresholds: dict,
) -> list[EvaluationMetric]:
    task_logs = [
        (run["contract_id"], index, log)
        for run in runs
        for index, log in enumerate(run["task"].get("logs") or [])
    ]
    provider_groups = []
    json_checks = []
    for contract_id, log_index, log in task_logs:
        contract = tool_contracts.get(str(log.get("tool_name") or ""))
        if contract is None or not contract.calls_llm:
            continue
        provider = (log.get("trace_summary") or {}).get("provider") or {}
        if provider.get("mode") != "openai_compatible":
            continue
        calls = [item for item in (provider.get("calls") or []) if isinstance(item, dict)]
        if calls:
            provider_groups.append((contract_id, log_index, calls))
        for call_index, call in enumerate(calls):
            error_type = str(call.get("error_type") or "")
            if error_type not in {"", "schema_error"}:
                continue
            json_checks.append(
                (
                    error_type == "",
                    FailureSample(
                        sample_id=f"{contract_id}:{log_index + 1}:{call_index + 1}",
                        failure_type="llm_json_invalid",
                        reason="DeepSeek response did not pass JSON and output Schema validation",
                        expected="valid_structured_json",
                        actual=error_type or "valid",
                    ),
                )
            )

    schema_repair_checks = []
    retry_recovery_checks = []
    retryable_errors = {"timeout", "rate_limit", "temporary_error"}
    for contract_id, log_index, calls in provider_groups:
        errors = [str(item.get("error_type") or "") for item in calls]
        if "schema_error" in errors:
            first_error = errors.index("schema_error")
            recovered = "" in errors[first_error + 1 :]
            schema_repair_checks.append(
                (
                    recovered,
                    FailureSample(
                        sample_id=f"{contract_id}:{log_index + 1}",
                        failure_type="llm_schema_repair_failed",
                        reason="Schema-invalid output did not recover within the fixed retry path",
                        expected="success_after_schema_error",
                        actual=errors,
                    ),
                )
            )
        if any(error in retryable_errors for error in errors):
            first_error = next(
                index for index, error in enumerate(errors) if error in retryable_errors
            )
            recovered = "" in errors[first_error + 1 :]
            retry_recovery_checks.append(
                (
                    recovered,
                    FailureSample(
                        sample_id=f"{contract_id}:{log_index + 1}",
                        failure_type="retry_recovery_failed",
                        reason="Retryable Provider failure did not recover within budget",
                        expected="success_after_retryable_error",
                        actual=errors,
                    ),
                )
            )
    for contract_id, log_index, log in task_logs:
        if int(log.get("retry_index") or 0) <= 0:
            continue
        retry_recovery_checks.append(
            (
                log.get("status") == "success",
                FailureSample(
                    sample_id=f"{contract_id}:{log_index + 1}",
                    failure_type="execution_recovery_failed",
                    reason="Recovered execution step did not complete successfully",
                    expected="success",
                    actual=str(log.get("status") or ""),
                ),
            )
        )

    retrieval_repair_checks = _retrieval_repair_checks(runs)

    unsupported_violations = []
    for run in runs:
        for risk in run["task"].get("risk_findings") or []:
            supported = bool(
                str(risk.get("clause_id") or "").strip()
                and str(risk.get("evidence_text") or "").strip()
                and (risk.get("matched_rule_ids") or [])
            )
            unsupported_violations.append(
                (
                    not supported,
                    FailureSample(
                        sample_id=str(risk.get("risk_id") or run["contract_id"]),
                        failure_type="unsupported_formal_finding",
                        reason="Formal finding lacks clause, evidence, or Playbook rule binding",
                        expected="evidence_bound_finding",
                        actual="unsupported" if not supported else "supported",
                    ),
                )
            )

    memory_checks = [
        (
            bool(item.get("with_memory_consistent")),
            FailureSample(
                sample_id=str(item.get("case_id") or "memory-case"),
                failure_type="memory_preference_inconsistent",
                reason="Memory-influenced suggestion did not match the annotated expectation",
                expected=True,
                actual=bool(item.get("with_memory_consistent")),
            ),
        )
        for item in memory_comparison.get("results") or []
    ]
    injection_checks = [_prompt_injection_check(case) for case in annotations.prompt_injection]

    return [
        _checks_metric(
            "llm_json_valid_rate",
            "LLM JSON 合法率",
            json_checks,
            thresholds,
        ),
        _checks_metric(
            "llm_schema_repair_rate",
            "LLM Schema 修复率",
            schema_repair_checks,
            thresholds,
        ),
        _checks_metric(
            "retrieval_repair_success_rate",
            "检索修复成功率",
            retrieval_repair_checks,
            thresholds,
        ),
        _checks_metric(
            "retry_recovery_rate",
            "重试恢复率",
            retry_recovery_checks,
            thresholds,
        ),
        _violation_rate_metric(
            "unsupported_finding_rate",
            "无证据正式风险率",
            unsupported_violations,
            thresholds,
        ),
        _checks_metric(
            "memory_preference_consistency",
            "Memory 偏好一致率",
            memory_checks,
            thresholds,
        ),
        _checks_metric(
            "prompt_injection_block_rate",
            "Prompt Injection 阻断率",
            injection_checks,
            thresholds,
        ),
    ]


def _embedding_call_success_metric(
    logs: list[dict],
    thresholds: dict,
) -> EvaluationMetric:
    checks = []
    embedding_tools = {"retrieve_related_clauses", "retrieve_memory"}
    for log_index, log in enumerate(logs):
        if log.get("tool_name") not in embedding_tools:
            continue
        provider = (log.get("trace_summary") or {}).get("provider") or {}
        for call_index, call in enumerate(provider.get("calls") or []):
            if not isinstance(call, dict):
                continue
            error_type = str(call.get("error_type") or "")
            checks.append(
                (
                    not error_type,
                    FailureSample(
                        sample_id=f"embedding:{log_index + 1}:{call_index + 1}",
                        failure_type="embedding_call_failed",
                        reason="Embedding Provider call did not complete successfully",
                        expected="success",
                        actual=error_type or "success",
                    ),
                )
            )
    return _checks_metric(
        "embedding_call_success_rate",
        "Embedding 调用成功率",
        checks,
        thresholds,
    )


def _retrieval_repair_checks(
    runs: list[dict],
) -> list[tuple[bool, FailureSample]]:
    checks = []
    for run in runs:
        logs = run["task"].get("logs") or []
        for index, log in enumerate(logs):
            decision = (log.get("trace_summary") or {}).get("decision") or {}
            if decision.get("type") != "planner" or decision.get("action") != "RETRIEVE_AGAIN":
                continue
            later_logs = logs[index + 1 :]
            retrieval_recovered = any(
                later.get("tool_name") == "retrieve_related_clauses"
                and later.get("status") == "success"
                and _is_positive_count(
                    ((later.get("trace_summary") or {}).get("decision") or {}).get(
                        "candidate_count"
                    )
                )
                for later in later_logs
            )
            reason_code = str(decision.get("reason_code") or "")
            evidence_recovered = any(
                later.get("tool_name") == "verify_evidence"
                and later.get("status") == "success"
                and bool(
                    (
                        ((later.get("trace_summary") or {}).get("decision") or {})
                    ).get("is_valid")
                )
                for later in later_logs
            )
            repaired = retrieval_recovered and (
                reason_code == "RETRIEVAL_INSUFFICIENT" or evidence_recovered
            )
            checks.append(
                (
                    repaired,
                    FailureSample(
                        sample_id=f"{run['contract_id']}:{index + 1}",
                        failure_type="retrieval_repair_failed",
                        reason=(
                            "Planner retrieval repair did not recover candidates or, for an "
                            "evidence-triggered repair, a valid evidence result"
                        ),
                        expected="recovered_retrieval_outcome",
                        actual=(
                            f"candidates_recovered={retrieval_recovered},"
                            f"evidence_recovered={evidence_recovered}"
                        ),
                    ),
                )
            )
    return checks


def _is_positive_count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _prompt_injection_check(case) -> tuple[bool, FailureSample]:
    text = case.text * case.repeat
    clause = {
        "clause_id": "CL-INJECTION",
        "title": "Synthetic security evaluation clause",
        "text": "保密信息包括商业信息。",
        "clause_type": "定义",
        "key_fields": {"right_holder": ["披露方"]},
        "source_location": {"start_order": 1},
    }
    memory = []
    if case.target == "current_clause_text":
        clause["text"] = text
    else:
        memory = [{"memory_id": "MEM-INJECTION", "note": text}]
    actual = "NOT_BLOCKED"
    try:
        context_input = {
            "contract_type": "NDA",
            "review_position": "甲方",
            "current_clause": clause,
            "matched_rule": _injection_test_rule(),
            "related_clauses": [],
            "related_memory": memory,
        }
        if case.expected_outcome == "CONTEXT_BUDGET_EXCEEDED":
            context_input["max_tokens"] = 1000
        context = build_review_context(
            **context_input,
        )
        if (context.get("prompt_security") or {}).get("detected"):
            actual = "PROMPT_INJECTION_DETECTED"
    except ContextBudgetExceededError:
        actual = "CONTEXT_BUDGET_EXCEEDED"
    passed = actual == case.expected_outcome
    return (
        passed,
        FailureSample(
            sample_id=case.id,
            failure_type="prompt_injection_not_blocked",
            reason="Synthetic malicious input did not reach its annotated blocked state",
            expected=case.expected_outcome,
            actual=actual,
        ),
    )


def _injection_test_rule() -> dict:
    position = {
        "severity_default": "中",
        "risk_focus": "验证保密信息定义具有明确边界。",
        "revision_template": "限定保密信息范围。",
    }
    return {
        "rule_id": "NDA-R001",
        "contract_type": "NDA",
        "clause_type": "定义",
        "risk_type": "保密信息范围过宽",
        "check_point": "检查保密信息定义是否具有明确边界。",
        "review_position": "甲方",
        "severity_default": "中",
        "risk_focus": position["risk_focus"],
        "revision_template": position["revision_template"],
        "positions": {"甲方": dict(position)},
        "position_config": dict(position),
    }


def _operational_metrics(
    annotations: AnnotationBundle,
    runs: list[dict],
    additional_logs: list[dict],
    thresholds: dict,
) -> list[EvaluationMetric]:
    log_checks = []
    for run in runs:
        for index, log in enumerate(run["task"].get("logs") or []):
            log_checks.append(
                (
                    log.get("status") == "success",
                    FailureSample(
                        sample_id=f"{run['contract_id']}:{index + 1}",
                        failure_type="tool_call_failed",
                        reason=str(log.get("error_message") or "tool call did not succeed"),
                        expected="success",
                        actual=str(log.get("status", "")),
                    ),
                )
            )
    for index, log in enumerate(additional_logs):
        log_checks.append(
            (
                log.get("status") == "success",
                FailureSample(
                    sample_id=f"effect-extra:{index + 1}",
                    failure_type="tool_call_failed",
                    reason=str(log.get("error_message") or "tool call did not succeed"),
                    expected="success",
                    actual=str(log.get("status", "")),
                ),
            )
        )

    run_by_id = {run["contract_id"]: run for run in runs}
    e2e_checks = []
    for annotation in annotations.contracts:
        run = run_by_id[annotation.contract_id]
        actual_status = str(run["task"].get("status", ""))
        e2e_checks.append(
            (
                actual_status == annotation.expected_terminal_status,
                FailureSample(
                    sample_id=annotation.contract_id,
                    failure_type=run["failure_type"] or "terminal_status_mismatch",
                    reason=run["failure_reason"] or "task terminal status does not match annotation",
                    expected=annotation.expected_terminal_status,
                    actual=actual_status,
                ),
            )
        )
    return [
        _checks_metric(
            "tool_call_success_rate",
            "工具调用成功率",
            log_checks,
            thresholds,
        ),
        _checks_metric(
            "end_to_end_success_rate",
            "端到端任务成功率",
            e2e_checks,
            thresholds,
        ),
    ]


def _checks_metric(
    metric: str,
    label: str,
    checks: list[tuple[bool, FailureSample]],
    thresholds: dict,
) -> EvaluationMetric:
    sample_count = len(checks)
    passed_count = sum(1 for passed, _failure in checks if passed)
    failed_count = sample_count - passed_count
    score = passed_count / sample_count if sample_count else 0.0
    threshold = float(thresholds[metric])
    return EvaluationMetric(
        metric=metric,
        label=label,
        sample_count=sample_count,
        passed_count=passed_count,
        failed_count=failed_count,
        score=round(score, 4),
        threshold=threshold,
        threshold_met=sample_count > 0 and score >= threshold,
        failure_samples=[failure for passed, failure in checks if not passed],
    )


def _mean_score_metric(
    metric: str,
    label: str,
    scores: list[tuple[float, FailureSample]],
    thresholds: dict,
) -> EvaluationMetric:
    sample_count = len(scores)
    passed_count = sum(1 for score, _failure in scores if score > 0)
    failed_count = sample_count - passed_count
    mean_score = sum(score for score, _failure in scores) / sample_count if sample_count else 0.0
    threshold = float(thresholds[metric])
    return EvaluationMetric(
        metric=metric,
        label=label,
        sample_count=sample_count,
        passed_count=passed_count,
        failed_count=failed_count,
        score=round(mean_score, 4),
        threshold=threshold,
        threshold_met=sample_count > 0 and mean_score >= threshold,
        failure_samples=[failure for score, failure in scores if score <= 0],
    )


def _violation_rate_metric(
    metric: str,
    label: str,
    violations: list[tuple[bool, FailureSample]],
    thresholds: dict,
) -> EvaluationMetric:
    sample_count = len(violations)
    failed_count = sum(1 for violated, _failure in violations if violated)
    passed_count = sample_count - failed_count
    score = failed_count / sample_count if sample_count else 0.0
    threshold = float(thresholds[metric])
    return EvaluationMetric(
        metric=metric,
        label=label,
        sample_count=sample_count,
        passed_count=passed_count,
        failed_count=failed_count,
        score=round(score, 4),
        threshold=threshold,
        threshold_met=sample_count > 0 and score <= threshold,
        failure_samples=[failure for violated, failure in violations if violated],
    )


def _empty_runtime(run_label: str) -> EvaluationRuntime:
    return EvaluationRuntime(
        run_label=run_label,
        provider_call_count=0,
        provider_success_count=0,
        request_id_hashes=[],
        measurements=[
            EvaluationMeasurement(
                measurement="provider_status",
                label="Provider 状态",
                value="not_run",
                unit="status",
            )
        ],
    )


def _runtime_summary(
    runs: list[dict],
    additional_logs: list[dict],
    run_label: str,
) -> EvaluationRuntime:
    logs = [
        log
        for run in runs
        for log in (run["task"].get("logs") or [])
    ]
    logs.extend(additional_logs)
    calls = []
    prompt_versions = set()
    for log in logs:
        trace = log.get("trace_summary") or {}
        versions = trace.get("versions") or {}
        prompt_version = str(versions.get("prompt") or "")
        if prompt_version and prompt_version != "not_applicable":
            prompt_versions.add(prompt_version)
        provider = trace.get("provider") or {}
        if provider.get("mode") != "openai_compatible":
            continue
        calls.extend(
            item for item in (provider.get("calls") or []) if isinstance(item, dict)
        )

    request_hashes = []
    latencies = []
    prompt_tokens = 0
    completion_tokens = 0
    estimated_costs = []
    cost_statuses = set()
    for call in calls:
        request_id = str(call.get("provider_request_id") or "")
        if request_id:
            request_hashes.append(sha256(request_id.encode("utf-8")).hexdigest()[:12])
        latency = call.get("latency_ms")
        if isinstance(latency, int) and not isinstance(latency, bool) and latency >= 0:
            latencies.append(latency)
        for field_name, target in (
            ("prompt_tokens", "prompt"),
            ("completion_tokens", "completion"),
        ):
            value = call.get(field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                continue
            if target == "prompt":
                prompt_tokens += value
            else:
                completion_tokens += value
        raw_cost = call.get("estimated_cost")
        if raw_cost not in {None, ""}:
            try:
                estimated_costs.append(float(raw_cost))
            except (TypeError, ValueError):
                pass
        cost_status = str(call.get("cost_status") or "")
        if cost_status:
            cost_statuses.add(cost_status)

    p95_latency = (
        sorted(latencies)[max(0, ceil(len(latencies) * 0.95) - 1)]
        if latencies
        else None
    )
    complete_cost = bool(calls) and len(estimated_costs) == len(calls)
    cost_value = round(sum(estimated_costs), 8) if complete_cost else None
    if not calls:
        cost_status = "not_available"
    elif complete_cost:
        cost_status = ",".join(sorted(cost_statuses)) or "calculated"
    elif estimated_costs:
        cost_status = (
            "partial_unavailable:"
            + (",".join(sorted(cost_statuses)) or "provider_cost_missing")
        )
    else:
        cost_status = ",".join(sorted(cost_statuses)) or "not_available"
    return EvaluationRuntime(
        run_label=run_label,
        provider_call_count=len(calls),
        provider_success_count=sum(
            1 for call in calls if not str(call.get("error_type") or "")
        ),
        request_id_hashes=sorted(set(request_hashes)),
        measurements=[
            EvaluationMeasurement(
                measurement="prompt_tokens",
                label="Prompt Token 总数",
                value=prompt_tokens,
                unit="tokens",
            ),
            EvaluationMeasurement(
                measurement="completion_tokens",
                label="Completion Token 总数",
                value=completion_tokens,
                unit="tokens",
            ),
            EvaluationMeasurement(
                measurement="total_tokens",
                label="Token 总数",
                value=prompt_tokens + completion_tokens,
                unit="tokens",
            ),
            EvaluationMeasurement(
                measurement="estimated_cost",
                label="估算成本",
                value=cost_value,
                unit="configured_currency",
            ),
            EvaluationMeasurement(
                measurement="cost_status",
                label="成本配置状态",
                value=cost_status,
                unit="status",
            ),
            EvaluationMeasurement(
                measurement="provider_latency_p95",
                label="Provider P95 延迟",
                value=p95_latency,
                unit="ms",
            ),
            EvaluationMeasurement(
                measurement="prompt_versions",
                label="Prompt 版本",
                value=",".join(sorted(prompt_versions)) or "not_available",
                unit="version",
            ),
        ],
    )


def _risk_annotation_key(annotation) -> tuple[str, str, str]:
    return annotation.contract_id, annotation.clause_id, annotation.risk_type


def _risk_key(contract_id: str, risk: dict) -> tuple[str, str, str]:
    return contract_id, str(risk.get("clause_id", "")), str(risk.get("risk_type", ""))


def _normalized_text(value) -> str:
    return "\n".join(line.strip() for line in str(value or "").strip().splitlines() if line.strip())


def _normalized_span(value) -> str:
    return re.sub(r"[\s。.;；]+", "", str(value or ""))


def _effect_summary_markdown(summary: dict) -> str:
    versions = summary["versions"]
    parameters = versions.get("parameters") or {}
    runtime = summary["runtime"]
    lines = [
        "# 人工标注效果评测摘要",
        "",
        summary["claim"],
        "",
        f"- 评测 ID：{summary['evaluation_id']}",
        f"- 状态：{summary['status']}",
        f"- 合同样本数：{summary['sample_count']}",
        f"- 代码版本：{versions['code_version']}",
        f"- Git 工作区脏状态：{versions['git_dirty']}",
        f"- Playbook：{versions['playbook_version']}",
        f"- 标注版本：{versions['annotation_version']}",
        f"- LLM：{versions['llm_mode']} / {versions['llm_model']}",
        f"- Embedding：{versions['embedding_mode']} / {versions['embedding_model']}",
        f"- Playbook Recall@K：{parameters.get('playbook_recall_k', 'unavailable')}",
        f"- 相关条款 Recall@K：{parameters.get('related_clause_recall_k', 'unavailable')}",
        f"- 指标阈值：{json.dumps(parameters.get('thresholds') or {}, ensure_ascii=False, sort_keys=True)}",
        f"- 运行标签：{runtime.get('run_label') or 'unlabeled'}",
        f"- 外部 Provider 调用：{runtime.get('provider_success_count', 0)} / {runtime.get('provider_call_count', 0)} 成功",
        f"- Provider 请求 ID 摘要数：{len(runtime.get('request_id_hashes') or [])}",
        "",
        "| 运行测量 | 数值 | 单位 |",
        "| --- | ---: | --- |",
    ]
    for measurement in runtime.get("measurements") or []:
        lines.append(
            f"| {_cell(measurement['label'])} | {_cell(measurement.get('value'))} | "
            f"{_cell(measurement['unit'])} |"
        )
    lines.extend([
        "",
        "| 指标 | 样本 | 通过 | 失败 | 分数 | 阈值 | 达标 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ])
    for metric in summary["metrics"]:
        lines.append(
            f"| {_cell(metric['label'])} | {metric['sample_count']} | {metric['passed_count']} | "
            f"{metric['failed_count']} | {metric['score']:.4f} | {metric['threshold']:.4f} | "
            f"{'是' if metric['threshold_met'] else '否'} |"
        )
    for metric in summary["metrics"]:
        if not metric["failure_samples"]:
            continue
        lines.extend(["", f"## {metric['label']}失败样本", ""])
        for failure in metric["failure_samples"]:
            lines.append(
                f"- `{_cell(failure['sample_id'])}` [{_cell(failure['failure_type'])}] "
                f"{_cell(failure['reason'])}"
            )
    if summary["evaluation_failures"]:
        lines.extend(["", "## 评测执行失败", ""])
        for failure in summary["evaluation_failures"]:
            lines.append(
                f"- `{_cell(failure['sample_id'])}` [{_cell(failure['failure_type'])}] "
                f"{_cell(failure['reason'])}"
            )
    lines.append("")
    return "\n".join(lines)


def _load_manifest(samples_dir: Path) -> list[dict]:
    manifest_path = samples_dir / "manifest.json"
    if not manifest_path.exists():
        raise ValueError("samples manifest not found")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    samples = payload.get("samples")
    sample_count = payload.get("basic_evaluation_sample_count", 10)
    if (
        isinstance(sample_count, bool)
        or not isinstance(sample_count, int)
        or sample_count != 10
    ):
        raise ValueError("basic evaluation sample count must remain 10")
    if not isinstance(samples, list) or len(samples) < sample_count:
        raise ValueError("samples manifest must contain at least 10 samples")
    return samples[:sample_count]


def _run_sample(sample: dict, samples_dir: Path, report_dir: Path, memory_db_path: str) -> dict:
    sample_name = str(sample.get("sample_name", "")).strip()
    result = {
        "sample_name": sample_name,
        "file": str(sample.get("file", "")),
        "review_position": str(sample.get("review_position", "")),
        "source": str(sample.get("source", "")),
        "task_ran": False,
        "document_parsed": False,
        "clauses_structured": False,
        "playbook_hit": False,
        "risk_has_evidence": False,
        "feedback_memory_written": False,
        "report_exported": False,
        "failure_reason": "",
    }

    try:
        text = (samples_dir / result["file"]).read_text(encoding="utf-8")
        review_position = _review_position(result["review_position"])
        event_store = ReviewEventStore()
        agent = ReviewOrchestratorAgent(event_store)
        state = agent.run_sync(
            file_name=f"{Path(result['file']).stem}.docx",
            file_type="docx",
            content=_docx_bytes_from_text(text),
            review_position=review_position,
        )
        task = state.to_dict()
        result["task_ran"] = True
        result["document_parsed"] = bool(task.get("document"))
        result["clauses_structured"] = bool(task.get("clauses"))
        result["playbook_hit"] = any(group.get("matched_rules") for group in task.get("matched_rules") or [])
        result["risk_has_evidence"] = _risks_have_evidence(task.get("risk_findings") or [])

        risks = list(task.get("risk_findings") or [])
        risk = _first_risk(risks)
        if risk is None:
            raise ValueError("no exportable risk candidate found")

        feedback_result = None
        for index, candidate in enumerate(risks):
            action = "accept" if index == 0 else "ignore"
            feedback_result = apply_feedback_to_task(
                task["task_id"],
                {
                    "risk_id": candidate["risk_id"],
                    "action": action,
                    "include_in_report": index == 0,
                    "ignore_reason": "基础评测仅导出首条风险" if action == "ignore" else "",
                },
                event_store=event_store,
                db_path=memory_db_path,
            )
        result["feedback_memory_written"] = bool(
            feedback_result
            and feedback_result.get("memory_item", {}).get("memory_id")
        )
        updated_task = feedback_result["task"]
        logs = [StepLog(**item) for item in updated_task.get("logs") or []]
        report_result = invoke_tool(
            updated_task["task_id"],
            tool_registry,
            "generate_report",
            {
                "task_id": updated_task["task_id"],
                "task": updated_task,
                "report_dir": str(report_dir),
            },
            logs,
            step_name="evaluation_report_generation",
        )
        report_file = report_result["report_file"]
        event_store.update_task(
            updated_task["task_id"],
            ReviewStatus.REPORT_READY,
            "评测样本报告已生成。",
            step_name="evaluation_report_ready",
            tool_name="generate_report",
            report_file=report_file,
            logs=[log.to_dict() for log in logs],
        )
        result["report_exported"] = Path(report_file["path"]).exists() and report_file["risk_count"] > 0
    except Exception as exc:
        result["failure_reason"] = str(exc)

    if not result["failure_reason"]:
        missing = [field for field in SUMMARY_FIELDS if not result[field]]
        if missing:
            result["failure_reason"] = f"missing checks: {', '.join(missing)}"
    return result


def _review_position(value: str) -> ReviewPosition:
    if value == ReviewPosition.PARTY_A.value:
        return ReviewPosition.PARTY_A
    if value == ReviewPosition.PARTY_B.value:
        return ReviewPosition.PARTY_B
    raise ValueError("review_position must be 甲方 or 乙方")


def _docx_bytes_from_text(text: str) -> bytes:
    document = WordDocument()
    paragraph_count = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        document.add_paragraph(stripped)
        paragraph_count += 1
    if not paragraph_count:
        raise ValueError("sample text is empty")

    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _risks_have_evidence(risks: list[dict]) -> bool:
    return bool(risks) and all(
        str(risk.get("clause_id", "")).strip() and str(risk.get("evidence_text", "")).strip()
        for risk in risks
    )


def _first_risk(risks: list[dict]) -> dict | None:
    for risk in risks:
        if str(risk.get("review_status", "")) in {"CONFIRMED_RISK", "NEED_MANUAL_REVIEW"}:
            return risk
    return None


def _sample_passed(result: dict) -> bool:
    return not result.get("failure_reason") and all(result[field] for field in SUMMARY_FIELDS)


def _summary_markdown(summary: dict) -> str:
    lines = [
        "# 基础评测摘要",
        "",
        summary["claim"],
        "",
        f"- 样本数：{summary['sample_count']}",
        f"- 跑通数：{summary['passed_count']}",
        "",
        "| 样本 | 任务跑通 | 文档解析 | 条款结构化 | Playbook 命中 | 风险证据 | Memory 写入 | 报告导出 | 失败原因 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in summary["results"]:
        lines.append(
            "| "
            + " | ".join(
                [
                    _cell(item["sample_name"]),
                    _yes_no(item["task_ran"]),
                    _yes_no(item["document_parsed"]),
                    _yes_no(item["clauses_structured"]),
                    _yes_no(item["playbook_hit"]),
                    _yes_no(item["risk_has_evidence"]),
                    _yes_no(item["feedback_memory_written"]),
                    _yes_no(item["report_exported"]),
                    _cell(item["failure_reason"]),
                ]
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def _yes_no(value: bool) -> str:
    return "是" if value else "否"


def _cell(value) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")
