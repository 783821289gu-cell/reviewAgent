import { MemoryTrace } from "./MemoryTrace.js";
import { ReviewActions } from "./ReviewActions.js";

export function RiskDetail(root, props) {
  root.textContent = "";

  const formalRisks = (props.risks || []).filter((risk) => risk.clause_id && risk.evidence_text);
  const selectedRisk = formalRisks.find((risk) => risk.risk_id === props.activeRiskId);
  const risks = selectedRisk ? [selectedRisk] : formalRisks;
  if (risks.length === 0) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "没有可展示的风险证据和修改建议。";
    root.appendChild(empty);
    return;
  }

  const wrapper = document.createElement("div");
  wrapper.className = "risk-detail";

  risks.forEach((risk) => {
    const item = document.createElement("article");
    item.className = "risk-detail-card";

    const title = document.createElement("h3");
    title.textContent = `${risk.risk_type || "未命名风险"} / ${risk.clause_id || "未知条款"}`;
    item.appendChild(title);

    item.appendChild(renderFieldList(risk));
    const memoryTrace = document.createElement("div");
    MemoryTrace(memoryTrace, { risk });
    item.appendChild(memoryTrace);

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
    wrapper.appendChild(item);
  });

  root.appendChild(wrapper);
}

function renderFieldList(risk) {
  const fields = document.createElement("dl");
  fields.className = "risk-detail-fields";

  appendField(fields, "证据文本", risk.evidence_text);
  appendField(fields, "风险原因", risk.risk_reason);
  appendField(fields, "命中规则", (risk.matched_rule_ids || []).join("、"));
  appendField(fields, "修改建议", risk.revision_suggestion);
  appendField(fields, "复核状态", reviewStatusLabel(risk.review_status));
  appendField(fields, "报告选择", reportChoiceLabel(risk.include_in_report));

  return fields;
}

function reviewStatusLabel(status) {
  if (status === "NEED_MANUAL_REVIEW") {
    return "待人工复核";
  }
  if (status === "CONFIRMED_RISK") {
    return "已确认";
  }
  if (status === "IGNORED_RISK") {
    return "已忽略";
  }
  return status || "未知状态";
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

function appendField(root, label, value) {
  if (!value) {
    return;
  }

  const term = document.createElement("dt");
  term.textContent = label;

  const detail = document.createElement("dd");
  detail.textContent = value;

  root.append(term, detail);
}
