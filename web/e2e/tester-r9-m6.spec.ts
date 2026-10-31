/**
 * Tester round 9 · M6 "Ask Hisaab" (real Claude + Sarvam where noted; keep the number of asks small).
 *
 * Spec (docs/ARCHITECTURE.md): moment 1 ("Ram ne pichhle 4 mahine mein kitna diya?" answered with a spoken reply
 * plus the payments behind it); voice path 6 (number guard: "retry once on Sonnet, then fall back to showing the data
 * with no speech"); 7 ("Text renders first, and the audio plays when ready"); Risks ("Wi-Fi or mic failure: the text
 * fallback"). UI copy: "Hindi · English · Hinglish", "Spoken back in …", "Show all", dialog below xl.
 *
 * Seeded facts: Ram ₹12,100 / 121 payments since 1 Jul; Suresh Bhatt ₹2,430 / 25 payments in the last 30 days
 * (2 Oct – 31 Oct).
 */
import { expect, test, type Locator, type Page } from "@playwright/test";
import { shot, watchErrors } from "./helpers";

function panelOf(page: Page) {
  return page.getByRole("complementary", { name: "Ask Hisaab" }).or(page.getByRole("dialog", { name: "Ask Hisaab" }));
}

async function ask(page: Page, question: string, label: string): Promise<Locator> {
  const panel = panelOf(page);
  const before = await panel.locator("p.leading-relaxed").count();
  await panel.getByLabel("Type a question").fill(question);
  const t0 = Date.now();
  await panel.getByRole("button", { name: "Send question" }).click();
  const answer = panel.locator("p.leading-relaxed").nth(before);
  await expect(answer).toBeVisible({ timeout: 60_000 });
  console.log(`[r9 latency] ${label}: ${Date.now() - t0} ms (question → answer on screen)`);
  return answer;
}

test.describe("tester r9 · M6 moment 1 and follow-ups (real Claude)", () => {
  test.setTimeout(150_000);

  test("moment 1 from the example chip, its speech plays, and 'all payments' lists all 121", async ({ page }) => {
    const errors = watchErrors(page);
    await page.goto("/");
    const panel = panelOf(page);
    const t0 = Date.now();
    await panel.getByRole("button", { name: "Ram ne pichhle 4 mahine mein kitna diya?" }).click();
    const answer = panel.locator("p.leading-relaxed").first();
    await expect(answer).toBeVisible({ timeout: 60_000 });
    console.log(`[r9 latency] moment 1 example chip: ${Date.now() - t0} ms`);
    await expect(answer).toContainText("12,100");
    await expect(answer).toContainText("121");
    const card = panel.getByRole("link", { name: /Ram/ }).first();
    await expect(card).toBeVisible();
    await expect(card).toContainText("₹12,100");
    const speak = panel.getByRole("button", { name: "Spoken back in Hinglish" });
    await expect(speak).toBeVisible();
    const t1 = Date.now();
    const tts = page.waitForResponse((r) => r.url().endsWith("/voice/tts"), { timeout: 30_000 });
    await speak.click();
    const res = await tts;
    console.log(`[r9 latency] TTS after tap: ${Date.now() - t1} ms`);
    expect(res.status()).toBe(200);
    await shot(page, "m6", "r9-01-moment1-example-chip");

    await ask(page, "Ram ke pichhle 4 mahine ke saare payments dikhao", "all payments follow-up");
    const table = panel.locator("div.rounded-2xl", { hasText: /121 payments/ }).last();
    await expect(table).toBeVisible();
    await table.getByRole("button", { name: /^Show all/ }).click();
    await expect(table.locator("li")).toHaveCount(121);
    await shot(page, "m6", "r9-02-all-121-payments");
    expect(errors.filter((e) => !e.includes("play()"))).toEqual([]);
  });

  test("'pichhle 30 din' for another customer gets a grounded spoken answer, not an apology", async ({ page }) => {
    // Voice path 6: a correct answer must pass the number guard. "30" from the question must be repeatable
    // (question_numbers() turns it into "3E+1"), and a guard failure must still show the data.
    await page.goto("/");
    const panel = panelOf(page);
    const answer = await ask(page, "Suresh Bhatt ne pichhle 30 din mein kitna diya?", "30 din Suresh");
    await shot(page, "m6", "r9-03-30-din-suresh");
    await expect(answer).not.toContainText("Maaf kijiye");
    await expect(answer).toContainText("2,430");
    await expect(panel.getByRole("button", { name: "Spoken back in Hinglish" })).toBeVisible();
  });
});

// ---------------------------------------------------------------------------
// No model calls below
// ---------------------------------------------------------------------------
test.describe("tester r9 · M6 dialog and failure paths (no model calls)", () => {
  test("1100px dialog keeps keyboard focus inside while it is open", async ({ page }) => {
    await page.setViewportSize({ width: 1100, height: 900 });
    await page.goto("/");
    await page.getByRole("button", { name: "Ask Hisaab", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "Ask Hisaab" });
    await expect(dialog).toBeVisible();
    const escaped: string[] = [];
    for (let i = 0; i < 12; i++) {
      await page.keyboard.press("Tab");
      const where = await page.evaluate(() => {
        const a = document.activeElement as HTMLElement | null;
        const inside = !!a?.closest('[role="dialog"]');
        return inside ? null : `${a?.tagName} "${(a?.getAttribute("aria-label") ?? a?.textContent ?? "").trim().slice(0, 30)}"`;
      });
      if (where) escaped.push(where);
    }
    await shot(page, "m6", "r9-04-dialog-focus");
    expect(escaped, `Tab moved focus behind the modal to: ${escaped.join(", ")}`).toEqual([]);
  });

  test("text question with the network down shows a readable message", async ({ page }) => {
    await page.goto("/");
    await page.route("**/ask", (route) => route.abort("internetdisconnected"));
    const panel = panelOf(page);
    await panel.getByLabel("Type a question").fill("Ram ne kitna diya?");
    await panel.getByRole("button", { name: "Send question" }).click();
    const alert = panel.getByRole("alert");
    await expect(alert).toBeVisible();
    await shot(page, "m6", "r9-05-text-offline");
    await expect(alert).not.toHaveText(/^(Failed to fetch|Load failed|NetworkError.*)$/);
  });

  test("picking a chip after a long question is not rejected with a validation message", async ({ page }) => {
    // The panel re-asks "<question> (<name>)"; the input allows 500 characters, the API's text limit is also 500.
    const long = "Asha ne " + "pichhle kuch mahino mein ".repeat(19) + "kitna diya?";
    expect(long.length).toBeLessThanOrEqual(500);
    const canned = {
      query_id: "00000000-0000-0000-0000-000000000009", display: "Kaunsi Asha?", speech: null, lang: "hi-IN",
      components: [{ type: "chips", result_id: "r1", data: { choices: [
        { payer_id: "00000000-0000-0000-0000-00000000000a", label: "Asha Saxena", sub: "asha@paytm" }] } }],
      follow_ups: [], pending_action: null, model: "stub", fallback: false, latency_ms: 1,
    };
    let first = true;
    await page.route("**/ask", async (route) => {
      if (first) {
        first = false;
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(canned) });
      }
      return route.continue();
    });
    await page.goto("/");
    const panel = panelOf(page);
    await panel.getByLabel("Type a question").fill(long);
    await panel.getByRole("button", { name: "Send question" }).click();
    const sent = page.waitForResponse((r) => r.url().endsWith("/ask"));
    await panel.getByRole("group", { name: "Which customer?" }).getByRole("button", { name: /Asha Saxena/ }).click();
    const res = await sent;
    console.log(`[r9] chip pick after a ${long.length}-char question → /ask ${res.status()}`);
    await page.waitForTimeout(500);
    await shot(page, "m6", "r9-06-long-question-chip");
    const alerts = await panel.getByRole("alert").allInnerTexts();
    expect(alerts.join(" ")).not.toMatch(/String should have at most|characters/);
  });
});
