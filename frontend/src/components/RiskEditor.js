export function RiskEditor(root, risk) {
  const editor = document.createElement("div");
  editor.className = "risk-editor";

  const severityField = document.createElement("label");
  severityField.className = "feedback-field";
  const severityLabel = document.createElement("span");
  severityLabel.textContent = "风险等级";
  const severity = document.createElement("select");
  ["高", "中", "低"].forEach((level) => {
    const option = document.createElement("option");
    option.value = level;
    option.textContent = level;
    option.selected = level === risk.severity;
    severity.appendChild(option);
  });
  severityField.append(severityLabel, severity);

  const suggestionField = document.createElement("label");
  suggestionField.className = "feedback-field feedback-field-wide";
  const suggestionLabel = document.createElement("span");
  suggestionLabel.textContent = "修改建议";
  const suggestion = document.createElement("textarea");
  suggestion.rows = 3;
  suggestion.value = risk.revision_suggestion || "";
  suggestionField.append(suggestionLabel, suggestion);

  const ignoreField = document.createElement("label");
  ignoreField.className = "feedback-field feedback-field-wide";
  const ignoreLabel = document.createElement("span");
  ignoreLabel.textContent = "忽略原因";
  const ignoreReason = document.createElement("input");
  ignoreReason.type = "text";
  ignoreReason.placeholder = "忽略风险时必填";
  ignoreField.append(ignoreLabel, ignoreReason);

  const reportField = document.createElement("label");
  reportField.className = "feedback-checkbox";
  const includeInReport = document.createElement("input");
  includeInReport.type = "checkbox";
  includeInReport.checked = risk.include_in_report === true;
  const reportLabel = document.createElement("span");
  reportLabel.textContent = "加入报告";
  reportField.append(includeInReport, reportLabel);

  editor.append(severityField, suggestionField, ignoreField, reportField);
  root.appendChild(editor);

  return {
    readValues() {
      return {
        finalSeverity: severity.value,
        finalSuggestion: suggestion.value.trim(),
        ignoreReason: ignoreReason.value.trim(),
        includeInReport: includeInReport.checked,
      };
    },
  };
}
