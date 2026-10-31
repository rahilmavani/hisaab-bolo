import { join } from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { EVIDENCE, reseed, watchErrors } from "./helpers";

/**
 * Tester round 6, M4: one paid toast per open screen at the 1400 px layout (Ask panel docked), a dropped
 * webhook on an invoice that is then cancelled, and the collections/invoice pages at phone width.
 * Spec: ARCHITECTURE.md moment 2 ("the payment is made, and the invoice flips to Paid live"),
 * Collections flow 5 ("Realtime flips the dashboard"), Risks ("The Paytm webhook may never arrive: a
 * 'Check payment' button and cron polling cover it").
 * UI copy: mock-pay page "The merchant wasn't notified (webhook dropped on purpose). Their “Check payment”
 * button or the 5-minute check will pick it up."; invoice cancel confirm "The customer can still open
 * the link, but it won't count as outstanding."
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

test.describe("tester r6 · M4 toasts at 1400 px, dropped webhook + cancel, phone width", () => {
  test.describe.configure({ timeout: 120_000 });
  test.beforeEach(async () => {
    reseed();
    await new Promise((r) => setTimeout(r, 3_000));
  });
  test.afterAll(() => reseed());

  test("at 1400 px each open screen shows exactly one paid toast, inside the main column", async ({ page, context }) => {
    await page.setViewportSize({ width: 1400, height: 900 });
    const errors = watchErrors(page);
    const inv = await newInvoice(page, "Tanvi Bose", "Party order", "1875");
    const dashboard = await context.newPage();
    await dashboard.setViewportSize({ width: 1400, height: 900 });
    await dashboard.goto("/");
    await expect(dashboard.locator("section", { hasText: "Pending payments" })).toContainText(inv.number);

    const payer = await context.newPage();
    await payer.goto(inv.link);
    await payer.getByRole("button", { name: "Pay ₹1,875" }).click();
    await expect(payer.getByText("Payment successful")).toBeVisible();

    for (const p of [page, dashboard]) {
      const toast = p.getByRole("status").filter({ hasText: `${inv.number} paid` });
      await expect(toast).toBeVisible({ timeout: 15_000 });
      await p.waitForTimeout(2_500); // late Realtime events must not add a second toast
      await expect(toast).toHaveCount(1);
      const box = (await toast.boundingBox())!;
      const ask = (await p.getByRole("complementary", { name: "Ask Hisaab" }).boundingBox())!;
      expect(box.x, "toast starts left of the sidebar edge").toBeGreaterThanOrEqual(232);
      expect(box.x + box.width, "toast runs under the docked Ask panel").toBeLessThanOrEqual(ask.x);
    }
    await expect(page.getByTestId("invoice-status")).toHaveText("Paid");
    await shot(page, "r6-e2e-01-invoice-paid-toast-1400");
    await shot(dashboard, "r6-e2e-02-dashboard-paid-toast-1400");
    expect(errors).toEqual([]);
  });

  test("a payment whose webhook was dropped is still picked up after the merchant cancels the invoice", async ({ page, context }) => {
    const inv = await newInvoice(page, "Zoya Nair", "Saree fall and pico", "950");
    const payer = await context.newPage();
    await payer.goto(inv.link);
    await payer.getByRole("button", { name: "Pay, but drop the merchant notification" }).click();
    await expect(payer.getByText(/The merchant wasn’t notified \(webhook dropped on purpose\)/)).toBeVisible();
    await shot(payer, "r6-e2e-03-mock-pay-webhook-dropped");

    // The merchant still sees it unpaid, and cancels it (the dialog says the link stays usable).
    await page.reload();
    await expect(page.getByTestId("invoice-status")).toHaveText(/Due in 7 days/);
    await page.getByRole("button", { name: "Cancel invoice" }).click();
    await page.getByRole("button", { name: "Cancel invoice" }).last().click();
    await expect(page.getByTestId("invoice-status")).toHaveText("Cancelled");
    // Fixed after round 6: a cancelled invoice's link stays payable, so the page still offers the check.
    await expect(page.getByRole("button", { name: "Check payment" })).toHaveCount(1);

    // "...or the 5-minute check will pick it up": run the background jobs (demo clock +1 day).
    await page.getByRole("button", { name: "+1 day" }).first().click();
    await expect(page.getByRole("region", { name: "Demo clock" }).first().getByRole("status")).not.toHaveText("Running jobs…", {
      timeout: 20_000,
    });
    await page.reload();
    await shot(page, "r6-e2e-04-cancelled-after-dropped-webhook");
    await expect(
      page.getByTestId("invoice-status"),
      `Paytm took ₹950 for ${inv.number}; neither Check payment nor the 5-minute check records it once cancelled`,
    ).toHaveText("Paid");
  });

  test.describe("phone width", () => {
    test.use({ viewport: { width: 390, height: 844 } });

    test("collections, an invoice and the dashboard fit a phone without sideways scrolling", async ({ page }) => {
      const errors = watchErrors(page);
      for (const path of ["/collections", "/"]) {
        await page.goto(path);
        await expect(page.getByText("Pending payments").first()).toBeVisible();
        const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
        expect(overflow, `${path} scrolls sideways by ${overflow}px`).toBeLessThanOrEqual(0);
      }
      await page.goto("/collections");
      await page.getByRole("link", { name: "Sharma Traders" }).first().click();
      await expect(page.getByTestId("invoice-status")).toHaveText(/9 days overdue/);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      expect(overflow).toBeLessThanOrEqual(0);
      await shot(page, "r6-e2e-05-invoice-phone");
      expect(errors).toEqual([]);
    });
  });
});
