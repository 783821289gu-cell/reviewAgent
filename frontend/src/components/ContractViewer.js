const KEY_FIELD_LABELS = {
  obligation_subject: "义务主体",
  right_holder: "权利主体",
  confidentiality_period: "保密期限",
  permitted_disclosure_targets: "允许披露对象",
  use_purpose: "使用目的",
  liability_scope: "责任范围",
  breach_liability: "违约责任",
};

export function ContractViewer(root, props) {
  root.textContent = "";

  const clauses = props.clauses || [];
  const risks = (props.risks || []).filter((risk) => risk.clause_id && risk.evidence_text);
  if (props.status === "PARSE_FAILED") {
    const message = document.createElement("p");
    message.className = "empty-state";
    message.textContent = "合同解析失败。请确认上传文件为可读取的 DOCX 或 PDF。";
    root.appendChild(message);
    return;
  }

  if (clauses.length === 0) {
    const message = document.createElement("p");
    message.className = "empty-state";
    message.textContent = "尚未解析合同正文。上传 DOCX 或 PDF 后将在这里展示条款编号和正文。";
    root.appendChild(message);
    return;
  }

  const list = document.createElement("div");
  list.className = "contract-viewer";

  clauses.forEach((clause) => {
    const article = document.createElement("article");
    const clauseRisks = risks.filter((risk) => risk.clause_id === clause.clause_id);
    article.className = clauseCardClassName(clause.clause_id, clauseRisks, props.activeClauseId);
    article.id = clause.clause_id;
    article.addEventListener("mouseup", () => handleSelection(article, clause, props.onSelectionChange));

    const header = document.createElement("div");
    header.className = "clause-header";

    const title = document.createElement("h3");
    title.textContent = `${clause.clause_id} ${clause.title || "未命名条款"}`;

    const type = document.createElement("span");
    type.className = "clause-type";
    type.textContent = clause.clause_type || "其他";

    header.append(title, type);
    article.append(header, renderClauseText(clause.text || "", clauseRisks, props.activeRiskId));
    article.appendChild(renderKeyFields(clause.key_fields || {}));
    list.appendChild(article);
  });

  root.appendChild(list);
}

function clauseCardClassName(clauseId, clauseRisks, activeClauseId) {
  const classNames = ["clause-card"];
  if (clauseRisks.length > 0) {
    classNames.push("clause-has-risk");
  }
  if (activeClauseId && clauseId === activeClauseId) {
    classNames.push("clause-active");
  }
  return classNames.join(" ");
}

function renderClauseText(text, risks, activeRiskId) {
  const paragraph = document.createElement("p");
  paragraph.className = "clause-text";

  const ranges = risks
    .map((risk) => {
      const evidenceText = String(risk.evidence_text || "");
      const start = evidenceText ? text.indexOf(evidenceText) : -1;
      return {
        start,
        end: start + evidenceText.length,
        evidenceText,
        riskId: risk.risk_id,
      };
    })
    .filter((range) => range.start >= 0 && range.end > range.start)
    .sort((left, right) => left.start - right.start || right.end - left.end);

  if (ranges.length === 0) {
    paragraph.textContent = text;
    return paragraph;
  }

  let cursor = 0;
  ranges.forEach((range) => {
    if (range.start < cursor) {
      return;
    }
    if (range.start > cursor) {
      paragraph.appendChild(document.createTextNode(text.slice(cursor, range.start)));
    }
    const mark = document.createElement("mark");
    mark.className = range.riskId === activeRiskId ? "risk-highlight risk-highlight-active" : "risk-highlight";
    mark.textContent = text.slice(range.start, range.end);
    paragraph.appendChild(mark);
    cursor = range.end;
  });

  if (cursor < text.length) {
    paragraph.appendChild(document.createTextNode(text.slice(cursor)));
  }
  return paragraph;
}

function handleSelection(article, clause, onSelectionChange) {
  if (typeof onSelectionChange !== "function") {
    return;
  }
  const selection = window.getSelection();
  const selectedText = selection?.toString().trim() || "";
  if (!selectedText || !article.contains(selection.anchorNode) || !article.contains(selection.focusNode)) {
    return;
  }
  onSelectionChange({
    clause_id: clause.clause_id,
    selected_text: selectedText,
  });
}

function renderKeyFields(keyFields) {
  const wrapper = document.createElement("dl");
  wrapper.className = "key-fields";

  Object.entries(KEY_FIELD_LABELS).forEach(([fieldName, label]) => {
    const values = normalizeValues(keyFields[fieldName]);
    if (values.length === 0) {
      return;
    }

    const term = document.createElement("dt");
    term.textContent = label;

    const detail = document.createElement("dd");
    detail.textContent = values.join("、");

    wrapper.append(term, detail);
  });

  if (wrapper.childElementCount === 0) {
    const term = document.createElement("dt");
    term.textContent = "关键字段";

    const detail = document.createElement("dd");
    detail.textContent = "未抽取到明确字段";

    wrapper.append(term, detail);
  }

  return wrapper;
}

function normalizeValues(value) {
  if (Array.isArray(value)) {
    return value.filter(Boolean);
  }
  if (typeof value === "string" && value.trim()) {
    return [value.trim()];
  }
  return [];
}
