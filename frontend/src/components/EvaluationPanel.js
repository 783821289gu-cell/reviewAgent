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
  const result = evaluation.result || null;

  root.innerHTML = `
    <section class="evaluation-panel">
      <div class="evaluation-head">
        <div>
          <strong>基础评测</strong>
          <p>10 份合成 NDA 样本，验证第一版流程闭环。</p>
        </div>
        <button class="secondary-button compact-button" type="button" ${evaluation.loading ? "disabled" : ""}>
          ${evaluation.loading ? "评测中" : "运行评测"}
        </button>
      </div>
      <div data-evaluation-body></div>
    </section>
  `;

  root.querySelector("button").addEventListener("click", () => props.onRunEvaluation?.());
  const body = root.querySelector("[data-evaluation-body]");

  if (evaluation.error) {
    body.innerHTML = `<p class="error-message">${escapeHtml(evaluation.error)}</p>`;
    return;
  }
  if (!result) {
    body.innerHTML = `<p class="empty-state">尚未运行基础评测。</p>`;
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

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}
