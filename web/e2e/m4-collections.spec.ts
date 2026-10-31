import { expect, test, type Page } from "@playwright/test";
import { reseed, shot, watchErrors } from "./helpers";

async function newInvoice(page: Page, customer: string, item: string, rupees: string, email = "") {
  await page.goto("/collections");
  await page.getByRole("button", { name: "New invoice" }).click();
  const dialog = page.getByRole("dialog", { name: "New invoice" });
  await dialog.getByRole("searchbox").fill(customer);
  await dialog.getByRole("button", { name: new RegExp(customer) }).first().click();
  await dialog.getByLabel("Item 1 description").fill(item);
  await dialog.getByLabel("Item 1 price in rupees").fill(rupees);
  await dialog.getByLabel("Send to (email)").fill(email);
  await dialog.getByRole("button", { name: /^Create/ }).click();
  await expect(page).toHaveURL(/\/collections\/[0-9a-f-]{36}$/);
  return page.url();
}

test.describe("M4 collections, mock Paytm and webhooks", () => {
  test.afterAll(reseed);

  test("collections lists pending payments from the seed", async ({ page }) => {
    const errors = watchErrors(page);
    await page.goto("/collections");
    await expect(page.getByText("₹11,700 outstanding · 2 customers")).toBeVisible();
    await expect(page.getByRole("row", { name: /Sharma Traders.*₹8,500.*Overdue · 9 days/ })).toBeVisible();
    await expect(page.getByRole("row", { name: /Priya Caterers.*₹3,200.*Due tomorrow/ })).toBeVisible();
    await expect(page.getByRole("button", { name: "Send 1 overdue reminder" })).toBeVisible();
    await shot(page, "m4", "01-collections");
    expect(errors).toEqual([]);
  });

  test("seeded overdue invoice has a link and QR and can be reminded", async ({ page }) => {
    await page.goto("/collections");
    await page.getByRole("link", { name: "Sharma Traders", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Invoice INV-0412" })).toBeVisible();
    await expect(page.getByTestId("invoice-status")).toHaveText("Overdue · 9 days");
    await expect(page.getByRole("img", { name: /UPI QR code for ₹8,500/ })).toBeVisible();
    await page.getByRole("button", { name: "Send reminder" }).click();
    await expect(page.getByText(/Reminder emailed to accounts@sharmatraders.in/)).toBeVisible();
    await expect(page.getByText("Overdue reminder emailed")).toBeVisible();
    await shot(page, "m4", "02-invoice-overdue-reminded");
  });

  test("invoice to paid: pay on the mock gateway and the invoice flips live", async ({ page, context }) => {
    const errors = watchErrors(page);
    await newInvoice(page, "Vikram", "Monthly milk", "1500");
    await expect(page.getByTestId("invoice-status")).toHaveText("Due in 7 days");
    await shot(page, "m4", "03-invoice-created");
    const link = await page.getByRole("link", { name: "Open payment link" }).getAttribute("href");
    expect(link).toMatch(/\/mock-pay\/HB_INV\d{4}_[0-9A-F]{8}$/);

    const payer = await context.newPage();
    await payer.goto(link!);
    await expect(payer.getByText("Test payment gateway")).toBeVisible();
    await expect(payer.getByRole("heading", { name: "₹1,500" })).toBeVisible();
    await shot(payer, "m4", "04-mock-gateway");
    await payer.getByRole("button", { name: "Pay ₹1,500" }).click();
    await expect(payer.getByText("Payment successful")).toBeVisible();
    await shot(payer, "m4", "05-mock-gateway-paid");

    // No reload: Supabase Realtime flips the merchant's page.
    await expect(page.getByTestId("invoice-status")).toHaveText("Paid · auto-matched", { timeout: 15_000 });
    await expect(page.getByRole("img", { name: "Paid" })).toBeVisible();
    await expect(page.getByText(/paid$/).first()).toBeVisible();
    await shot(page, "m4", "06-invoice-paid-live");
    expect(errors).toEqual([]);
  });

  test("dashboard pending payments update live when an invoice is paid", async ({ page, context }) => {
    await page.goto("/");
    const pending = page.locator("section", { hasText: "Pending payments" });
    await expect(pending.getByText("₹11,700 outstanding · 2 customers")).toBeVisible();
    const res = await page.request.get(`${process.env.API_BASE ?? "http://localhost:8000"}/health`);
    expect(res.ok()).toBeTruthy();

    // Pay Priya's seeded invoice from the payer side.
    const payer = await context.newPage();
    await payer.goto("/collections");
    await payer.getByRole("link", { name: "Priya Caterers", exact: true }).click();
    const link = await payer.getByRole("link", { name: "Open payment link" }).getAttribute("href");
    await payer.goto(link!);
    await payer.getByRole("button", { name: /^Pay ₹3,200/ }).click();
    await expect(payer.getByText("Payment successful")).toBeVisible();

    await expect(pending.getByText("₹8,500 outstanding · 1 customer")).toBeVisible({ timeout: 15_000 });
    await expect(page.getByRole("status").filter({ hasText: "INV-0414 paid" })).toBeVisible();
    await shot(page, "m4", "07-dashboard-live-paid");
  });

  test("a lost webhook is recovered with Check payment", async ({ page, context }) => {
    await newInvoice(page, "Anita", "Gift hampers", "750");
    const link = await page.getByRole("link", { name: "Open payment link" }).getAttribute("href");
    const payer = await context.newPage();
    await payer.goto(link!);
    await payer.getByRole("button", { name: "Pay, but drop the merchant notification" }).click();
    await expect(payer.getByText(/webhook dropped on purpose/)).toBeVisible();
    await page.waitForTimeout(1500);
    await expect(page.getByTestId("invoice-status")).not.toHaveText(/Paid/);
    await page.getByRole("button", { name: "Check payment" }).click();
    await expect(page.getByText("Paytm confirms the payment. Marked paid.")).toBeVisible();
    await expect(page.getByTestId("invoice-status")).toHaveText("Paid · auto-matched");
    await shot(page, "m4", "08-check-payment-recovered");
  });

  test("a failed payment leaves the invoice open", async ({ page, context }) => {
    await newInvoice(page, "Geeta", "Household order", "420");
    const link = await page.getByRole("link", { name: "Open payment link" }).getAttribute("href");
    const payer = await context.newPage();
    await payer.goto(link!);
    await payer.getByRole("button", { name: "Simulate a failed payment" }).click();
    await expect(payer.getByText("The last attempt failed. You can try again.")).toBeVisible();
    await page.reload();
    await expect(page.getByTestId("invoice-status")).toHaveText("Due in 7 days");
  });

  test("new invoice form validates input", async ({ page }) => {
    await page.goto("/collections");
    await page.getByRole("button", { name: "New invoice" }).click();
    const dialog = page.getByRole("dialog", { name: "New invoice" });
    await dialog.getByRole("button", { name: /^Create/ }).click();
    await expect(dialog.getByRole("alert")).toHaveText("Pick who this invoice is for.");
    await dialog.getByRole("searchbox").fill("sharma");
    await dialog.getByRole("button", { name: /Sharma Traders/ }).click();
    await expect(dialog.getByLabel("Customer GSTIN")).toHaveValue(/^27AAPFS1234K1Z/);
    await dialog.getByRole("button", { name: /^Create/ }).click();
    await expect(dialog.getByRole("alert")).toHaveText("Every line needs a description, a quantity and a price.");
    await shot(page, "m4", "09-new-invoice-validation");
  });
});
