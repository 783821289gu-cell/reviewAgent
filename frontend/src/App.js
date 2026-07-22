import { UploadPanel } from "./components/UploadPanel.js";
import { WorkbenchLayout } from "./components/WorkbenchLayout.js";
import { icon } from "./components/Icon.js";

const API_BASE_URL = "";
const ACTIVE_TASK_STORAGE_KEY = "review-agent.active-task-id";
const TERMINAL_STATUSES = new Set([
  "EVIDENCE_VERIFIED",
  "HUMAN_REVIEW_PENDING",
  "PARSE_FAILED",
  "RETRIEVAL_FAILED",
  "LLM_OUTPUT_INVALID",
  "EVIDENCE_MISSING",
  "NEED_MANUAL_REVIEW",
  "UNSUPPORTED_CONTRACT_TYPE",
  "CANCELLED",
  "NODE_TIMEOUT",
  "TASK_TIMEOUT",
  "MEMORY_UPDATED",
  "REPORT_READY",
  "TASK_ERROR",
]);

export function renderApp(root) {
  let eventSource = null;

  const state = {
    backendStatus: "checking",
    task: null,
    error: "",
    loading: false,
    activeRiskId: "",
    activeClauseId: "",
    workspaceView: "review",
    mobileView: "risk",
    riskFilter: "all",
    executionLogOpen: false,
    showUpload: true,
    localReview: emptyLocalReview(),
    feedback: emptyFeedback(),
    report: emptyReport(),
    evaluation: emptyEvaluation(),
    taskControl: emptyTaskControl(),
  };

  function setState(nextState) {
    Object.assign(state, nextState);
    render();
  }

  function closeEventStream() {
    if (eventSource) {
      eventSource.close();
      eventSource = null;
    }
  }

  async function checkBackend() {
    try {
      const response = await fetch(`${API_BASE_URL}/health`);
      if (!response.ok) {
        throw new Error("health check failed");
      }
      setState({ backendStatus: "online" });
      await restoreRememberedTask();
    } catch {
      setState({ backendStatus: "offline" });
    }
  }

  async function restoreRememberedTask() {
    const taskId = rememberedTaskId();
    if (!taskId) {
      return;
    }
    try {
      const response = await fetch(`${API_BASE_URL}/api/tasks/${encodeURIComponent(taskId)}`);
      const payload = await response.json();
      if (response.status === 404) {
        forgetTask();
        return;
      }
      if (!response.ok) {
        throw new Error(payload.message || payload.error || "任务恢复失败");
      }
      const complete = TERMINAL_STATUSES.has(payload.status);
      setState({
        task: payload,
        loading: !complete,
        showUpload: false,
        error: "",
        workspaceView: "review",
        mobileView: "risk",
      });
      if (!complete) {
        subscribeToTaskEvents(payload.task_id, latestEventId(payload));
      }
    } catch (error) {
      setState({ error: error.message || "任务恢复失败", loading: false });
    }
  }

  async function createTaskShell(formState) {
    setState({ error: "", loading: true });

    try {
      const formData = new FormData();
      formData.append("contract_file", formState.file);
      formData.append("review_position", formState.reviewPosition);
      formData.append("llm_mode", formState.llmMode || "local_structured");

      const response = await fetch(`${API_BASE_URL}/api/tasks`, {
        method: "POST",
        body: formData,
      });

      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.message || "任务创建失败");
      }

      rememberTask(payload.task_id);
      setState({
        task: payload,
        loading: true,
        activeRiskId: "",
        activeClauseId: "",
        workspaceView: "review",
        mobileView: "risk",
        riskFilter: "all",
        executionLogOpen: false,
        showUpload: false,
        localReview: emptyLocalReview(),
        feedback: emptyFeedback(),
        report: emptyReport(),
      });
      subscribeToTaskEvents(payload.task_id, latestEventId(payload));
    } catch (error) {
      setState({ error: error.message || "任务创建失败", loading: false, showUpload: true });
    }
  }

  function handleRiskSelect(risk) {
    setState({
      activeRiskId: risk.risk_id || "",
      activeClauseId: risk.clause_id || "",
    });
    focusDocumentTarget(risk.clause_id, risk.risk_id);
  }

  function handleRiskLocate(risk) {
    setState({
      activeRiskId: risk.risk_id || "",
      activeClauseId: risk.clause_id || "",
      workspaceView: "review",
      mobileView: "contract",
    });
    focusDocumentTarget(risk.clause_id, risk.risk_id);
  }

  function handleClauseSelect(clauseId) {
    setState({
      activeClauseId: clauseId || "",
      workspaceView: "review",
      mobileView: "contract",
    });
    focusDocumentTarget(clauseId);
  }

  function handleRiskFilter(riskFilter) {
    const risks = (state.task?.risk_findings || []).filter((risk) => risk.clause_id && risk.evidence_text);
    const filteredRisks = riskFilter === "all"
      ? risks
      : risks.filter((risk) => risk.severity === riskFilter);
    const activeRisk = filteredRisks.find((risk) => risk.risk_id === state.activeRiskId)
      || filteredRisks[0]
      || null;
    setState({
      riskFilter,
      activeRiskId: activeRisk?.risk_id || "",
      activeClauseId: activeRisk?.clause_id || "",
    });
  }

  function handleSelectionChange(selection) {
    setState({
      activeClauseId: selection.clause_id || "",
      localReview: {
        clauseId: selection.clause_id || "",
        selectedText: selection.selected_text || "",
        loading: false,
        error: "",
        result: null,
      },
    });
  }

  async function runLocalReview(selection) {
    if (!state.task?.task_id) {
      setState({
        localReview: {
          ...state.localReview,
          loading: false,
          error: "请先完成合同上传和审查任务。",
        },
      });
      return;
    }

    setState({
      localReview: {
        clauseId: selection.clauseId,
        selectedText: selection.selectedText,
        loading: true,
        error: "",
        result: null,
      },
    });

    try {
      const response = await fetch(`${API_BASE_URL}/api/local-review`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          task_id: state.task.task_id,
          clause_id: selection.clauseId,
          selected_text: selection.selectedText,
        }),
      });
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.message || "局部审查失败");
      }
      setState({
        localReview: {
          clauseId: selection.clauseId,
          selectedText: selection.selectedText,
          loading: false,
          error: "",
          result: payload,
        },
      });
    } catch (error) {
      setState({
        localReview: {
          ...state.localReview,
          loading: false,
          error: error.message || "局部审查失败",
          result: null,
        },
      });
    }
  }

  async function submitFeedback(feedbackPayload) {
    if (!state.task?.task_id) {
      setState({
        feedback: {
          riskId: feedbackPayload.riskId || "",
          loadingRiskId: "",
          action: feedbackPayload.action || "",
          error: "请先完成合同上传和审查任务。",
          message: "",
        },
      });
      return;
    }

    setState({
      feedback: {
        riskId: feedbackPayload.riskId,
        loadingRiskId: feedbackPayload.riskId,
        action: feedbackPayload.action,
        error: "",
        message: "",
      },
    });

    try {
      const response = await fetch(`${API_BASE_URL}/api/tasks/${state.task.task_id}/feedback`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          risk_id: feedbackPayload.riskId,
          action: feedbackPayload.action,
          final_severity: feedbackPayload.finalSeverity,
          final_suggestion: feedbackPayload.finalSuggestion,
          ignore_reason: feedbackPayload.ignoreReason,
          include_in_report: feedbackPayload.includeInReport,
        }),
      });
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.message || "人工反馈提交失败");
      }
      setState({
        task: payload.task,
        activeRiskId: payload.risk?.risk_id || feedbackPayload.riskId,
        activeClauseId: payload.risk?.clause_id || state.activeClauseId,
        loading: false,
        feedback: {
          riskId: feedbackPayload.riskId,
          loadingRiskId: "",
          action: feedbackPayload.action,
          error: "",
          message: payload.message || "人工反馈已记录。",
        },
      });
    } catch (error) {
      setState({
        feedback: {
          riskId: feedbackPayload.riskId,
          loadingRiskId: "",
          action: feedbackPayload.action,
          error: error.message || "人工反馈提交失败",
          message: "",
        },
      });
    }
  }

  async function exportReport() {
    if (!state.task?.task_id) {
      setState({
        report: {
          loading: false,
          error: "请先完成合同上传和审查任务。",
          result: null,
        },
      });
      return;
    }

    setState({
      report: {
        loading: true,
        error: "",
        result: state.report.result,
      },
    });

    try {
      const response = await fetch(`${API_BASE_URL}/api/tasks/${state.task.task_id}/report`, {
        method: "POST",
      });
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.message || "报告导出失败");
      }
      downloadMarkdown(payload.report_file);
      setState({
        task: payload.task,
        loading: false,
        report: {
          loading: false,
          error: "",
          result: payload,
        },
      });
    } catch (error) {
      setState({
        report: {
          loading: false,
          error: error.message || "报告导出失败",
          result: state.report.result,
        },
      });
    }
  }

  async function runEvaluation() {
    setState({
      evaluation: {
        ...state.evaluation,
        activeView: "flow",
        loading: true,
        error: "",
        result: state.evaluation.result,
      },
    });

    try {
      const response = await fetch(`${API_BASE_URL}/api/evaluation/run`, {
        method: "POST",
      });
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.message || "基础评测失败");
      }
      setState({
        evaluation: {
          ...state.evaluation,
          activeView: "flow",
          loading: false,
          error: "",
          result: payload,
        },
      });
    } catch (error) {
      setState({
        evaluation: {
          ...state.evaluation,
          activeView: "flow",
          loading: false,
          error: error.message || "基础评测失败",
          result: state.evaluation.result,
        },
      });
    }
  }

  async function runEffectEvaluation() {
    setState({
      evaluation: {
        ...state.evaluation,
        activeView: "effect",
        effectLoading: true,
        effectError: "",
      },
    });

    try {
      const response = await fetch(`${API_BASE_URL}/api/evaluation/effect/run`, {
        method: "POST",
      });
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.message || "效果评测失败");
      }
      setState({
        evaluation: {
          ...state.evaluation,
          activeView: "effect",
          effectLoading: false,
          effectError: "",
          effectResult: payload,
        },
      });
    } catch (error) {
      setState({
        evaluation: {
          ...state.evaluation,
          activeView: "effect",
          effectLoading: false,
          effectError: error.message || "效果评测失败",
        },
      });
    }
  }

  async function cancelTask() {
    if (!state.task?.task_id || TERMINAL_STATUSES.has(state.task.status)) {
      return;
    }
    const reason = window.prompt("请输入取消原因", "用户主动取消");
    if (reason == null) {
      return;
    }
    setState({ taskControl: { action: "cancel", error: "" } });
    try {
      const response = await fetch(`${API_BASE_URL}/api/tasks/${state.task.task_id}/cancel`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reason }),
      });
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.message || "取消任务失败");
      }
      setState({
        task: payload.task,
        loading: !TERMINAL_STATUSES.has(payload.task.status),
        taskControl: emptyTaskControl(),
      });
    } catch (error) {
      setState({
        taskControl: { action: "", error: error.message || "取消任务失败" },
      });
    }
  }

  async function recoverTask() {
    if (!state.task?.task_id) {
      return;
    }
    const reason = window.prompt("请输入恢复原因", "人工确认后重试失败节点");
    if (reason == null) {
      return;
    }
    setState({ taskControl: { action: "recover", error: "" } });
    try {
      const response = await fetch(`${API_BASE_URL}/api/tasks/${state.task.task_id}/recover`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          reason,
          operator_action: "web_manual_retry",
        }),
      });
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.message || "恢复任务失败");
      }
      const recoveryComplete = TERMINAL_STATUSES.has(payload.task.status);
      setState({
        task: payload.task,
        loading: !recoveryComplete,
        taskControl: emptyTaskControl(),
      });
      if (!recoveryComplete) {
        subscribeToTaskEvents(payload.task.task_id, latestEventId(payload.task));
      }
    } catch (error) {
      setState({
        taskControl: { action: "", error: error.message || "恢复任务失败" },
      });
    }
  }

  function selectEvaluationView(view) {
    if (!new Set(["flow", "effect"]).has(view)) {
      return;
    }
    setState({
      evaluation: {
        ...state.evaluation,
        activeView: view,
      },
    });
  }

  function requestLocalRerun(risk) {
    if (!risk?.clause_id || !risk?.evidence_text) {
      setState({
        localReview: {
          ...state.localReview,
          loading: false,
          error: "当前风险缺少可重审的条款或证据文本。",
          result: null,
        },
      });
      return;
    }
    runLocalReview({
      clauseId: risk.clause_id,
      selectedText: risk.evidence_text,
    });
  }

  function subscribeToTaskEvents(taskId, afterEventId = -1) {
    closeEventStream();
    const cursor = Number.isInteger(afterEventId) ? afterEventId : -1;
    eventSource = new EventSource(
      `${API_BASE_URL}/api/tasks/${taskId}/events?after_event_id=${cursor}`,
    );

    eventSource.addEventListener("review_event", (event) => {
      const payload = JSON.parse(event.data);
      const nextTask = payload.task;
      setState({
        task: nextTask,
        loading: !TERMINAL_STATUSES.has(nextTask.status),
        taskControl: emptyTaskControl(),
      });
      if (TERMINAL_STATUSES.has(nextTask.status)) {
        closeEventStream();
      }
    });

    eventSource.onerror = async () => {
      closeEventStream();
      if (!state.task || state.task.task_id !== taskId || TERMINAL_STATUSES.has(state.task.status)) {
        return;
      }
      try {
        const response = await fetch(`${API_BASE_URL}/api/tasks/${taskId}`);
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.message || payload.error || "任务状态查询失败");
        }
        if (state.task?.task_id !== taskId) {
          return;
        }
        setState({
          task: payload,
          loading: !TERMINAL_STATUSES.has(payload.status),
          error: TERMINAL_STATUSES.has(payload.status)
            ? ""
            : "流式状态连接中断，请刷新任务状态。",
        });
      } catch {
        if (state.task?.task_id === taskId) {
          setState({ loading: false, error: "流式状态连接中断，请刷新任务状态。" });
        }
      }
    };
  }

  function render() {
    const scrollPositions = captureScrollPositions(root);
    const taskId = state.task?.task_id || "";
    const traceId = state.task?.trace_id || "";
    root.innerHTML = `
      <main class="app-shell">
        <nav class="tool-rail" aria-label="工作区导航">
          <div class="rail-brand" title="ContractReviewAgent">
            ${icon("shield-alert")}
            <span class="sr-only">ContractReviewAgent</span>
          </div>
          <div class="rail-nav">
            ${workspaceButton("review", "审查工作区", "layout-dashboard", state.workspaceView, false)}
            ${workspaceButton("memory", "Memory 工作区", "database", state.workspaceView, !state.task)}
            ${workspaceButton("evaluation", "评测工作区", "chart-no-axes-column-increasing", state.workspaceView, false)}
          </div>
          <span class="rail-status status-${state.backendStatus}" title="${backendStatusLabel(state.backendStatus)}"></span>
        </nav>

        <section class="app-frame">
          <header class="command-bar">
            <div class="command-brand">
              ${icon("shield-alert", { className: "mobile-brand-icon" })}
              <div>
                <h1>ContractReviewAgent</h1>
                <p>合同审查工作台</p>
              </div>
            </div>
            <div class="command-context" aria-label="当前任务信息">
              ${taskId ? `<span><b>Task</b>${escapeHtml(shortId(taskId))}</span>` : ""}
              ${traceId ? `<span><b>Trace</b>${escapeHtml(shortId(traceId))}</span>` : ""}
              ${state.task?.review_position ? `<span><b>立场</b>${escapeHtml(state.task.review_position)}</span>` : ""}
            </div>
            <div class="command-actions">
              <span class="service-status status-${state.backendStatus}">
                <i aria-hidden="true"></i>${backendStatusLabel(state.backendStatus)}
              </span>
              <button class="button button-primary new-review-button" type="button" data-new-review aria-label="新建审查" ${state.loading ? "disabled" : ""}>
                ${icon("plus")}<span>新建审查</span>
              </button>
            </div>
          </header>

          <nav class="mobile-workspace-nav" aria-label="移动端工作区导航">
            ${workspaceButton("review", "审查", "layout-dashboard", state.workspaceView, false)}
            ${workspaceButton("memory", "Memory", "database", state.workspaceView, !state.task)}
            ${workspaceButton("evaluation", "评测", "chart-no-axes-column-increasing", state.workspaceView, false)}
          </nav>

          <div class="app-content ${state.showUpload ? "is-uploading" : ""}">
            <section id="upload-root" ${state.showUpload ? "" : "hidden"}></section>
            <section id="workbench-root" ${state.showUpload ? "hidden" : ""}></section>
          </div>
        </section>
      </main>
    `;

    const uploadRoot = root.querySelector("#upload-root");
    const workbenchRoot = root.querySelector("#workbench-root");

    if (state.showUpload) {
      UploadPanel(uploadRoot, {
        disabled: state.backendStatus !== "online",
        loading: state.loading,
        error: state.error,
        canCancel: Boolean(state.task),
        onCancel: () => setState({ showUpload: false, error: "" }),
        onSubmit: createTaskShell,
      });
    } else {
      WorkbenchLayout(workbenchRoot, {
        task: state.task,
        activeRiskId: state.activeRiskId,
        activeClauseId: state.activeClauseId,
        workspaceView: state.workspaceView,
        mobileView: state.mobileView,
        riskFilter: state.riskFilter,
        executionLogOpen: state.executionLogOpen,
        localReview: state.localReview,
        feedback: state.feedback,
        report: state.report,
        evaluation: state.evaluation,
        taskControl: state.taskControl,
        onSelectRisk: handleRiskSelect,
        onLocateRisk: handleRiskLocate,
        onSelectClause: handleClauseSelect,
        onSelectMobileView: (mobileView) => setState({ mobileView }),
        onSelectRiskFilter: handleRiskFilter,
        onExecutionLogToggle: (executionLogOpen) => setState({ executionLogOpen }),
        onSelectionChange: handleSelectionChange,
        onRunLocalReview: runLocalReview,
        onSubmitFeedback: submitFeedback,
        onRequestLocalReview: requestLocalRerun,
        onExportReport: exportReport,
        onRunEvaluation: runEvaluation,
        onRunEffectEvaluation: runEffectEvaluation,
        onSelectEvaluationView: selectEvaluationView,
        onCancelTask: cancelTask,
        onRecoverTask: recoverTask,
      });
      restoreScrollPositions(root, scrollPositions);
    }

    root.querySelectorAll("[data-workspace]").forEach((button) => {
      button.addEventListener("click", () => {
        const workspaceView = button.dataset.workspace;
        setState({
          workspaceView,
          showUpload: workspaceView === "review" && !state.task,
        });
      });
    });
    root.querySelector("[data-new-review]")?.addEventListener("click", () => {
      setState({ showUpload: true, error: "" });
    });
  }

  render();
  checkBackend();
}

function emptyFeedback() {
  return {
    riskId: "",
    loadingRiskId: "",
    action: "",
    error: "",
    message: "",
  };
}

function emptyTaskControl() {
  return {
    action: "",
    error: "",
  };
}

function latestEventId(task) {
  const events = task?.events || [];
  const eventId = events.at(-1)?.event_id;
  return Number.isInteger(eventId) ? eventId : -1;
}

function emptyLocalReview() {
  return {
    clauseId: "",
    selectedText: "",
    loading: false,
    error: "",
    result: null,
  };
}

function emptyReport() {
  return {
    loading: false,
    error: "",
    result: null,
  };
}

function emptyEvaluation() {
  return {
    activeView: "flow",
    loading: false,
    error: "",
    result: null,
    effectLoading: false,
    effectError: "",
    effectResult: null,
  };
}

function downloadMarkdown(reportFile) {
  if (!reportFile?.markdown) {
    return;
  }
  const blob = new Blob([reportFile.markdown], { type: "text/markdown;charset=utf-8" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `${fileStem(reportFile.file_name || reportFile.task_id || "review-report")}.md`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(link.href), 0);
}

function fileStem(fileName) {
  const base = String(fileName || "review-report").split(/[\\/]/).pop() || "review-report";
  return base.replace(/\.[^.]+$/, "").replace(/[^\w.-]+/g, "_") || "review-report";
}

function focusDocumentTarget(clauseId, riskId = "") {
  if (!clauseId) {
    return;
  }
  window.setTimeout(() => {
    const container = document.querySelector('[data-scroll-region="document"]');
    const clause = document.getElementById(clauseId);
    if (!container || !clause || !container.contains(clause)) {
      return;
    }
    const evidence = riskId
      ? Array.from(clause.querySelectorAll("[data-risk-id]")).find(
        (element) => element.dataset.riskId === riskId,
      )
      : null;
    const target = evidence || clause;
    if (isDocumentTargetVisible(container, target)) {
      return;
    }
    const containerRect = container.getBoundingClientRect();
    const targetRect = target.getBoundingClientRect();
    const nextTop = container.scrollTop
      + targetRect.top
      - containerRect.top
      - (container.clientHeight - Math.min(targetRect.height, container.clientHeight)) / 2;
    container.scrollTo({
      top: Math.max(0, nextTop),
      behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
    });
  }, 0);
}

function isDocumentTargetVisible(container, target) {
  const containerRect = container.getBoundingClientRect();
  const targetRect = target.getBoundingClientRect();
  const margin = 20;
  const visibleTargetBottom = targetRect.top + Math.min(targetRect.height, 40);
  return targetRect.top >= containerRect.top + margin
    && visibleTargetBottom <= containerRect.bottom - margin;
}

function captureScrollPositions(root) {
  return Object.fromEntries(
    Array.from(root.querySelectorAll("[data-scroll-region]"), (element) => [
      element.dataset.scrollRegion,
      element.scrollTop,
    ]),
  );
}

function restoreScrollPositions(root, positions) {
  root.querySelectorAll("[data-scroll-region]").forEach((element) => {
    const position = positions[element.dataset.scrollRegion];
    if (Number.isFinite(position)) {
      element.scrollTop = position;
    }
  });
}

function rememberTask(taskId) {
  try {
    window.localStorage.setItem(ACTIVE_TASK_STORAGE_KEY, taskId);
  } catch {
    // Backend persistence remains authoritative when browser storage is unavailable.
  }
}

function rememberedTaskId() {
  try {
    return window.localStorage.getItem(ACTIVE_TASK_STORAGE_KEY) || "";
  } catch {
    return "";
  }
}

function forgetTask() {
  try {
    window.localStorage.removeItem(ACTIVE_TASK_STORAGE_KEY);
  } catch {
    // No action is required when browser storage is unavailable.
  }
}

function workspaceButton(view, label, iconName, activeView, disabled) {
  return `
    <button class="rail-button ${activeView === view ? "is-active" : ""}" type="button"
      data-workspace="${view}" title="${label}" aria-label="${label}"
      ${activeView === view ? 'aria-current="page"' : ""} ${disabled ? "disabled" : ""}>
      ${icon(iconName)}<span>${label}</span>
    </button>
  `;
}

function shortId(value) {
  const text = String(value || "");
  return text.length > 12 ? `${text.slice(0, 6)}…${text.slice(-4)}` : text;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function backendStatusLabel(status) {
  if (status === "online") {
    return "后端已连接";
  }
  if (status === "offline") {
    return "后端未连接";
  }
  return "检查后端中";
}
