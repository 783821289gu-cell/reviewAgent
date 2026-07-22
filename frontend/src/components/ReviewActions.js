import { RiskEditor } from "./RiskEditor.js";
import { icon } from "./Icon.js";
import { riskFeedbackState } from "./riskFeedback.js";

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
  const feedbackState = riskFeedbackState(risk);
  status.className = `review-entry-status decision-${feedbackState.tone}`;
  status.textContent = feedbackState.label;

  panel.append(label, status);

  if (props.error || props.message) {
    const notice = document.createElement("p");
    notice.className = props.error ? "review-feedback-notice is-error" : "review-feedback-notice is-success";
    notice.setAttribute("role", props.error ? "alert" : "status");
    notice.textContent = props.error || props.message;
    panel.appendChild(notice);
  }

  const editor = RiskEditor(panel, risk);
  const actions = document.createElement("div");
  actions.className = "feedback-actions";

  actions.append(
    actionButton("采纳风险", "accept", "check", props, editor, "button-primary", feedbackState.action),
    actionButton("忽略风险", "ignore", "x", props, editor, "button-secondary", feedbackState.action),
    actionButton("保存等级", "update_severity", "pencil", props, editor, "button-secondary", feedbackState.action),
    actionButton("保存建议", "update_suggestion", "pencil", props, editor, "button-secondary", feedbackState.action),
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

  root.appendChild(panel);
}

function actionButton(label, action, iconName, props, editor, variant, currentAction) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `button ${variant} compact-button`;
  const selected = currentAction === action;
  const visibleLabel = selected ? completedActionLabel(action) : label;
  button.innerHTML = `${icon(iconName)}<span>${props.loading ? "提交中" : visibleLabel}</span>`;
  button.disabled = Boolean(props.loading);
  button.setAttribute("aria-pressed", String(selected));
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

function completedActionLabel(action) {
  if (action === "accept") return "已采纳";
  if (action === "ignore") return "已忽略";
  if (action === "update_severity") return "等级已保存";
  if (action === "update_suggestion") return "建议已保存";
  return "已保存";
}
