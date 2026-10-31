import { join } from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { EVIDENCE, reseed, watchErrors } from "./helpers";

/**
 * Tester round 6, M5: Stop then Mark recurring again, more grace given in an edit, and the demo clock.
 * Spec: ARCHITECTURE.md moment 4 ("the merchant marks a payer recurring, and a missed due triggers a
 * reminder email"), Decisions/Recurring, Risks ("the clock jump").
 * UI copy: recurring-card.tsx "Invoices and overdue reminders go to {email}." and "{n} days grace";
 * recurring-dialog.tsx "{INV} for {d} is already invoiced." and "If it's still unpaid {grace} days after
 * the due date, a reminder goes out the following day."
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

const recurringCard = (page: Page) =>
  page.locator("section", { has: page.getByRole("heading", { name: "Recurring", exact: true }) });
const invoicesCard = (page: Page) =>
  page.locator("section", { has: page.getByRole("heading", { name: "Invoices", exact: true }) });
const clockStatus = (page: Page) => page.getByRole("region", { name: "Demo clock" }).first().getByRole("status");

async function jump(page: Page, label: "+1 day" | "+7 days") {
  const now = page.getByTestId("demo-clock-now").first();
  const before = (await now.textContent()) ?? "";
  await page.getByRole("button", { name: label }).first().click();
  await expect(now).not.toHaveText(before, { timeout: 20_000 });
  await expect(page.getByRole("button", { name: label }).first()).toBeEnabled({ timeout: 20_000 });
}

test.describe("tester r6 · M5 stop/re-mark, grace edits", () => {
  test.describe.configure({ timeout: 150_000 });
  test.beforeEach(async () => {
    reseed();
    await new Promise((r) => setTimeout(r, 3_000));
  });
  test.afterAll(() => reseed());

  test("Stop, then Mark recurring again: the due it reports as already invoiced is reminded when missed", async ({ page }) => {
    const errors = watchErrors(page);
    await openCustomer(page, "Kavita Verma");
    await page.getByRole("button", { name: "Mark recurring" }).click();
    let dialog = page.getByRole("dialog", { name: "Mark Kavita Verma as recurring" });
    await dialog.getByRole("radio", { name: /Weekly/ }).click();
    await dialog.getByLabel("Amount (₹)").fill("600");
    await dialog.getByLabel("Grace (days)").fill("2");
    await dialog.getByRole("button", { name: "Mark recurring" }).click();
    const note = recurringCard(page).getByRole("status");
    await expect(note).toHaveText(/^INV-\d{4} for \d+ \w+ created and emailed\.$/);
    const number = ((await note.textContent()) ?? "").split(" ")[0];

    await recurringCard(page).getByRole("button", { name: "Stop recurring" }).click();
    await expect(note).toHaveText("Recurring stopped. Open invoices stay payable; no more automatic emails go out.");
    await page.getByRole("button", { name: "Mark recurring" }).click();
    dialog = page.getByRole("dialog", { name: "Mark Kavita Verma as recurring" });
    await dialog.getByRole("radio", { name: /Weekly/ }).click();
    await dialog.getByLabel("Amount (₹)").fill("600");
    await dialog.getByLabel("Grace (days)").fill("2");
    await dialog.getByRole("button", { name: "Mark recurring" }).click();
    await expect(note).toHaveText(new RegExp(`^${number} for \\d+ \\w+ is already invoiced\\.$`));
    await expect(recurringCard(page)).toContainText("Invoices and overdue reminders go to kavita.verma@example.com.");
    await shot(page, "r6-e2e-01-re-marked-already-invoiced");

    await jump(page, "+7 days");
    await page.reload();
    await expect(invoicesCard(page).getByRole("listitem").filter({ hasText: number })).toContainText(/Overdue · 7 days/);
    await shot(page, "r6-e2e-02-re-marked-due-overdue");
    await invoicesCard(page).getByRole("link", { name: number }).click();
    await expect(page.getByTestId("invoice-status")).toHaveText(/Overdue/);
    await expect(
      page.getByText("Overdue reminder emailed"),
      `${number} is missed and the card says reminders go to kavita.verma@example.com, but no reminder was sent`,
    ).toHaveCount(1);
    expect(errors).toEqual([]);
  });

  test("giving more grace in Edit schedule holds the reminder for the due already billed", async ({ page }) => {
    await openCustomer(page, "Naveen Dubey");
    await page.getByRole("button", { name: "Mark recurring" }).click();
    const dialog = page.getByRole("dialog", { name: "Mark Naveen Dubey as recurring" });
    await dialog.getByRole("radio", { name: /Weekly/ }).click();
    await dialog.getByLabel("Amount (₹)").fill("700");
    await dialog.getByLabel("Grace (days)").fill("0");
    await dialog.getByRole("button", { name: "Mark recurring" }).click();
    const note = recurringCard(page).getByRole("status");
    await expect(note).toHaveText(/^INV-\d{4} for \d+ \w+ created and emailed\.$/);
    const number = ((await note.textContent()) ?? "").split(" ")[0];

    await recurringCard(page).getByRole("button", { name: "Edit schedule" }).click();
    const edit = page.getByRole("dialog", { name: "Edit Naveen Dubey's schedule" });
    await edit.getByLabel("Grace (days)").fill("5");
    await expect(edit).toContainText("If it's still unpaid 5 days after the due date, a reminder goes out the following day.");
    await edit.getByRole("button", { name: "Save schedule" }).click();
    await expect(recurringCard(page)).toContainText("5 days grace");

    await jump(page, "+1 day");
    await shot(page, "r6-e2e-03-grace-edit-after-one-day");
    await expect(
      clockStatus(page),
      "the merchant gave 5 days grace, yet the due billed before the edit was reminded the next day",
    ).not.toContainText(number);
    await page.reload();
    await expect(invoicesCard(page).getByRole("listitem").filter({ hasText: number })).not.toContainText("Overdue");
  });
});
