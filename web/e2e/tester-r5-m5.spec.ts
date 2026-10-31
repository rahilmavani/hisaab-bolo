import { join } from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { EVIDENCE, reseed, watchErrors } from "./helpers";

/**
 * Tester round 5, M5: moment 4 with an edited schedule, the monthly day-of-month after an edit,
 * one reminder across clock jumps, tracking-only (Ram) and Stop.
 * Spec: ARCHITECTURE.md moment 4 ("the merchant marks a payer recurring, and a missed due triggers a
 * reminder email"), Decisions/Recurring, Collections flow, Risks ("the clock jump").
 * UI copy: recurring-card.tsx "Invoices and overdue reminders go to {email}."; recurring-dialog.tsx
 * "Monthly · Same date each month".
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
const clockStatus = (page: Page) => page.getByRole("region", { name: "Demo clock" }).first().getByRole("status");

async function jump(page: Page, label: "+1 day" | "+7 days") {
  const now = page.getByTestId("demo-clock-now").first();
  const before = (await now.textContent()) ?? "";
  await page.getByRole("button", { name: label }).first().click();
  await expect(now).not.toHaveText(before, { timeout: 20_000 });
  await expect(page.getByRole("button", { name: label }).first()).toBeEnabled({ timeout: 20_000 });
}

test.describe("tester r5 · M5 recurring edits, clock jumps, reminders", () => {
  test.describe.configure({ timeout: 180_000 });
  test.beforeEach(async () => {
    reseed();
    await new Promise((r) => setTimeout(r, 3_000));
  });
  test.afterAll(() => reseed());

  test("moment 4: mark Usha recurring, change the schedule's email, the missed due's reminder goes to the new email", async ({ page }) => {
    const errors = watchErrors(page);
    await openCustomer(page, "Usha Das");
    await page.getByRole("button", { name: "Mark recurring" }).click();
    const dialog = page.getByRole("dialog", { name: "Mark Usha Das as recurring" });
    await dialog.getByRole("radio", { name: /Weekly/ }).click();
    await dialog.getByLabel("Amount (₹)").fill("500");
    await dialog.getByRole("button", { name: "Mark recurring" }).click();
    const note = recurringCard(page).getByRole("status");
    await expect(note).toHaveText(/^INV-\d{4} for \d+ \w+ created and emailed\.$/);
    const number = ((await note.textContent()) ?? "").split(" ")[0];

    await recurringCard(page).getByRole("button", { name: "Edit schedule" }).click();
    const edit = page.getByRole("dialog", { name: "Edit Usha Das's schedule" });
    await edit.getByLabel(/Email for invoices/).fill("usha.new@example.com");
    await edit.getByRole("button", { name: "Save schedule" }).click();
    await expect(recurringCard(page)).toContainText("Invoices and overdue reminders go to usha.new@example.com.");
    await shot(page, "r5-e2e-01-schedule-email-edited");

    await jump(page, "+7 days");
    await expect(clockStatus(page)).toContainText(new RegExp(`reminder sent for .*${number}`, "i"));
    await invoicesCard(page).getByRole("link", { name: number }).click();
    await expect(page.getByText("Overdue reminder emailed")).toHaveCount(1);
    await shot(page, "r5-e2e-02-reminder-recipient-after-edit");
    const history = page.locator("section", { hasText: "History" });
    await expect(history, "the card promised reminders go to usha.new@example.com").toContainText(
      /Overdue reminder emailed .* to usha\.new@example\.com/,
    );
    expect(errors).toEqual([]);
  });

  test("a monthly schedule on the 31st still bills the 31st after an amount-only edit", async ({ page }) => {
    await openCustomer(page, "Usha Das");
    await page.getByRole("button", { name: "Mark recurring" }).click();
    const dialog = page.getByRole("dialog", { name: "Mark Usha Das as recurring" });
    await dialog.getByRole("radio", { name: /Monthly/ }).click();
    const first = await dialog.getByLabel("First due").inputValue();
    test.skip(!first.endsWith("-31") || !["2026-10-31", "2027-01-31", "2027-03-31", "2027-05-31", "2027-08-31"].includes(first),
      `needs the demo clock on a 31st followed by a shorter month (today is ${first})`);
    await dialog.getByLabel("Amount (₹)").fill("2700");
    await dialog.getByRole("button", { name: "Mark recurring" }).click();
    await expect(recurringCard(page)).toContainText(/Next due 30 \w+/);
    await recurringCard(page).getByRole("button", { name: "Edit schedule" }).click();
    const edit = page.getByRole("dialog", { name: "Edit Usha Das's schedule" });
    await edit.getByLabel("Amount (₹)").fill("2800");
    await edit.getByRole("button", { name: "Save schedule" }).click();
    await expect(recurringCard(page)).toContainText("₹2,800 monthly");
    // Walk the clock to 3 days before the 31st two months on, so that month's due is created.
    for (let i = 0; i < 8; i++) await jump(page, "+7 days");
    await jump(page, "+1 day");
    await jump(page, "+1 day");
    await page.reload();
    await shot(page, "r5-e2e-03-monthly-dues-after-edit");
    const dues = (await invoicesCard(page).getByText(/^Due \d+ \w+$/).allTextContents()).join(", ");
    expect(dues, "Monthly is 'Same date each month'; the edit moved the anchor from the 31st to the 30th").toMatch(/Due 31 Dec/);
  });

  test("tracking-only Ram: dues and a missed flag, no emails; Stop keeps existing dues; one reminder total for a missed due", async ({ page }) => {
    const errors = watchErrors(page);
    await openCustomer(page, "Ram");
    const profile = page.url();
    await expect(recurringCard(page)).toContainText(/Ram pays a steady ₹100 every day\. Track it as ₹3,000 monthly\?/);
    await page.getByRole("button", { name: "Mark recurring" }).click();
    const dialog = page.getByRole("dialog", { name: "Mark Ram as recurring" });
    await expect(dialog.getByLabel(/Email for invoices/)).toHaveValue("");
    await expect(dialog.getByText("Without an email, Hisaab tracks each due and flags missed payments; nothing is sent.")).toBeVisible();
    await dialog.getByRole("button", { name: "Mark recurring" }).click();
    await expect(recurringCard(page)).toContainText("Tracking only");
    const row = invoicesCard(page).getByRole("listitem").first();
    await expect(row).toContainText("Due today");
    const number = ((await row.getByRole("link").textContent()) ?? "").trim();

    await jump(page, "+7 days");
    await expect(row).toContainText(/7 days overdue/);
    await jump(page, "+7 days");
    await row.getByRole("link", { name: number }).click();
    await expect(page.getByText(/emailed/)).toHaveCount(0);
    await page.goto(profile);
    await recurringCard(page).getByRole("button", { name: "Stop recurring" }).click();
    await expect(recurringCard(page).getByRole("status")).toHaveText("Recurring stopped. Open invoices stay payable; no more automatic emails go out.");
    await expect(invoicesCard(page).getByRole("listitem").filter({ hasText: number })).toBeVisible();
    await shot(page, "r5-e2e-04-ram-tracking-only-stopped");
    expect(errors).toEqual([]);
  });
});
