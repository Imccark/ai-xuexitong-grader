const { test, expect } = require("playwright/test");

test("Agent result filter remains usable on a narrow viewport", async ({ page }) => {
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));

  await page.goto("/");
  await expect(page).toHaveTitle("作业审阅台");
  await page.getByRole("button", { name: "Agent 结果" }).first().click();

  const reviewFilter = page.locator("#reviewFilter");
  await expect(reviewFilter).toBeVisible();
  await reviewFilter.selectOption("review_required");
  await expect(page.locator("#reviewRiskPanel")).toBeVisible();

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(reviewFilter).toBeVisible();
  const bodyScrollWidth = await page.evaluate(() => document.body.scrollWidth);
  expect(bodyScrollWidth).toBeLessThanOrEqual(390);
  expect(pageErrors).toEqual([]);
});

test("Agent evidence is read-only and can switch image view and draw a bbox", async ({ page }) => {
  const tinyPng = Buffer.from(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
    "base64",
  );
  await page.route("**/images/**", (route) => route.fulfill({ status: 200, contentType: "image/png", body: tinyPng }));
  await page.route("**/agent-images/**", (route) => route.fulfill({ status: 200, contentType: "image/png", body: tinyPng }));
  await page.route("**/api/student/fixture-student", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({
      id: "fixture-student",
      images: ["/images/fixture-student/page_1.png"],
      imageVariants: [{
        page: 1,
        original: "/images/fixture-student/page_1.png",
        normalized: "/agent-images/fixture-student/1/normalized.png",
        enhanced: "/agent-images/fixture-student/1/enhanced.png",
      }],
      resultJson: {
        student_name_or_id: "fixture-student",
        overall: "部分错误",
        modules: { "错误细节": { items: ["1.1.1：负号错误"] } },
      },
      review: {
        status: "review_required",
        revision: 1,
        submitReady: false,
        readOnly: true,
        candidate: {
          overall: "partial",
          status: "review_required",
          unresolved_risk_count: 1,
          question_results: {
            "1.1.1": {
              verdict: "partial",
              confidence: 0.6,
              risk_level: "high",
              needs_verification: true,
              evidence_refs: [{ span_id: "s1", page: 1, bbox: [0, 0, 1, 1], artifact_ref: "page_1.png" }],
              transcription: [],
              rubric_decisions: [],
            },
          },
        },
      },
      exportImage: { status: "missing" },
    }),
  }));

  await page.goto("/");
  await page.evaluate(() => window.loadStudent("fixture-student"));
  await page.evaluate(() => window.switchView("review"));

  await expect(page.getByText("只读 · Agent 正式结果")).toBeVisible();
  await expect(page.getByRole("button", { name: /接受|拒绝|重跑|确认|保存编辑/ })).toHaveCount(0);
  await expect(page.locator("#saveBtn")).toHaveCount(0);
  await expect(page.locator('[data-view="gold"]')).toHaveCount(0);
  await expect(page.locator(".image-view-select")).toHaveCount(1);
  await page.locator(".image-view-select").selectOption("enhanced");
  await expect(page.locator(".image-card img")).toHaveAttribute("src", /agent-images\/fixture-student\/1\/enhanced\.png/);
  await page.getByRole("button", { name: /证据：第 1 页/ }).click();
  await expect(page.locator(".evidence-bbox.is-visible")).toHaveCount(1);
});
