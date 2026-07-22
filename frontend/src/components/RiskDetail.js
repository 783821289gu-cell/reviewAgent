import { MemoryTrace } from "./MemoryTrace.js";
import { ReviewActions } from "./ReviewActions.js";
import { icon } from "./Icon.js";
import { riskFeedbackState } from "./riskFeedback.js";

export function RiskDetail(root, props) {
  root.textContent = "";

  const formalRisks = (props.risks || []).filter((risk) => risk.clause_id && risk.evidence_text);
  const risk = formalRisks.find((item) => item.risk_id === props.activeRiskId) || formalRisks[0];
  if (!risk) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "没有可展示的风险证据和修改建议。";
    root.appendChild(empty);
    return;
  }

  const item = document.createElement("article");
  item.className = "risk-detail-card";

  const header = document.createElement("header");
  header.className = "risk-detail-header";
  const titleGroup = document.createElement("div");
  const id = document.createElement("span");
  id.textContent = risk.risk_id || "未命名风险";
  const title = document.createElement("h3");
  title.textContent = risk.risk_type || "未命名风险";
  titleGroup.append(id, title);
  const locate = document.createElement("button");
  locate.type = "button";
  locate.className = "button button-secondary compact-button";
  locate.innerHTML = `${icon("locate-fixed")}<span>定位证据</span>`;
  locate.addEventListener("click", () => props.onLocateRisk?.(risk));
  header.append(titleGroup, locate);

  const statusLine = document.createElement("div");
  statusLine.className = "risk-status-line";
  const severity = document.createElement("span");
  severity.className = `risk-severity severity-${severityToken(risk.severity)}`;
  severity.textContent = `${risk.severity || "未分级"}风险`;
  const confidence = document.createElement("span");
  confidence.textContent = `置信度 ${formatConfidence(risk.confidence)}`;
  const feedbackState = riskFeedbackState(risk);
  const reviewStatus = document.createElement("span");
  reviewStatus.className = `risk-decision decision-${feedbackState.tone}`;
  reviewStatus.textContent = feedbackState.label;
  statusLine.append(severity, confidence, reviewStatus);
  item.append(header, statusLine, renderFieldList(risk));

  const memoryContent = document.createElement("div");
  MemoryTrace(memoryContent, { risk });
  if (memoryContent.childElementCount > 0) {
    const memory = document.createElement("details");
    memory.className = "risk-trace-disclosure";
    const summary = document.createElement("summary");
    summary.innerHTML = `${icon("database")}Memory 记录`;
    memory.append(summary, memoryContent);
    item.appendChild(memory);
  }

  const actions = document.createElement("div");
  ReviewActions(actions, {
    risk,
    loading: props.feedback?.loadingRiskId === risk.risk_id,
    error: props.feedback?.riskId === risk.risk_id ? props.feedback.error : "",
    message: props.feedback?.riskId === risk.risk_id ? props.feedback.message : "",
    localReviewLoading: props.localReviewLoading,
    onSubmitFeedback: props.onSubmitFeedback,
    onRequestLocalReview: props.onRequestLocalReview,
  });
  item.appendChild(actions);
  root.appendChild(item);
}

function renderFieldList(risk) {
  const fields = document.createElement("dl");
  fields.className = "risk-detail-fields";

  appendField(fields, "证据", risk.evidence_text, "evidence");
  appendField(fields, "原因", risk.risk_reason);
  appendField(fields, "规则", (risk.matched_rule_ids || []).join("、"));
  appendField(fields, "风险重点", risk.risk_focus);
  appendField(fields, "修改建议", risk.revision_suggestion, "suggestion");
  appendField(fields, "报告", reportChoiceLabel(risk.include_in_report));

  return fields;
}

function reportChoiceLabel(includeInReport) {
  if (includeInReport === true) {
    return "加入报告";
  }
  if (includeInReport === false) {
    return "不加入报告";
  }
  return "未选择";
}

function appendField(root, label, value, className = "") {
  if (!value) {
    return;
  }

  const term = document.createElement("dt");
  term.textContent = label;

  const detail = document.createElement("dd");
  detail.textContent = value;
  if (className) detail.className = `risk-field-${className}`;

  root.append(term, detail);
}

function formatConfidence(value) {
  return typeof value === "number" ? `${Math.round(value * 100)}%` : "-";
}

function severityToken(value) {
  return new Set(["高", "中", "低"]).has(value) ? value : "unknown";
}
