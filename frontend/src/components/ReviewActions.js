import { RiskEditor } from "./RiskEditor.js";
import { icon } from "./Icon.js";

export function ReviewActions(root, props) {
  root.textContent = "";

  const risk = props.risk;
  const panel = document.createElement("section");
  panel.className = "review-entry";
  panel.setAttribute("aria-label", "人工复核入口");

  const label = document.createElement("strong");
  label.className = "review-entry-label";
  label.textContent = "人工复核";

  const status = document.createElement("span");
  status.className = "review-entry-status";
  status.textContent = reviewStatusLabel(risk.review_status);

  panel.append(label, status);

  const editor = RiskEditor(panel, risk);
  const actions = document.createElement("div");
  actions.className = "feedback-actions";

  actions.append(
    actionButton("采纳风险", "accept", "check", props, editor, "button-primary"),
    actionButton("忽略风险", "ignore", "x", props, editor, "button-secondary"),
    actionButton("保存等级", "update_severity", "pencil", props, editor, "button-secondary"),
    actionButton("保存建议", "update_suggestion", "pencil", props, editor, "button-secondary"),
  );

  const rerun = document.createElement("button");
  rerun.type = "button";
  rerun.className = "button button-ghost compact-button";
  rerun.innerHTML = `${icon("rotate-ccw")}<span>${props.localReviewLoading ? "重审中" : "局部重审"}</span>`;
  rerun.disabled = Boolean(props.localReviewLoading);
  rerun.addEventListener("click", () => {
    if (typeof props.onRequestLocalReview === "function") {
      props.onRequestLocalReview(risk);
    }
  });
  actions.appendChild(rerun);
  panel.appendChild(actions);

  if (props.error) {
    const error = document.createElement("p");
    error.className = "error-message";
    error.textContent = props.error;
    panel.appendChild(error);
  }

  if (props.message) {
    const message = document.createElement("p");
    message.className = "feedback-message";
    message.textContent = props.message;
    panel.appendChild(message);
  }

  root.appendChild(panel);
}

function actionButton(label, action, iconName, props, editor, variant) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `button ${variant} compact-button`;
  button.innerHTML = `${icon(iconName)}<span>${props.loading ? "提交中" : label}</span>`;
  button.disabled = Boolean(props.loading);
  button.addEventListener("click", () => {
    if (typeof props.onSubmitFeedback !== "function") {
      return;
    }
    const values = editor.readValues();
    props.onSubmitFeedback({
      riskId: props.risk.risk_id,
      action,
      finalSeverity: values.finalSeverity,
      finalSuggestion: values.finalSuggestion,
      ignoreReason: values.ignoreReason,
      includeInReport: values.includeInReport,
    });
  });
  return button;
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
