import { ContractViewer } from "./ContractViewer.js";
import { ContextTrace } from "./ContextTrace.js";
import { ExecutionLog } from "./ExecutionLog.js";
import { EvaluationPanel } from "./EvaluationPanel.js";
import { LocalReviewPanel } from "./LocalReviewPanel.js";
import { ReportExport } from "./ReportExport.js";
import { ReviewProgress } from "./ReviewProgress.js";
import { RiskDetail } from "./RiskDetail.js";
import { RiskList } from "./RiskList.js";
import { RuleDetail } from "./RuleDetail.js";

export function WorkbenchLayout(root, props) {
  const task = props.task;
  const risks = (task?.risk_findings || []).filter((risk) => risk.clause_id && risk.evidence_text);
  const activeRisk = risks.find((risk) => risk.risk_id === props.activeRiskId) || null;
  const activeClauseId = props.activeClauseId || activeRisk?.clause_id || props.localReview?.clauseId || "";
  const activeRuleIds = activeRisk?.matched_rule_ids || [];

  root.innerHTML = `
    <section class="workbench" aria-label="审查工作台">
      <div class="workbench-top">
        <div>
          <span class="section-label">合同名称</span>
          <strong data-contract-name></strong>
        </div>
        <div>
          <span class="section-label">审查状态</span>
          <strong data-review-status></strong>
        </div>
        <div>
          <span class="section-label">Playbook</span>
          <strong data-playbook-version></strong>
        </div>
        <div>
          <span class="section-label">报告</span>
          <div data-report-export></div>
        </div>
      </div>

      <div class="workbench-main">
        <section class="document-pane">
          <h2>合同原文</h2>
          <div data-contract-viewer></div>
        </section>

        <section class="result-pane">
          <h2>合同类型</h2>
          <div data-contract-classification></div>
          <h2>流式进度</h2>
          <div data-review-progress></div>
          <h2 class="secondary-heading">命中规则</h2>
          <div data-rule-detail></div>
          <h2 class="secondary-heading">上下文 Trace</h2>
          <div data-context-trace></div>
          <h2 class="secondary-heading">风险列表</h2>
          <div data-risk-list></div>
          <h2 class="secondary-heading">审查结果</h2>
          <div data-risk-detail></div>
          <h2 class="secondary-heading">局部审查</h2>
          <div data-local-review></div>
          <h2 class="secondary-heading">基础评测</h2>
          <div data-evaluation-panel></div>
        </section>
      </div>

      <details class="execution-log">
        <summary>Agent 执行记录</summary>
        <div data-execution-log></div>
      </details>
    </section>
  `;

  root.querySelector("[data-contract-name]").textContent = task?.file_name || "未创建任务";
  root.querySelector("[data-review-status]").textContent = task?.status || "START";
  root.querySelector("[data-playbook-version]").textContent = playbookVersion(task);
  renderContractClassification(
    root.querySelector("[data-contract-classification]"),
    task?.contract_classification || null,
  );

  ReportExport(root.querySelector("[data-report-export]"), {
    task,
    report: props.report,
    onExportReport: props.onExportReport,
  });

  ContractViewer(root.querySelector("[data-contract-viewer]"), {
    clauses: task?.clauses || [],
    risks,
    activeRiskId: props.activeRiskId,
    activeClauseId,
    status: task?.status || "START",
    onSelectionChange: props.onSelectionChange,
  });

  ReviewProgress(root.querySelector("[data-review-progress]"), {
    events: task?.events || [],
  });
  RuleDetail(root.querySelector("[data-rule-detail]"), {
    matchedRules: task?.matched_rules || [],
    activeRuleIds,
  });
  ContextTrace(root.querySelector("[data-context-trace]"), {
    contexts: task?.review_contexts || [],
  });
  RiskList(root.querySelector("[data-risk-list]"), {
    risks,
    activeRiskId: props.activeRiskId,
    onSelectRisk: props.onSelectRisk,
  });
  RiskDetail(root.querySelector("[data-risk-detail]"), {
    risks,
    activeRiskId: props.activeRiskId,
    feedback: props.feedback,
    localReviewLoading: Boolean(props.localReview?.loading),
    onSubmitFeedback: props.onSubmitFeedback,
    onRequestLocalReview: props.onRequestLocalReview,
  });
  LocalReviewPanel(root.querySelector("[data-local-review]"), {
    selection: props.localReview || {},
    result: props.localReview?.result || null,
    error: props.localReview?.error || "",
    loading: Boolean(props.localReview?.loading),
    onRunLocalReview: props.onRunLocalReview,
  });
  EvaluationPanel(root.querySelector("[data-evaluation-panel]"), {
    evaluation: props.evaluation,
    onRunEvaluation: props.onRunEvaluation,
    onRunEffectEvaluation: props.onRunEffectEvaluation,
    onSelectView: props.onSelectEvaluationView,
  });
  ExecutionLog(root.querySelector("[data-execution-log]"), {
    task,
  });
}

function renderContractClassification(root, classification) {
  root.textContent = "";
  if (!classification) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "等待合同类型识别。";
    root.appendChild(empty);
    return;
  }

  const summary = document.createElement("dl");
  summary.className = "task-summary";
  appendClassificationField(summary, "类型", classification.contract_type || "UNKNOWN");
  appendClassificationField(summary, "置信度", formatConfidence(classification.confidence));
  appendClassificationField(summary, "判定", classificationDecisionLabel(classification.decision));
  appendClassificationField(summary, "识别依据", (classification.evidence || []).join("；") || "无");
  root.appendChild(summary);
}

function appendClassificationField(root, label, value) {
  const term = document.createElement("dt");
  term.textContent = label;
  const detail = document.createElement("dd");
  detail.textContent = value;
  root.append(term, detail);
}

function formatConfidence(confidence) {
  const value = Number(confidence);
  return Number.isFinite(value) ? `${Math.round(value * 100)}%` : "未知";
}

function classificationDecisionLabel(decision) {
  if (decision === "SUPPORTED") {
    return "支持审查";
  }
  if (decision === "UNSUPPORTED_CONTRACT_TYPE") {
    return "不支持该合同类型";
  }
  if (decision === "NEED_MANUAL_REVIEW") {
    return "需要人工复核";
  }
  return decision || "未知";
}

function playbookVersion(task) {
  const groups = task?.matched_rules || [];
  for (const group of groups) {
    const rules = group.matched_rules || [];
    const ruleWithVersion = rules.find((rule) => rule.playbook_version);
    if (ruleWithVersion) {
      return ruleWithVersion.playbook_version;
    }
  }
  return task?.matched_rules?.length ? "nda-v1" : "未检索";
}
