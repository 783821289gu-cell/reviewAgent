const fs = require("node:fs");
const path = require("node:path");
const { test, expect } = require("playwright/test");


const PROJECT_ROOT = path.resolve(__dirname, "../../..");
const FIXTURES = path.join(PROJECT_ROOT, "frontend", "tests", "fixtures");
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

  const riskCards = page.locator(".risk-card");
  await expect(riskCards).toHaveCount(4);
  const firstRiskId = await riskId(riskCards.nth(0));
  const secondRiskId = await riskId(riskCards.nth(1));

  await riskCards.nth(0).getByRole("button", { name: "定位" }).click();
  await expect(page.locator(".clause-active")).toBeVisible();
  await expect(page.locator(".risk-highlight-active")).toBeVisible();

  await selectClauseText(page.locator(".clause-active .clause-text"));
  await expect(page.locator(".local-selection")).toContainText("框选文本");
  await page.getByRole("button", { name: "局部审查", exact: true }).click();
  await expect(page.locator(".local-review-result")).toContainText("局部审查完成");

  await page.getByLabel("加入报告").check();
  await submitFeedback(page, "采纳风险");
  await expect(page.locator(".memory-trace")).toContainText("本次人工反馈已写入 Memory");

  await riskCards.nth(1).click();
  await page.getByLabel("忽略原因").fill("E2E 合成样本中确认不纳入报告");
  await submitFeedback(page, "忽略风险");
  await expect(page.locator(".review-entry-status")).toHaveText("已忽略");

  await riskCards.nth(2).click();
  await page.getByLabel("风险等级").selectOption("低");
  await submitFeedback(page, "保存等级");
  await expect(page.locator(".review-entry-status")).toHaveText("已确认");
  await page.getByLabel("修改建议").fill("E2E 更新后的合成修改建议");
  await submitFeedback(page, "保存建议");
  await expect(page.locator("[data-risk-detail] .risk-detail-fields")).toContainText(
    "E2E 更新后的合成修改建议",
  );

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


async function upload(page, filePath, reviewPosition) {
  await page.getByLabel("合同文件").setInputFiles(filePath);
  await page.getByLabel(reviewPosition, { exact: true }).check();
  await page.getByRole("button", { name: "上传并解析" }).click();
}


async function submitFeedback(page, actionName) {
  const responsePromise = page.waitForResponse(
    (response) => response.url().includes("/feedback") && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: actionName }).click();
  const response = await responsePromise;
  expect(response.status()).toBe(200);
  await expect(page.locator(".feedback-message")).toContainText("人工反馈");
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
