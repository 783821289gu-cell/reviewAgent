export function RuleDetail(root, props) {
  root.textContent = "";

  const matches = props.matchedRules || [];
  if (matches.length === 0) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "尚未完成 Playbook 规则检索。";
    root.appendChild(empty);
    return;
  }

  const matchedGroups = matches.filter((item) => Array.isArray(item.matched_rules) && item.matched_rules.length > 0);
  const unmatchedCount = matches.length - matchedGroups.length;
  if (matchedGroups.length === 0) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "未命中 Playbook 规则。未命中规则不会生成正式风险。";
    root.appendChild(empty);
    return;
  }

  const wrapper = document.createElement("div");
  wrapper.className = "rule-detail";

  if (unmatchedCount > 0) {
    const notice = document.createElement("p");
    notice.className = "rule-notice";
    notice.textContent = `${unmatchedCount} 个条款未命中 Playbook 规则，不生成正式风险。`;
    wrapper.appendChild(notice);
  }

  matchedGroups.forEach((group) => {
    const section = document.createElement("section");
    section.className = "rule-group";

    const title = document.createElement("h3");
    title.textContent = `${group.clause_id || "未知条款"} / ${group.clause_type || "其他"}`;
    section.appendChild(title);

    group.matched_rules.forEach((rule) => {
      section.appendChild(renderRuleCard(rule, props.activeRuleIds || []));
    });

    wrapper.appendChild(section);
  });

  root.appendChild(wrapper);
}

function renderRuleCard(rule, activeRuleIds) {
  const card = document.createElement("article");
  card.className = activeRuleIds.includes(rule.rule_id) ? "rule-card rule-card-active" : "rule-card";

  const header = document.createElement("div");
  header.className = "rule-card-header";

  const title = document.createElement("strong");
  title.textContent = `${rule.rule_id || "未命名规则"} ${rule.risk_type || ""}`.trim();

  const severity = document.createElement("span");
  severity.className = "severity-badge";
  severity.textContent = rule.severity_default || "未分级";

  header.append(title, severity);
  card.appendChild(header);

  const fields = document.createElement("dl");
  fields.className = "rule-fields";

  appendField(fields, "检查点", rule.check_point);
  appendField(fields, "触发标准", rule.risk_criteria);
  appendField(fields, "审查立场", rule.review_position);
  appendField(fields, "立场风险重点", rule.risk_focus);
  appendField(fields, "立场修改模板", rule.revision_template);
  appendField(fields, "匹配字段", formatMatchedFields(rule.matched_key_fields));

  card.appendChild(fields);
  return card;
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

function formatMatchedFields(value) {
  if (!Array.isArray(value) || value.length === 0) {
    return "";
  }
  return value.join("、");
}
