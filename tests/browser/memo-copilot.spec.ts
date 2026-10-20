import { expect, test, type Page } from "@playwright/test";

const original =
  "# Browser test memo\n\n" +
  "- Built an accessible tool used by approximately five colleagues.\n".repeat(2) +
  "- Looking for technical delivery roles close to users.\n" +
  "- No people-management authority established.\n\n" +
  "## Application notes\n\nEmployer-specific posting analysis to remove.\n";

async function openMemo(page: Page) {
  await page.goto("/resumes");
  await page.getByText("Career memo — detailed work-history evidence", { exact: true }).click();
  await expect(page.getByLabel("Career memo Markdown")).toHaveValue(original);
  const panel = page.locator(".career-memo-panel");
  await expect(panel.getByRole("button", { name: /AI Copilot$/ })).toHaveText("AI Copilot");
  return panel;
}

test.beforeEach(async ({ request }) => {
  const current = await (await request.get("/api/career-memo")).json();
  const saved = await request.put("/api/career-memo", {
    data: { markdown: original, source: "browser test seed", base_revision_id: current.id },
  });
  expect(saved.ok()).toBeTruthy();
});

test("compact, refine, apply, save, reload, and recover an older revision", async ({
  page,
  request,
}, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const baseline = await (await request.get("/api/career-memo")).json();
  const panel = await openMemo(page);
  const editor = page.getByLabel("Career memo Markdown");
  const unsaved = original + "\n- Preference: Interested in platform tooling.\n";
  await editor.fill(unsaved);
  await panel.getByRole("button", { name: /AI Copilot$/ }).click();
  const chatRequest = page.waitForRequest((r) => r.url().endsWith("/api/career-memo/chat"));
  await page.getByRole("button", { name: "Compact memo", exact: true }).click();
  expect((await chatRequest).postDataJSON().current_markdown).toBe(unsaved);
  const proposal = page.getByLabel("Proposed career memo");
  await expect(proposal).toBeVisible();
  const proposed = await proposal.inputValue();
  expect(proposed).toContain("Preference: Interested in platform tooling.");
  await expect(editor).toHaveValue(unsaved);
  expect(await (await request.get("/api/career-memo")).json()).toEqual(baseline);

  const dialog = page.getByRole("dialog", { name: "Career memo copilot" });
  const bounds = await dialog.locator(".profile-copilot-modal").boundingBox();
  const viewport = page.viewportSize()!;
  expect(bounds).not.toBeNull();
  expect(bounds!.x).toBeGreaterThanOrEqual(0);
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(viewport.width);
  expect(bounds!.y).toBeGreaterThanOrEqual(0);
  expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(viewport.height);
  await expect(page.getByLabel("Message memo copilot")).toBeInViewport();
  await page.getByRole("button", { name: "Apply to memo editor" }).scrollIntoViewIfNeeded();
  await expect(page.getByRole("button", { name: "Apply to memo editor" })).toBeInViewport();
  await page.screenshot({ path: testInfo.outputPath("memo-proposal.png") });

  await page
    .getByLabel("Message memo copilot")
    .fill("Keep the factual limits and career direction.");
  const refinement = page.waitForRequest((r) => r.url().endsWith("/api/career-memo/chat"));
  await page.getByRole("button", { name: "Send message", exact: true }).click();
  expect((await refinement).postDataJSON().current_markdown).toBe(proposed);
  await expect(page.getByRole("button", { name: "Apply to memo editor" })).toBeEnabled();
  await page.getByRole("button", { name: "Apply to memo editor" }).click();
  await expect(dialog).toBeHidden();
  await expect(editor).toHaveValue(proposed);
  expect(await (await request.get("/api/career-memo")).json()).toEqual(baseline);
  await page.getByRole("button", { name: "Save memo revision", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("Saved revision #");
  const saved = await (await request.get("/api/career-memo")).json();
  expect(saved.markdown).toBe(proposed);
  expect(saved.source).toBe("AI memo copilot");
  expect(await (await request.get("/api/career-memo/download")).text()).toBe(proposed);
  await page.reload();
  if (!(await panel.getAttribute("open").then((value) => value !== null))) {
    await page.getByText("Career memo — detailed work-history evidence", { exact: true }).click();
  }
  await expect(editor).toBeVisible();
  await expect(editor).toHaveValue(proposed);
  await page.getByText("Revision history", { exact: true }).click();
  await panel
    .locator(".workspace-actions")
    .filter({ hasText: `#${baseline.id} ·` })
    .getByRole("button", { name: "Load into editor" })
    .click();
  await expect(editor).toHaveValue(original);
  expect((await (await request.get("/api/career-memo")).json()).markdown).toBe(proposed);
  expect(errors).toEqual([]);
});

test("discard and provider failure preserve evidence; stale saves preserve the newer revision", async ({
  page,
  request,
}) => {
  const baseline = await (await request.get("/api/career-memo")).json();
  const panel = await openMemo(page);
  const editor = page.getByLabel("Career memo Markdown");
  await panel.getByRole("button", { name: /AI Copilot$/ }).click();
  await page.getByRole("button", { name: "Compact memo", exact: true }).click();
  await expect(page.getByLabel("Proposed career memo")).toBeVisible();
  await page.getByRole("button", { name: "Discard proposal" }).click();
  await expect(page.getByLabel("Proposed career memo")).toBeHidden();
  await expect(editor).toHaveValue(original);
  await page.getByLabel("Message memo copilot").fill("[simulate failure]");
  await page.getByRole("button", { name: "Send message", exact: true }).click();
  await expect(page.getByText(/Error:.*Memo copilot failed/)).toBeVisible();
  await expect(editor).toHaveValue(original);
  expect(await (await request.get("/api/career-memo")).json()).toEqual(baseline);
  await page.getByRole("button", { name: "Compact memo", exact: true }).click();
  await expect(page.getByLabel("Proposed career memo")).toBeVisible();
  const proposal = await page.getByLabel("Proposed career memo").inputValue();
  await page.getByRole("button", { name: "Apply to memo editor" }).click();
  const newer = await request.put("/api/career-memo", {
    data: { markdown: "New clarification from another session", base_revision_id: baseline.id },
  });
  expect(newer.ok()).toBeTruthy();
  await page.getByRole("button", { name: "Save memo revision", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("The career memo changed");
  await expect(editor).toHaveValue(proposal);
  expect((await (await request.get("/api/career-memo")).json()).markdown).toBe(
    "New clarification from another session",
  );
});

test("memo and profile copilot open and close with the keyboard", async ({ page }) => {
  const panel = await openMemo(page);
  await panel.getByRole("button", { name: /AI Copilot$/ }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toBeHidden();
  await page
    .locator(".profile-header-card")
    .getByRole("button", { name: /AI Copilot$/ })
    .click();
  await expect(page.getByText("AI PROFILE COPILOT", { exact: true })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByText("AI PROFILE COPILOT", { exact: true })).toBeHidden();
});
