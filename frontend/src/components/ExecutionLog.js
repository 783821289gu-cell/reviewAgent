export function ExecutionLog(root, props) {
  root.textContent = "";

  const task = props.task;
  const summary = document.createElement("dl");
  summary.className = "task-summary";
  appendSummary(summary, "当前节点", task?.status || "START");
  appendSummary(summary, "说明", task?.message || "尚未创建审查任务。");
  appendSummary(summary, "任务 Trace", task?.trace_id || "尚未生成");
  appendSummary(summary, "恢复次数", String(task?.recovery_count || 0));
  if (task?.progress) {
    const total = Number(task.progress.total || 0);
    const completed = Number(task.progress.completed || 0);
    appendSummary(
      summary,
      "当前进度",
      [
        task.progress.stage_label || task.progress.stage,
        total > 0 ? `${completed}/${total}` : task.progress.state,
        task.progress.current_item,
      ].filter(Boolean).join(" / "),
    );
  }
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

  root.appendChild(renderCapabilityOverview(task));

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

function renderCapabilityOverview(task) {
  const section = document.createElement("section");
  section.className = "agent-capability-status";
  section.setAttribute("aria-label", "Agent 能力状态");

  const heading = document.createElement("h3");
  heading.textContent = "Agent 能力状态";
  const list = document.createElement("ul");
  list.className = "agent-capability-list";

  for (const capability of capabilityStates(task)) {
    const item = document.createElement("li");
    item.className = `capability-${capability.tone}`;
    item.dataset.capability = capability.key;
    const label = document.createElement("span");
    label.textContent = capability.label;
    const value = document.createElement("strong");
    value.textContent = capability.value;
    item.append(label, value);
    list.appendChild(item);
  }

  section.append(heading, list);
  return section;
}

function capabilityStates(task) {
  const logs = task?.logs || [];
  const plannerLogs = logs.filter((log) => log.tool_name === "plan_review_action");
  const retrievalRepairLogs = logs.filter(
    (log) => log.tool_name === "retrieve_related_clauses" && Number(log.retry_index || 0) > 0,
  );
  const criticLogs = logs.filter((log) => log.tool_name === "criticize_risk");
  const evidenceLogs = logs.filter((log) => log.tool_name === "verify_evidence");
  const memoryLogs = logs.filter((log) => ["retrieve_memory", "write_memory"].includes(log.tool_name));
  const injectionBlocked = logs.some((log) => (
    String(log.step_name || "").includes("injection_blocked")
    || String(log.error_message || "").toLowerCase().includes("prompt injection")
  ));

  return [
    taskProviderStatus(task),
    plannerCapability(plannerLogs),
    retrievalRepairCapability(retrievalRepairLogs),
    decisionCapability("critic", "Critic", criticLogs),
    decisionCapability("evidence", "Evidence", evidenceLogs),
    memoryCapability(memoryLogs),
    executionCapability(task),
    {
      key: "prompt-injection",
      label: "Injection",
      value: injectionBlocked ? "已阻断并转人工复核" : "未检测到阻断事件",
      tone: injectionBlocked ? "warning" : "neutral",
    },
  ];
}

export function taskProviderStatus(task) {
  const capability = providerCapability(task?.logs || [], task?.llm_mode);
  if (capability.outcome !== "unknown") {
    return capability;
  }
  if (task?.llm_mode === "openai_compatible") {
    return {
      key: "provider",
      label: "DeepSeek",
      value: "本任务已启用，等待实际调用",
      tone: "warning",
      outcome: "external_enabled",
    };
  }
  if (task?.llm_mode === "local_structured") {
    return {
      key: "provider",
      label: "LLM",
      value: "本地模式 / 无外部 LLM",
      tone: "neutral",
      outcome: "local",
    };
  }
  return capability;
}

function retrievalRepairCapability(logs) {
  if (logs.length === 0) {
    return { key: "retrieval-repair", label: "检索修复", value: "未触发", tone: "neutral" };
  }
  const succeeded = logs.filter((log) => log.status === "success").length;
  const failed = logs.length - succeeded;
  return {
    key: "retrieval-repair",
    label: "检索修复",
    value: failed > 0
      ? `调用 ${logs.length} 次 / 成功 ${succeeded} / 失败 ${failed}`
      : `已成功执行 ${succeeded} 次`,
    tone: failed > 0 ? "failure" : "warning",
  };
}

function memoryCapability(logs) {
  if (logs.length === 0) {
    return { key: "memory", label: "Memory", value: "未调用", tone: "neutral" };
  }
  const readSuccess = logs.filter(
    (log) => log.tool_name === "retrieve_memory" && log.status === "success",
  ).length;
  const writeSuccess = logs.filter(
    (log) => log.tool_name === "write_memory" && log.status === "success",
  ).length;
  const failed = logs.filter((log) => log.status !== "success").length;
  return {
    key: "memory",
    label: "Memory",
    value: `读取调用 ${readSuccess} / 写入调用 ${writeSuccess}${failed ? ` / 失败 ${failed}` : ""}`,
    tone: failed > 0 ? "failure" : "success",
  };
}

function providerCapability(logs, taskLlmMode = "") {
  const providers = logs
    .filter((log) => log.tool_name !== "retrieve_related_clauses")
    .map(providerRecord)
    .filter(Boolean);
  const external = providers.filter((provider) => provider.mode === "openai_compatible");
  const externalCalls = external.flatMap((provider) => provider.calls || []);
  const failedCall = externalCalls.find((call) => call.error_type);
  const successfulCall = externalCalls.find((call) => !call.error_type);
  const hasUnadoptedInFlightCall = providers.some(
    (provider) => provider.summary === "openai_compatible_in_flight_result_not_adopted",
  );
  const models = externalCalls.map((call) => String(call.model || "").toLowerCase());
  const isDeepSeek = taskLlmMode === "openai_compatible"
    || models.some((model) => model.includes("deepseek"));

  if (failedCall && !successfulCall) {
    return {
      key: "provider",
      label: isDeepSeek ? "DeepSeek" : "外部 LLM",
      value: `调用失败：${failedCall.error_type}`,
      tone: "failure",
      outcome: "external_failure",
    };
  }
  if (successfulCall) {
    const successfulCount = externalCalls.filter((call) => !call.error_type).length;
    return {
      key: "provider",
      label: isDeepSeek ? "DeepSeek" : "外部 LLM",
      value: hasUnadoptedInFlightCall
        ? `真实调用 ${successfulCount}/${externalCalls.length} 成功；另有在途调用结果未采纳`
        : `真实调用 ${successfulCount}/${externalCalls.length} 成功`,
      tone: failedCall || hasUnadoptedInFlightCall ? "warning" : "success",
      outcome: "external_success",
    };
  }
  if (hasUnadoptedInFlightCall) {
    return {
      key: "provider",
      label: taskLlmMode === "openai_compatible" ? "DeepSeek" : "外部 LLM",
      value: "调用超时，在途结果未采纳",
      tone: "failure",
      outcome: "external_failure",
    };
  }
  if (external.length > 0 || providers.some((provider) => provider.summary === "openai_compatible_not_invoked")) {
    return {
      key: "provider",
      label: taskLlmMode === "openai_compatible" ? "DeepSeek" : "外部 LLM",
      value: "外部模式已配置，本任务未产生调用",
      tone: "neutral",
      outcome: "external_not_invoked",
    };
  }
  if (providers.some((provider) => String(provider.summary || "").includes("no_external_llm"))) {
    return {
      key: "provider",
      label: "LLM",
      value: "本地模式 / 无外部 LLM",
      tone: "neutral",
      outcome: "local",
    };
  }
  return {
    key: "provider",
    label: "LLM",
    value: "未产生调用记录",
    tone: "neutral",
    outcome: "unknown",
  };
}

function plannerCapability(logs) {
  if (logs.length === 0) {
    return { key: "planner", label: "Planner", value: "确定性路径未触发", tone: "neutral" };
  }
  const rejected = logs.find((log) => log.status !== "success");
  if (rejected) {
    const illegal = /not allowed|whitelist|非法|白名单/i.test(String(rejected.error_message || ""));
    return {
      key: "planner",
      label: "Planner",
      value: illegal ? "非法动作已拒绝" : "输出无效，未执行动作",
      tone: "failure",
    };
  }
  const actions = logs.map((log) => log.trace_summary?.decision?.action).filter(Boolean);
  return {
    key: "planner",
    label: "Planner",
    value: actions.length > 0 ? [...new Set(actions)].join(" / ") : "已调用，未记录动作",
    tone: actions.includes("REQUEST_HUMAN_REVIEW") ? "warning" : "success",
  };
}

function decisionCapability(key, label, logs) {
  if (logs.length === 0) {
    return { key, label, value: "未调用", tone: "neutral" };
  }
  const failed = logs.find((log) => log.status !== "success");
  if (failed) {
    return { key, label, value: "执行失败，需人工复核", tone: "failure" };
  }
  const decisions = logs.map((log) => log.trace_summary?.decision || {});
  if (key === "critic") {
    const values = decisions.map((decision) => decision.decision).filter(Boolean);
    const hasConflict = values.some((value) => value !== "PASS");
    return {
      key,
      label,
      value: values.length > 0 ? `历史记录：${[...new Set(values)].join(" / ")}` : "已执行，未记录结论",
      tone: hasConflict ? "warning" : "success",
    };
  }
  const passed = decisions.filter((decision) => decision.is_valid === true).length;
  const failedCount = decisions.filter((decision) => decision.is_valid === false).length;
  if (passed === 0 && failedCount === 0) {
    return { key, label, value: "已执行，未记录结论", tone: "neutral" };
  }
  return {
    key,
    label,
    value: failedCount > 0
      ? `历史记录：通过 ${passed} / 失败 ${failedCount}`
      : `历史记录：通过 ${passed}`,
    tone: failedCount > 0 ? "failure" : "success",
  };
}

function executionCapability(task) {
  if (task?.status === "CANCEL_REQUESTED") {
    return { key: "execution", label: "执行控制", value: "取消请求已提交", tone: "warning" };
  }
  if (task?.status === "CANCELLED") {
    return { key: "execution", label: "执行控制", value: "任务已取消", tone: "warning" };
  }
  if (task?.last_timeout) {
    const recoveries = Number(task.recovery_count || 0);
    return {
      key: "execution",
      label: "执行控制",
      value: recoveries > 0 ? `发生超时 / 已人工恢复 ${recoveries} 次` : "发生超时 / 可人工恢复",
      tone: "failure",
    };
  }
  if (Number(task?.recovery_count || 0) > 0) {
    return {
      key: "execution",
      label: "执行控制",
      value: `已人工恢复 ${task.recovery_count} 次`,
      tone: "warning",
    };
  }
  return { key: "execution", label: "执行控制", value: "无取消、超时或恢复", tone: "neutral" };
}

function providerRecord(log) {
  const traceProvider = log.trace_summary?.provider;
  if (traceProvider?.mode || traceProvider?.summary) {
    return traceProvider;
  }
  const summary = parseProviderSummary(log.token_cost_summary);
  if (summary) {
    return {
      mode: summary.mode,
      calls: summary.calls || summary.embedding_calls || [],
      summary: summary.summary,
    };
  }
  return log.token_cost_summary ? { summary: log.token_cost_summary, calls: [] } : null;
}

function formatProviderSummary(rawSummary) {
  if (!rawSummary || !rawSummary.startsWith("{")) {
    if (String(rawSummary || "").includes("no_external_llm")) {
      return "本地模式（无外部 LLM）";
    }
    if (String(rawSummary || "").includes("no_external_embedding")) {
      return "本地 Embedding（无外部调用）";
    }
    if (rawSummary === "openai_compatible_not_invoked") {
      return "外部 LLM 模式（本步骤未调用）";
    }
    if (rawSummary === "openai_compatible_in_flight_result_not_adopted") {
      return "外部 LLM 调用超时（在途结果未采纳）";
    }
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
      const cost = call.estimated_cost == null ? (call.cost_status || "未配置") : call.estimated_cost;
      const error = call.error_type ? `；错误 ${call.error_type}` : "";
      const provider = String(call.model || "").toLowerCase().includes("deepseek") ? "DeepSeek" : "LLM";
      const callStatus = call.error_type ? "调用失败" : "真实调用";
      return `${provider}${callStatus}#${index + 1} ${call.model}；${requestId}；${tokens}；${call.latency_ms}ms；成本 ${cost}${error}`;
    }).join(" | ") || `${summary.mode || "unknown"}（未产生调用）`;
  } catch (_error) {
    return rawSummary;
  }
}

function parseProviderSummary(rawSummary) {
  if (!rawSummary || !String(rawSummary).startsWith("{")) {
    return null;
  }
  try {
    return JSON.parse(rawSummary);
  } catch (_error) {
    return null;
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
    decision.type === "planner" && decision.query_adjustment_fields?.length
      ? `调整 ${decision.query_adjustment_fields.join(",")}`
      : "",
    decision.type === "critic" ? `Critic ${decision.decision}/${decision.reason_code || ""}` : "",
    decision.type === "evidence_verifier" ? `Evidence ${decision.is_valid ? "pass" : "fail"}` : "",
    decision.type === "retrieval" ? `Candidates ${decision.candidate_count}` : "",
    decision.type === "retrieval" && decision.top_k?.effective != null
      ? `TopK ${decision.top_k.effective}/${decision.top_k.maximum ?? "?"}`
      : "",
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
