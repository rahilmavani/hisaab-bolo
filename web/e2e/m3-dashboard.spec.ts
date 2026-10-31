import { expect, test } from "@playwright/test";
import { shot, watchErrors } from "./helpers";

test.describe("M3 dashboard and ledger", () => {
  test("dashboard shows KPIs, trend, alerts, pending payments and peak hours", async ({ page }) => {
    const errors = watchErrors(page);
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Today’s hisaab" })).toBeVisible();
    await expect(page.getByText("Collected today")).toBeVisible();
    await expect(page.getByText("Repeat customers")).toBeVisible();
    await expect(page.getByText(/Slow day: takings are \d+% below a usual/)).toBeVisible();
    await expect(page.getByRole("img", { name: /Collections by day/ })).toBeVisible();

    const pending = page.locator("section", { hasText: "Pending payments" });
    await expect(pending.getByText("₹11,700 outstanding from 2 customers")).toBeVisible();
    await expect(pending.getByRole("row", { name: /Sharma Traders.*₹8,500.*9 days overdue/ })).toBeVisible();
    await expect(pending.getByRole("row", { name: /Priya Caterers.*₹3,200.*Due tomorrow/ })).toBeVisible();

    const quiet = page.locator("section", { hasText: "Gone quiet" });
    await expect(quiet.getByText("Meena Kapoor")).toBeVisible();
    await expect(quiet.getByText(/Last paid 12 days ago/)).toBeVisible();

    await expect(page.getByText(/^Busiest: /)).toBeVisible();
    const height = await page.evaluate(() => document.documentElement.scrollHeight);
    // 2600, not 2400, since the unslop pass: the main chart grew to 280px and gained a legend and a previous-period line.
    expect(height).toBeLessThan(2600);
    await shot(page, "m3", "01-dashboard");
    expect(errors).toEqual([]);
  });

  test("trend range toggles to 30 days and 90 days by week", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("button", { name: "30D" }).click();
    await expect(page.getByText("Collections, last 30 days")).toBeVisible();
    await page.getByRole("button", { name: "90D" }).click();
    await expect(page.getByText("Collections, last 90 days by week")).toBeVisible();
    await expect(page.getByRole("img", { name: /Collections by week/ })).toBeVisible();
    await shot(page, "m3", "02-trend-90d");
  });

  test("customer search finds Ram with his habit line", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("searchbox").fill("ram");
    const row = page.getByRole("link", { name: /Ram.*ram@paytm/ });
    await expect(row).toBeVisible();
    await expect(row).toContainText(/₹100 daily, ~8:30 AM.*121 payments since/);
    await expect(row).toContainText("Regular");
    await shot(page, "m3", "03-search-ram");
    await row.click();
    await expect(page.getByRole("heading", { name: "Ram", exact: true })).toBeVisible();
    await expect(page.getByText("₹12,100")).toBeVisible();
    await expect(page.getByRole("cell", { name: "ram@paytm" }).or(page.getByText("ram@paytm").first())).toBeVisible();
    await shot(page, "m3", "04-payer-ram");
  });

  test("lapsed regular profile explains the gap", async ({ page }) => {
    await page.goto("/customers");
    await page.getByRole("searchbox").fill("meena");
    await page.getByRole("link", { name: /Meena Kapoor/ }).click();
    await expect(page.getByText(/Gone quiet\./)).toBeVisible();
    await expect(page.getByText("Lapsed", { exact: true }).first()).toBeVisible();
  });

  test("customers page lists everyone", async ({ page }) => {
    await page.goto("/customers");
    await expect(page.getByRole("heading", { name: "Customers" })).toBeVisible();
    await expect(page.locator('a[href^="/customers/"]')).toHaveCount(40);
    await shot(page, "m3", "05-customers");
  });

  test("editing a payment description saves as a merchant edit", async ({ page }) => {
    const errors = watchErrors(page);
    await page.goto("/customers");
    await page.getByRole("searchbox").fill("anita");
    await page.getByRole("link", { name: /Anita Desai/ }).click();
    const first = page.getByRole("button", { name: /Edit description for/ }).first();
    await first.click();
    const input = page.getByRole("textbox", { name: "Payment description" });
    const text = `Weekly groceries ${Date.now() % 1000}`;
    await input.fill(text);
    await input.press("Enter");
    await expect(page.getByText(text)).toBeVisible();
    await page.reload();
    await expect(page.getByText(text)).toBeVisible();
    expect(errors).toEqual([]);
  });

  test("editing customer details validates GSTIN", async ({ page }) => {
    await page.goto("/customers");
    await page.getByRole("searchbox").fill("priya");
    await page.getByRole("link", { name: /Priya Caterers/ }).click();
    await page.getByRole("button", { name: "Edit details" }).click();
    const dialog = page.getByRole("dialog", { name: "Edit customer" });
    await dialog.getByLabel("GSTIN").fill("bad");
    await dialog.getByRole("button", { name: "Save changes" }).click();
    await expect(dialog.getByRole("alert")).toContainText("GSTIN should be 15 characters");
    await dialog.getByLabel("GSTIN").fill("07AABCP1234C1Z9");
    await dialog.getByRole("button", { name: "Save changes" }).click();
    await expect(dialog).toBeHidden();
    await expect(page.getByText("07AABCP1234C1Z9", { exact: true })).toBeVisible();
    await shot(page, "m3", "06-payer-edited");
  });

  test("merge dialog previews what moves before merging", async ({ page }) => {
    await page.goto("/customers");
    await page.getByRole("searchbox").fill("ram");
    await page.getByRole("link", { name: /Ram.*ram@paytm/ }).click();
    await page.getByRole("button", { name: "Merge duplicate" }).click();
    const dialog = page.getByRole("dialog", { name: "Merge into Ram" });
    await dialog.getByRole("searchbox").fill("vikram");
    await dialog.getByRole("button", { name: /Vikram/ }).first().click();
    await expect(dialog.getByText(/will be folded into/)).toBeVisible();
    await shot(page, "m3", "07-merge-preview");
    await dialog.getByRole("button", { name: "Back" }).click();
    await dialog.getByRole("button", { name: "Close" }).click();
    await expect(dialog).toBeHidden();
  });

  test("signed-out visitors are sent to login", async ({ browser }) => {
    const ctx = await browser.newContext({ storageState: { cookies: [], origins: [] } });
    const page = await ctx.newPage();
    await page.goto("/customers");
    await expect(page).toHaveURL(/\/login\?next=%2Fcustomers/);
    await expect(page.getByRole("button", { name: "Sign in" })).toBeVisible();
    await shot(page, "m3", "00-login");
    await ctx.close();
  });
});
