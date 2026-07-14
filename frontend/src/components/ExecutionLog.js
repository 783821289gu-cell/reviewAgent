export function ExecutionLog(root, props) {
  root.textContent = "";

  const task = props.task;
  const summary = document.createElement("dl");
  summary.className = "task-summary";
  appendSummary(summary, "当前节点", task?.status || "START");
  appendSummary(summary, "说明", task?.message || "尚未创建审查任务。");
  root.appendChild(summary);

  const logs = task?.logs || [];
  if (logs.length === 0) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "暂无工具调用记录。";
    root.appendChild(empty);
    return;
  }

  const list = document.createElement("ol");
  list.className = "log-list";
  logs.forEach((log) => {
    const item = document.createElement("li");
    item.className = log.status === "failed" ? "log-failed" : "";

    const title = document.createElement("strong");
    title.textContent = `${log.step_name} / ${log.tool_name}：${log.status}`;

    const detail = document.createElement("span");
    detail.textContent = `${log.latency_ms}ms；${log.output_summary || log.error_message || "无输出摘要"}；${log.token_cost_summary}`;

    item.append(title, detail);
    list.appendChild(item);
  });
  root.appendChild(list);
}

function appendSummary(root, termText, detailText) {
  const item = document.createElement("div");
  const term = document.createElement("dt");
  const detail = document.createElement("dd");
  term.textContent = termText;
  detail.textContent = detailText;
  item.append(term, detail);
  root.appendChild(item);
}
