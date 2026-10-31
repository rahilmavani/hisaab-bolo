/**
 * Tester round 8 · M6 voice through Chrome's fake mic (real Sarvam + Claude). See tester-r8-m6.spec.ts for the spec
 * references. launchOptions force a worker per file, so each fake-mic recording lives in its own file.
 */
import { join } from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { shot } from "./helpers";

const FILE = "r8-silence.wav";
test.use({
  launchOptions: {
    args: ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", `--use-file-for-fake-audio-capture=${join(__dirname, "fixtures", FILE)}`],
  },
  permissions: ["microphone"],
});

function panelOf(page: Page) {
  return page.getByRole("complementary", { name: "Ask Hisaab" });
}

async function holdMic(page: Page, ms: number) {
  const mic = panelOf(page).getByRole("button", { name: "Hold to speak" });
  await mic.hover();
  await page.mouse.down();
  await expect(panelOf(page).getByText("Release to send")).toBeVisible();
  await page.waitForTimeout(ms);
  await page.mouse.up();
  return Date.now();
}

test.describe("tester r8 · M6 voice with a silent fake mic (real Sarvam)", () => {

  test("silence gets the 'Didn't catch that' message and the text box still works", async ({ page }) => {
    await page.goto("/");
    await holdMic(page, 2000);
    const panel = panelOf(page);
    await expect(panel.getByRole("alert").filter({ hasText: "Didn't catch that" })).toBeVisible({ timeout: 30_000 });
    await expect(panel.getByLabel("Type a question")).toBeEditable();
    await shot(page, "m6", "r8-10-voice-silence");
  });
});

