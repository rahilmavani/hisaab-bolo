import { join } from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { EVIDENCE, reseed, watchErrors } from "./helpers";

/**
 * Tester round 7, M5: demo moment 4 driven with "+1 day" then "+7 days", and a manual reminder sent while
 * a due is inside its grace period.
 * Spec: ARCHITECTURE.md moment 4 ("the merchant marks a payer recurring, and a missed due triggers a
 * reminder email"), Decisions/Recurring ("Track, auto-create dues and auto-remind"), Collections flow
 * ("Recurring schedules with an email create invoices per due date"), Risks ("the clock jump").
 * UI copy under test:
 *   recurring-dialog.tsx  "Each due gets an invoice with a Paytm link and QR, emailed to {email} 3 days ahead.
 *                          If it's still unpaid {grace} days after the due date, a reminder goes out the
 *                          following day."
 *   recurring-card.tsx    "Invoices and overdue reminders go to {email}."
 *   collections/[id]      "Not emailed yet: the invoice email to {email} hasn't gone out."
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

async function markWeekly(page: Page, name: string, rupees: string, grace: string, email: string) {
  await openCustomer(page, name);
  await page.getByRole("button", { name: "Mark recurring" }).click();
  const dialog = page.getByRole("dialog", { name: `Mark ${name} as recurring` });
  await dialog.getByRole("radio", { name: /Weekly/ }).click();
  await dialog.getByLabel("Amount (₹)").fill(rupees);
  await dialog.getByLabel("Grace (days)").fill(grace);
  await dialog.getByLabel(/Email for invoices and reminders/).fill(email);
  await expect(dialog).toContainText(`Each due gets an invoice with a Paytm link and QR, emailed to ${email}`);
  await dialog.getByRole("button", { name: "Mark recurring" }).click();
  const note = recurringCard(page).getByRole("status");
  await expect(note).toHaveText(/^INV-\d{4} for \d+ \w+ created and emailed\.$/);
  await expect(recurringCard(page)).toContainText(`Invoices and overdue reminders go to ${email}.`);
  return ((await note.textContent()) ?? "").split(" ")[0];
}

test.describe("tester r7 · M5 dues billed by a clock jump, manual nudge in grace", () => {
  test.describe.configure({ timeout: 180_000 });
  test.beforeEach(async () => {
    reseed();
    await new Promise((r) => setTimeout(r, 3_000));
  });
  test.afterAll(() => reseed());

  test("moment 4 with +1 day then +7 days: the missed due gets one reminder and the next due is emailed", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 950 });
    const errors = watchErrors(page);
    const first = await markWeekly(page, "Kavita Verma", "600", "3", "kavita.verma@example.com");

    await jump(page, "+1 day");
    await jump(page, "+7 days");
    await expect(clockStatus(page)).toContainText(`reminder sent for ${first}`);
    await page.reload();
    const rows = invoicesCard(page).getByRole("listitem");
    await expect(rows).toHaveCount(2);
    const second = ((await rows.first().getByRole("link").textContent()) ?? "").trim();
    expect(second).not.toBe(first);

    await invoicesCard(page).getByRole("link", { name: first }).click();
    await expect(page.getByTestId("invoice-status")).toHaveText(/Overdue/);
    await expect(page.getByText("Overdue reminder emailed")).toHaveCount(1);
    await page.goBack();

    await invoicesCard(page).getByRole("link", { name: second }).click();
    await expect(page.getByTestId("invoice-status")).toHaveText(/Past due · 1 day, in grace/);
    await shot(page, "r7-e2e-01-due-billed-by-clock-jump");
    await expect(
      page.getByText(/^Not emailed yet/),
      `${second} was created by the +7 day jump, one day past its due date and inside its 3-day grace; ` +
        "the popup promised each due is emailed, but it never was",
    ).toHaveCount(0);
    await expect(page.getByText(/Invoice emailed .* to kavita\.verma@example\.com/)).toHaveCount(1);
    expect(errors).toEqual([]);
  });

  test("a reminder sent by hand inside the grace period does not cancel the promised automatic one", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 950 });
    const first = await markWeekly(page, "Naveen Dubey", "700", "3", "naveen.dubey@example.com");

    await jump(page, "+1 day");
    await page.reload();
    await invoicesCard(page).getByRole("link", { name: first }).click();
    await expect(page.getByTestId("invoice-status")).toHaveText("Past due · 1 day, in grace");
    await page.getByRole("button", { name: "Send reminder" }).click();
    await expect(page.getByRole("status").filter({ hasText: "emailed to naveen.dubey@example.com" })).toBeVisible();

    await jump(page, "+1 day");
    await jump(page, "+1 day");
    await jump(page, "+1 day"); // due + 3 days grace has passed: overdue now
    await page.reload();
    await expect(page.getByTestId("invoice-status")).toHaveText(/^Overdue · 4 days$/);
    await shot(page, "r7-e2e-02-manual-nudge-in-grace-then-overdue");
    await expect(
      page.getByText("Overdue reminder emailed"),
      `${first} went overdue after its 3 days of grace but only the merchant's own nudge (sent in grace) is in its ` +
        "history; the popup promised a reminder the day after grace ends",
    ).toHaveCount(2);
  });
});
