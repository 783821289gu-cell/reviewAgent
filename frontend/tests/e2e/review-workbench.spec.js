const fs = require("node:fs");
const path = require("node:path");
const { test, expect } = require("playwright/test");


const PROJECT_ROOT = path.resolve(__dirname, "../../..");
const FIXTURES = path.join(PROJECT_ROOT, "frontend", "tests", "fixtures");
const AGENT_STATES = JSON.parse(
  fs.readFileSync(path.join(FIXTURES, "agent-task-states.json"), "utf8"),
);
const TERMINAL_STATUS = /EVIDENCE_VERIFIED|HUMAN_REVIEW_PENDING/;


test("选择立场但未选择文件时仍禁止提交", async ({ page }) => {
  await page.goto("/");
  await page.locator('input[name="review-position"]').first().check();

  await expect(page.locator("#start-review")).toBeDisabled();
});


test("FastAPI 托管首页并阻止未选择文件或立场的提交", async ({ page }) => {
  const response = await page.goto("/");

  expect(response.status()).toBe(200);
  await expect(page.getByRole("heading", { name: "ContractReviewAgent" })).toBeVisible();
  await expect(page.getByText("后端已连接")).toBeVisible();
  await expect(page.getByRole("button", { name: "上传并解析" })).toBeDisabled();

  await page.getByLabel("合同文件").setInputFiles(path.join(FIXTURES, "nda_high_risk.docx"));
  await expect(page.getByRole("button", { name: "上传并解析" })).toBeDisabled();
  await page.getByLabel("甲方", { exact: true }).check();
  await expect(page.getByRole("button", { name: "上传并解析" })).toBeEnabled();
  await expect(page.getByRole("switch", { name: "使用 DeepSeek LLM" })).not.toBeChecked();
  await page.getByRole("switch", { name: "使用 DeepSeek LLM" }).check();
  await expect(page.locator("[data-llm-mode-label]")).toContainText("DeepSeek LLM");
});


test("DeepSeek 开关向新任务提交任务级 LLM 模式", async ({ page }) => {
  await page.goto("/");
  await page.evaluate(async () => {
    document.body.innerHTML = '<main id="component-test-root"></main>';
    const { UploadPanel } = await import("/src/components/UploadPanel.js");
    window.__uploadPayload = null;
    UploadPanel(document.querySelector("#component-test-root"), {
      disabled: false,
      loading: false,
      error: "",
      canCancel: false,
      onSubmit: (payload) => { window.__uploadPayload = payload; },
    });
  });
  await page.getByLabel("合同文件").setInputFiles(path.join(FIXTURES, "nda_high_risk.docx"));
  await page.getByLabel("甲方", { exact: true }).check();
  await page.getByRole("switch", { name: "使用 DeepSeek LLM" }).check();
  await page.getByRole("button", { name: "上传并解析" }).click();

  await expect.poll(() => page.evaluate(() => window.__uploadPayload?.llmMode)).toBe(
    "openai_compatible",
  );
});


test("DOCX 主流程覆盖 SSE、风险定位、局部审查、反馈、Memory、报告和评测", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByText("后端已连接")).toBeVisible();

  const eventResponse = page.waitForResponse(
    (response) => response.url().includes("/events") && response.status() === 200,
  );
  await upload(page, path.join(FIXTURES, "nda_high_risk.docx"), "甲方");
  const sse = await eventResponse;
  expect(sse.headers()["content-type"]).toContain("text/event-stream");
  const sseBody = sse.text();
  await expect(page.locator("[data-review-status]")).toHaveText(TERMINAL_STATUS, {
    timeout: 45_000,
  });
  expect(await sseBody).toMatch(
    /"status":\s*"(?:EVIDENCE_VERIFIED|HUMAN_REVIEW_PENDING)"/,
  );
  await expect(page.locator(".progress-list")).toContainText(/证据已验证|等待人工复核/);
  await expect(page.locator("[data-human-review-attention]")).toContainText("人工复核待处理");

  const riskCards = page.locator(".risk-card");
  await expect(riskCards).toHaveCount(4);
  await expect(page.locator("[data-pending-risk-count]")).toHaveText("4");
  await expect(page.locator("[data-task-provider-brief]")).toContainText("未使用 DeepSeek");
  const firstRiskId = await riskId(riskCards.nth(0));
  const secondRiskId = await riskId(riskCards.nth(1));

  await page.emulateMedia({ reducedMotion: "reduce" });
  const contextScroll = page.locator('[data-scroll-region="context"]');
  const documentScroll = page.locator('[data-scroll-region="document"]');
  const inspectorScroll = page.locator('[data-scroll-region="inspector"]');
  await contextScroll.evaluate((element) => { element.scrollTop = 80; });
  await inspectorScroll.evaluate((element) => { element.scrollTop = 40; });
  const sideScrollBeforeLocate = await sideScrollPositions(contextScroll, inspectorScroll);
  await riskCards.nth(0).getByRole("button", { name: "定位" }).dispatchEvent("click");
  await expect(page.locator(".clause-active")).toBeVisible();
  await expect(page.locator(".risk-highlight-active")).toBeVisible();
  await expect.poll(() => sideScrollPositions(contextScroll, inspectorScroll)).toEqual(
    sideScrollBeforeLocate,
  );
  const firstLocateTop = await documentScroll.evaluate((element) => element.scrollTop);
  await riskCards.nth(0).getByRole("button", { name: "定位" }).dispatchEvent("click");
  await expect.poll(() => documentScroll.evaluate((element) => element.scrollTop)).toBe(
    firstLocateTop,
  );

  await selectClauseText(page.locator(".clause-active .clause-text"));
  await expect(page.locator(".local-selection")).toContainText("框选文本");
  await page.getByRole("button", { name: "局部审查", exact: true }).click();
  await expect(page.locator(".local-review-result")).toContainText("局部审查完成");

  await page.getByLabel("加入报告").check();
  await submitFeedback(page, "采纳风险");
  await expect(page.locator("[data-pending-risk-count]")).toHaveText("3");
  await expect(riskCards.nth(0).locator(".risk-decision")).toHaveText("已采纳");
  await expect(page.getByRole("button", { name: "已采纳" })).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator(".memory-trace")).toContainText("本次人工反馈已写入 Memory");

  await riskCards.nth(1).click();
  await page.getByLabel("忽略原因").fill("E2E 合成样本中确认不纳入报告");
  await submitFeedback(page, "忽略风险");
  await expect(page.locator("[data-pending-risk-count]")).toHaveText("2");
  await expect(riskCards.nth(1).locator(".risk-decision")).toHaveText("已忽略");
  await expect(page.locator(".review-entry-status")).toHaveText("已忽略");

  await riskCards.nth(2).click();
  await page.getByLabel("风险等级").selectOption("低");
  await submitFeedback(page, "保存等级");
  await expect(page.locator("[data-pending-risk-count]")).toHaveText("1");
  await expect(page.locator(".review-entry-status")).toHaveText("等级已修改");
  await page.getByLabel("修改建议").fill("E2E 更新后的合成修改建议");
  await submitFeedback(page, "保存建议");
  await expect(page.locator("[data-pending-risk-count]")).toHaveText("1");
  await expect(page.locator(".review-entry-status")).toHaveText("建议已修改");
  await expect(page.locator("[data-risk-detail] .risk-detail-fields")).toContainText(
    "E2E 更新后的合成修改建议",
  );

  const persistedTaskId = await page.evaluate(() => (
    window.localStorage.getItem("review-agent.active-task-id")
  ));
  await page.reload();
  await expect(page.getByText("后端已连接")).toBeVisible();
  await expect(page.locator("[data-review-status]")).toHaveText("MEMORY_UPDATED");
  await expect(page.locator(".risk-card")).toHaveCount(4);
  await expect(page.locator("[data-pending-risk-count]")).toHaveText("1");
  await expect(page.locator(".risk-card").nth(0).locator(".risk-decision")).toHaveText("已采纳");
  await expect(page.locator(".risk-card").nth(1).locator(".risk-decision")).toHaveText("已忽略");
  await expect(page.locator(".risk-card").nth(2).locator(".risk-decision")).toHaveText("建议已修改");
  await expect.poll(() => page.evaluate(() => (
    window.localStorage.getItem("review-agent.active-task-id")
  ))).toBe(persistedTaskId);

  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "导出报告" }).click();
  const download = await downloadPromise;
  const reportPath = await download.path();
  const reportText = fs.readFileSync(reportPath, "utf8");
  expect(reportText).toContain(firstRiskId);
  expect(reportText).not.toContain(secondRiskId);
  await expect(page.locator("[data-report-status]")).toContainText("共 1 项风险");

  await page.getByRole("button", { name: "评测工作区" }).click();
  await page.getByRole("button", { name: "运行流程评测" }).click();
  await expect(page.locator(".evaluation-summary")).toContainText("10");
  await expect(page.locator(".evaluation-claim")).toContainText("不代表生产级准确率");

  await page.locator("details.execution-log summary").click();
  await expect(page.locator("[data-execution-log]")).toContainText("任务 Trace");
  await expect(page.locator("[data-execution-log]")).toContainText("Step step_");
  await expect(page.locator("[data-execution-log]")).not.toContainText("Traceback");
});


test("非 NDA 在合同类型识别后停止且不生成正式风险", async ({ page }) => {
  await page.goto("/");
  await upload(page, path.join(FIXTURES, "service_agreement.docx"), "甲方");

  await expect(page.locator("[data-review-status]")).toHaveText("UNSUPPORTED_CONTRACT_TYPE");
  await expect(page.locator("[data-contract-classification]")).toContainText("不支持该合同类型");
  await expect(page.locator(".risk-card")).toHaveCount(0);
});


test("超限文件显示具体限制提示", async ({ page }) => {
  await page.goto("/");
  const oversized = Buffer.concat([Buffer.from("PK\x03\x04"), Buffer.alloc(10 * 1024 * 1024)]);
  await page.getByLabel("合同文件").setInputFiles({
    name: "oversized.docx",
    mimeType: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    buffer: oversized,
  });
  await page.getByLabel("甲方", { exact: true }).check();
  await page.getByRole("button", { name: "上传并解析" }).click();

  await expect(page.locator("#upload-error")).toContainText("超过大小限制");
});


test("损坏 DOCX 进入解析失败而不显示成功", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("合同文件").setInputFiles({
    name: "broken.docx",
    mimeType: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    buffer: Buffer.from("PK\x03\x04broken-docx"),
  });
  await page.getByLabel("甲方", { exact: true }).check();
  await page.getByRole("button", { name: "上传并解析" }).click();

  await expect(page.locator("[data-review-status]")).toHaveText("PARSE_FAILED");
  await expect(page.locator(".progress-failed")).toContainText("解析失败");
  await expect(page.locator("[data-contract-viewer]")).toContainText("合同解析失败");
  await expect(page.locator("[data-recover-task]")).toBeVisible();

  page.once("dialog", (dialog) => dialog.accept("E2E 人工恢复损坏文档"));
  const recoveryResponse = page.waitForResponse(
    (response) => response.url().endsWith("/recover") && response.request().method() === "POST",
  );
  await page.locator("[data-recover-task]").click();
  expect((await recoveryResponse).status()).toBe(202);
  await expect(page.locator(".progress-list")).toContainText("人工恢复");
  await expect(page.locator("[data-review-status]")).toHaveText("PARSE_FAILED");

  await page.locator("details.execution-log summary").click();
  await expect(page.locator("[data-execution-log]")).toContainText("最近人工恢复");
  await expect(page.locator("[data-execution-log]")).toContainText("web_manual_retry");
  await expect(page.locator("[data-execution-log]")).toContainText("parse 2/2");
});


test("扫描 PDF 显示需要 OCR 的支持边界", async ({ page }) => {
  await page.goto("/");
  await upload(
    page,
    path.join(PROJECT_ROOT, "backend", "tests", "fixtures", "pdf", "scanned_image.pdf"),
    "甲方",
  );

  await expect(page.locator("[data-review-status]")).toHaveText("PARSE_FAILED");
  await expect(page.locator(".progress-failed")).toContainText("OCR");
});


test("健康检查失败时页面显示服务错误并禁用上传", async ({ page }) => {
  await page.route("**/health", (route) => route.abort("failed"));
  await page.goto("/");

  await expect(page.getByText("后端未连接")).toBeVisible();
  await expect(page.getByLabel("合同文件")).toBeDisabled();
  await expect(page.locator("#upload-message")).toContainText("请先启动后端服务");
});


test("执行记录区分 DeepSeek、本地模式和受控 Agent 决策", async ({ page }) => {
  await page.goto("/");

  await renderExecutionLog(page, AGENT_STATES.local_mode);
  await expect(capability(page, "provider")).toContainText("本地模式 / 无外部 LLM");
  await renderWorkbench(page, AGENT_STATES.local_mode);
  await expect(page.locator("[data-task-provider-brief]")).toContainText("未使用 DeepSeek");

  await renderExecutionLog(page, AGENT_STATES.deepseek_repair);
  await expect(capability(page, "provider")).toContainText("DeepSeek");
  await expect(capability(page, "provider")).toContainText("真实调用 1/1 成功");
  await expect(capability(page, "planner")).toContainText("RETRIEVE_AGAIN");
  await expect(capability(page, "retrieval-repair")).toContainText("已成功执行 1 次");
  await expect(capability(page, "critic")).toContainText("REQUEST_HUMAN_REVIEW");
  await expect(capability(page, "evidence")).toContainText("失败 1");
  await expect(capability(page, "memory")).toContainText("读取调用 1 / 写入调用 1");
  await expect(page.locator("#component-test-root")).toContainText("调整 additional_keywords,top_k");
  await renderWorkbench(page, AGENT_STATES.deepseek_repair);
  await expect(page.locator("[data-task-provider]")).toContainText("DeepSeek · 真实调用 1/1 成功");
  await expect(page.locator("[data-task-provider-brief]")).toHaveText("DeepSeek 已调用");

  const deepseekWaiting = { ...AGENT_STATES.local_mode, llm_mode: "openai_compatible", logs: [] };
  await renderWorkbench(page, deepseekWaiting);
  await expect(page.locator("[data-task-provider-brief]")).toHaveText(
    "DeepSeek 已启用 · 等待调用",
  );

  await renderWorkbench(page, AGENT_STATES.evidence_recovered);
  await expect(page.locator("[data-human-review-attention]")).toHaveCount(0);

  await renderWorkbench(page, AGENT_STATES.critic_conflict);
  await expect(page.locator("[data-human-review-attention]")).toContainText(
    "Critic 与风险分析存在冲突",
  );
});


test("浏览器验收配置错误、非法 Planner、Injection、取消、超时和恢复", async ({ page }) => {
  await page.goto("/");

  await renderExecutionLog(page, AGENT_STATES.deepseek_config_error);
  await expect(capability(page, "provider")).toContainText("调用失败：configuration_error");
  await expect(page.locator("#component-test-root")).not.toContainText("本地模式 / 无外部 LLM");

  await renderExecutionLog(page, AGENT_STATES.illegal_planner);
  await expect(capability(page, "planner")).toContainText("非法动作已拒绝");
  await expect(capability(page, "retrieval-repair")).toContainText("未触发");

  await renderExecutionLog(page, AGENT_STATES.injection_blocked);
  await expect(capability(page, "prompt-injection")).toContainText("已阻断并转人工复核");

  await renderExecutionLog(page, AGENT_STATES.cancelled);
  await expect(capability(page, "execution")).toContainText("任务已取消");
  await expect(page.locator("#component-test-root")).toContainText("Operator stopped the review.");

  await renderExecutionLog(page, AGENT_STATES.cancel_requested);
  await expect(capability(page, "execution")).toContainText("取消请求已提交");
  await expect(capability(page, "execution")).not.toContainText("任务已取消");

  await renderExecutionLog(page, AGENT_STATES.timeout_recovered);
  await expect(capability(page, "execution")).toContainText("发生超时 / 已人工恢复 1 次");
  await expect(page.locator("#component-test-root")).toContainText("node_timeout / parse_document / 5");
  await expect(page.locator("#component-test-root")).toContainText("web_manual_retry");

  await renderExecutionLog(page, AGENT_STATES.failed_side_effects);
  await expect(capability(page, "retrieval-repair")).toContainText("成功 0 / 失败 1");
  await expect(capability(page, "memory")).toContainText("写入调用 0 / 失败 1");
});


test("效果面板如实展示真实 Provider 运行信息、未达标项和零样本", async ({ page }) => {
  await page.goto("/");
  await renderEvaluationPanel(page, AGENT_STATES.effect_result);

  await expect(page.locator("[data-effect-provider]")).toContainText(
    "DeepSeek 真实调用 1/2 成功",
  );
  await expect(page.locator(".effect-summary")).toContainText("已完成，存在未达标项");
  await expect(page.locator(".evaluation-runtime")).toContainText("deepseek-stability-fixture");
  await expect(page.locator(".evaluation-runtime")).toContainText("1 个脱敏摘要");
  await expect(page.locator(".evaluation-runtime")).toContainText("估算成本");
  await expect(page.locator(".evaluation-runtime")).toContainText("未配置");
  await expect(page.locator(".effect-metric-row").filter({ hasText: "检索修复成功率" })).toContainText(
    "无样本",
  );

  const failedProviderResult = JSON.parse(JSON.stringify(AGENT_STATES.effect_result));
  failedProviderResult.runtime.provider_success_count = 0;
  await renderEvaluationPanel(page, failedProviderResult);
  await expect(page.locator("[data-effect-provider]")).toContainText("DeepSeek 调用失败 0/2 成功");
  await expect(page.locator("[data-effect-provider]")).not.toContainText("DeepSeek 真实调用");
});


async function upload(page, filePath, reviewPosition) {
  await page.getByLabel("合同文件").setInputFiles(filePath);
  await page.getByLabel(reviewPosition, { exact: true }).check();
  await page.getByRole("button", { name: "上传并解析" }).click();
}


async function renderExecutionLog(page, task) {
  await page.evaluate(async (fixtureTask) => {
    document.body.innerHTML = '<main id="component-test-root"></main>';
    const { ExecutionLog } = await import("/src/components/ExecutionLog.js");
    ExecutionLog(document.querySelector("#component-test-root"), { task: fixtureTask });
  }, task);
}


async function renderWorkbench(page, task) {
  await page.evaluate(async (fixtureTask) => {
    document.body.innerHTML = '<main id="component-test-root"></main>';
    const { WorkbenchLayout } = await import("/src/components/WorkbenchLayout.js");
    WorkbenchLayout(document.querySelector("#component-test-root"), {
      task: fixtureTask,
      workspaceView: "review",
      mobileView: "risk",
      riskFilter: "all",
      localReview: {},
      feedback: {},
      report: {},
      evaluation: {},
      taskControl: {},
    });
  }, task);
}


async function renderEvaluationPanel(page, effectResult) {
  await page.evaluate(async (fixtureResult) => {
    document.body.innerHTML = '<main id="component-test-root"></main>';
    const { EvaluationPanel } = await import("/src/components/EvaluationPanel.js");
    EvaluationPanel(document.querySelector("#component-test-root"), {
      evaluation: { activeView: "effect", effectResult: fixtureResult },
    });
  }, effectResult);
}


function capability(page, name) {
  return page.locator(`[data-capability="${name}"]`);
}


async function sideScrollPositions(contextScroll, inspectorScroll) {
  return {
    context: await contextScroll.evaluate((element) => element.scrollTop),
    inspector: await inspectorScroll.evaluate((element) => element.scrollTop),
  };
}


async function submitFeedback(page, actionName) {
  const responsePromise = page.waitForResponse(
    (response) => response.url().includes("/feedback") && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: actionName }).click();
  const response = await responsePromise;
  expect(response.status()).toBe(200);
  await expect(page.locator("[data-feedback-notice]")).toContainText("人工反馈");
}


async function riskId(card) {
  const title = await card.locator("strong").first().innerText();
  return title.split(/\s+/, 1)[0];
}


async function selectClauseText(paragraph) {
  await paragraph.evaluate((element) => {
    const range = document.createRange();
    range.selectNodeContents(element);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    element.closest(".clause-card").dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
  });
}
