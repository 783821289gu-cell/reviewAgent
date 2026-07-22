const ACTION_STATES = {
  accept: { label: "已采纳", tone: "accepted" },
  ignore: { label: "已忽略", tone: "ignored" },
  update_severity: { label: "等级已修改", tone: "updated" },
  update_suggestion: { label: "建议已修改", tone: "updated" },
};

export function riskFeedbackState(risk) {
  const action = String(risk?.feedback?.user_action || "");
  if (ACTION_STATES[action]) {
    return { ...ACTION_STATES[action], action, handled: true };
  }
  if (risk?.review_status === "IGNORED_RISK") {
    return { label: "已忽略", tone: "ignored", action: "ignore", handled: true };
  }
  if (risk?.review_status === "NEED_MANUAL_REVIEW") {
    return { label: "待人工复核", tone: "pending", action: "", handled: false };
  }
  if (risk?.review_status === "CONFIRMED_RISK") {
    return { label: "待人工处理", tone: "pending", action: "", handled: false };
  }
  return {
    label: risk?.review_status || "待人工处理",
    tone: "pending",
    action: "",
    handled: false,
  };
}

export function pendingRiskCount(risks) {
  return (risks || []).filter((risk) => !riskFeedbackState(risk).handled).length;
}
