/**
 * Tester round 10 · M6 voice through Chrome's fake mic. The fixture r10-hinglish-question.wav is Sarvam Bulbul (hi-IN)
 * saying "राम ने पिछले चार महीने में कितना pay किया?" (the deck's moment-1 wording), padded with 1.5 s of silence.
 *
 * Spec (docs/ARCHITECTURE.md): moment 1 by voice; "Web push-to-talk"; voice path 7 ("Text renders first, and the audio
 * plays when ready"). ask-panel.tsx: "Closing also drops a recording in progress and stops speech, so nothing carries
 * on unseen."
 */
import { join } from "node:path";
import { readFileSync } from "node:fs";
import { expect, test, type Page } from "@playwright/test";
import { shot } from "./helpers";

test.use({
  launchOptions: {
    args: ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
      `--use-file-for-fake-audio-capture=${join(__dirname, "fixtures", "r10-hinglish-question.wav")}`,
      "--autoplay-policy=no-user-gesture-required"],
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

test.describe("tester r10 · M6 Hinglish voice (real Sarvam + Claude)", () => {
  test.setTimeout(120_000);

  test("Hinglish moment 1 by voice: transcript, ₹12,100 / 121, payer card, Hinglish speech plays by itself", async ({ page }) => {
    await page.goto("/");
    const panel = panelOf(page);
    const voice = page.waitForResponse((r) => r.url().endsWith("/voice/ask"), { timeout: 60_000 });
    const tts = page.waitForResponse((r) => r.url().endsWith("/voice/tts"), { timeout: 90_000 });
    const up = await holdMic(page, 5000);
    const body = await (await voice).json();
    const answer = panel.locator("p.leading-relaxed").first();
    await expect(answer).toContainText("12,100", { timeout: 30_000 });
    const onScreen = Date.now() - up;
    expect((await tts).status()).toBe(200);
    console.log(`[r10 latency] hinglish voice: stt ${body.stt_ms} ms, agent ${body.latency_ms} ms, on screen ${onScreen} ms, `
      + `audio ready ${Date.now() - up} ms after release; transcript ${JSON.stringify(body.transcript)}; model ${body.model}`);
    await expect(answer).toContainText("121");
    expect(body.lang).toBe("hi-IN");
    await expect(panel.getByText(/“राम|“Ram/).first()).toBeVisible();
    await expect(panel.getByRole("button", { name: /Speaking…|Spoken back in Hinglish/ })).toBeVisible();
    await expect(panel.getByRole("link", { name: /Ram/ }).first()).toContainText("₹12,100");
    await shot(page, "m6", "r10-06-voice-hinglish-moment1");
  });
});

test.describe("tester r10 · M6 voice and the dialog (no model calls)", () => {
  test("closing the 1100px dialog while a voice answer is on its way: the reply is not spoken behind the closed dialog", async ({ page }) => {
    const mp3 = readFileSync(join(__dirname, "fixtures", "r10-reply.mp3"));
    let ttsAfterClose = 0;
    let closed = false;
    await page.addInitScript(() => {
      const w = window as unknown as { __plays: number };
      w.__plays = 0;
      const orig = HTMLMediaElement.prototype.play;
      HTMLMediaElement.prototype.play = function (this: HTMLMediaElement) {
        w.__plays++;
        return orig.call(this);
      };
    });
    const canned = {
      query_id: "00000000-0000-0000-0000-000000000011", display: "Ram ne pichhle 4 mahine mein ₹12,100 diye.",
      speech: "राम ने पिछले 4 महीने में ₹12,100 दिए।", lang: "hi-IN", components: [], follow_ups: [],
      pending_action: null, model: "stub", fallback: false, latency_ms: 1, transcript: "राम ने पिछले चार महीने में कितना pay किया?",
      stt_ms: 1,
    };
    await page.route("**/voice/ask", async (route) => {
      await new Promise((r) => setTimeout(r, 3000)); // the agent is still thinking when the merchant closes the dialog
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(canned) });
    });
    await page.route("**/voice/tts", (route) => {
      if (closed) ttsAfterClose++;
      return route.fulfill({ status: 200, contentType: "audio/mpeg", body: mp3 });
    });
    await page.setViewportSize({ width: 1100, height: 900 });
    await page.goto("/");
    await page.getByRole("button", { name: "Ask Hisaab", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "Ask Hisaab" });
    // In dev, Next's overlay badge sits over the dialog's mic at 1100px, so drive it by keyboard (Enter, Enter).
    const mic = dialog.getByRole("button", { name: "Hold to speak" });
    await mic.focus();
    await page.keyboard.press("Enter");
    await expect(dialog.getByText("Tap the mic to send")).toBeVisible();
    await page.waitForTimeout(1500);
    await page.keyboard.press("Enter");
    await expect(dialog.getByText("Checking your hisaab…")).toBeVisible();
    await page.keyboard.press("Escape");
    closed = true;
    await expect(dialog).toBeHidden();
    await page.waitForTimeout(6000);
    const plays = await page.evaluate(() => (window as unknown as { __plays: number }).__plays);
    await page.getByRole("button", { name: "Ask Hisaab", exact: true }).click();
    await shot(page, "m6", "r10-07-closed-dialog-voice-reply");
    console.log(`[r10] after closing the dialog: ${ttsAfterClose} TTS request(s), ${plays} audio play() call(s)`);
    expect(plays, "the voice reply played while the dialog was closed").toBe(0);
  });
});
