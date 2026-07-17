const ICON_NAMES = new Set([
  "bot",
  "chart-no-axes-column-increasing",
  "check",
  "chevron-down",
  "chevron-right",
  "database",
  "download",
  "file-text",
  "layout-dashboard",
  "locate-fixed",
  "panel-bottom",
  "pencil",
  "plus",
  "rotate-ccw",
  "shield-alert",
  "upload",
  "x",
]);

export function icon(name, options = {}) {
  if (!ICON_NAMES.has(name)) {
    throw new Error(`Unknown icon: ${name}`);
  }

  const className = options.className ? ` ${escapeAttribute(options.className)}` : "";
  const accessibility = options.label
    ? `role="img" aria-label="${escapeAttribute(options.label)}"`
    : 'aria-hidden="true"';

  return `<span class="icon${className}" style="--icon-image: url('/src/assets/icons/${name}.svg')" ${accessibility}></span>`;
}

function escapeAttribute(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll('"', "&quot;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}
