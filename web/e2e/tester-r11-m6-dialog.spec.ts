/**
 * Tester round 11 · M6 voice through Chrome's fake mic, plus a stubbed dialog check.
 *
 * Fixtures: r11-hindi-mahinon.wav is Sarvam Bulbul (hi-IN) saying "राम ने पिछले चार महीनों में कितना दिया?" (standard
 * Hindi plural, the round-10 failure), padded with silence. r9-english-question.wav is the English moment-1 question.
 * r11-long-reply.mp3 is an 8 s tone standing in for a spoken reply.
 *
 * Spec (docs/ARCHITECTURE.md): moment 1 by voice ("answered with a spoken reply plus the payments behind it");
 * "Web push-to-talk"; voice path 7 ("Text renders first, and the audio plays when ready"). ask-panel.tsx: "Closing also
 * drops a recording in progress and stops speech, so nothing carries on unseen." UI copy: "Spoken back in …".
 */
import { join } from "node:path";
import { readFileSync } from "node:fs";
import { expect, test, type Page } from "@playwright/test";
import { shot, watchErrors } from "./helpers";

function panelOf(page: Page) {
  return page.getByRole("complementary", { name: "Ask Hisaab" }).or(page.getByRole("dialog", { name: "Ask Hisaab" }));
}

const fakeMic = (file: string) => ({
  launchOptions: {
    args: ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
      `--use-file-for-fake-audio-capture=${join(__dirname, "fixtures", file)}`, "--autoplay-policy=no-user-gesture-required"],
  },
  permissions: ["microphone"],
});

async function askByVoice(page: Page, label: string, holdMs: number) {
  const errors = watchErrors(page);
  await page.goto("/");
  const panel = panelOf(page);
  const voice = page.waitForResponse((r) => r.url().endsWith("/voice/ask"), { timeout: 60_000 });
  const tts = page.waitForResponse((r) => r.url().endsWith("/voice/tts"), { timeout: 90_000 });
  const mic = panel.getByRole("button", { name: "Hold to speak" });
  await mic.hover();
  await page.mouse.down();
  await expect(panel.getByText("Release to send")).toBeVisible();
  await page.waitForTimeout(holdMs);
  await page.mouse.up();
  const up = Date.now();
  const res = await voice;
  const body = await res.json();
  const answer = panel.locator("p.leading-relaxed").first();
  await expect(answer).toBeVisible({ timeout: 30_000 });
  const onScreen = Date.now() - up;
  const ttsRes = await tts;
  console.log(`[r11 voice] ${label}: status ${res.status()}, stt ${body.stt_ms} ms, agent ${body.latency_ms} ms, on screen `
    + `${onScreen} ms, audio ready ${Date.now() - up} ms after release (tts ${ttsRes.status()}); model ${body.model}; `
    + `fallback ${body.fallback}; transcript ${JSON.stringify(body.transcript)}; display ${JSON.stringify(body.display)}; `
    + `speech ${JSON.stringify(body.speech)}; components ${JSON.stringify(body.components.map((c: { type: string }) => c.type))}`);
  return { panel, answer, body, ttsStatus: ttsRes.status(), errors };
}

test.use({ launchOptions: { args: ["--autoplay-policy=no-user-gesture-required"] } });

test.describe("tester r11 · M6 reply audio and the dialog (no model calls)", () => {

  test("closing the dialog while the reply is being spoken, then reopening: the reply can be played again", async ({ page }) => {
    const mp3 = readFileSync(join(__dirname, "fixtures", "r11-long-reply.mp3"));
    await page.addInitScript(() => {
      const w = window as unknown as { __plays: number; __audio?: HTMLMediaElement };
      w.__plays = 0;
      const orig = HTMLMediaElement.prototype.play;
      HTMLMediaElement.prototype.play = function (this: HTMLMediaElement) {
        w.__plays++;
        w.__audio = this;
        return orig.call(this);
      };
    });
    const canned = {
      query_id: "00000000-0000-0000-0000-000000000111", display: "Ram ne pichhle 4 mahine mein ₹12,100 diye.",
      speech: "राम ने पिछले 4 महीने में ₹12,100 दिए।", lang: "hi-IN", components: [], follow_ups: [],
      pending_action: null, model: "stub", fallback: false, latency_ms: 1,
    };
    await page.route("**/ask", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(canned) }));
    await page.route("**/voice/tts", (route) => route.fulfill({ status: 200, contentType: "audio/mpeg", body: mp3 }));
    await page.setViewportSize({ width: 1100, height: 900 });
    await page.goto("/");
    const opener = page.getByRole("button", { name: "Ask Hisaab", exact: true });
    await opener.click();
    const dialog = page.getByRole("dialog", { name: "Ask Hisaab" });
    await dialog.getByLabel("Type a question").fill("Ram ne pichhle 4 mahine mein kitna diya?");
    await dialog.getByRole("button", { name: "Send question" }).click();
    const play = dialog.getByRole("button", { name: /Spoken back in Hinglish|Speaking…/ });
    await expect(play).toHaveText(/Spoken back in Hinglish/);
    await play.click();
    await expect(play).toHaveText(/Speaking…/);
    // Close only once the reply is audibly playing (well into the 8 s clip).
    await page.waitForFunction(() => {
      const a = (window as unknown as { __audio?: HTMLMediaElement }).__audio;
      return !!a && !a.paused && a.currentTime > 1;
    }, null, { timeout: 10_000 });
    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
    await page.waitForTimeout(500);
    await opener.click();
    await expect(dialog).toBeVisible();
    await page.waitForTimeout(9000); // longer than the whole 8 s reply
    await shot(page, "m6", "r11-pw-03-reopened-after-closing-mid-reply");
    const label = await play.innerText();
    const enabled = await play.isEnabled();
    console.log(`[r11] after closing mid-reply and reopening: button ${JSON.stringify(label)}, enabled ${enabled}`);
    expect(label, "the reply button is stuck on 'Speaking…' with nothing playing").toMatch(/Spoken back in Hinglish/);
    expect(enabled).toBe(true);
  });
});
