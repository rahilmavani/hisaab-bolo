import { join } from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { EVIDENCE, reseed, watchErrors } from "./helpers";

/**
 * Tester round 4, M5 UI: Mark recurring (with / without email), dues on the profile, the demo clock
 * (+1 / +7 days, reset), the missed due going overdue with one reminder, Stop recurring, the
 * suggestion chip.
 * Spec: ARCHITECTURE.md moment 4 ("the merchant marks a payer recurring, and a missed due triggers a
 * reminder email"), Decisions/Recurring ("An optional email popup appears when marking recurring.
 * With no email, the schedule only tracks"), Collections flow ("Overdue (due + grace) triggers a
 * reminder email"), Risks ("The recurring demo needs time to pass: the clock jump").
 * Every test starts from a fresh seed (the demo clock pinned where the seed put it).
 */

async function shot(page: Page, name: string) {
  await page.screenshot({ path: join(EVIDENCE, "m5", `${name}.png`), fullPage: true });
}

async function openCustomer(page: Page, name: string) {
  await page.goto("/customers");
  await page.getByRole("searchbox").fill(name);
  await page.getByRole("link", { name: new RegExp(`^${name}`) }).first().click();
  await expect(page.getByRole("heading", { name, exact: true })).toBeVisible();
}

const recurringCard = (page: Page) => page.locator("section", { has: page.getByRole("heading", { name: "Recurring", exact: true }) });
const invoicesCard = (page: Page) => page.locator("section", { has: page.getByRole("heading", { name: "Invoices", exact: true }) });
const clockStatus = (page: Page) => page.getByRole("region", { name: "Demo clock" }).getByRole("status");

test.describe("tester r4 · M5 recurring, demo clock, auto-reminder", () => {
  test.describe.configure({ timeout: 120_000 });
  // Supabase Realtime delivers the seed's ~7.8k transaction INSERTs to pages opened in the next ~10 s,
  // and every event triggers a full refetch (see the r4 report). Let that backlog drain first.
  test.beforeEach(async () => {
    reseed();
    await new Promise((r) => setTimeout(r, 15_000));
  });
  test.afterAll(() => reseed());

  test("moment 4: mark recurring with email, jump +1/+7 days, one reminder for the missed due, stop", async ({ page }) => {
    const errors = watchErrors(page);
    await openCustomer(page, "Usha Das");
    await page.getByRole("button", { name: "Mark recurring" }).click();
    const dialog = page.getByRole("dialog", { name: "Mark Usha Das as recurring" });
    await dialog.getByRole("radio", { name: /Weekly/ }).click();
    await dialog.getByLabel("Amount (₹)").fill("500");
    await expect(dialog.getByLabel(/Email for invoices/)).toHaveValue("usha.das@example.com");
    await expect(dialog.getByText(/a reminder goes out/)).toBeVisible();
    await dialog.getByRole("button", { name: "Mark recurring" }).click();
    await expect(dialog).toBeHidden();
    const created = recurringCard(page).getByRole("status");
    await expect(created).toHaveText(/^INV-\d{4} for \d+ \w+ created and emailed\.$/);
    const number = ((await created.textContent()) ?? "").split(" ")[0];
    await expect(recurringCard(page)).toContainText("₹500 weekly");
    await expect(recurringCard(page)).toContainText(/Invoices and overdue reminders go to \S+@\S+/);
    await expect(invoicesCard(page).getByRole("listitem").filter({ hasText: number })).toContainText("Due today");
    await expect.soft(page.getByText("No payments from this customer yet."), "payments table emptied by Mark recurring").toHaveCount(0);
    await shot(page, "r4-e2e-01-usha-marked-recurring");

    // +1 day: inside the 3-day grace, so nothing is overdue yet and no reminder goes out.
    await page.getByRole("button", { name: "+1 day" }).click();
    await expect(clockStatus(page)).toHaveText("Jobs ran; nothing new was due.", { timeout: 20_000 });
    const row = invoicesCard(page).getByRole("listitem").filter({ hasText: number });
    // Spec: overdue is "due + grace". The due is still within its grace period.
    await expect.soft(row, "a due inside its grace period is labelled Overdue").not.toContainText("Overdue");
    await shot(page, "r4-e2e-02-plus-1-day-in-grace");

    await page.getByRole("button", { name: "+7 days" }).click();
    await expect(clockStatus(page)).toContainText(new RegExp(`reminder sent for .*${number}`), { timeout: 20_000 });
    await expect(row).toContainText(/Overdue · 8 days/);
    await shot(page, "r4-e2e-03-plus-7-days-reminded");

    await row.getByRole("link", { name: number }).click();
    await expect(page.getByTestId("invoice-status")).toHaveText("Overdue · 8 days");
    await expect(page.getByText("Overdue reminder emailed")).toHaveCount(1);
    await expect(page.getByText(/to usha\.das@example\.com/).first()).toBeVisible();
    // Another jump does not send a second auto-reminder for the same missed due.
    await page.getByRole("button", { name: "+1 day" }).click();
    await expect(clockStatus(page)).toBeVisible({ timeout: 20_000 });
    await page.reload();
    await expect(page.getByText("Overdue reminder emailed")).toHaveCount(1);
    await shot(page, "r4-e2e-04-one-overdue-reminder");

    await openCustomer(page, "Usha Das");
    await recurringCard(page).getByRole("button", { name: "Stop recurring" }).click();
    await expect(recurringCard(page).getByRole("status")).toHaveText("Recurring stopped. Open invoices stay payable; no more automatic emails go out.");
    await expect(recurringCard(page).getByRole("button", { name: "Mark recurring" })).toBeVisible();
    await page.getByRole("button", { name: "Reset demo clock" }).click();
    await expect(page.getByRole("button", { name: "Reset demo clock" })).toHaveCount(0, { timeout: 20_000 });
    expect(errors).toEqual([]);
  });

  test("clearing the email in the popup means nothing is sent, as the popup says", async ({ page }) => {
    await openCustomer(page, "Kavita Verma");
    await page.getByRole("button", { name: "Mark recurring" }).click();
    const dialog = page.getByRole("dialog", { name: "Mark Kavita Verma as recurring" });
    await dialog.getByLabel(/Email for invoices/).fill("");
    await expect(dialog.getByText(/Without an email, Hisaab tracks each due and flags missed payments; nothing is sent\./)).toBeVisible();
    await shot(page, "r4-e2e-05-cleared-email-promises-nothing-sent");
    await dialog.getByRole("button", { name: "Mark recurring" }).click();
    await expect(dialog).toBeHidden();
    await shot(page, "r4-e2e-06-cleared-email-result");
    await expect(recurringCard(page).getByRole("status"), "the popup promised 'nothing is sent'").not.toContainText("emailed");
    await expect(recurringCard(page)).toContainText("Tracking only");
  });

  test("tracking-only schedule with a later first due keeps the customer's payments on screen", async ({ page }) => {
    await openCustomer(page, "Gopal Sinha");
    await expect(page.getByRole("button", { name: /Show all \d+ payments/ })).toBeVisible();
    await page.getByRole("button", { name: "Mark recurring" }).click();
    const dialog = page.getByRole("dialog", { name: "Mark Gopal Sinha as recurring" });
    await dialog.getByRole("radio", { name: /Weekly/ }).click();
    await dialog.getByLabel("Amount (₹)").fill("400");
    const first = dialog.getByLabel("First due");
    const today = await first.inputValue();
    const later = new Date(`${today}T12:00:00Z`);
    later.setUTCDate(later.getUTCDate() + 10);
    await first.fill(later.toISOString().slice(0, 10));
    await dialog.getByRole("button", { name: "Mark recurring" }).click();
    await expect(recurringCard(page).getByRole("status")).toHaveText(/^Tracking weekly dues\./);
    await expect(recurringCard(page)).toContainText("Tracking only: dues and missed payments, no emails.");
    await page.waitForTimeout(1500);
    await shot(page, "r4-e2e-07-tracking-only-payments-table");
    await expect(page.getByText("No payments from this customer yet.")).toHaveCount(0);
    await expect(page.getByRole("button", { name: /Show all \d+ payments/ })).toBeVisible();
  });

  test("tracking-only without email: the missed due is flagged overdue, nothing is emailed", async ({ page }) => {
    await openCustomer(page, "Gopal Sinha");
    await page.getByRole("button", { name: "Mark recurring" }).click();
    const dialog = page.getByRole("dialog", { name: "Mark Gopal Sinha as recurring" });
    await dialog.getByRole("radio", { name: /Weekly/ }).click();
    await dialog.getByLabel("Amount (₹)").fill("400");
    await dialog.getByLabel("Grace (days)").fill("1");
    await dialog.getByRole("button", { name: "Mark recurring" }).click();
    await expect(recurringCard(page).getByRole("status")).toHaveText(/^Tracking weekly dues\./);
    await page.getByRole("button", { name: "+7 days" }).click();
    await expect(clockStatus(page)).toContainText(/went overdue/, { timeout: 20_000 });
    await page.goto("/collections");
    const gopal = page.getByRole("row", { name: /Gopal Sinha/ }).first();
    await expect(gopal).toContainText(/Overdue · 7 days/);
    await expect(gopal).toContainText("Not emailed");
    await expect(gopal.getByRole("button", { name: "Send reminder" })).toBeDisabled();
    await shot(page, "r4-e2e-08-tracking-only-missed-due");
  });

  test("stop then mark recurring again the same day does not bill the same due twice", async ({ page }) => {
    await openCustomer(page, "Kavita Verma");
    for (let i = 0; i < 2; i++) {
      await recurringCard(page).getByRole("button", { name: "Mark recurring" }).click();
      const dialog = page.getByRole("dialog", { name: "Mark Kavita Verma as recurring" });
      await dialog.getByLabel("Amount (₹)").fill("2000");
      await dialog.getByRole("button", { name: "Mark recurring" }).click();
      // The second time, the same day's due already exists: it is reported, not billed again.
      await expect(recurringCard(page).getByRole("status")).toContainText(
        i === 0 ? "created and emailed" : "is already invoiced",
      );
      if (i === 0) {
        await recurringCard(page).getByRole("button", { name: "Stop recurring" }).click();
        await expect(recurringCard(page).getByRole("status")).toContainText("Recurring stopped");
      }
    }
    await page.reload();
    await shot(page, "r4-e2e-09-stop-then-remark");
    await expect(invoicesCard(page).getByRole("listitem").filter({ hasText: "Due today" })).toHaveCount(1);
  });

  test("the suggestion chip pre-fills the dialog (Ram: ₹100 daily → ₹3,000 monthly)", async ({ page }) => {
    await openCustomer(page, "Ram");
    await expect(recurringCard(page)).toContainText("Ram pays a steady ₹100 every day. Track it as ₹3,000 monthly?");
    await recurringCard(page).getByRole("button", { name: "Mark recurring" }).click();
    const dialog = page.getByRole("dialog", { name: "Mark Ram as recurring" });
    await expect(dialog.getByRole("radio", { name: /Monthly/ })).toHaveAttribute("aria-checked", "true");
    await expect(dialog.getByLabel("Amount (₹)")).toHaveValue("3000");
    await shot(page, "r4-e2e-10-ram-suggestion");
  });

  test("demo clock reset returns to where the seed put the clock", async ({ page }) => {
    await page.goto("/");
    const label = page.getByTestId("demo-clock-now").first();
    await expect(label).not.toHaveText("");
    const before = (await label.textContent()) ?? "";
    // A freshly seeded clock is not "moved" (the seed's instant is the reset point), pinned or not.
    await expect(page.getByText("Demo clock (moved)")).toHaveCount(0);
    const collected = await page.getByText("Collected today").locator("..").textContent();
    await page.getByRole("button", { name: "+1 day" }).click();
    await expect(label).not.toHaveText(before, { timeout: 20_000 });
    await page.getByRole("button", { name: "Reset demo clock" }).click();
    await expect(page.getByRole("button", { name: "+1 day" })).toBeEnabled({ timeout: 20_000 });
    await page.reload();
    await shot(page, "r4-e2e-11-after-reset");
    // Same day and hour as the seeded instant (minutes keep ticking).
    const sameHour = new RegExp(before.replace(/:\d\d /, ":\\d\\d ").replace(/[()]/g, ""));
    await expect.soft(label, `seeded at "${before}"`).toHaveText(sameHour);
    await expect(page.getByText("Collected today").locator(".."), "today's seeded takings after reset").toHaveText(collected ?? "");
  });
});
