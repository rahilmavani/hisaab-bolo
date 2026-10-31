import { expect, test, type Page } from "@playwright/test";
import { reseed, shot, watchErrors } from "./helpers";

/**
 * Tester round 3, M4 UI: Collections, New invoice, invoice page, mock gateway, live flips.
 * Spec: ARCHITECTURE.md moment 2 ("an email goes out with a Paytm link and QR, the payment is made,
 * and the invoice flips to Paid live"), Collections flow 5 ("Realtime flips the dashboard"), Risks
 * ("Check payment" covers a lost webhook), pitch deck slide 6 (Pending payments table + reminder).
 */

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
  await expect(page.getByRole("heading", { name: /^Invoice INV-\d{4}$/ })).toBeVisible();
  const number = ((await page.getByRole("heading", { name: /^Invoice INV-\d{4}$/ }).textContent()) ?? "").replace("Invoice ", "");
  const link = (await page.getByRole("link", { name: "Open payment link" }).getAttribute("href")) ?? "";
  return { url: page.url(), number, link };
}

async function noSidewaysScroll(page: Page) {
  const { sw, cw } = await page.evaluate(() => ({
    sw: document.documentElement.scrollWidth,
    cw: document.documentElement.clientWidth,
  }));
  expect(sw, `page is ${sw}px wide in a ${cw}px viewport`).toBeLessThanOrEqual(cw);
}

test.describe("tester r3 · M4 collections UI", () => {
  test.afterAll(reseed);

  test("invoice to paid flips live on the invoice page, Collections and the dashboard", async ({ page, context }) => {
    const errors = watchErrors(page);
    const inv = await newInvoice(page, "Hema Bhat", "Monthly ration", "1234.50");
    expect(inv.link).toMatch(/^http:\/\/localhost:3000\/mock-pay\/HB_INV\d{4}_[0-9A-F]{8}$/);
    await expect(page.getByTestId("invoice-status")).toHaveText("Due in 7 days");
    // Paper invoice, QR and link.
    const paper = page.getByRole("article", { name: `Invoice ${inv.number}` });
    await expect(paper).toContainText("Gupta Kirana Store");
    await expect(paper).toContainText("Hema Bhat");
    await expect(paper).toContainText("Monthly ration");
    await expect(paper).toContainText("₹1,234.50");
    const qr = page.getByRole("img", { name: "UPI QR code for ₹1,234.50" });
    await expect(qr).toBeVisible();
    expect(await qr.evaluate((img: HTMLImageElement) => img.complete && img.naturalWidth > 100)).toBeTruthy();
    await expect(page.getByText(`Order ${inv.link.split("/").pop()}`)).toBeVisible();
    await shot(page, "m4", "r3-01-invoice-created");

    const collections = await context.newPage();
    const cErrors = watchErrors(collections);
    await collections.goto("/collections");
    await expect(collections.getByRole("row", { name: /Hema Bhat.*₹1,234\.50.*Due in 7 days/ })).toBeVisible();
    const dashboard = await context.newPage();
    const dErrors = watchErrors(dashboard);
    await dashboard.goto("/");
    const pending = dashboard.locator("section", { hasText: "Pending payments" });
    await expect(pending.getByRole("row", { name: /Hema Bhat.*₹1,234\.50/ })).toBeVisible();
    const before = await pending.getByText(/outstanding ·/).textContent();

    const payer = await context.newPage();
    await payer.goto(inv.link);
    await expect(payer.getByRole("heading", { name: "₹1,234.50" })).toBeVisible();
    await expect(payer.getByText(`Invoice ${inv.number} for Hema Bhat`)).toBeVisible();
    await payer.getByRole("button", { name: "Pay ₹1,234.50" }).click();
    await expect(payer.getByText("Payment successful")).toBeVisible();
    await expect(payer.getByText(/^Txn \d{8}/)).toBeVisible();

    // No reloads from here on.
    await expect(page.getByTestId("invoice-status")).toHaveText("Paid · auto-matched", { timeout: 15_000 });
    await expect(page.getByRole("img", { name: "Paid" })).toBeVisible();
    await expect(page.getByText(/₹1,234\.50 via UPI on/)).toBeVisible();
    await expect(page.getByText("Paid and matched")).toBeVisible();
    await expect(page.getByText(/^Txn \d{8}/)).toBeVisible();
    await expect(page.getByRole("button", { name: "Send reminder" })).toHaveCount(0);
    await shot(page, "m4", "r3-02-invoice-paid-live");

    await expect(collections.getByRole("row", { name: /Hema Bhat/ })).toHaveCount(0, { timeout: 15_000 });
    await expect(
      collections.locator("section", { hasText: "Matched automatically" }).getByRole("link", { name: /Hema Bhat/ }),
    ).toBeVisible();
    await shot(collections, "m4", "r3-03-collections-paid-live");

    await expect(pending.getByText(/outstanding ·|Nothing outstanding/)).not.toHaveText(before ?? "", { timeout: 15_000 });
    await expect(dashboard.getByRole("status").filter({ hasText: `${inv.number} paid` })).toBeVisible();
    await shot(dashboard, "m4", "r3-04-dashboard-after-pay");
    expect([...errors, ...cErrors, ...dErrors]).toEqual([]);
  });

  test("slide 6: a paid invoice reads 'Paid · auto-matched' in the dashboard's Pending payments", async ({ page, context }) => {
    const inv = await newInvoice(page, "Rekha Malhotra", "Sweets order", "640");
    await page.goto("/");
    const pending = page.locator("section", { hasText: "Pending payments" });
    await expect(pending.getByRole("row", { name: /Rekha Malhotra/ })).toBeVisible();
    const payer = await context.newPage();
    await payer.goto(inv.link);
    await payer.getByRole("button", { name: "Pay ₹640" }).click();
    await expect(payer.getByText("Payment successful")).toBeVisible();
    await expect(page.getByRole("status").filter({ hasText: `${inv.number} paid` })).toBeVisible({ timeout: 15_000 });
    await shot(page, "m4", "r3-05-dashboard-pending-after-pay");
    await expect(pending.getByRole("row", { name: /Rekha Malhotra.*₹640.*Paid · auto-matched/ })).toBeVisible();
  });

  test("a failed attempt keeps the invoice open; a retry then flips it live", async ({ page, context }) => {
    const inv = await newInvoice(page, "Pooja Pandey", "Festival order", "420");
    const payer = await context.newPage();
    await payer.goto(inv.link);
    await payer.getByRole("button", { name: "Simulate a failed payment" }).click();
    await expect(payer.getByText("The last attempt failed. You can try again.")).toBeVisible();
    await shot(payer, "m4", "r3-06-mock-gateway-failed");
    await page.waitForTimeout(2000);
    await expect(page.getByTestId("invoice-status")).toHaveText("Due in 7 days");
    await payer.getByRole("button", { name: "Pay ₹420" }).click();
    await expect(payer.getByText("Payment successful")).toBeVisible();
    await expect(page.getByTestId("invoice-status")).toHaveText("Paid · auto-matched", { timeout: 15_000 });
  });

  test("dropped webhook: Check payment on the Collections row finds and matches it", async ({ page, context }) => {
    const inv = await newInvoice(page, "Suresh Bhatt", "Bulk rice", "980");
    const payer = await context.newPage();
    await payer.goto(inv.link);
    await payer.getByRole("button", { name: "Pay, but drop the merchant notification" }).click();
    await expect(payer.getByText(/webhook dropped on purpose/)).toBeVisible();
    await page.goto("/collections");
    const row = page.getByRole("row", { name: /Suresh Bhatt/ });
    await expect(row).toBeVisible();
    await row.getByRole("button", { name: "Check payment" }).click();
    await expect(page.getByText("Payment found and matched").or(page.getByRole("link", { name: /Suresh Bhatt/ }))).toBeVisible();
    await expect(page.getByRole("row", { name: /Suresh Bhatt/ })).toHaveCount(0, { timeout: 10_000 });
    await expect(page.locator("section", { hasText: "Matched automatically" }).getByRole("link", { name: /Suresh Bhatt/ })).toBeVisible();
    await shot(page, "m4", "r3-07-collections-check-payment");
  });

  test("Send reminder works once the customer has an email, on the invoice page too", async ({ page }) => {
    const inv = await newInvoice(page, "Bhavesh Jain", "Snacks", "300");
    await expect(page.getByRole("button", { name: "Send reminder" })).toBeDisabled();
    // Add an email to the customer.
    await page.goto("/customers");
    await page.getByRole("searchbox").fill("Bhavesh");
    await page.getByRole("link", { name: /Bhavesh Jain/ }).first().click();
    await page.getByRole("button", { name: "Edit details" }).click();
    const dialog = page.getByRole("dialog", { name: "Edit customer" });
    await dialog.getByLabel("Email").fill("bhavesh.jain@example.com");
    await dialog.getByRole("button", { name: "Save changes" }).click();
    await expect(dialog).toBeHidden();
    // Collections enables the row's reminder (it falls back to the customer's email)...
    await page.goto("/collections");
    await expect(page.getByRole("row", { name: /Bhavesh Jain/ }).getByRole("button", { name: "Send reminder" })).toBeEnabled();
    // ...and so should the invoice page.
    await page.goto(inv.url);
    await expect(page.getByTestId("invoice-status")).toBeVisible();
    await shot(page, "m4", "r3-08-invoice-reminder-after-email-added");
    await expect(page.getByRole("button", { name: "Send reminder" })).toBeEnabled();
  });

  test("reminder from the invoice page shows in history", async ({ page }) => {
    const inv = await newInvoice(page, "Neha Thakur", "Gift box", "550", "neha.thakur@example.com");
    await expect(page.getByText("Invoice emailed")).toBeVisible();
    await page.getByRole("button", { name: "Send reminder" }).click();
    await expect(page.getByText(/Reminder emailed to neha\.thakur@example\.com/)).toBeVisible();
    await expect(page.getByText("Invoice emailed")).toHaveCount(2);
    await shot(page, "m4", "r3-09-reminder-sent");
    expect(inv.number).toMatch(/^INV-\d{4}$/);
  });

  test("cancel takes an invoice out of Pending payments", async ({ page }) => {
    await page.goto("/collections");
    const sub = page.getByText(/outstanding · \d+ customer/);
    const before = await sub.textContent();
    const inv = await newInvoice(page, "Ritu Rao", "Old order", "700");
    await page.getByRole("button", { name: "Cancel invoice" }).click();
    await expect(page.getByText(`Cancel ${inv.number}?`)).toBeVisible();
    await page.getByRole("button", { name: "Cancel invoice" }).click();
    await expect(page.getByText("Invoice cancelled.")).toBeVisible();
    await expect(page.getByTestId("invoice-status")).toHaveText("Cancelled");
    await expect(page.getByRole("button", { name: "Send reminder" })).toHaveCount(0);
    // Only the payment check is left: it records money paid before the cancel (rounds 6-7).
    await expect(page.getByText("If they paid before you cancelled", { exact: false })).toBeVisible();
    await shot(page, "m4", "r3-10-invoice-cancelled");
    await page.goto("/collections");
    await expect(page.getByRole("row", { name: /Ritu Rao/ })).toHaveCount(0);
    await expect(sub).toHaveText(before ?? "");
  });

  test("new invoice form refuses bad input and sends nothing", async ({ page }) => {
    let posts = 0;
    page.on("request", (r) => {
      if (r.method() === "POST" && r.url().endsWith(":8000/invoices")) posts++;
    });
    await page.goto("/collections");
    await page.getByRole("button", { name: "New invoice" }).click();
    const dialog = page.getByRole("dialog", { name: "New invoice" });
    await dialog.getByRole("searchbox").fill("Ajay");
    await dialog.getByRole("button", { name: /Ajay Reddy/ }).click();
    await dialog.getByLabel("Item 1 description").fill("Item");
    await dialog.getByLabel("Item 1 quantity").fill("0");
    await dialog.getByLabel("Item 1 price in rupees").fill("50");
    await dialog.getByRole("button", { name: /^Create/ }).click();
    await expect(dialog.getByRole("alert")).toHaveText("Every line needs a description, a quantity and a price.");
    await dialog.getByLabel("Item 1 quantity").fill("1");
    await dialog.getByLabel("Item 1 price in rupees").fill("0.5");
    await dialog.getByRole("button", { name: /^Create/ }).click();
    await expect(dialog.getByRole("alert")).toHaveText("The total must be at least ₹1.");
    expect(posts).toBe(0);
    // A bad email is stopped by the browser; a bad GSTIN by the API.
    await dialog.getByLabel("Item 1 price in rupees").fill("50");
    await dialog.getByLabel("Send to (email)").fill("not-an-email");
    await expect(dialog.getByRole("button", { name: "Create and email" })).toBeVisible();
    await dialog.getByRole("button", { name: "Create and email" }).click();
    expect(posts).toBe(0);
    await dialog.getByLabel("Send to (email)").fill("");
    await dialog.getByLabel("GST invoice (B2B)").check();
    await dialog.getByLabel("Customer GSTIN").fill("27abc");
    await dialog.getByRole("button", { name: "Create invoice" }).click();
    await expect(dialog.getByRole("alert")).toBeVisible();
    await shot(page, "m4", "r3-11-new-invoice-bad-gstin");
    await expect(page).toHaveURL(/\/collections$/);
  });

  test("the mock gateway is public and refuses unknown links", async ({ browser, page }) => {
    const inv = await newInvoice(page, "Ajay Reddy", "Dry fruits", "1500");
    const anon = await browser.newContext({ storageState: { cookies: [], origins: [] } });
    const p = await anon.newPage();
    await p.goto(inv.link);
    await expect(p.getByText("Paying Gupta Kirana Store")).toBeVisible();
    await expect(p.getByRole("heading", { name: "₹1,500" })).toBeVisible();
    await expect(p.getByText("Dry fruits")).toBeVisible();
    await p.goto("/mock-pay/HB_INV9999_DEADBEEF");
    await expect(p.locator("main [role=alert]")).toHaveText("This payment link doesn't exist or has expired.");
    await anon.close();
  });

  for (const [name, width, height] of [
    ["390", 390, 844],
    ["412", 412, 915],
  ] as const) {
    test(`Collections, invoice page and mock gateway fit ${name}px`, async ({ browser, page }) => {
      const inv = await newInvoice(page, "Farhan Joshi", "A rather long line item description for a phone", "2500");
      const ctx = await browser.newContext({
        storageState: "e2e/.auth/demo.json",
        viewport: { width, height },
        isMobile: true,
        hasTouch: true,
      });
      const p = await ctx.newPage();
      for (const path of ["/collections", new URL(inv.url).pathname, new URL(inv.link).pathname]) {
        await p.goto(path);
        await expect(p.locator("main, body").first()).toBeVisible();
        await p.waitForLoadState("networkidle");
        await noSidewaysScroll(p);
      }
      await p.goto(new URL(inv.url).pathname);
      await expect(p.getByTestId("invoice-status")).toBeVisible();
      await shot(p, "m4", `r3-12-invoice-${name}`);
      await ctx.close();
    });
  }
});
