import { join } from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { EVIDENCE, reseed, watchErrors } from "./helpers";

/**
 * Tester round 7, M4: demo moment 2 re-run at 1440 px (create → pay on the mock gateway in a second tab →
 * the invoice page, Collections and the dashboard flip to Paid live, one toast each), and what a cancelled
 * invoice's page tells the merchant about its payment link.
 * Spec: ARCHITECTURE.md moment 2 ("an email goes out with a Paytm link and QR, the payment is made, and the
 * invoice flips to Paid live"), Collections flow 1-5, deck slide 6 ("Paid · auto-matched").
 * UI copy under test (collections/[id]/page.tsx and mock-pay/[orderId]/page.tsx):
 *   cancel confirm   "The customer can still open the link, but it won’t count as outstanding."
 *   status panel     "The link stays payable until it expires. If they paid anyway, checking records it."
 *   link panel       "Payment link closed: this invoice is cancelled."
 *   gateway          "This invoice was cancelled by {merchant}. There’s nothing to pay."
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
  await dialog.getByRole("button", { name: /^Create/ }).click();
  await expect(page).toHaveURL(/\/collections\/[0-9a-f-]{36}$/);
  const heading = page.getByRole("heading", { name: /^Invoice INV-\d{4}$/ });
  await expect(heading).toBeVisible();
  const number = ((await heading.textContent()) ?? "").replace("Invoice ", "");
  const link = (await page.getByRole("link", { name: "Open payment link" }).getAttribute("href")) ?? "";
  return { url: page.url(), number, link };
}

/** Counts every paid toast that ever appears (they auto-dismiss after 8 s). */
async function countToasts(page: Page) {
  await page.evaluate(() => {
    const w = window as unknown as { __toasts: string[] };
    w.__toasts = [];
    new MutationObserver(() => {
      document.querySelectorAll<HTMLElement>("[aria-live=polite] [role=status]").forEach((el) => {
        if (!el.dataset.r7) {
          el.dataset.r7 = "1";
          w.__toasts.push(el.innerText);
        }
      });
    }).observe(document.body, { childList: true, subtree: true });
  });
  return () => page.evaluate(() => (window as unknown as { __toasts: string[] }).__toasts);
}

test.describe("tester r7 · M4 moment 2 and cancelled-invoice link copy", () => {
  test.describe.configure({ timeout: 120_000 });
  test.beforeEach(async () => {
    reseed();
    await new Promise((r) => setTimeout(r, 3_000));
  });
  test.afterAll(() => reseed());

  test("moment 2 at 1440 px: pay in a second tab; invoice, Collections and dashboard flip live with one toast each", async ({ page, context }) => {
    await page.setViewportSize({ width: 1440, height: 950 });
    const errors = watchErrors(page);
    const inv = await newInvoice(page, "Kavita Verma", "Monthly ration", "1250");
    await expect(page.getByText(/Invoice emailed .* to kavita\.verma@example\.com/)).toBeVisible();

    const collections = await context.newPage();
    await collections.setViewportSize({ width: 1440, height: 950 });
    await collections.goto("/collections");
    await expect(collections.locator("section", { hasText: "Pending payments" }).first()).toContainText(inv.number);
    const dashboard = await context.newPage();
    await dashboard.setViewportSize({ width: 1440, height: 950 });
    await dashboard.goto("/");
    const pending = dashboard.locator("section", { has: dashboard.getByRole("heading", { name: "Pending payments" }) });
    await expect(pending).toContainText(inv.number);
    await expect(pending).toContainText("₹12,950 outstanding from 3 customers");
    const toasts = await Promise.all([page, collections, dashboard].map(countToasts));

    const payer = await context.newPage();
    await payer.goto(inv.link);
    await payer.getByRole("button", { name: "Pay ₹1,250" }).click();
    await expect(payer.getByText("Payment successful")).toBeVisible();

    await expect(page.getByTestId("invoice-status")).toHaveText("Paid", { timeout: 15_000 });
    await expect(page.getByRole("img", { name: "Paid" })).toBeVisible();
    await expect(pending.getByRole("row").filter({ hasText: inv.number })).toContainText("Paid", { timeout: 15_000 });
    await expect(pending).toContainText("₹11,700 outstanding from 2 customers");
    const paidList = collections.locator("section", { hasText: "Matched automatically to the Paytm payment" });
    await expect(paidList).toContainText(inv.number, { timeout: 15_000 });
    await page.waitForTimeout(3_000); // late Realtime events must not add a second toast
    for (const read of toasts) {
      expect((await read()).filter((t) => t.startsWith(`${inv.number} paid`))).toHaveLength(1);
    }
    await shot(page, "r7-e2e-01-invoice-paid-live");
    await shot(dashboard, "r7-e2e-02-dashboard-paid-live");
    expect(errors).toEqual([]);
  });

  test("a cancelled invoice's page does not tell the merchant the link is both payable and closed", async ({ page, context }) => {
    await page.setViewportSize({ width: 1440, height: 950 });
    const inv = await newInvoice(page, "Naveen Dubey", "Weekly vegetables", "640");
    await page.getByRole("button", { name: "Cancel invoice" }).click();
    await page.getByRole("button", { name: "Cancel invoice" }).last().click();
    await expect(page.getByTestId("invoice-status")).toHaveText("Cancelled");

    const gateway = await context.newPage();
    await gateway.goto(inv.link);
    await expect(gateway.getByText(/There’s nothing to pay/)).toBeVisible();
    await expect(gateway.getByRole("button", { name: /^Pay / })).toHaveCount(0);
    await shot(page, "r7-e2e-03-cancelled-invoice-link-copy");

    const payable = page.getByText("The link stays payable until it expires.");
    const closed = page.getByText("Payment link closed: this invoice is cancelled.");
    const says = { payable: await payable.count(), closed: await closed.count() };
    expect(
      says.payable && says.closed,
      `the cancelled invoice page says both "The link stays payable until it expires" and "Payment link closed", ` +
        `while the customer's link says "There’s nothing to pay"`,
    ).toBeFalsy();
  });
});
