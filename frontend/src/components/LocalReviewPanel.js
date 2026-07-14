export function LocalReviewPanel(root, props) {
  root.textContent = "";

  const panel = document.createElement("section");
  panel.className = "local-review-panel";

  const selectedText = props.selection?.selectedText || "";
  const clauseId = props.selection?.clauseId || "";
  if (!selectedText || !clauseId) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "在左侧合同原文中框选文本后，可发起局部审查。";
    panel.appendChild(empty);
    root.appendChild(panel);
    return;
  }

  const selection = document.createElement("dl");
  selection.className = "local-selection";
  appendField(selection, "条款", clauseId);
  appendField(selection, "框选文本", selectedText);
  panel.appendChild(selection);

  const button = document.createElement("button");
  button.type = "button";
  button.textContent = props.loading ? "审查中" : "局部审查";
  button.disabled = props.loading;
  button.addEventListener("click", () => {
    if (typeof props.onRunLocalReview === "function") {
      props.onRunLocalReview({ clauseId, selectedText });
    }
  });
  panel.appendChild(button);

  if (props.error) {
    const error = document.createElement("p");
    error.className = "error-message";
    error.textContent = props.error;
    panel.appendChild(error);
  }

  if (props.result) {
    panel.appendChild(renderResult(props.result));
  }

  root.appendChild(panel);
}

function renderResult(result) {
  const wrapper = document.createElement("div");
  wrapper.className = "local-review-result";

  const status = document.createElement("p");
  status.className = "local-review-status";
  status.textContent = result.message || "局部审查完成。";
  wrapper.appendChild(status);

  const badge = document.createElement("span");
  badge.className = "local-badge";
  badge.textContent = "局部结果";
  wrapper.appendChild(badge);

  if (Array.isArray(result.related_formal_risks) && result.related_formal_risks.length > 0) {
    const related = document.createElement("p");
    related.className = "risk-meta";
    related.textContent = `关联正式风险 ${result.related_formal_risks.length} 条`;
    wrapper.appendChild(related);
  }

  const findings = result.local_findings || [];
  if (findings.length === 0) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "当前框选文本没有局部风险结果。";
    wrapper.appendChild(empty);
    return wrapper;
  }

  const list = document.createElement("div");
  list.className = "risk-detail";
  findings.forEach((finding) => {
    const card = document.createElement("article");
    card.className = "risk-detail-card local-finding-card";

    const title = document.createElement("h3");
    title.textContent = `${finding.risk_type || "局部风险"} / ${finding.clause_id || ""}`;
    card.appendChild(title);

    const fields = document.createElement("dl");
    fields.className = "risk-detail-fields";
    appendField(fields, "证据文本", finding.evidence_text);
    appendField(fields, "风险原因", finding.risk_reason);
    appendField(fields, "命中规则", (finding.matched_rule_ids || []).join("、"));
    appendField(fields, "修改建议", finding.revision_suggestion);
    card.appendChild(fields);

    list.appendChild(card);
  });
  wrapper.appendChild(list);
  return wrapper;
}

function appendField(root, label, value) {
  if (!value) {
    return;
  }

  const term = document.createElement("dt");
  term.textContent = label;

  const detail = document.createElement("dd");
  detail.textContent = value;

  root.append(term, detail);
}
