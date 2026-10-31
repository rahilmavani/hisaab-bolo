/**
 * Tester round 9 · M6 voice through Chrome's fake mic. The fixture r9-english-question.wav is Sarvam Bulbul saying
 * "How much did Ram pay in the last four months?" (en-IN), padded with 1.5 s of silence.
 *
 * Spec (docs/ARCHITECTURE.md): moment 1 by voice; "Web push-to-talk"; voice path 7 ("Text renders first, and the
 * audio plays when ready"); languages Hindi, English, Hinglish (UI header); Risks ("Wi-Fi or mic failure: the text
 * fallback"). The panel is a dialog below xl (Escape closes it).
 */
import { join } from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { shot } from "./helpers";

test.use({
  launchOptions: {
    args: ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
      `--use-file-for-fake-audio-capture=${join(__dirname, "fixtures", "r9-english-question.wav")}`],
  },
  permissions: ["microphone"],
});

function panelOf(page: Page) {
  return page.getByRole("complementary", { name: "Ask Hisaab" }).or(page.getByRole("dialog", { name: "Ask Hisaab" }));
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

test.describe("tester r9 · M6 English voice (real Sarvam + Claude)", () => {
  test.setTimeout(120_000);

  test("English moment 1 by voice: English transcript, ₹12,100 / 121, English speech plays by itself", async ({ page }) => {
    await page.goto("/");
    const panel = panelOf(page);
    const voice = page.waitForResponse((r) => r.url().endsWith("/voice/ask"), { timeout: 60_000 });
    const tts = page.waitForResponse((r) => r.url().endsWith("/voice/tts"), { timeout: 90_000 });
    const up = await holdMic(page, 4600);
    const body = await (await voice).json();
    const answer = panel.locator("p.leading-relaxed").first();
    await expect(answer).toContainText("12,100", { timeout: 30_000 });
    const onScreen = Date.now() - up;
    expect((await tts).status()).toBe(200);
    console.log(`[r9 latency] english voice: stt ${body.stt_ms} ms, agent ${body.latency_ms} ms, on screen ${onScreen} ms, `
      + `audio ready ${Date.now() - up} ms after release; transcript ${JSON.stringify(body.transcript)}`);
    await expect(answer).toContainText("121");
    expect(body.lang).toBe("en-IN");
    await expect(panel.getByText(/“How much did Ram pay/i)).toBeVisible();
    await expect(panel.getByRole("button", { name: /Speaking…|Spoken back in English/ })).toBeVisible();
    await expect(panel.getByRole("link", { name: /Ram/ }).first()).toBeVisible();
    await shot(page, "m6", "r9-07-voice-english-moment1");
  });
});

test.describe("tester r9 · M6 voice failure paths (no model calls)", () => {
  test("Escape on the 1100px dialog while recording stops the microphone", async ({ page }) => {
    let voiceCalls = 0;
    await page.route("**/voice/ask", (route) => {
      voiceCalls++;
      return route.fulfill({ status: 422, contentType: "application/json", body: '{"detail":"stub"}' });
    });
    await page.addInitScript(() => {
      const w = window as unknown as { __tracks: MediaStreamTrack[] };
      w.__tracks = [];
      const orig = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
      navigator.mediaDevices.getUserMedia = async (c) => {
        const s = await orig(c);
        w.__tracks.push(...s.getTracks());
        return s;
      };
    });
    await page.setViewportSize({ width: 1100, height: 900 });
    await page.goto("/");
    await page.getByRole("button", { name: "Ask Hisaab", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "Ask Hisaab" });
    const mic = dialog.getByRole("button", { name: "Hold to speak" });
    await mic.focus();
    await page.keyboard.press("Enter"); // Enter starts a tap-to-stop recording
    await expect(dialog.getByText("Tap the mic to send")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
    await page.waitForTimeout(1500);
    const live = await page.evaluate(() =>
      (window as unknown as { __tracks: MediaStreamTrack[] }).__tracks.filter((t) => t.readyState === "live").length);
    await page.getByRole("button", { name: "Ask Hisaab", exact: true }).click();
    await shot(page, "m6", "r9-08-escape-while-recording");
    console.log(`[r9] after Escape: ${live} live mic track(s); voice calls so far ${voiceCalls}`);
    expect(live, "the microphone is still recording behind the closed dialog").toBe(0);
  });

  test("voice question with the network down shows a readable message", async ({ page }) => {
    await page.route("**/voice/ask", (route) => route.abort("internetdisconnected"));
    await page.goto("/");
    await holdMic(page, 1200);
    const alert = panelOf(page).getByRole("alert");
    await expect(alert).toBeVisible();
    await shot(page, "m6", "r9-09-voice-offline");
    await expect(alert).not.toHaveText(/^(Failed to fetch|Load failed|NetworkError.*)$/);
  });
});
