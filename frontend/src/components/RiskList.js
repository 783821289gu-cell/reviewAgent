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
    const item = document.createElement("article");
    item.className = risk.risk_id === props.activeRiskId ? "risk-card risk-card-active" : "risk-card";

    const selectRisk = () => {
      if (typeof props.onSelectRisk === "function") {
        props.onSelectRisk(risk);
      }
    };
    item.addEventListener("click", selectRisk);

    const header = document.createElement("div");
    header.className = "risk-card-header";

    const title = document.createElement("strong");
    title.textContent = `${risk.risk_id || "未命名风险"} ${risk.risk_type || ""}`.trim();

    const severity = document.createElement("span");
    severity.className = severityClassName(risk.severity);
    severity.textContent = risk.severity || "未分级";

    const locate = document.createElement("button");
    locate.className = "secondary-button compact-button";
    locate.type = "button";
    locate.textContent = "定位";
    locate.addEventListener("click", (event) => {
      event.stopPropagation();
      selectRisk();
    });

    header.append(title, severity, locate);
    item.appendChild(header);

    const meta = document.createElement("p");
    meta.className = "risk-meta";
    meta.textContent = `${risk.clause_id || "未知条款"} / 置信度 ${formatConfidence(risk.confidence)} / ${reviewStatusLabel(risk.review_status)}`;
    item.appendChild(meta);

    const reason = document.createElement("p");
    reason.className = "risk-reason";
    reason.textContent = risk.risk_reason || "";
    item.appendChild(reason);

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

function reviewStatusLabel(status) {
  if (status === "NEED_MANUAL_REVIEW") {
    return "待人工复核";
  }
  if (status === "CONFIRMED_RISK") {
    return "已验证";
  }
  return status || "未知状态";
}
