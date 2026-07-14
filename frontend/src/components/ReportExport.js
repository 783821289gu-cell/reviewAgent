const REPORTABLE_STATUSES = new Set(["EVIDENCE_VERIFIED", "HUMAN_REVIEW_PENDING", "MEMORY_UPDATED", "REPORT_READY"]);

export function ReportExport(root, props) {
  const report = props.report || {};
  const reportFile = report.result?.report_file || null;
  const hasTask = Boolean(props.task?.task_id);
  const reportable = REPORTABLE_STATUSES.has(props.task?.status);
  const disabled = !hasTask || !reportable || Boolean(report.loading);

  root.innerHTML = `
    <div class="report-export">
      <button class="secondary-button compact-button" type="button" ${disabled ? "disabled" : ""}>
        ${report.loading ? "导出中" : "导出报告"}
      </button>
      <p class="inline-status" data-report-status></p>
    </div>
  `;

  const button = root.querySelector("button");
  button.addEventListener("click", () => {
    if (!disabled) {
      props.onExportReport?.();
    }
  });

  const status = root.querySelector("[data-report-status]");
  if (report.error) {
    status.className = "inline-status error-message";
    status.textContent = report.error;
    return;
  }
  if (reportFile) {
    status.textContent = `已导出 ${reportFile.risk_count} 项风险`;
    return;
  }
  if (!hasTask) {
    status.textContent = "未创建任务";
    return;
  }
  status.textContent = reportable ? "等待导出" : "审查完成后可导出";
}
