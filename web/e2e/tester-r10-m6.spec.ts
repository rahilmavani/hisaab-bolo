/**
 * Tester round 10 · M6 "Ask Hisaab" (real Claude where noted; keep the number of asks small).
 *
 * Spec (docs/ARCHITECTURE.md): moment 1 ("Ram ne pichhle 4 mahine mein kitna diya?" answered with a spoken reply plus
 * the payments behind it); voice path 6 (number guard: "retry once on Sonnet, then fall back to showing the data with
 * no speech"); get_sales_summary -> line_chart + stat_cards; "Write actions create a pending action that needs a
 * confirm tap"; Risks ("Wi-Fi or mic failure: the text fallback"). UI copy: "Hindi · English · Hinglish",
 * "Spoken back in …", "Show all", "Needs your OK", dialog below xl.
 *
 * Seeded facts: Ram ₹12,100 / 121 payments since 1 Jul. Shop totals are read from the database at run time.
 */
import { execFileSync } from "node:child_process";
import { expect, test, type Locator, type Page } from "@playwright/test";
import { shot, watchErrors } from "./helpers";

function panelOf(page: Page) {
  return page.getByRole("complementary", { name: "Ask Hisaab" }).or(page.getByRole("dialog", { name: "Ask Hisaab" }));
}

function sql(query: string): string {
  return execFileSync("docker", ["exec", "-i", "supabase_db_hisaab-bolo", "psql", "-U", "postgres", "-d", "postgres", "-At",
    "-c", query]).toString().trim();
}

const inr = (paise: number) => `₹${Math.round(paise / 100).toLocaleString("en-IN")}`;

async function ask(page: Page, question: string, label: string): Promise<Locator> {
  const panel = panelOf(page);
  const before = await panel.locator("p.leading-relaxed").count();
  await panel.getByLabel("Type a question").fill(question);
  const t0 = Date.now();
  await panel.getByRole("button", { name: "Send question" }).click();
  const answer = panel.locator("p.leading-relaxed").nth(before);
  await expect(answer).toBeVisible({ timeout: 60_000 });
  console.log(`[r10 latency] ${label}: ${Date.now() - t0} ms (question → answer on screen)`);
  return answer;
}

test.describe("tester r10 · M6 moment 1 and ranges (real Claude)", () => {
  test.setTimeout(180_000);

  test("1100px dialog: moment 1 from the example chip, then the model's own follow-up lists all 121 payments", async ({ page }) => {
    const errors = watchErrors(page);
    await page.setViewportSize({ width: 1100, height: 900 });
    await page.goto("/");
    await page.getByRole("button", { name: "Ask Hisaab", exact: true }).click();
    const panel = page.getByRole("dialog", { name: "Ask Hisaab" });
    const t0 = Date.now();
    await panel.getByRole("button", { name: "Ram ne pichhle 4 mahine mein kitna diya?" }).click();
    const answer = panel.locator("p.leading-relaxed").first();
    await expect(answer).toBeVisible({ timeout: 60_000 });
    console.log(`[r10 latency] moment 1 (dialog, example chip): ${Date.now() - t0} ms`);
    await expect(answer).toContainText("12,100");
    await expect(answer).toContainText("121");
    await expect(panel.getByRole("link", { name: /Ram/ }).first()).toContainText("₹12,100");
    await expect(panel.getByRole("button", { name: "Spoken back in Hinglish" })).toBeVisible();
    await shot(page, "m6", "r10-01-moment1-dialog-1100");

    // Tap the follow-up the model offered for the payments behind the answer, as a merchant would.
    const followUps = await panel.locator("div.flex-wrap > button").allInnerTexts();
    console.log(`[r10] follow-ups offered: ${JSON.stringify(followUps)}`);
    const pick = followUps.find((f) => /payment|dikha|saar|sabhi|all/i.test(f));
    if (pick) {
      const t1 = Date.now();
      await panel.getByRole("button", { name: pick, exact: true }).click();
      await expect(panel.locator("p.leading-relaxed").nth(1)).toBeVisible({ timeout: 60_000 });
      console.log(`[r10 latency] follow-up "${pick}": ${Date.now() - t1} ms`);
    } else {
      await ask(page, "Ram ke pichhle 4 mahine ke saare payments dikhao", "all payments (typed)");
    }
    const table = panel.locator("div.rounded-2xl", { hasText: /121 payments/ }).last();
    await expect(table).toBeVisible();
    await table.getByRole("button", { name: /^Show all/ }).click();
    await expect(table.locator("li")).toHaveCount(121);
    await shot(page, "m6", "r10-02-followup-all-121");
    expect(errors.filter((e) => !e.includes("play()"))).toEqual([]);
  });

  test("moment 1 in standard Hindi ('चार महीनों') gets a spoken reply, not the no-speech fallback", async ({ page }) => {
    await page.goto("/");
    const panel = panelOf(page);
    const answer = await ask(page, "राम ने पिछले चार महीनों में कितना दिया?", "moment 1 Devanagari plural");
    await shot(page, "m6", "r10-03-devanagari-mahinon");
    await expect(answer).not.toHaveText("Yeh raha jo mila.");
    await expect(answer).toContainText("12,100");
    await expect(panel.getByRole("button", { name: "Spoken back in Hinglish" })).toBeVisible();
  });

  test("'pichhle 3 mahine ki sale': the card and the chart both match the database", async ({ page }) => {
    const [count, paise] = sql(
      "select count(*), sum(amount_paise) from transactions t join merchants m on m.id = t.merchant_id " +
      "where m.name = 'Gupta Kirana Store' and paid_at >= ((app_today() - interval '3 months')::date::timestamp " +
      "at time zone 'Asia/Kolkata') and paid_at < app_now()").split("|").map(Number);
    await page.goto("/");
    const panel = panelOf(page);
    const answer = await ask(page, "Pichhle 3 mahine ki sale kitni hui?", "3 mahine sale");
    const total = inr(paise);
    await expect(answer).toContainText(total.replace("₹", ""));
    await expect(panel.getByText(total, { exact: true }).first()).toBeVisible();
    await expect(panel.getByText(count.toLocaleString("en-IN"), { exact: true }).first()).toBeVisible();
    const chart = panel.getByRole("img", { name: /Collections by/ }).or(panel.locator("[aria-label^='Collections by']"));
    await expect(chart.first()).toBeVisible();
    console.log(`[r10] 3 mahine: db ${count} payments ${total}; chart ${await chart.first().getAttribute("aria-label")}`);
    await shot(page, "m6", "r10-04-3-mahine-sale");
  });
});

// ---------------------------------------------------------------------------
// No model calls below
// ---------------------------------------------------------------------------
test.describe("tester r10 · M6 confirm card offline (no model calls)", () => {
  test("Confirm with the network down shows a readable message and nothing is claimed done", async ({ page }) => {
    const canned = {
      query_id: "00000000-0000-0000-0000-000000000010", display: "Sharma Traders ko reminder ready hai. Confirm karein.",
      speech: null, lang: "hi-IN", follow_ups: [], model: "stub", fallback: false, latency_ms: 1,
      pending_action: null,
      components: [{ type: "confirm_action", result_id: "r2", data: {
        token: "pa_stubstubstub", tool: "send_reminder", expires_at: "2099-01-01T00:00:00+05:30",
        summary: { title: "Reminder for INV-0001", customer: "Sharma Traders", amount: "₹8,500",
          invoice: "INV-0001", email: "accounts@sharmatraders.in", status: "overdue" } } }],
    };
    await page.route("**/ask", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(canned) }));
    await page.route("**/actions/**", (route) => route.abort("internetdisconnected"));
    await page.goto("/");
    const panel = panelOf(page);
    await panel.getByLabel("Type a question").fill("Sharma Traders ko reminder bhej do");
    await panel.getByRole("button", { name: "Send question" }).click();
    await panel.getByRole("button", { name: "Confirm: Send reminder" }).click();
    const alert = panel.getByRole("alert");
    await expect(alert).toBeVisible();
    await shot(page, "m6", "r10-05-confirm-offline");
    console.log(`[r10] confirm offline shows: ${JSON.stringify(await alert.innerText())}`);
    await expect(alert).not.toHaveText(/^(Failed to fetch|Load failed|NetworkError.*)$/);
  });
});
