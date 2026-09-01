const { defineConfig } = require("playwright/test");
const python = process.env.XUEXITONG_PYTHON || "python";

module.exports = defineConfig({
  testDir: "./tests/e2e",
  timeout: 30_000,
  reporter: [["list"], ["json", { outputFile: "temp/playwright-report.json" }]],
  use: {
    baseURL: "http://127.0.0.1:8765",
    headless: true,
    trace: "retain-on-failure",
  },
  webServer: {
    command: `"${python}" -u -m app.review_app --assignment "tests/fixtures/assignment.json" --host 127.0.0.1 --port 8765`,
    url: "http://127.0.0.1:8765",
    reuseExistingServer: true,
    timeout: 120_000,
  },
});
