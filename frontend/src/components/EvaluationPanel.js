import { icon } from "./Icon.js";

const CHECK_FIELDS = [
  ["task_ran", "任务"],
  ["document_parsed", "解析"],
  ["clauses_structured", "条款"],
  ["playbook_hit", "规则"],
  ["risk_has_evidence", "证据"],
  ["feedback_memory_written", "Memory"],
  ["report_exported", "报告"],
];

export function EvaluationPanel(root, props) {
  const evaluation = props.evaluation || {};
  const activeView = evaluation.activeView === "effect" ? "effect" : "flow";
  const loading = activeView === "effect" ? evaluation.effectLoading : evaluation.loading;

  root.innerHTML = `
    <section class="evaluation-panel">
      <div class="evaluation-head">
        <div>
          <strong>评测</strong>
          <div class="evaluation-tabs" role="tablist" aria-label="评测类型">
            <button type="button" role="tab" data-evaluation-view="flow" aria-selected="${activeView === "flow"}">流程摘要</button>
            <button type="button" role="tab" data-evaluation-view="effect" aria-selected="${activeView === "effect"}">效果摘要</button>
          </div>
        </div>
        <button class="button button-primary compact-button" data-run-evaluation type="button" ${loading ? "disabled" : ""}>
          ${icon("chart-no-axes-column-increasing")}<span>${loading ? "评测中" : activeView === "effect" ? "运行效果评测" : "运行流程评测"}</span>
        </button>
      </div>
      <div data-evaluation-body></div>
    </section>
  `;

  root.querySelectorAll("[data-evaluation-view]").forEach((button) => {
    button.addEventListener("click", () => props.onSelectView?.(button.dataset.evaluationView));
  });
  root.querySelector("[data-run-evaluation]").addEventListener("click", () => {
    if (activeView === "effect") {
      props.onRunEffectEvaluation?.();
      return;
    }
    props.onRunEvaluation?.();
  });

  const body = root.querySelector("[data-evaluation-body]");
  if (activeView === "effect") {
    renderEffect(body, evaluation);
    return;
  }
  renderFlow(body, evaluation);
}

function renderFlow(body, evaluation) {
  if (evaluation.error) {
    body.innerHTML = `<p class="error-message">${escapeHtml(evaluation.error)}</p>`;
    return;
  }
  const result = evaluation.result || null;
  if (!result) {
    body.innerHTML = `<p class="empty-state">尚未运行流程评测。</p>`;
    return;
  }

  body.innerHTML = `
    <p class="evaluation-claim">${escapeHtml(result.claim || "基础评测仅验证流程跑通，不代表生产级准确率。")}</p>
    <dl class="evaluation-summary">
      <div><dt>样本数</dt><dd>${Number(result.sample_count || 0)}</dd></div>
      <div><dt>跑通数</dt><dd>${Number(result.passed_count || 0)}</dd></div>
    </dl>
    <div class="evaluation-results"></div>
  `;

  const list = body.querySelector(".evaluation-results");
  for (const item of result.results || []) {
    const row = document.createElement("article");
    row.className = "evaluation-row";
    row.innerHTML = `
      <strong>${escapeHtml(item.sample_name || "未命名样本")}</strong>
      <div class="evaluation-checks">
        ${CHECK_FIELDS.map(([field, label]) => `<span class="${item[field] ? "check-pass" : "check-fail"}">${label}</span>`).join("")}
      </div>
      ${item.failure_reason ? `<p class="error-message">${escapeHtml(item.failure_reason)}</p>` : ""}
    `;
    list.appendChild(row);
  }
}

function renderEffect(body, evaluation) {
  if (evaluation.effectError) {
    body.innerHTML = `<p class="error-message">${escapeHtml(evaluation.effectError)}</p>`;
    return;
  }
  const result = evaluation.effectResult || null;
  if (!result) {
    body.innerHTML = `<p class="empty-state">尚未运行效果评测。</p>`;
    return;
  }

  const versions = result.versions || {};
  body.innerHTML = `
    <p class="evaluation-claim">${escapeHtml(result.claim || "仅展示当前标注集上的实际结果。")}</p>
    <dl class="evaluation-summary effect-summary">
      <div><dt>合同样本</dt><dd>${Number(result.sample_count || 0)}</dd></div>
      <div><dt>评测状态</dt><dd>${escapeHtml(result.status || "unknown")}</dd></div>
      <div><dt>Playbook</dt><dd>${escapeHtml(versions.playbook_version || "unknown")}</dd></div>
      <div><dt>标注版本</dt><dd>${escapeHtml(versions.annotation_version || "unknown")}</dd></div>
      <div><dt>LLM</dt><dd>${escapeHtml(`${versions.llm_mode || "unknown"} / ${versions.llm_model || "unknown"}`)}</dd></div>
      <div><dt>Embedding</dt><dd>${escapeHtml(`${versions.embedding_mode || "unknown"} / ${versions.embedding_model || "unknown"}`)}</dd></div>
      <div class="effect-code-version"><dt>代码版本</dt><dd>${escapeHtml(codeVersion(versions))}</dd></div>
    </dl>
    <div class="evaluation-results effect-metrics"></div>
    <div class="evaluation-failures"></div>
  `;

  const metricsRoot = body.querySelector(".effect-metrics");
  for (const metric of result.metrics || []) {
    const row = document.createElement("article");
    row.className = "evaluation-row effect-metric-row";
    row.innerHTML = `
      <div class="effect-metric-head">
        <strong>${escapeHtml(metric.label || metric.metric || "未命名指标")}</strong>
        <span class="${metric.threshold_met ? "check-pass" : "check-fail"}">${formatScore(metric.score)}</span>
      </div>
      <p>样本 ${Number(metric.sample_count || 0)}；通过 ${Number(metric.passed_count || 0)}；失败 ${Number(metric.failed_count || 0)}；阈值 ${formatScore(metric.threshold)}</p>
      ${renderFailures(metric.failure_samples || [])}
    `;
    metricsRoot.appendChild(row);
  }

  const failuresRoot = body.querySelector(".evaluation-failures");
  if ((result.evaluation_failures || []).length > 0) {
    failuresRoot.innerHTML = `
      <strong>评测执行失败</strong>
      ${renderFailures(result.evaluation_failures)}
    `;
  }
}

function renderFailures(failures) {
  if (failures.length === 0) {
    return "";
  }
  return `
    <ul class="effect-failure-list">
      ${failures.map((failure) => `
        <li><code>${escapeHtml(failure.sample_id || "unknown")}</code> ${escapeHtml(failure.reason || failure.failure_type || "评测失败")}</li>
      `).join("")}
    </ul>
  `;
}

function formatScore(value) {
  const number = Number(value);
  return Number.isFinite(number) ? `${(number * 100).toFixed(1)}%` : "0.0%";
}

function codeVersion(versions) {
  const version = versions.code_version || "unknown";
  return versions.git_dirty === true ? `${version} (dirty)` : version;
}

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}
