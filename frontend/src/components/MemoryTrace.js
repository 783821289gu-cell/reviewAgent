export function MemoryTrace(root, props) {
  root.textContent = "";

  const risk = props.risk || {};
  const references = risk.memory_references || [];
  const feedback = risk.feedback || null;
  if (references.length === 0 && !feedback?.memory_id) {
    return;
  }

  const panel = document.createElement("section");
  panel.className = "memory-trace";

  const title = document.createElement("strong");
  title.textContent = "Memory";
  panel.appendChild(title);

  if (references.length > 0) {
    const list = document.createElement("ul");
    references.forEach((reference) => {
      const item = document.createElement("li");
      item.textContent = `已参考历史反馈 ${reference.memory_id || ""}：${reference.final_suggestion || reference.user_action || "无建议文本"}`;
      list.appendChild(item);
    });
    panel.appendChild(list);
  }

  if (feedback?.memory_id) {
    const written = document.createElement("p");
    written.textContent = `本次人工反馈已写入 Memory：${feedback.memory_id}`;
    panel.appendChild(written);
  }

  root.appendChild(panel);
}
