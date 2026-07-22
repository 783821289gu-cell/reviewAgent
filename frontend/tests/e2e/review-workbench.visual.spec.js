const path = require("node:path");
const { test, expect } = require("playwright/test");

const PROJECT_ROOT = path.resolve(__dirname, "../../..");
const FIXTURE = path.join(PROJECT_ROOT, "frontend", "tests", "fixtures", "nda_high_risk.docx");
const TERMINAL_STATUS = /EVIDENCE_VERIFIED|HUMAN_REVIEW_PENDING/;

test("工作台在桌面和移动端视口保持可用且不产生横向溢出", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.goto("/");
  await uploadContract(page);
  await expect(page.locator("[data-review-status]")).toHaveText(TERMINAL_STATUS, {
    timeout: 45_000,
  });
  await expect(page.locator(".risk-card")).toHaveCount(4);
  await expect(page.locator("[data-human-review-attention]")).toBeVisible();

  const iconResponse = await page.request.get("/src/assets/icons/shield-alert.svg");
  expect(iconResponse.status()).toBe(200);
  await expectDesktopGeometry(page);
  await expectNoHorizontalOverflow(page);
  await page.locator('[data-risk-filter="高"]').click();
  await expect(page.locator(".risk-card")).toHaveCount(2);
  await expect(page.locator("[data-risk-detail] .risk-severity")).toContainText("高风险");
  await page.locator('[data-risk-filter="all"]').click();
  await expect(page.locator(".risk-card")).toHaveCount(4);
  await page.screenshot({
    path: testInfo.outputPath("workbench-1440x960.png"),
    fullPage: false,
  });

  await page.setViewportSize({ width: 1280, height: 800 });
  await expectDesktopGeometry(page);
  await expectNoHorizontalOverflow(page);
  await page.screenshot({
    path: testInfo.outputPath("workbench-1280x800.png"),
    fullPage: false,
  });

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole("button", { name: "待处理", exact: true })).toBeVisible();
  await expect(page.locator(".inspector-pane")).toBeVisible();
  await expect(page.locator(".document-pane")).toBeHidden();
  await expectNoHorizontalOverflow(page);
  await page.screenshot({
    path: testInfo.outputPath("workbench-390x844.png"),
    fullPage: false,
  });

  await page.getByRole("button", { name: "合同", exact: true }).click();
  await expect(page.locator(".document-pane")).toBeVisible();
  await expect(page.locator(".inspector-pane")).toBeHidden();
  await page.getByRole("button", { name: "执行记录", exact: true }).click();
  await expect(page.locator("details.execution-log")).toHaveAttribute("open", "");
  await expect(page.locator("[data-execution-log]")).toContainText("任务 Trace");
  await expect(page.locator(".agent-capability-list")).toBeVisible();
  await expectNoHorizontalOverflow(page);
});

test("上传入口和工作台控件支持键盘焦点与减少动态效果", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");

  await expect(page.getByRole("heading", { name: "新建合同审查" })).toBeVisible();
  await expectNoHorizontalOverflow(page);
  await page.getByLabel("合同文件").focus();
  await expect(page.getByLabel("合同文件")).toBeFocused();
  await page.getByLabel("甲方", { exact: true }).focus();
  await page.keyboard.press("Space");
  await expect(page.getByLabel("甲方", { exact: true })).toBeChecked();

  const transitionDuration = await page.locator(".button-primary").first().evaluate((element) => (
    getComputedStyle(element).transitionDuration
  ));
  expect(parseFloat(transitionDuration)).toBeLessThanOrEqual(0.00001);

  const unnamedIconButtons = await page.locator("button:has(.icon)").evaluateAll((buttons) => (
    buttons.filter((button) => !button.getAttribute("aria-label") && !button.textContent.trim()).length
  ));
  expect(unnamedIconButtons).toBe(0);
});

async function uploadContract(page) {
  await page.getByLabel("合同文件").setInputFiles(FIXTURE);
  await page.getByLabel("甲方", { exact: true }).check();
  await page.getByRole("button", { name: "上传并解析" }).click();
}

async function expectDesktopGeometry(page) {
  const geometry = await page.evaluate(() => {
    const box = (selector) => {
      const rectangle = document.querySelector(selector).getBoundingClientRect();
      return { left: rectangle.left, right: rectangle.right, top: rectangle.top, bottom: rectangle.bottom };
    };
    return {
      context: box(".context-pane"),
      document: box(".document-pane"),
      inspector: box(".inspector-pane"),
      drawer: box(".execution-log"),
    };
  });

  expect(geometry.context.right).toBeLessThanOrEqual(geometry.document.left + 1);
  expect(geometry.document.right).toBeLessThanOrEqual(geometry.inspector.left + 1);
  expect(geometry.drawer.top).toBeGreaterThanOrEqual(geometry.document.bottom - 1);
}

async function expectNoHorizontalOverflow(page) {
  const overflow = await page.evaluate(() => ({
    viewport: document.documentElement.clientWidth,
    content: document.documentElement.scrollWidth,
  }));
  expect(overflow.content).toBeLessThanOrEqual(overflow.viewport);
}
