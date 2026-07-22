import { icon } from "./Icon.js";
import { riskFeedbackState } from "./riskFeedback.js";

export function RiskList(root, props) {
  root.textContent = "";

  const risks = (props.risks || []).filter((risk) => risk.clause_id && risk.evidence_text);
  if (risks.length === 0) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "暂无已通过证据验证的正式风险。";
    root.appendChild(empty);
    return;
  }

  const list = document.createElement("div");
  list.className = "risk-list";

  risks.forEach((risk) => {
    const feedbackState = riskFeedbackState(risk);
    const item = document.createElement("article");
    item.className = [
      "risk-card",
      `risk-card-${feedbackState.tone}`,
      risk.risk_id === props.activeRiskId ? "risk-card-active" : "",
    ].filter(Boolean).join(" ");

    const selectRisk = () => {
      if (typeof props.onSelectRisk === "function") {
        props.onSelectRisk(risk);
      }
    };
    item.addEventListener("click", selectRisk);

    const header = document.createElement("div");
    header.className = "risk-card-header";

    const titleGroup = document.createElement("button");
    titleGroup.type = "button";
    titleGroup.className = "risk-card-title";
    titleGroup.setAttribute("aria-label", `查看风险 ${risk.risk_id || risk.risk_type || "未命名风险"}`);
    titleGroup.addEventListener("click", (event) => {
      event.stopPropagation();
      selectRisk();
    });
    const title = document.createElement("strong");
    title.textContent = `${risk.risk_id || "未命名风险"} ${risk.risk_type || ""}`.trim();
    titleGroup.appendChild(title);

    const severity = document.createElement("span");
    severity.className = severityClassName(risk.severity);
    severity.textContent = risk.severity || "未分级";

    const locate = document.createElement("button");
    locate.className = "icon-button compact-icon-button";
    locate.type = "button";
    locate.title = "定位合同证据";
    locate.setAttribute("aria-label", "定位");
    locate.innerHTML = icon("locate-fixed");
    locate.addEventListener("click", (event) => {
      event.stopPropagation();
      props.onLocateRisk?.(risk);
    });

    header.append(titleGroup, severity, locate);
    item.appendChild(header);

    const meta = document.createElement("p");
    meta.className = "risk-meta";
    const context = document.createElement("span");
    context.textContent = `${risk.clause_id || "未知条款"} / 置信度 ${formatConfidence(risk.confidence)}`;
    const decision = document.createElement("span");
    decision.className = `risk-decision decision-${feedbackState.tone}`;
    decision.textContent = feedbackState.label;
    meta.append(context, decision);
    item.appendChild(meta);

    list.appendChild(item);
  });

  root.appendChild(list);
}

function severityClassName(severity) {
  return `risk-severity severity-${severity || "unknown"}`;
}

function formatConfidence(value) {
  if (typeof value !== "number") {
    return "-";
  }
  return `${Math.round(value * 100)}%`;
}
