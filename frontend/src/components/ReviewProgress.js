const STATUS_LABELS = {
  START: "任务创建",
  UPLOAD_RECEIVED: "上传已接收",
  DOCUMENT_PARSED: "文档已解析",
  CONTRACT_TYPE_CLASSIFIED: "合同类型已识别",
  CLAUSES_STRUCTURED: "条款已结构化",
  PLAYBOOK_RETRIEVED: "Playbook 已检索",
  CONTEXT_BUILT: "上下文已构建",
  RISK_ANALYZED: "风险已分析",
  EVIDENCE_VERIFIED: "证据已验证",
  HUMAN_REVIEW_PENDING: "等待人工复核",
  MEMORY_UPDATED: "Memory 已更新",
  REPORT_READY: "报告已生成",
  PARSE_FAILED: "解析失败",
  RETRIEVAL_FAILED: "检索失败",
  LLM_OUTPUT_INVALID: "模型输出无效",
  EVIDENCE_MISSING: "证据缺失",
  NEED_MANUAL_REVIEW: "需要人工复核",
  UNSUPPORTED_CONTRACT_TYPE: "不支持的合同类型",
  TASK_ERROR: "任务失败",
};

const EXCEPTION_STATUSES = new Set([
  "PARSE_FAILED",
  "RETRIEVAL_FAILED",
  "LLM_OUTPUT_INVALID",
  "EVIDENCE_MISSING",
  "UNSUPPORTED_CONTRACT_TYPE",
  "TASK_ERROR",
]);

const REVIEW_STATUSES = new Set([
  "HUMAN_REVIEW_PENDING",
  "NEED_MANUAL_REVIEW",
]);

export function ReviewProgress(root, props) {
  root.textContent = "";

  const events = props.events || [];
  if (events.length === 0) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "暂无流式状态事件。";
    root.appendChild(empty);
    return;
  }

  const list = document.createElement("ol");
  list.className = "progress-list";
  events.forEach((event) => {
    const item = document.createElement("li");
    item.className = progressClassName(event.status);

    const status = document.createElement("strong");
    status.textContent = event.step_name === "recovery_started"
      ? "任务恢复"
      : STATUS_LABELS[event.status] || event.status || "未知状态";

    const meta = document.createElement("span");
    meta.textContent = [event.step_name, event.tool_name].filter(Boolean).join(" / ");

    const message = document.createElement("p");
    message.textContent = event.message || "";

    item.append(status, meta, message);
    list.appendChild(item);
  });

  root.appendChild(list);
}

function progressClassName(status) {
  if (EXCEPTION_STATUSES.has(status)) {
    return "progress-item progress-failed";
  }
  if (REVIEW_STATUSES.has(status)) {
    return "progress-item progress-review";
  }
  return "progress-item";
}
