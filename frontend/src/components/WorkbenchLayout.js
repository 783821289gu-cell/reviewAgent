import { ContractViewer } from "./ContractViewer.js";
import { ContextTrace } from "./ContextTrace.js";
import { EvaluationPanel } from "./EvaluationPanel.js";
import { ExecutionLog } from "./ExecutionLog.js";
import { icon } from "./Icon.js";
import { LocalReviewPanel } from "./LocalReviewPanel.js";
import { MemoryTrace } from "./MemoryTrace.js";
import { ReportExport } from "./ReportExport.js";
import { ReviewProgress } from "./ReviewProgress.js";
import { RiskDetail } from "./RiskDetail.js";
import { RiskList } from "./RiskList.js";
import { RuleDetail } from "./RuleDetail.js";

export function WorkbenchLayout(root, props) {
  const task = props.task;
  const risks = formalRisks(task);
  const filteredRisks = filterRisks(risks, props.riskFilter);
  const activeRisk = filteredRisks.find((risk) => risk.risk_id === props.activeRiskId)
    || filteredRisks[0]
    || null;
  const activeRiskId = activeRisk?.risk_id || "";
  const activeClauseId = props.activeClauseId || activeRisk?.clause_id || props.localReview?.clauseId || "";
  const activeRuleIds = activeRisk?.matched_rule_ids || [];
  const workspaceView = validWorkspace(props.workspaceView);
  const reviewAttention = workspaceView === "review" ? reviewAttentionState(task, risks) : null;

  root.innerHTML = `
    <section class="workbench workspace-${workspaceView}${reviewAttention ? " has-review-attention" : ""}" aria-label="审查工作台">
      ${renderTaskStrip(task, props.report, props.taskControl)}
      ${reviewAttention ? renderReviewAttention(reviewAttention) : ""}
      ${workspaceView === "review" ? renderReviewWorkspace(props.mobileView, risks, props.riskFilter) : ""}
      ${workspaceView === "memory" ? renderMemoryWorkspace(task, risks) : ""}
      ${workspaceView === "evaluation" ? renderEvaluationWorkspace() : ""}
      ${renderExecutionDrawer(task, props.executionLogOpen, props.mobileView)}
    </section>
  `;

  ReportExport(root.querySelector("[data-report-export]"), {
    task,
    report: props.report,
    onExportReport: props.onExportReport,
  });
  root.querySelector("[data-cancel-task]")?.addEventListener("click", () => {
    props.onCancelTask?.();
  });
  root.querySelector("[data-recover-task]")?.addEventListener("click", () => {
    props.onRecoverTask?.();
  });
  root.querySelector("[data-human-review-action]")?.addEventListener("click", (event) => {
    const riskId = event.currentTarget.dataset.humanReviewAction;
    if (riskId) {
      props.onLocateRisk?.(riskId);
    }
  });

  if (workspaceView === "review") {
    renderReviewComponents(root, props, {
      task,
      risks,
      filteredRisks,
      activeRisk,
      activeRiskId,
      activeClauseId,
      activeRuleIds,
    });
  }

  if (workspaceView === "memory") {
    renderMemoryComponents(root, risks);
  }

  if (workspaceView === "evaluation") {
    EvaluationPanel(root.querySelector("[data-evaluation-panel]"), {
      evaluation: props.evaluation,
      onRunEvaluation: props.onRunEvaluation,
      onRunEffectEvaluation: props.onRunEffectEvaluation,
      onSelectView: props.onSelectEvaluationView,
    });
  }

  ExecutionLog(root.querySelector("[data-execution-log]"), { task });
  root.querySelector("details.execution-log")?.addEventListener("toggle", (event) => {
    if (event.target.open !== Boolean(props.executionLogOpen)) {
      props.onExecutionLogToggle?.(event.target.open);
    }
  });
}

function reviewAttentionState(task, risks) {
  if (!task) return null;
  const evidenceFailed = task.status === "EVIDENCE_MISSING";
  const pendingRisks = risks.filter((risk) => (
    risk.review_status === "NEED_MANUAL_REVIEW"
    || (risk.severity === "高" && !["CONFIRMED_RISK", "IGNORED_RISK"].includes(risk.review_status))
  ));
  const statusRequiresHuman = [
    "HUMAN_REVIEW_PENDING",
    "NEED_MANUAL_REVIEW",
    "EVIDENCE_MISSING",
    "LLM_OUTPUT_INVALID",
  ].includes(task.status);
  const criticConflict = (statusRequiresHuman || pendingRisks.length > 0)
    && (task.review_contexts || []).some((context) => {
      const latestDecision = context.critic_trace?.at(-1)?.decision;
      return latestDecision && latestDecision !== "PASS";
    });

  if (!evidenceFailed && !criticConflict && pendingRisks.length === 0 && !statusRequiresHuman) {
    return null;
  }
  let message = "Agent 结论需要人工复核，不能作为确定结论。";
  if (evidenceFailed) {
    message = "证据校验未通过，相关候选未形成可确认的正式结论。";
  } else if (criticConflict) {
    message = "Critic 与风险分析存在冲突，当前结果需要人工裁决。";
  } else if (pendingRisks.some((risk) => risk.severity === "高")) {
    message = "存在高风险或低置信度结果，需由人工确认后再进入报告。";
  }
  return { message, riskId: pendingRisks[0]?.risk_id || "" };
}

function renderReviewAttention(attention) {
  return `
    <aside class="review-attention" role="status" data-human-review-attention>
      <div>${icon("shield-alert")}<p><strong>人工复核待处理</strong><span>${escapeHtml(attention.message)}</span></p></div>
      ${attention.riskId ? `<button class="button button-secondary compact-button" type="button" data-human-review-action="${escapeHtml(attention.riskId)}">${icon("shield-alert")}<span>查看待复核风险</span></button>` : ""}
    </aside>
  `;
}

function renderReviewComponents(root, props, context) {
  const { task, risks, filteredRisks, activeRisk, activeRiskId, activeClauseId, activeRuleIds } = context;

  renderContractClassification(
    root.querySelector("[data-contract-classification]"),
    task?.contract_classification || null,
  );
  ReviewProgress(root.querySelector("[data-review-progress]"), { events: task?.events || [] });
  renderClauseNavigation(root.querySelector("[data-clause-navigation]"), {
    clauses: task?.clauses || [],
    risks,
    activeClauseId,
    onSelectClause: props.onSelectClause,
  });
  renderMemorySummary(root.querySelector("[data-memory-summary]"), risks);

  ContractViewer(root.querySelector("[data-contract-viewer]"), {
    clauses: task?.clauses || [],
    risks,
    activeRiskId,
    activeClauseId,
    status: task?.status || "START",
    onSelectRisk: props.onSelectRisk,
    onSelectionChange: props.onSelectionChange,
  });
  LocalReviewPanel(root.querySelector("[data-local-review]"), {
    selection: props.localReview || {},
    result: props.localReview?.result || null,
    error: props.localReview?.error || "",
    loading: Boolean(props.localReview?.loading),
    onRunLocalReview: props.onRunLocalReview,
  });

  RiskList(root.querySelector("[data-risk-list]"), {
    risks: filteredRisks,
    activeRiskId,
    onSelectRisk: props.onSelectRisk,
    onLocateRisk: props.onLocateRisk,
  });
  RiskDetail(root.querySelector("[data-risk-detail]"), {
    risks: filteredRisks,
    activeRiskId,
    feedback: props.feedback,
    localReviewLoading: Boolean(props.localReview?.loading),
    onLocateRisk: props.onLocateRisk,
    onSubmitFeedback: props.onSubmitFeedback,
    onRequestLocalReview: props.onRequestLocalReview,
  });
  RuleDetail(root.querySelector("[data-rule-detail]"), {
    matchedRules: activeRisk ? filterRuleGroups(task?.matched_rules || [], activeRisk) : [],
    activeRuleIds,
  });
  ContextTrace(root.querySelector("[data-context-trace]"), {
    contexts: activeRisk ? filterContexts(task?.review_contexts || [], activeClauseId) : [],
  });

  root.querySelectorAll("[data-mobile-view]").forEach((button) => {
    button.addEventListener("click", () => props.onSelectMobileView?.(button.dataset.mobileView));
  });
  root.querySelectorAll("[data-risk-filter]").forEach((button) => {
    button.addEventListener("click", () => props.onSelectRiskFilter?.(button.dataset.riskFilter));
  });
}

function renderTaskStrip(task, report, taskControl) {
  const canCancel = task
    && task.status !== "CANCEL_REQUESTED"
    && !TASK_TERMINAL_STATUSES.has(task.status);
  const canRecover = task && RECOVERABLE_TASK_STATUSES.has(task.status);
  const controlBusy = Boolean(taskControl?.action);
  return `
    <header class="task-strip">
      <div class="task-title">
        ${icon("file-text")}
        <div>
          <strong data-contract-name>${escapeHtml(task?.file_name || "未创建任务")}</strong>
          <span>${escapeHtml(task?.contract_classification?.contract_type || "等待合同识别")}</span>
        </div>
      </div>
      <dl class="task-strip-meta">
        <div><dt>状态</dt><dd data-review-status>${escapeHtml(task?.status || "START")}</dd></div>
        <div><dt>立场</dt><dd>${escapeHtml(task?.review_position || "未选择")}</dd></div>
        <div><dt>Playbook</dt><dd data-playbook-version>${escapeHtml(playbookVersion(task))}</dd></div>
      </dl>
      <div class="task-actions">
        <div class="task-control-actions">
          ${canCancel ? `
            <button class="button button-secondary compact-button" type="button" data-cancel-task ${controlBusy ? "disabled" : ""}>
              ${icon("x")}<span>${task?.status === "CANCEL_REQUESTED" ? "取消中" : "取消"}</span>
            </button>
          ` : ""}
          ${canRecover ? `
            <button class="button button-secondary compact-button" type="button" data-recover-task ${controlBusy ? "disabled" : ""}>
              ${icon("rotate-ccw")}<span>人工恢复</span>
            </button>
          ` : ""}
        </div>
        ${taskControl?.error ? `<p class="inline-error" role="alert">${escapeHtml(taskControl.error)}</p>` : ""}
        <div class="task-report" data-report-export data-report-state="${report?.loading ? "loading" : "idle"}"></div>
      </div>
    </header>
  `;
}

const TASK_TERMINAL_STATUSES = new Set([
  "EVIDENCE_VERIFIED",
  "HUMAN_REVIEW_PENDING",
  "MEMORY_UPDATED",
  "REPORT_READY",
  "PARSE_FAILED",
  "RETRIEVAL_FAILED",
  "LLM_OUTPUT_INVALID",
  "EVIDENCE_MISSING",
  "NEED_MANUAL_REVIEW",
  "UNSUPPORTED_CONTRACT_TYPE",
  "CANCELLED",
  "NODE_TIMEOUT",
  "TASK_TIMEOUT",
  "TASK_ERROR",
]);

const RECOVERABLE_TASK_STATUSES = new Set([
  "PARSE_FAILED",
  "RETRIEVAL_FAILED",
  "LLM_OUTPUT_INVALID",
  "EVIDENCE_MISSING",
  "NODE_TIMEOUT",
  "TASK_TIMEOUT",
  "TASK_ERROR",
]);

function renderReviewWorkspace(mobileView, risks, activeFilter) {
  const view = validMobileView(mobileView);
  return `
    <nav class="mobile-mode-tabs" aria-label="审查视图">
      ${mobileModeButton("risk", "风险", view, risks.length)}
      ${mobileModeButton("contract", "合同", view)}
      ${mobileModeButton("log", "执行记录", view)}
    </nav>
    <div class="workbench-grid mobile-view-${view}">
      <aside class="context-pane" aria-label="任务上下文">
        <section class="context-section-block">
          <div class="pane-heading"><div><span class="section-kicker">CONTEXT</span><h2>合同上下文</h2></div></div>
          <div data-contract-classification></div>
        </section>
        <section class="context-section-block progress-block">
          <div class="pane-heading compact"><h3>审查进度</h3></div>
          <div data-review-progress></div>
        </section>
        <section class="context-section-block clause-navigation-block">
          <div class="pane-heading compact"><h3>条款导航</h3></div>
          <div data-clause-navigation></div>
        </section>
        <section class="context-section-block memory-summary-block">
          <div class="pane-heading compact"><h3>Memory</h3></div>
          <div data-memory-summary></div>
        </section>
      </aside>

      <section class="document-pane" aria-label="合同原文">
        <header class="pane-toolbar">
          <div>
            <span class="section-kicker">DOCUMENT</span>
            <h2>合同原文与证据</h2>
          </div>
          <div class="document-tools" aria-label="文档工具">
            <span class="document-mode">证据联动</span>
          </div>
        </header>
        <div class="document-scroll" data-contract-viewer></div>
        <div class="local-review-dock">
          <div class="dock-heading"><strong>局部审查</strong><span>框选合同原文后提交</span></div>
          <div data-local-review></div>
        </div>
      </section>

      <aside class="inspector-pane" aria-label="风险检查器">
        <header class="pane-toolbar inspector-toolbar">
          <div><span class="section-kicker">RISKS</span><h2>风险检查器 <b>${risks.length}</b></h2></div>
        </header>
        ${renderRiskFilters(risks, activeFilter)}
        <div class="inspector-scroll">
          <div data-risk-list></div>
          <div data-risk-detail></div>
          <details class="inspector-disclosure">
            <summary>${icon("chevron-right")}命中规则</summary>
            <div data-rule-detail></div>
          </details>
          <details class="inspector-disclosure">
            <summary>${icon("chevron-right")}上下文 Trace</summary>
            <div data-context-trace></div>
          </details>
        </div>
      </aside>
    </div>
  `;
}

function renderRiskFilters(risks, activeFilter) {
  const counts = severityCounts(risks);
  return `
    <div class="risk-filters" role="group" aria-label="风险筛选">
      ${riskFilterButton("all", "全部", risks.length, activeFilter)}
      ${riskFilterButton("高", "高", counts["高"], activeFilter)}
      ${riskFilterButton("中", "中", counts["中"], activeFilter)}
      ${riskFilterButton("低", "低", counts["低"], activeFilter)}
    </div>
  `;
}

function riskFilterButton(value, label, count, activeFilter) {
  const selected = (activeFilter || "all") === value;
  return `<button type="button" data-risk-filter="${value}" aria-pressed="${selected}" class="${selected ? "is-active" : ""}"><span>${label}</span><b>${count}</b></button>`;
}

function renderMemoryWorkspace(task, risks) {
  const referenceCount = risks.reduce((count, risk) => count + (risk.memory_references || []).length, 0);
  const feedbackCount = risks.filter((risk) => risk.feedback?.memory_id).length;
  return `
    <section class="standalone-workspace memory-workspace" aria-label="Memory 工作区">
      <header class="standalone-heading">
        <div><span class="section-kicker">MEMORY</span><h2>任务 Memory</h2></div>
        <dl class="workspace-metrics">
          <div><dt>历史引用</dt><dd>${referenceCount}</dd></div>
          <div><dt>本次写入</dt><dd>${feedbackCount}</dd></div>
        </dl>
      </header>
      <p class="workspace-context">${escapeHtml(task?.file_name || "当前没有审查任务")}</p>
      <div class="memory-workspace-list" data-memory-workspace-list></div>
    </section>
  `;
}

function renderMemoryComponents(root, risks) {
  const list = root.querySelector("[data-memory-workspace-list]");
  const withMemory = risks.filter((risk) => (risk.memory_references || []).length || risk.feedback?.memory_id);
  if (withMemory.length === 0) {
    list.innerHTML = '<p class="empty-state">当前任务没有可展示的 Memory 引用或人工反馈写入。</p>';
    return;
  }
  withMemory.forEach((risk) => {
    const article = document.createElement("article");
    article.className = "memory-workspace-item";
    const title = document.createElement("h3");
    title.textContent = `${risk.risk_id || "未命名风险"} / ${risk.risk_type || "风险"}`;
    const content = document.createElement("div");
    MemoryTrace(content, { risk });
    article.append(title, content);
    list.appendChild(article);
  });
}

function renderEvaluationWorkspace() {
  return `
    <section class="standalone-workspace evaluation-workspace" aria-label="评测工作区">
      <header class="standalone-heading">
        <div><span class="section-kicker">EVALUATION</span><h2>流程与效果评测</h2></div>
      </header>
      <div data-evaluation-panel></div>
    </section>
  `;
}

function renderExecutionDrawer(task, open, mobileView) {
  const logs = task?.logs || [];
  const duration = logs.reduce((total, log) => total + Number(log.latency_ms || 0), 0);
  const shouldOpen = open || mobileView === "log";
  return `
    <details class="execution-log" ${shouldOpen ? "open" : ""}>
      <summary>
        <span class="execution-title">${icon("panel-bottom")}<strong>Agent 执行记录</strong></span>
        <span class="execution-summary"><b>${logs.length}</b> Steps <i></i> ${formatDuration(duration)}</span>
        ${icon("chevron-down", { className: "drawer-chevron" })}
      </summary>
      <div class="execution-body" data-execution-log></div>
    </details>
  `;
}

function renderClauseNavigation(root, props) {
  root.textContent = "";
  if (props.clauses.length === 0) {
    root.innerHTML = '<p class="empty-state">等待条款解析。</p>';
    return;
  }
  const list = document.createElement("ol");
  list.className = "clause-navigation";
  props.clauses.forEach((clause) => {
    const clauseRisks = props.risks.filter((risk) => risk.clause_id === clause.clause_id);
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.className = clause.clause_id === props.activeClauseId ? "is-active" : "";
    button.innerHTML = `<span>${escapeHtml(clause.clause_id)}</span><strong>${escapeHtml(clause.title || "未命名条款")}</strong>${clauseRisks.length ? `<b>${clauseRisks.length}</b>` : ""}`;
    button.addEventListener("click", () => props.onSelectClause?.(clause.clause_id));
    item.appendChild(button);
    list.appendChild(item);
  });
  root.appendChild(list);
}

function renderMemorySummary(root, risks) {
  const references = risks.reduce((count, risk) => count + (risk.memory_references || []).length, 0);
  const writes = risks.filter((risk) => risk.feedback?.memory_id).length;
  root.innerHTML = `
    <dl class="memory-summary">
      <div><dt>召回</dt><dd>${references}</dd></div>
      <div><dt>写入</dt><dd>${writes}</dd></div>
    </dl>
  `;
}

function renderContractClassification(root, classification) {
  root.textContent = "";
  if (!classification) {
    root.innerHTML = '<p class="empty-state">等待合同类型识别。</p>';
    return;
  }
  const summary = document.createElement("dl");
  summary.className = "classification-summary";
  appendClassificationField(summary, "类型", classification.contract_type || "UNKNOWN");
  appendClassificationField(summary, "置信度", formatConfidence(classification.confidence));
  appendClassificationField(summary, "判定", classificationDecisionLabel(classification.decision));
  appendClassificationField(summary, "识别依据", (classification.evidence || []).join("；") || "无");
  root.appendChild(summary);
}

function appendClassificationField(root, label, value) {
  const wrapper = document.createElement("div");
  const term = document.createElement("dt");
  term.textContent = label;
  const detail = document.createElement("dd");
  detail.textContent = value;
  wrapper.append(term, detail);
  root.appendChild(wrapper);
}

function filterRisks(risks, filter) {
  return filter && filter !== "all" ? risks.filter((risk) => risk.severity === filter) : risks;
}

function filterRuleGroups(groups, activeRisk) {
  if (!activeRisk?.clause_id) {
    return groups;
  }
  const selected = groups.filter((group) => group.clause_id === activeRisk.clause_id);
  return selected.length ? selected : groups;
}

function filterContexts(contexts, activeClauseId) {
  if (!activeClauseId) {
    return contexts;
  }
  const selected = contexts.filter((context) => context.current_clause?.clause_id === activeClauseId);
  return selected.length ? selected : contexts;
}

function severityCounts(risks) {
  return risks.reduce((counts, risk) => {
    counts[risk.severity] = (counts[risk.severity] || 0) + 1;
    return counts;
  }, { 高: 0, 中: 0, 低: 0 });
}

function formalRisks(task) {
  return (task?.risk_findings || []).filter((risk) => risk.clause_id && risk.evidence_text);
}

function mobileModeButton(view, label, activeView, count) {
  return `<button type="button" data-mobile-view="${view}" aria-label="${label}" aria-selected="${activeView === view}">${label}${count == null ? "" : `<b>${count}</b>`}</button>`;
}

function validWorkspace(value) {
  return new Set(["review", "memory", "evaluation"]).has(value) ? value : "review";
}

function validMobileView(value) {
  return new Set(["risk", "contract", "log"]).has(value) ? value : "risk";
}

function formatConfidence(confidence) {
  const value = Number(confidence);
  return Number.isFinite(value) ? `${Math.round(value * 100)}%` : "未知";
}

function classificationDecisionLabel(decision) {
  if (decision === "SUPPORTED") return "支持审查";
  if (decision === "UNSUPPORTED_CONTRACT_TYPE") return "不支持该合同类型";
  if (decision === "NEED_MANUAL_REVIEW") return "需要人工复核";
  return decision || "未知";
}

function playbookVersion(task) {
  const groups = task?.matched_rules || [];
  for (const group of groups) {
    const rule = (group.matched_rules || []).find((item) => item.playbook_version);
    if (rule) return rule.playbook_version;
  }
  return groups.length ? "nda-v1" : "未检索";
}

function formatDuration(duration) {
  if (duration < 1000) return `${duration}ms`;
  return `${(duration / 1000).toFixed(1)}s`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
