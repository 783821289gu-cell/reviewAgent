from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from xml.sax.saxutils import escape
from collections import Counter
import json
import re
import subprocess
from zipfile import ZipFile
from io import BytesIO

from pydantic import ValidationError

from config import settings
from models.evaluation import (
    AnnotationBundle,
    AnnotationSource,
    EffectEvaluationSummary,
    EvaluationMetric,
    EvaluationVersion,
    FailureSample,
)
from models.log import StepLog
from models.review import ReviewPosition, ReviewStatus
from providers.embedding_provider import create_embedding_provider
from services.event_service import ReviewEventStore
from services.feedback_service import apply_feedback_to_task
from services.log_service import invoke_tool
from services.review_service import ReviewOrchestratorAgent
from tools.registry import tool_registry


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SAMPLES_DIR = PROJECT_ROOT / "samples"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "evaluation"
NO_PRODUCTION_CLAIM = "基础评测仅验证流程跑通，不代表生产级准确率。"
EFFECT_NO_PRODUCTION_CLAIM = "效果评测仅报告当前人工标注集上的实际指标和失败样本，不代表生产级准确率。"
ANNOTATION_SCHEMA_VERSION = "effect-v1"
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
    output_dir = Path(str(tool_input.get("output_dir") or DEFAULT_OUTPUT_DIR))
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
    samples_dir = Path(str(tool_input.get("samples_dir") or DEFAULT_SAMPLES_DIR))
    output_dir = Path(str(tool_input.get("output_dir") or DEFAULT_OUTPUT_DIR)) / "effect"
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
                evaluation_failures=[_load_failure(exc)],
            ),
            output_dir,
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    report_dir = output_dir / "reports" / evaluation_id
    memory_db_path = str(output_dir / "effect_evaluation_memory.sqlite3")
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
    metrics.extend(
        _operational_metrics(
            annotations,
            runs,
            additional_logs,
            dict(effect_config["parameters"]["thresholds"]),
        )
    )
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
    required = {"annotation_version", "schema", "contracts", "clauses", "risks", "related_clauses", "parameters"}
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
    for name in ("playbook_recall_k", "related_clause_recall_k"):
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
    return config


def _load_annotations(samples_dir: Path, config: dict) -> AnnotationBundle:
    schema_path = _safe_annotation_path(samples_dir, str(config["schema"]))
    schema = _read_annotation_json(schema_path, "schema.json")
    _validate_annotation_schema(schema)

    payloads = {}
    for key in ("contracts", "clauses", "risks", "related_clauses"):
        path = _safe_annotation_path(samples_dir, str(config[key]))
        payload = _read_annotation_json(path, path.name)
        if payload.get("annotation_version") != config["annotation_version"]:
            raise AnnotationLoadError(
                "invalid_annotation",
                path.name,
                "annotation version does not match manifest",
            )
        if key == "related_clauses":
            try:
                AnnotationSource.model_validate(payload.get("source"))
            except ValidationError as exc:
                raise AnnotationLoadError(
                    "invalid_annotation",
                    path.name,
                    "related clause source metadata is invalid",
                ) from exc
        items = payload.get("items") if key != "related_clauses" else payload.get("cases")
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
    return metrics, [*related_logs, *report_logs]


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
        "",
        "| 指标 | 样本 | 通过 | 失败 | 分数 | 阈值 | 达标 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
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
    if not isinstance(samples, list) or len(samples) != 10:
        raise ValueError("samples manifest must contain exactly 10 samples")
    return samples


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

        risk = _first_risk(task.get("risk_findings") or [])
        if risk is None:
            raise ValueError("no exportable risk candidate found")

        feedback_result = apply_feedback_to_task(
            task["task_id"],
            {
                "risk_id": risk["risk_id"],
                "action": "accept",
                "include_in_report": True,
            },
            event_store=event_store,
            db_path=memory_db_path,
        )
        result["feedback_memory_written"] = bool(feedback_result.get("memory_item", {}).get("memory_id"))
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
    paragraphs = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        paragraphs.append(f"<w:p><w:r><w:t>{escape(stripped)}</w:t></w:r></w:p>")
    if not paragraphs:
        raise ValueError("sample text is empty")

    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        + "".join(paragraphs)
        + "</w:body></w:document>"
    )
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", document_xml)
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
