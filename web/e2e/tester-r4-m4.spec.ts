import { execFileSync } from "node:child_process";
import { join } from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { EVIDENCE, reseed, watchErrors } from "./helpers";

/**
 * Tester round 4, M4 regression with the M5 clock and jobs in the loop.
 * Spec: ARCHITECTURE.md moment 2 ("the payment is made, and the invoice flips to Paid live"),
 * Collections flow 5 ("Realtime flips the dashboard"), Risks ("The Paytm webhook may never arrive:
 * a 'Check payment' button and cron polling cover it"), pitch deck slide 6 (Pending payments).
 */

async function shot(page: Page, name: string) {
  await page.screenshot({ path: join(EVIDENCE, "m4", `${name}.png`), fullPage: true });
}

async function newInvoice(page: Page, customer: string, item: string, rupees: string) {
  await page.goto("/collections");
  await page.getByRole("button", { name: "New invoice" }).click();
  const dialog = page.getByRole("dialog", { name: "New invoice" });
  await dialog.getByRole("searchbox").fill(customer);
  await dialog.getByRole("button", { name: new RegExp(customer) }).first().click();
  await dialog.getByLabel("Item 1 description").fill(item);
  await dialog.getByLabel("Item 1 price in rupees").fill(rupees);
  const due = await dialog.getByLabel("Due date").inputValue();
  await dialog.getByRole("button", { name: /^Create/ }).click();
  await expect(page).toHaveURL(/\/collections\/[0-9a-f-]{36}$/);
  const heading = page.getByRole("heading", { name: /^Invoice INV-\d{4}$/ });
  await expect(heading).toBeVisible();
  const number = ((await heading.textContent()) ?? "").replace("Invoice ", "");
  const link = (await page.getByRole("link", { name: "Open payment link" }).getAttribute("href")) ?? "";
  return { url: page.url(), number, link, due };
}

test.describe("tester r4 · M4 regression on the demo clock", () => {
  test.describe.configure({ timeout: 120_000 });
  // Supabase Realtime delivers the seed's ~7.8k transaction INSERTs to pages opened in the next ~10 s,
  // and every event triggers a full refetch (see the r4 report). Let that backlog drain first.
  test.beforeEach(async () => {
    reseed();
    await new Promise((r) => setTimeout(r, 15_000));
  });
  test.afterAll(() => reseed());

  test("after a clock jump, a new invoice defaults to app-today + 7 and flips Paid live everywhere", async ({ page, context }) => {
    const errors = watchErrors(page);
    await page.goto("/");
    const clock = page.getByTestId("demo-clock-now").first();
    const before = (await clock.textContent()) ?? "";
    await page.getByRole("button", { name: "+7 days" }).click();
    await expect(clock).not.toHaveText(before, { timeout: 20_000 });
    const inv = await newInvoice(page, "Hema Bhat", "Weekly ration", "777");
    // The form's default due date follows the demo clock: issued (app now) + 7 days.
    const paper = page.getByRole("article", { name: `Invoice ${inv.number}` });
    const issued = ((await paper.getByText(/^Issued /).textContent()) ?? "").replace("Issued ", "");
    const issuedDay = new Date(`${issued} 12:00 UTC`);
    const wallToday = new Date(Date.now() + 5.5 * 3600_000).toISOString().slice(0, 10);
    expect(issuedDay.toISOString().slice(0, 10)).not.toBe(wallToday);
    issuedDay.setUTCDate(issuedDay.getUTCDate() + 7);
    expect(inv.due).toBe(issuedDay.toISOString().slice(0, 10));
    await expect(page.getByTestId("invoice-status")).toHaveText("Due in 7 days");

    const dashboard = await context.newPage();
    const dErrors = watchErrors(dashboard);
    await dashboard.goto("/");
    const pending = dashboard.locator("section", { hasText: "Pending payments" });
    await expect(pending.getByRole("row", { name: new RegExp(`Hema Bhat.*${inv.number}.*₹777.*Due in 7 days`) })).toBeVisible();
    const total = await pending.getByText(/outstanding ·/).textContent();

    const payer = await context.newPage();
    await payer.goto(inv.link);
    await payer.getByRole("button", { name: "Pay ₹777" }).click();
    await expect(payer.getByText("Payment successful")).toBeVisible();

    await expect(page.getByTestId("invoice-status")).toHaveText("Paid · auto-matched", { timeout: 15_000 });
    await expect(pending.getByRole("row", { name: new RegExp(`Hema Bhat.*${inv.number}.*Paid · auto-matched`) })).toBeVisible({
      timeout: 15_000,
    });
    await expect(pending.getByText(/outstanding ·/)).not.toHaveText(total ?? "");
    await shot(dashboard, "r4-e2e-01-dashboard-paid-live-after-clock-jump");
    await shot(page, "r4-e2e-02-invoice-paid-live-after-clock-jump");
    expect([...errors, ...dErrors]).toEqual([]);
  });

  test("a dropped webhook is settled by the jobs the next clock jump runs (cron status polling)", async ({ page, context }) => {
    const inv = await newInvoice(page, "Suresh Bhatt", "Rice sack", "1450");
    const payer = await context.newPage();
    await payer.goto(inv.link);
    await payer.getByRole("button", { name: "Pay, but drop the merchant notification" }).click();
    await expect(payer.getByText(/webhook dropped on purpose/)).toBeVisible();
    await page.waitForTimeout(1500);
    await expect(page.getByTestId("invoice-status")).toHaveText("Due in 7 days");
    await page.goto("/");
    await page.getByRole("button", { name: "+1 day" }).click();
    const pending = page.locator("section", { hasText: "Pending payments" });
    await expect(pending.getByRole("row", { name: new RegExp(`Suresh Bhatt.*${inv.number}.*Paid · auto-matched`) })).toBeVisible({
      timeout: 20_000,
    });
    await shot(page, "r4-e2e-03-dropped-webhook-settled-by-tick");
    await page.goto(inv.url);
    await expect(page.getByTestId("invoice-status")).toHaveText("Paid · auto-matched");
    await expect(page.getByText("Paid and matched")).toBeVisible();
  });

  test("overdue reminder from Collections uses the same link; the bulk button counts real overdue rows", async ({ page }) => {
    await page.goto("/collections");
    const sharma = page.getByRole("row", { name: /Sharma Traders.*INV-0412.*Overdue · \d+ days/ });
    await expect(sharma).toBeVisible();
    await sharma.getByRole("button", { name: "Send reminder" }).click();
    await expect(sharma.getByRole("status")).toHaveText("Reminder sent to accounts@sharmatraders.in");
    const overdueRows = page.getByRole("row").filter({ hasText: /Overdue ·/ });
    const n = await overdueRows.count();
    await expect(page.getByRole("button", { name: new RegExp(`Send ${n} overdue reminders?`) })).toBeVisible();
    await shot(page, "r4-e2e-04-collections-reminder");
  });
});

test.describe("tester r4 · M4 live updates under a burst of inserts", () => {
  test.describe.configure({ timeout: 120_000 });
  test.beforeAll(async () => {
    reseed();
    await new Promise((r) => setTimeout(r, 15_000));
  });
  test.afterAll(async () => {
    reseed();
    await new Promise((r) => setTimeout(r, 15_000)); // let the next spec start after the seed's realtime backlog
  });

  test("a burst of 2,000 inserted payments does not flood the API or break the open dashboard", async ({ page }) => {
    let apiCalls = 0;
    page.on("request", (r) => {
      if (r.url().includes(":8000/dashboard/summary")) apiCalls++;
    });
    const errors = watchErrors(page);
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Today’s hisaab" })).toBeVisible();
    await page.waitForTimeout(3_000);
    const before = apiCalls;
    // One bulk write (an import, a back-fill, a re-seed): 2,000 rows in one statement.
    execFileSync("docker", ["exec", "-i", "supabase_db_hisaab-bolo", "psql", "-U", "postgres", "-d", "postgres", "-c",
      "insert into transactions (merchant_id, amount_paise, paid_at, source, description, description_by) " +
      "select m.id, 1000, app_now() - interval '2 days', 'seed', 'Bulk import', 'merchant' " +
      "from merchants m, generate_series(1, 2000) where m.owner_user_id = " +
      "(select id from auth.users where email = 'demo@hisaabbolo.test')"], { stdio: "ignore" });
    await page.waitForTimeout(12_000);
    await shot(page, "r4-e2e-05-dashboard-after-bulk-insert");
    const calls = apiCalls - before;
    await expect.soft(page.getByText(/Couldn't load today's numbers/), "dashboard left in its error state").toHaveCount(0);
    expect.soft(calls, "GET /dashboard/summary calls after one bulk insert").toBeLessThan(20);
    expect(errors.filter((e) => e.includes("ERR_INSUFFICIENT_RESOURCES")).length).toBe(0);
  });
});
