export function ContextTrace(root, props) {
  root.textContent = "";

  const contexts = props.contexts || [];
  if (contexts.length === 0) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "尚未构建风险分析上下文。";
    root.appendChild(empty);
    return;
  }

  const wrapper = document.createElement("div");
  wrapper.className = "context-trace";

  contexts.forEach((context) => {
    const item = document.createElement("article");
    item.className = "context-card";

    const title = document.createElement("h3");
    title.textContent = `${context.context_id || "未命名上下文"} / ${context.matched_rule?.risk_type || "未命名风险类型"}`;
    item.appendChild(title);

    item.appendChild(renderMeta(context));
    item.appendChild(renderRelatedClauses(context.related_clauses || []));
    item.appendChild(renderMemory(context.related_memory || []));
    item.appendChild(renderConstraints(context));

    wrapper.appendChild(item);
  });

  root.appendChild(wrapper);
}

function renderMeta(context) {
  const meta = document.createElement("dl");
  meta.className = "context-meta";

  appendField(meta, "当前条款", context.current_clause?.clause_id || "");
  appendField(meta, "条款类型", context.clause_type || "");
  appendField(meta, "审查立场", context.review_position || "");
  appendField(meta, "正式风险", context.formal_risk_generated ? "已生成" : "未生成");

  return meta;
}

function renderRelatedClauses(clauses) {
  const section = document.createElement("section");
  section.className = "context-section";

  const title = document.createElement("h4");
  title.textContent = "相关条款";
  section.appendChild(title);

  if (clauses.length === 0) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "未召回相关条款。";
    section.appendChild(empty);
    return section;
  }

  const list = document.createElement("ol");
  list.className = "related-clause-list";
  clauses.forEach((clause) => {
    const item = document.createElement("li");
    const label = document.createElement("strong");
    label.textContent = `${clause.clause_id || "未知条款"} ${clause.clause_type || ""}`.trim();

    const score = document.createElement("span");
    const model = clause.embedding_model || "未知向量模型";
    score.textContent = `${model}；cosine ${clause.vector_similarity ?? "-"}；rerank ${clause.rerank_score ?? "-"}`;
    if (clause.rerank_factors) {
      score.title = Object.entries(clause.rerank_factors)
        .map(([name, value]) => `${name}=${value}`)
        .join("；");
    }

    item.append(label, score);
    list.appendChild(item);
  });
  section.appendChild(list);
  return section;
}

function renderMemory(memories) {
  const section = document.createElement("section");
  section.className = "context-section";

  const title = document.createElement("h4");
  title.textContent = "相关 Memory";
  section.appendChild(title);

  const text = document.createElement("p");
  text.className = "empty-state";
  text.textContent = memories.length === 0 ? "未召回 Memory。" : `已召回 ${memories.length} 条 Memory。`;
  section.appendChild(text);
  return section;
}

function renderConstraints(context) {
  const section = document.createElement("section");
  section.className = "context-section";

  const title = document.createElement("h4");
  title.textContent = "约束";
  section.appendChild(title);

  const list = document.createElement("ul");
  list.className = "constraint-list";
  appendConstraint(list, context.output_constraints?.no_formal_risk_without_playbook_rule, "无 Playbook 命中不得输出正式风险");
  appendConstraint(list, context.evidence_constraints?.must_bind_to_original_clause, "风险证据必须绑定合同原文");
  appendConstraint(list, context.evidence_constraints?.memory_cannot_override_playbook_or_original_text, "Memory 不能覆盖 Playbook 或原文证据");

  section.appendChild(list);
  return section;
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

function appendConstraint(root, enabled, text) {
  if (!enabled) {
    return;
  }
  const item = document.createElement("li");
  item.textContent = text;
  root.appendChild(item);
}
