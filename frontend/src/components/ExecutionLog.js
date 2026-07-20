export function ExecutionLog(root, props) {
  root.textContent = "";

  const task = props.task;
  const summary = document.createElement("dl");
  summary.className = "task-summary";
  appendSummary(summary, "当前节点", task?.status || "START");
  appendSummary(summary, "说明", task?.message || "尚未创建审查任务。");
  appendSummary(summary, "任务 Trace", task?.trace_id || "尚未生成");
  appendSummary(summary, "恢复次数", String(task?.recovery_count || 0));
  if (task?.recovery_from_status) {
    appendSummary(summary, "恢复来源", task.recovery_from_status);
  }
  if (task?.cancel_reason) {
    appendSummary(summary, "取消原因", task.cancel_reason);
  }
  if (task?.last_timeout) {
    appendSummary(
      summary,
      "最近超时",
      [task.last_timeout.type, task.last_timeout.step_name, task.last_timeout.limit_seconds]
        .filter((value) => value !== "" && value != null)
        .join(" / "),
    );
  }
  if (task?.retry_counts && Object.keys(task.retry_counts).length > 0) {
    appendSummary(
      summary,
      "错误重试",
      Object.entries(task.retry_counts)
        .map(([name, count]) => `${name} ${count}/${task.retry_limits?.[name] ?? "?"}`)
        .join("；"),
    );
  }
  const latestRecovery = task?.recovery_history?.at(-1);
  if (latestRecovery) {
    appendSummary(
      summary,
      "最近人工恢复",
      `${latestRecovery.operator_action} / ${latestRecovery.reason} / 起点 ${latestRecovery.resume_from_status}`,
    );
  }
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
    item.className = log.status === "success" ? "" : "log-failed";

    const title = document.createElement("strong");
    title.textContent = `${log.step_name} / ${log.tool_name}：${statusLabel(log.status)}`;

    const detail = document.createElement("span");
    detail.textContent = [
      log.step_id ? `Step ${log.step_id}` : "Step 未记录",
      log.parent_step_id ? `Parent ${log.parent_step_id}` : "Root Step",
      `Retry ${log.retry_index || 0}`,
      `${log.latency_ms}ms`,
      `输入 ${log.input_summary || "无摘要"}`,
      log.error_message ? `错误 ${log.error_message}` : `输出 ${log.output_summary || "无摘要"}`,
      formatProviderSummary(log.token_cost_summary),
      formatTraceSummary(log.trace_summary, log.idempotency_key),
    ].join("；");

    item.append(title, detail);
    list.appendChild(item);
  });
  root.appendChild(list);
}

function formatProviderSummary(rawSummary) {
  if (!rawSummary || !rawSummary.startsWith("{")) {
    return rawSummary || "not_applicable";
  }
  try {
    const summary = JSON.parse(rawSummary);
    const embeddingCalls = summary.embedding_calls || [];
    if (embeddingCalls.length > 0) {
      return embeddingCalls.map((call, index) => {
        const requestId = call.provider_request_id || "无请求 ID";
        const dimension = call.vector_dimension == null ? "维度未返回" : `维度 ${call.vector_dimension}`;
        const cost = call.cost_status || "未配置";
        const error = call.error_type ? `；错误 ${call.error_type}` : "";
        return `Embedding#${index + 1} ${call.model}；${requestId}；输入 ${call.input_count}；${dimension}；${call.latency_ms}ms；成本 ${cost}${error}`;
      }).join(" | ") || summary.mode;
    }
    const calls = summary.calls || [];
    return calls.map((call, index) => {
      const requestId = call.provider_request_id || "无请求 ID";
      const tokens = call.prompt_tokens == null
        ? "Token 未返回"
        : `Token ${call.prompt_tokens}/${call.completion_tokens ?? 0}`;
      const cost = call.estimated_cost == null ? call.cost_status : call.estimated_cost;
      const error = call.error_type ? `；错误 ${call.error_type}` : "";
      return `LLM#${index + 1} ${call.model}；${requestId}；${tokens}；${call.latency_ms}ms；成本 ${cost}${error}`;
    }).join(" | ") || summary.mode;
  } catch (_error) {
    return rawSummary;
  }
}

function formatTraceSummary(trace, idempotencyKey) {
  if (!trace && !idempotencyKey) {
    return "Trace 摘要未记录";
  }
  const versions = trace?.versions || {};
  const decision = trace?.decision || {};
  const token = trace?.token_allocation || {};
  const parts = [
    versions.prompt ? `Prompt ${versions.prompt}` : "",
    versions.llm_model ? `Model ${versions.llm_model}` : "",
    versions.playbook ? `Playbook ${versions.playbook}` : "",
    decision.action ? `Planner ${decision.action}/${decision.reason_code || ""}` : "",
    decision.type === "critic" ? `Critic ${decision.decision}/${decision.reason_code || ""}` : "",
    decision.type === "evidence_verifier" ? `Evidence ${decision.is_valid ? "pass" : "fail"}` : "",
    decision.type === "retrieval" ? `Candidates ${decision.candidate_count}` : "",
    token.final_prompt_tokens != null
      ? `Context ${token.final_prompt_tokens}/${token.max_prompt_tokens}`
      : "",
    idempotencyKey ? `Idempotency ${shortId(idempotencyKey)}` : "",
  ].filter(Boolean);
  return parts.join("；") || "Trace 摘要已记录";
}

function shortId(value) {
  const text = String(value || "");
  return text.length > 22 ? `${text.slice(0, 10)}…${text.slice(-8)}` : text;
}

function statusLabel(status) {
  if (status === "success") {
    return "成功";
  }
  if (status === "failed") {
    return "失败";
  }
  if (status === "timeout") {
    return "超时";
  }
  if (status === "cancelled") {
    return "已取消";
  }
  return status || "未知";
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
