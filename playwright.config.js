const { defineConfig, devices } = require("playwright/test");
const path = require("node:path");

const port = Number(process.env.REVIEW_AGENT_E2E_PORT || 8010);
const baseURL = process.env.PLAYWRIGHT_BASE_URL || `http://127.0.0.1:${port}`;
const runId = `run-${Date.now()}-${process.pid}`;
const runRoot = path.join("test-results", "e2e", runId);
const runtimeRoot = process.env.REVIEW_AGENT_RUNTIME_ROOT;
const pythonExecutable = process.env.REVIEW_AGENT_PYTHON
  || (runtimeRoot
    ? path.join(
        runtimeRoot,
        "venv",
        process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
      )
    : "python");

module.exports = defineConfig({
  testDir: "./frontend/tests/e2e",
  fullyParallel: false,
  workers: 1,
  timeout: 60_000,
  expect: {
    timeout: 15_000,
  },
  outputDir: "test-results/playwright",
  reporter: [
    ["line"],
    ["html", { outputFolder: "playwright-report", open: "never" }],
  ],
  use: {
    baseURL,
    acceptDownloads: true,
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  webServer: {
    command: `"${pythonExecutable}" -m uvicorn main:app --app-dir backend/app --host 127.0.0.1 --port ${port}`,
    url: `${baseURL}/health`,
    reuseExistingServer: false,
    timeout: 60_000,
    env: {
      ...process.env,
      REVIEW_AGENT_HOST: "127.0.0.1",
      REVIEW_AGENT_PORT: String(port),
      REVIEW_AGENT_ALLOWED_ORIGINS: baseURL,
      REVIEW_AGENT_LOAD_DOTENV: "0",
      REVIEW_AGENT_LLM_MODE: "local_structured",
      REVIEW_AGENT_EMBEDDING_MODE: "local_sparse",
      REVIEW_AGENT_MEMORY_DB_PATH: path.join(runRoot, "review-agent.sqlite3"),
      REVIEW_AGENT_UPLOAD_DIR: path.join(runRoot, "uploads"),
      REVIEW_AGENT_REPORT_DIR: path.join(runRoot, "reports"),
      REVIEW_AGENT_EVALUATION_OUTPUT_DIR: path.join(runRoot, "evaluation"),
    },
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
