/**
 * Tester round 8 · M6 "Ask Hisaab" against the real Claude + Sarvam APIs (keep the number of asks small).
 *
 * Spec (docs/ARCHITECTURE.md): moment 1 ("Ram ne pichhle 4 mahine mein kitna diya?" answered with a spoken reply
 * plus the payments behind it); Agent ("Write actions create a pending action that needs a confirm tap");
 * Voice ("Web push-to-talk. A text box fallback hits the same agent"); voice path 6-7 (number guard; "Text renders
 * first, and the audio plays when ready"; "Target: about 5 seconds"); Risks ("Speech-to-text mishears names:
 * trigram search plus disambiguation chips", "Wi-Fi or mic failure: the text fallback").
 * UI copy: "Hindi · English · Hinglish", "Hold to speak", "Spoken back in …", "Needs your OK", "Confirm: …",
 * "Cancelled. Nothing was sent.", "keep questions under 30 seconds" (API), "Didn't catch that. Hold the mic and
 * speak, or type your question." (API). Deck slide 5: Hinglish question, "Spoken back in Hinglish", payments shown.
 *
 * Seeded facts (seed is deterministic): Ram pays ₹100 daily ~8:30; 121 payments / ₹12,100 since 1 Jul;
 * Mon 26 Oct to today (Sat 31 Oct) = 6 payments / ₹600.
 */
import { execFileSync } from "node:child_process";
import { join } from "node:path";
import { expect, test, type Locator, type Page } from "@playwright/test";
import { shot, watchErrors } from "./helpers";

const API_DIR = join(__dirname, "../../api");
const timings: Record<string, number> = {};

function panelOf(page: Page) {
  // Docked beside the page from xl (complementary); below xl it opens over the page as a dialog.
  return page.getByRole("complementary", { name: "Ask Hisaab" }).or(page.getByRole("dialog", { name: "Ask Hisaab" }));
}

/** Types a question, sends it, and returns the newest answer bubble once it has text. */
async function ask(page: Page, question: string, label: string, via: "click" | "enter" = "click"): Promise<Locator> {
  const panel = panelOf(page);
  const before = await panel.locator("p.leading-relaxed").count();
  await panel.getByLabel("Type a question").fill(question);
  const t0 = Date.now();
  if (via === "enter") await panel.getByLabel("Type a question").press("Enter");
  else await panel.getByRole("button", { name: "Send question" }).click();
  const answer = panel.locator("p.leading-relaxed").nth(before);
  await expect(answer).toBeVisible({ timeout: 60_000 });
  timings[label] = Date.now() - t0;
  console.log(`[r8 latency] ${label}: ${timings[label]} ms (question → answer on screen)`);
  return answer;
}

function py(code: string): string {
  return execFileSync("uv", ["run", "python", "-c", code], { cwd: API_DIR, encoding: "utf8" }).trim().split("\n").pop()!;
}

// ---------------------------------------------------------------------------
// Moment 1 and ranges on the demo merchant (read-only questions)
// ---------------------------------------------------------------------------
test.describe("tester r8 · M6 moment 1 and date ranges (real Claude)", () => {
  test.setTimeout(120_000);

  test("English moment-1 question: English answer, ₹12,100 / 121, payer card, playable English speech", async ({ page }) => {
    const errors = watchErrors(page);
    await page.goto("/");
    const panel = panelOf(page);
    const answer = await ask(page, "How much did Ram pay in the last 4 months?", "english moment 1");
    await expect(answer).toContainText("12,100");
    await expect(answer).toContainText("121");
    expect(await answer.innerText()).not.toMatch(/\b(ne|mein|diye|hain|kul|roz)\b/i);
    await expect(panel.getByRole("link", { name: /Ram/ }).first()).toBeVisible();
    const speak = panel.getByRole("button", { name: "Spoken back in English" });
    await expect(speak).toBeVisible();
    const t0 = Date.now();
    const tts = page.waitForResponse((r) => r.url().endsWith("/voice/tts"), { timeout: 30_000 });
    await speak.click();
    const res = await tts;
    timings["tts after tap"] = Date.now() - t0;
    console.log(`[r8 latency] TTS after tap: ${timings["tts after tap"]} ms`);
    expect(res.status()).toBe(200);
    expect(res.headers()["content-type"]).toBe("audio/mpeg");
    await expect(panel.getByRole("button", { name: /Speaking…|Spoken back in English/ })).toBeVisible();
    await shot(page, "m6", "r8-01-english-moment1");
    expect(errors.filter((e) => !e.includes("play()"))).toEqual([]);
  });

  test("Devanagari moment-1 question: ₹12,100 / 121 and a Hindi-language spoken reply", async ({ page }) => {
    await page.goto("/");
    const panel = panelOf(page);
    const answer = await ask(page, "राम ने पिछले चार महीने में कितना दिया?", "devanagari moment 1");
    await expect(answer).toContainText("12,100");
    await expect(answer).toContainText("121");
    await expect(panel.getByRole("button", { name: /Spoken back in (Hindi|Hinglish)/ })).toBeVisible();
    await shot(page, "m6", "r8-02-devanagari-moment1");
  });

  test("'is hafte' typed and sent with Enter: Monday to today, ₹600 over 6 payments", async ({ page }) => {
    await page.goto("/");
    const answer = await ask(page, "Is hafte Ram ne kitna diya?", "is hafte", "enter");
    await expect(answer).toContainText("600");
    await expect(answer).toContainText(/\b6\b/);
    await expect(panelOf(page).getByRole("button", { name: "Spoken back in Hinglish" })).toBeVisible();
  });

  test("a customer who doesn't exist ('Ramesh') is not answered with Ram's money", async ({ page }) => {
    await page.goto("/");
    const panel = panelOf(page);
    const answer = await ask(page, "Ramesh ne pichhle 4 mahine mein kitna diya?", "unknown name Ramesh");
    await shot(page, "m6", "r8-03-unknown-ramesh");
    const chips = panel.getByRole("group", { name: "Which customer?" });
    // Either it says there is no Ramesh, or it asks (chips) whether Ram was meant; it must not state Ram's total.
    if (!(await chips.isVisible())) expect(await answer.innerText()).not.toContain("12,100");
  });
});

// ---------------------------------------------------------------------------
// Ambiguous name + write actions on a throwaway merchant
// ---------------------------------------------------------------------------
type Throwaway = { email: string; password: string; merchant_id: string; user_id: string; a: string; b: string };

const SETUP_PY = `
import json, uuid, psycopg
from tests.conftest import create_user
from app.config import get_settings
s = get_settings()
email = f"tester-r8-{uuid.uuid4().hex[:8]}@hisaabbolo.test"; pw = "pw-" + uuid.uuid4().hex
uid = create_user(s, email, pw)
with psycopg.connect(s.database_url, autocommit=True) as c:
    m = c.execute("insert into merchants (name, owner_user_id) values ('Tester R8 Store', %s) returning id", (uid,)).fetchone()[0]
    a = c.execute("insert into payers (merchant_id, display_name, email) values (%s, 'Asha Test', 'asha@example.com') returning id", (m,)).fetchone()[0]
    b = c.execute("insert into payers (merchant_id, display_name, phone) values (%s, 'Asha T (UPI)', '9000000001') returning id", (m,)).fetchone()[0]
    c.execute("insert into payer_identifiers (payer_id, merchant_id, kind, value) values (%s,%s,'vpa','asha@okaxis'), (%s,%s,'vpa','asha.t@ybl'), (%s,%s,'phone','9000000001')", (a, m, b, m, b, m))
    for i, p in enumerate([a, a, b]):
        c.execute("insert into transactions (merchant_id, payer_id, amount_paise, paid_at, source, description, description_by) values (%s, %s, %s, app_now() - %s * interval '1 day', 'seed', 'Groceries', 'ai')", (m, p, 25000 + i * 100, i + 1))
print(json.dumps({"email": email, "password": pw, "merchant_id": str(m), "user_id": str(uid), "a": str(a), "b": str(b)}))
`;

const TEARDOWN_PY = (t: Throwaway) => `
import psycopg
from tests.conftest import delete_user
from app.config import get_settings
s = get_settings()
with psycopg.connect(s.database_url, autocommit=True) as c:
    c.execute("delete from merchants where id = %s", ("${t.merchant_id}",))
delete_user(s, "${t.user_id}")
`;

const SQL_PY = (sql: string) => `
import psycopg
from app.config import get_settings
with psycopg.connect(get_settings().database_url, autocommit=True) as c:
    cur = c.execute("""${sql}""")
    print(cur.fetchone()[0] if cur.description else "ok")
`;

test.describe("tester r8 · M6 chips and confirm-before-write (throwaway merchant, real Claude)", () => {
  test.describe.configure({ mode: "serial" });
  test.setTimeout(120_000);
  let t: Throwaway;
  let page: Page;

  test.beforeAll(async ({ browser }) => {
    t = JSON.parse(py(SETUP_PY));
    const ctx = await browser.newContext({ storageState: { cookies: [], origins: [] }, viewport: { width: 1440, height: 1000 } });
    page = await ctx.newPage();
    await page.goto("/login");
    await page.getByLabel("Email").fill(t.email);
    await page.getByLabel("Password").fill(t.password);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByRole("heading", { name: "Today’s hisaab" })).toBeVisible();
  });

  test.afterAll(async () => {
    await page?.context().close();
    if (t) py(TEARDOWN_PY(t));
  });

  test("an ambiguous name shows chips; tapping one answers for that customer", async () => {
    const panel = panelOf(page);
    await ask(page, "Asha ne kitna diya?", "ambiguous Asha");
    const chips = panel.getByRole("group", { name: "Which customer?" });
    await expect(chips).toBeVisible();
    await expect(chips.getByRole("button")).toHaveCount(2);
    await shot(page, "m6", "r8-04-ambiguous-chips");
    const before = await panel.locator("p.leading-relaxed").count();
    await chips.getByRole("button", { name: /^Asha Test/ }).click();
    const answer = panel.locator("p.leading-relaxed").nth(before);
    await expect(answer).toBeVisible({ timeout: 60_000 });
    await expect(answer).toContainText("501"); // ₹250 + ₹251
    await expect(panel.getByText("“Asha ne kitna diya? (Asha Test)”")).toBeVisible();
  });

  test("an invoice by chat: nothing until Confirm, an expired card says so, a fresh one creates it", async () => {
    const panel = panelOf(page);
    const count = () => Number(py(SQL_PY(`select count(*) from invoices where merchant_id = '${t.merchant_id}'`)));
    const asked = page.waitForResponse((r) => r.url().endsWith("/ask") && r.status() === 200, { timeout: 60_000 });
    await ask(page, "Asha Test ko 300 rupaye ka invoice bhejo, 5 November tak due", "invoice by chat");
    const token = (await (await asked).json()).pending_action?.token as string;
    expect(token).toMatch(/^pa_/);
    const confirm = panel.getByRole("button", { name: "Confirm: Create invoice" });
    await expect(confirm).toBeVisible();
    await expect(panel.getByText("Needs your OK")).toBeVisible();
    await expect(panel.getByText("Amount: ₹300")).toBeVisible();
    expect(count()).toBe(0);
    await shot(page, "m6", "r8-05-invoice-confirm-card");

    py(SQL_PY(`update pending_actions set expires_at = app_now() - interval '1 minute' where token = '${token}'`));
    await confirm.click();
    await expect(panel.getByRole("alert").filter({ hasText: "This action expired" })).toBeVisible();
    expect(count()).toBe(0);

    py(SQL_PY(`update pending_actions set expires_at = app_now() + interval '10 minutes' where token = '${token}'`));
    await confirm.click();
    await expect(panel.getByText(/Invoice INV-\d+ created and emailed/)).toBeVisible({ timeout: 30_000 });
    await expect(panel.getByRole("link", { name: "Open" })).toBeVisible();
    expect(count()).toBe(1);
    await shot(page, "m6", "r8-06-invoice-confirmed");
  });

  test("Cancel on a recurring card writes nothing", async () => {
    const panel = panelOf(page);
    await ask(page, "Asha T (UPI) ko weekly 200 rupaye ka recurring mark karo", "recurring by chat");
    await expect(panel.getByRole("button", { name: "Confirm: Mark recurring" })).toBeVisible();
    await panel.getByRole("button", { name: "Cancel" }).click();
    await expect(panel.getByText("Cancelled. Nothing was sent.")).toBeVisible();
    expect(py(SQL_PY(`select count(*) from recurring_schedules where merchant_id = '${t.merchant_id}'`))).toBe("0");
  });
});

// ---------------------------------------------------------------------------
// Panel placement and keyboard basics (no model calls)
// ---------------------------------------------------------------------------
test.describe("tester r8 · M6 panel placement and keyboard", () => {
  test("xl: docked beside the page, no open button and no close button", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto("/");
    const panel = panelOf(page);
    await expect(panel).toBeVisible();
    await expect(page.getByRole("button", { name: "Ask Hisaab" })).toBeHidden();
    await expect(panel.getByRole("button", { name: "Close Ask Hisaab" })).toBeHidden();
    const box = (await panel.boundingBox())!;
    expect(Math.round(box.x + box.width)).toBe(1440);
    expect(Math.round(box.width)).toBe(400);
    await expect(panel.getByRole("button", { name: "Hold to speak" })).toBeVisible();
    await expect(panel.getByLabel("Type a question")).toBeVisible();
  });

  for (const [w, h, opener] of [[1100, 900, "Ask Hisaab"], [390, 844, "Ask"]] as const) {
    test(`${w}px: the ${opener} button opens a full panel without sideways scroll and Close hides it`, async ({ page }) => {
      await page.setViewportSize({ width: w, height: h });
      await page.goto("/");
      const panel = panelOf(page);
      await expect(panel).toBeHidden();
      await page.getByRole("button", { name: opener, exact: true }).click();
      await expect(panel).toBeVisible();
      const box = (await panel.boundingBox())!;
      expect(Math.round(box.width)).toBe(w);
      await expect(panel.getByLabel("Type a question")).toBeInViewport();
      await expect(panel.getByRole("button", { name: "Hold to speak" })).toBeInViewport();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
      await shot(page, "m6", `r8-07-panel-${w}`);
      await panel.getByRole("button", { name: "Close Ask Hisaab" }).click();
      await expect(panel).toBeHidden();
    });
  }

  test("keyboard: opening the overlay panel moves focus into it and Escape closes it", async ({ page }) => {
    await page.setViewportSize({ width: 1100, height: 900 });
    await page.goto("/");
    const opener = page.getByRole("button", { name: "Ask Hisaab", exact: true });
    await opener.focus();
    await page.keyboard.press("Enter");
    const panel = panelOf(page);
    await expect(panel).toBeVisible();
    expect(await panel.evaluate((el) => el.contains(document.activeElement))).toBe(true);
    await page.keyboard.press("Escape");
    await expect(panel).toBeHidden();
  });

  test("controls have accessible names and answers land in a live region", async ({ page }) => {
    await page.goto("/");
    const panel = panelOf(page);
    await expect(panel.getByRole("heading", { name: "Ask Hisaab" })).toBeVisible();
    await expect(panel.getByRole("button", { name: "Hold to speak" })).toBeVisible();
    await expect(panel.getByRole("textbox", { name: "Type a question" })).toBeVisible();
    await expect(panel.getByRole("button", { name: "Send question" })).toBeDisabled();
    await expect(panel.locator('[aria-live="polite"]')).toHaveCount(1);
  });
});

test.describe("tester r8 · M6 with the microphone blocked", () => {
  test.use({ permissions: [] });

  test("a blocked mic explains itself and points to typing", async ({ page }) => {
    await page.goto("/");
    const panel = panelOf(page);
    await panel.getByRole("button", { name: "Hold to speak" }).hover();
    await page.mouse.down();
    await page.mouse.up();
    await expect(panel.getByRole("alert")).toContainText(/type your question/i);
    await expect(panel.getByLabel("Type a question")).toBeEditable();
  });
});
