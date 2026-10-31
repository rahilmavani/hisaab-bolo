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

test.use(fakeMic("r9-english-question.wav"));

test.describe("tester r11 · M6 moment 1 by voice, English (real Sarvam + Claude)", () => {
  test.setTimeout(120_000);

  test("spoken English reply plus the payer card, ₹12,100 / 121", async ({ page }) => {
    const { panel, answer, body, ttsStatus, errors } = await askByVoice(page, "english", 4300);
    expect(body.fallback).toBe(false);
    expect(body.lang).toBe("en-IN");
    await expect(answer).toContainText("12,100");
    await expect(answer).toContainText("121");
    expect(ttsStatus).toBe(200);
    await expect(panel.getByRole("button", { name: /Speaking…|Spoken back in English/ })).toBeVisible();
    await expect(panel.getByRole("link", { name: /Ram/ }).first()).toContainText("₹12,100");
    await shot(page, "m6", "r11-pw-02-voice-english");
    expect(errors.filter((e) => !e.includes("play()"))).toEqual([]);
  });
});
