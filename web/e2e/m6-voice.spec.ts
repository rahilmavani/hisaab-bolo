import { expect, test } from "@playwright/test";
import { shot, watchErrors } from "./helpers";

// Chrome's fake microphone plays e2e/fixtures/ram-question.wav ("राम ने पिछले चार महीने में कितना दिया?").
test.use({
  launchOptions: {
    args: [
      "--use-fake-ui-for-media-stream",
      "--use-fake-device-for-media-stream",
      `--use-file-for-fake-audio-capture=${__dirname}/fixtures/ram-question.wav`,
    ],
  },
  permissions: ["microphone"],
});

test.describe("M6 voice", () => {
  test.setTimeout(90_000);


  test("hold the mic, ask in Hindi, get a spoken answer", async ({ page }) => {
    const errors = watchErrors(page);
    await page.goto("/");
    const panel = page.getByRole("complementary", { name: "Ask Hisaab" });
    const mic = panel.getByRole("button", { name: "Hold to speak" });
    await mic.hover();
    await page.mouse.down();
    await expect(panel.getByText("Release to send")).toBeVisible();
    await page.waitForTimeout(4500);
    await page.mouse.up();
    await expect(panel.getByText(/राम/).first()).toBeVisible({ timeout: 30_000 }); // transcript bubble
    await expect(panel.locator("p", { hasText: "12,100" }).first()).toBeVisible({ timeout: 45_000 });
    const tts = page.waitForResponse((r) => r.url().endsWith("/voice/tts") && r.status() === 200, { timeout: 30_000 });
    expect((await tts).headers()["content-type"]).toBe("audio/mpeg");
    await shot(page, "m6", "05-voice-ram");
    expect(errors.filter((e) => !e.includes("play()"))).toEqual([]);
  });
});
