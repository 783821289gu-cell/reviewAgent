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
  CANCEL_REQUESTED: "取消请求已接收",
  CANCELLED: "任务已取消",
  NODE_TIMEOUT: "节点执行超时",
  TASK_TIMEOUT: "任务执行超时",
  TASK_ERROR: "任务失败",
};

const EXCEPTION_STATUSES = new Set([
  "PARSE_FAILED",
  "RETRIEVAL_FAILED",
  "LLM_OUTPUT_INVALID",
  "EVIDENCE_MISSING",
  "UNSUPPORTED_CONTRACT_TYPE",
  "CANCELLED",
  "NODE_TIMEOUT",
  "TASK_TIMEOUT",
  "TASK_ERROR",
]);

const REVIEW_STATUSES = new Set([
  "HUMAN_REVIEW_PENDING",
  "NEED_MANUAL_REVIEW",
  "CANCEL_REQUESTED",
]);

export function ReviewProgress(root, props) {
  root.textContent = "";

  const progress = props.progress || null;
  if (progress) {
    root.appendChild(renderLiveProgress(progress));
  }

  const events = (props.events || []).filter(
    (event) => !String(event.step_name || "").endsWith("_progress"),
  );
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
    status.textContent = event.step_name === "manual_recovery_started"
      ? "人工恢复"
      : event.step_name === "recovery_started"
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

function renderLiveProgress(progress) {
  const total = Math.max(0, Number(progress.total) || 0);
  const completed = Math.max(0, Math.min(Number(progress.completed) || 0, total));
  const state = ["running", "completed", "failed", "cancelled"].includes(progress.state)
    ? progress.state
    : "running";
  const section = document.createElement("section");
  section.className = `live-progress state-${state}`;
  section.setAttribute("role", "status");
  section.setAttribute("aria-live", "polite");

  const heading = document.createElement("div");
  heading.className = "live-progress-heading";
  const label = document.createElement("strong");
  label.textContent = progress.stage_label || progress.stage || "任务处理中";
  const count = document.createElement("span");
  count.textContent = total > 0 ? `${completed} / ${total}` : progressStateLabel(state);
  heading.append(label, count);

  const bar = document.createElement("progress");
  bar.max = total || 1;
  bar.value = total > 0 ? completed : (state === "completed" ? 1 : 0);
  bar.setAttribute("aria-label", label.textContent);

  const detail = document.createElement("p");
  detail.textContent = progress.message
    || (progress.current_item ? `当前：${progress.current_item}` : progressStateLabel(state));

  section.append(heading, bar, detail);
  return section;
}

function progressStateLabel(state) {
  if (state === "completed") return "已完成";
  if (state === "failed") return "执行失败";
  if (state === "cancelled") return "已取消";
  return "运行中";
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
