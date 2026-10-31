import { expect, test } from "@playwright/test";
import { reseed, shot, watchErrors } from "./helpers";

test.describe("M5 recurring payers, demo clock and auto-reminders", () => {
  test.afterAll(reseed);

  test.afterEach(async ({ page }) => {
    // Leave the shared demo clock at real time.
    await page.goto("/");
    const reset = page.getByRole("button", { name: "Reset demo clock" }).first();
    if (await reset.isVisible()) await reset.click();
  });

  test("mark Ram recurring with email, jump the clock, and the missed due sends a reminder", async ({ page }) => {
    const errors = watchErrors(page);
    await page.goto("/customers");
    await page.getByRole("searchbox").fill("ram");
    await page.getByRole("link", { name: /Ram · ram@paytm/ }).click();
    await page.getByRole("button", { name: "Mark recurring" }).click();
    const dialog = page.getByRole("dialog", { name: "Mark Ram as recurring" });
    await expect(dialog.getByRole("radio", { name: /Monthly/ })).toHaveAttribute("aria-checked", "true");
    await expect(dialog.getByLabel("Amount (₹)")).toHaveValue("3000");
    await dialog.getByLabel(/Email for invoices/).fill("ram.customer@example.com");
    await shot(page, "m5", "01-mark-recurring-dialog");
    await dialog.getByRole("button", { name: "Mark recurring" }).click();
    await expect(dialog).toBeHidden();
    await expect(page.getByRole("status").filter({ hasText: /INV-\d{4} for .* created and emailed/ })).toBeVisible();
    await expect(page.getByText("Recurring", { exact: true }).first()).toBeVisible();
    await expect(page.getByText(/₹3,000 monthly/)).toBeVisible();
    await shot(page, "m5", "02-ram-recurring");

    // Demo time jump: due today + 3 grace days → overdue on day 4 → reminder.
    await page.getByRole("button", { name: "+7 days" }).click();
    await expect(page.getByRole("status").filter({ hasText: /reminder sent for INV-\d{4}/ })).toBeVisible({ timeout: 20_000 });
    await expect(page.getByTestId("demo-clock-now").first()).not.toHaveText("");
    await shot(page, "m5", "03-clock-jump-reminder");

    await page.goto("/collections");
    const row = page.getByRole("row", { name: /Ram .*₹3,000.*Overdue · \d+ days/ });
    await expect(row).toBeVisible();
    await row.getByRole("link", { name: "Ram" }).click();
    await expect(page.getByText("Overdue reminder emailed")).toBeVisible();
    await shot(page, "m5", "04-recurring-invoice-overdue-reminded");
    expect(errors).toEqual([]);
  });

  test("recurring without an email only tracks dues", async ({ page }) => {
    await page.goto("/customers");
    await page.getByRole("searchbox").fill("meena");
    await page.getByRole("link", { name: /Meena Kapoor/ }).click();
    await page.getByRole("button", { name: "Mark recurring" }).click();
    const dialog = page.getByRole("dialog", { name: "Mark Meena Kapoor as recurring" });
    await dialog.getByRole("radio", { name: /Weekly/ }).click();
    await dialog.getByLabel("Amount (₹)").fill("350");
    await expect(dialog.getByText(/Without an email, Hisaab tracks each due/)).toBeVisible();
    await dialog.getByRole("button", { name: "Mark recurring" }).click();
    await expect(page.getByRole("status").filter({ hasText: "Tracking weekly dues" })).toBeVisible();
    await expect(page.getByText(/Tracking only/)).toBeVisible();
    await page.getByRole("button", { name: "Stop recurring" }).click();
    await expect(page.getByRole("button", { name: "Mark recurring" })).toBeVisible();
  });
});
