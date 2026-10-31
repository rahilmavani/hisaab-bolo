/**
 * Tester round 8 · M6 voice through Chrome's fake mic (real Sarvam + Claude). See tester-r8-m6.spec.ts for the spec
 * references. launchOptions force a worker per file, so each fake-mic recording lives in its own file.
 */
import { join } from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { shot } from "./helpers";

const FILE = "ram-question.wav";
test.use({
  launchOptions: {
    args: ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", `--use-file-for-fake-audio-capture=${join(__dirname, "fixtures", FILE)}`],
  },
  permissions: ["microphone"],
});

const timings: Record<string, number> = {};
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

test.describe("tester r8 · M6 voice with the Ram question on the fake mic (real Sarvam + Claude)", () => {
  test.setTimeout(120_000);

  test("hold, release: transcript, ₹12,100, audio plays by itself; timings logged", async ({ page }) => {
    await page.goto("/");
    const panel = panelOf(page);
    const voice = page.waitForResponse((r) => r.url().endsWith("/voice/ask"), { timeout: 60_000 });
    const tts = page.waitForResponse((r) => r.url().endsWith("/voice/tts"), { timeout: 90_000 });
    const up = await holdMic(page, 3200);
    await expect(panel.getByText("Listening back…")).toBeVisible();
    const body = await (await voice).json();
    await expect(panel.locator("p.leading-relaxed").first()).toContainText("12,100", { timeout: 30_000 });
    timings["voice answer on screen"] = Date.now() - up;
    expect((await tts).status()).toBe(200);
    timings["voice audio ready"] = Date.now() - up;
    console.log(`[r8 latency] voice: stt ${body.stt_ms} ms, agent ${body.latency_ms} ms, answer on screen ${timings["voice answer on screen"]} ms, audio ready ${timings["voice audio ready"]} ms after release`);
    await expect(panel.getByText(/“राम|“Ram/).first()).toBeVisible();
    await expect(panel.getByRole("button", { name: /Speaking…|Spoken back in Hinglish/ })).toBeVisible();
    await shot(page, "m6", "r8-08-voice-ram");
  });

  test("holding past the 30-second limit sends automatically and never shows Sarvam's raw error", async ({ page }) => {
    test.setTimeout(150_000);
    await page.goto("/");
    const panel = panelOf(page);
    const mic = panel.getByRole("button", { name: "Hold to speak" });
    await mic.hover();
    await page.mouse.down();
    const voice = page.waitForResponse((r) => r.url().endsWith("/voice/ask"), { timeout: 60_000 });
    const res = await voice; // the recorder's own 30 s limit fires while the button is still held
    await page.mouse.up();
    const turn = panel.locator("p.leading-relaxed, [role=alert]").last();
    await expect(turn).toBeVisible({ timeout: 60_000 });
    await shot(page, "m6", "r8-09-voice-30s-limit");
    const text = await panel.locator("[role=alert]").allInnerTexts();
    console.log(`[r8] 30 s hold → /voice/ask ${res.status()}; alerts: ${JSON.stringify(text)}`);
    expect(text.join(" ")).not.toMatch(/invalid_request_error|\{"error"|speech-to-text 400/);
  });

  test("keyboard: holding Space (auto-repeat) keeps one recording going instead of toggling it", async ({ page }) => {
    // "Hold to speak" invites holding the key. A held key repeats keydown (repeat=true) after ~500 ms.
    await page.route("**/voice/ask", (route) => route.fulfill({ status: 422, contentType: "application/json", body: '{"detail":"stub"}' }));
    await page.addInitScript(() => {
      const w = window as unknown as { __gum: number; __errors: number };
      w.__gum = 0;
      w.__errors = 0;
      const orig = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
      navigator.mediaDevices.getUserMedia = (c) => { w.__gum++; return orig(c); };
      new MutationObserver(() => {
        if (document.body.innerText.includes("Hold the mic while you speak")) w.__errors++;
      }).observe(document, { subtree: true, childList: true, characterData: true });
    });
    await page.goto("/");
    const panel = panelOf(page);
    await panel.getByRole("button", { name: "Hold to speak" }).focus();
    await page.keyboard.down("Space");
    await expect(panel.getByRole("button", { name: "Stop and send recording" })).toBeVisible();
    await page.waitForTimeout(500);
    for (let i = 0; i < 40; i++) {
      await page.keyboard.down("Space");
      await page.waitForTimeout(33);
    }
    await page.keyboard.up("Space");
    const seen = await page.evaluate(() => ({ gum: (window as any).__gum, errors: (window as any).__errors }));
    console.log(`[r8] held Space for ~1.8 s: ${seen.gum} microphone starts, error shown ${seen.errors} times`);
    expect(seen.errors).toBe(0);
    expect(seen.gum).toBe(1);
  });

  test("a /voice/ask server error with a non-JSON body shows a readable message", async ({ page }) => {
    await page.route("**/voice/ask", (route) => route.fulfill({ status: 500, contentType: "text/plain", body: "Internal Server Error" }));
    await page.goto("/");
    await holdMic(page, 1200);
    const alert = panelOf(page).getByRole("alert");
    await expect(alert).toBeVisible();
    await expect(alert).not.toContainText(/JSON|Unexpected token/);
  });

  test("a TTS failure leaves the text answer and offers a retry", async ({ page }) => {
    const canned = {
      query_id: "00000000-0000-0000-0000-000000000001", display: "Ram ne ₹12,100 diye.", speech: "राम ने ₹12,100 दिए।",
      lang: "hi-IN", components: [], follow_ups: [], pending_action: null, model: "stub", fallback: false, latency_ms: 1,
      transcript: "राम ने कितना दिया?", stt_ms: 1,
    };
    await page.route("**/voice/ask", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(canned) }));
    await page.route("**/voice/tts", (route) => route.fulfill({ status: 502, contentType: "application/json", body: '{"detail":"x"}' }));
    await page.goto("/");
    await holdMic(page, 1200);
    const panel = panelOf(page);
    await expect(panel.getByText("Ram ne ₹12,100 diye.")).toBeVisible();
    await expect(panel.getByRole("button", { name: "Voice unavailable, tap to retry" })).toBeVisible();
  });
});

