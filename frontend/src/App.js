import { UploadPanel } from "./components/UploadPanel.js";
import { WorkbenchLayout } from "./components/WorkbenchLayout.js";

const API_BASE_URL = "";
const TERMINAL_STATUSES = new Set([
  "EVIDENCE_VERIFIED",
  "HUMAN_REVIEW_PENDING",
  "PARSE_FAILED",
  "RETRIEVAL_FAILED",
  "LLM_OUTPUT_INVALID",
  "EVIDENCE_MISSING",
  "NEED_MANUAL_REVIEW",
  "UNSUPPORTED_CONTRACT_TYPE",
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
    localReview: emptyLocalReview(),
    feedback: emptyFeedback(),
    report: emptyReport(),
    evaluation: emptyEvaluation(),
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
    } catch {
      setState({ backendStatus: "offline" });
    }
  }

  async function createTaskShell(formState) {
    setState({ error: "", loading: true });

    try {
      const formData = new FormData();
      formData.append("contract_file", formState.file);
      formData.append("review_position", formState.reviewPosition);

      const response = await fetch(`${API_BASE_URL}/api/tasks`, {
        method: "POST",
        body: formData,
      });

      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.message || "任务创建失败");
      }

      setState({
        task: payload,
        loading: true,
        activeRiskId: "",
        activeClauseId: "",
        localReview: emptyLocalReview(),
        feedback: emptyFeedback(),
        report: emptyReport(),
      });
      subscribeToTaskEvents(payload.task_id);
    } catch (error) {
      setState({ error: error.message || "任务创建失败", loading: false });
    }
  }

  function handleRiskSelect(risk) {
    setState({
      activeRiskId: risk.risk_id || "",
      activeClauseId: risk.clause_id || "",
    });
    focusClause(risk.clause_id);
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
          error: "",
          message: payload.message || "人工反馈已记录。",
        },
      });
    } catch (error) {
      setState({
        feedback: {
          riskId: feedbackPayload.riskId,
          loadingRiskId: "",
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

  function subscribeToTaskEvents(taskId) {
    closeEventStream();
    eventSource = new EventSource(`${API_BASE_URL}/api/tasks/${taskId}/events`);

    eventSource.addEventListener("review_event", (event) => {
      const payload = JSON.parse(event.data);
      const nextTask = payload.task;
      setState({
        task: nextTask,
        loading: !TERMINAL_STATUSES.has(nextTask.status),
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
    root.innerHTML = `
      <main class="page-shell">
        <header class="topbar">
          <div>
            <p class="eyebrow">NDA 合同审查 Agent 工作台</p>
            <h1>ContractReviewAgent</h1>
            <p class="summary">任务 10 支持合同审查、人工反馈、报告与评测，并展示可持久化的任务 Trace、工具 Step、恢复信息和外部模型调用摘要。</p>
          </div>
          <span class="status-pill status-${state.backendStatus}">
            ${backendStatusLabel(state.backendStatus)}
          </span>
        </header>

        <section id="upload-root"></section>
        <section id="workbench-root"></section>
      </main>
    `;

    UploadPanel(document.querySelector("#upload-root"), {
      disabled: state.backendStatus !== "online",
      loading: state.loading,
      error: state.error,
      onSubmit: createTaskShell,
    });

    WorkbenchLayout(document.querySelector("#workbench-root"), {
      task: state.task,
      activeRiskId: state.activeRiskId,
      activeClauseId: state.activeClauseId,
      localReview: state.localReview,
      feedback: state.feedback,
      report: state.report,
      evaluation: state.evaluation,
      onSelectRisk: handleRiskSelect,
      onSelectionChange: handleSelectionChange,
      onRunLocalReview: runLocalReview,
      onSubmitFeedback: submitFeedback,
      onRequestLocalReview: requestLocalRerun,
      onExportReport: exportReport,
      onRunEvaluation: runEvaluation,
      onRunEffectEvaluation: runEffectEvaluation,
      onSelectEvaluationView: selectEvaluationView,
    });
  }

  render();
  checkBackend();
}

function emptyFeedback() {
  return {
    riskId: "",
    loadingRiskId: "",
    error: "",
    message: "",
  };
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

function focusClause(clauseId) {
  if (!clauseId) {
    return;
  }
  window.setTimeout(() => {
    document.getElementById(clauseId)?.scrollIntoView({
      behavior: "smooth",
      block: "center",
    });
  }, 0);
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
