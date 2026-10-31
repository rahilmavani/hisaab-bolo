import { join } from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { EVIDENCE, reseed, watchErrors } from "./helpers";

/**
 * Tester round 5, M4: invoice → paid live (moment 2), the paid toast, link-only invoices and the
 * Pending payments labels after the demo clock moves back.
 * Spec: ARCHITECTURE.md moment 2 ("the payment is made, and the invoice flips to Paid live"),
 * Collections flow 5 ("Realtime flips the dashboard"; "Overdue (due + grace) triggers a reminder
 * email"), Data model app_clock ("every job and query uses now() from here"), pitch deck slide 6.
 * UI copy: new-invoice-dialog.tsx "Leave empty to just get a link"; live-updates.tsx "announce each
 * paid invoice once".
 */

async function shot(page: Page, name: string) {
  await page.screenshot({ path: join(EVIDENCE, "m4", `${name}.png`), fullPage: true });
}

async function newInvoice(page: Page, customer: string, item: string, rupees: string, opts: { dueToday?: boolean; noEmail?: boolean } = {}) {
  await page.goto("/collections");
  await page.getByRole("button", { name: "New invoice" }).click();
  const dialog = page.getByRole("dialog", { name: "New invoice" });
  await dialog.getByRole("searchbox").fill(customer);
  await dialog.getByRole("button", { name: new RegExp(customer) }).first().click();
  await dialog.getByLabel("Item 1 description").fill(item);
  await dialog.getByLabel("Item 1 price in rupees").fill(rupees);
  if (opts.dueToday) {
    const min = (await dialog.getByLabel("Due date").getAttribute("min")) ?? "";
    await dialog.getByLabel("Due date").fill(min);
  }
  if (opts.noEmail) {
    await dialog.getByLabel("Send to (email)").fill("");
    await expect(dialog.getByRole("button", { name: "Create invoice" })).toBeVisible();
  }
  await dialog.getByRole("button", { name: /^Create/ }).click();
  await expect(page).toHaveURL(/\/collections\/[0-9a-f-]{36}$/);
  const heading = page.getByRole("heading", { name: /^Invoice INV-\d{4}$/ });
  await expect(heading).toBeVisible();
  const number = ((await heading.textContent()) ?? "").replace("Invoice ", "");
  const link = (await page.getByRole("link", { name: "Open payment link" }).getAttribute("href")) ?? "";
  return { url: page.url(), number, link };
}

const clockStatus = (page: Page) => page.getByRole("region", { name: "Demo clock" }).first().getByRole("status");

test.describe("tester r5 · M4 invoice → paid, toasts, link-only, clock reset", () => {
  test.describe.configure({ timeout: 120_000 });
  test.beforeEach(async () => {
    reseed();
    await new Promise((r) => setTimeout(r, 3_000));
  });
  test.afterAll(() => reseed());

  test("moment 2: pay on the mock gateway in a second tab; invoice page and dashboard flip to Paid live", async ({ page, context }) => {
    const errors = watchErrors(page);
    const inv = await newInvoice(page, "Sharma Traders", "Canteen staples November", "4250");
    await expect(page.getByTestId("invoice-status")).toHaveText("Due in 7 days");
    await expect(page.getByText(/Invoice emailed .* to accounts@sharmatraders\.in/)).toBeVisible();
    const dashboard = await context.newPage();
    const dErrors = watchErrors(dashboard);
    await dashboard.goto("/");
    const pending = dashboard.locator("section", { hasText: "Pending payments" });
    await expect(pending.getByRole("row", { name: new RegExp(`${inv.number}.*₹4,250.*Due in 7 days`) })).toBeVisible();

    const payer = await context.newPage();
    await payer.goto(inv.link);
    await expect(payer.getByText("Test payment gateway")).toBeVisible();
    await payer.getByRole("button", { name: "Pay ₹4,250" }).click();
    await expect(payer.getByText("Payment successful")).toBeVisible();

    await expect(page.getByTestId("invoice-status")).toHaveText("Paid", { timeout: 15_000 });
    await expect(page.getByRole("img", { name: "Paid" })).toBeVisible();
    await expect(pending.getByRole("row", { name: new RegExp(`${inv.number}.*Paid`) })).toBeVisible({ timeout: 15_000 });
    await expect(dashboard.getByRole("status").filter({ hasText: `${inv.number} paid` })).toBeVisible();
    await shot(page, "r5-e2e-01-invoice-paid-live");
    await shot(dashboard, "r5-e2e-02-dashboard-paid-live");
    expect(errors).toEqual([]);
    expect(dErrors).toEqual([]);
  });

  test("merging a customer does not announce their old paid invoice as just paid", async ({ page, context }) => {
    // live-updates.tsx toasts "<number> paid · received and matched" for any UPDATE whose row is paid.
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Today’s hisaab" })).toBeVisible();
    await page.waitForTimeout(15_000); // let the Realtime channel join (postgres_changes can take several seconds to go live)
    const other = await context.newPage();
    await other.goto("/customers");
    await other.getByRole("searchbox").fill("Tanvi");
    await other.getByRole("link", { name: /^Tanvi Bose/ }).first().click();
    await other.getByRole("button", { name: "Merge duplicate" }).click();
    const dialog = other.getByRole("dialog", { name: "Merge into Tanvi Bose" });
    await dialog.getByRole("searchbox").fill("Anita");
    await dialog.getByRole("button", { name: /Anita Desai/ }).click();
    // INV-0413 (Anita) was paid days ago; the merge only moved it to Tanvi.
    // Start watching for the toast before the merge (it removes itself after 8 s).
    const toast = page.getByRole("status").filter({ hasText: /INV-0413 paid/ });
    const toastSeen = toast.waitFor({ state: "visible", timeout: 40_000 }).then(() => true, () => false);
    await dialog.getByRole("button", { name: "Merge Anita Desai" }).click();
    await expect(dialog).toBeHidden();
    // The dashboard only learns about the merge through Realtime: once INV-0413 shows under Tanvi,
    // the invoice UPDATE event (the one that drives the toast) has arrived.
    const pending = page.locator("section", { hasText: "Pending payments" });
    const delivered = await pending
      .getByRole("row", { name: /Tanvi Bose.*INV-0413/ })
      .waitFor({ timeout: 30_000 })
      .then(() => true, () => false);
    // Seen in about 1 of 3 runs: the merge's UPDATE never reached the open dashboard (no refresh either).
    test.skip(!delivered, "Realtime did not deliver the merge's invoice UPDATE within 30 s; the toast could not be checked");
    const appeared = await Promise.race([toastSeen, new Promise<boolean>((r) => setTimeout(() => r(false), 3_000))]);
    await shot(page, "r5-e2e-03-merge-paid-toast");
    expect(appeared, "a merge re-announced INV-0413 (paid days ago) with the 'paid · received and matched' toast").toBe(false);
  });

  test("a link-only invoice (email box cleared) is not emailed when it goes overdue", async ({ page }) => {
    const inv = await newInvoice(page, "Tanvi Bose", "Party order", "650", { dueToday: true, noEmail: true });
    await expect(page.getByText(/Invoice emailed/)).toHaveCount(0);
    await page.getByRole("button", { name: "+7 days" }).first().click();
    await expect(clockStatus(page)).toBeVisible({ timeout: 20_000 });
    await expect(page.getByTestId("invoice-status")).toHaveText(/7 days overdue/, { timeout: 15_000 });
    await shot(page, "r5-e2e-04-link-only-invoice-after-7-days");
    await expect(page.getByText(/Overdue reminder emailed/), `${inv.number}: the merchant chose "just get a link", yet a reminder was emailed`).toHaveCount(0);
  });

  test("after +7 days and Reset, Pending payments never reads 'Overdue · -N days'", async ({ page }) => {
    await page.goto("/");
    const pending = page.locator("section", { hasText: "Pending payments" });
    await expect(pending.getByRole("row", { name: /Priya Caterers.*Due tomorrow/ })).toBeVisible();
    await page.getByRole("button", { name: "+7 days" }).first().click();
    await expect(clockStatus(page)).toContainText(/reminder sent for .*INV-0414/i, { timeout: 20_000 });
    await page.getByRole("button", { name: "Reset demo clock" }).first().click();
    await expect(page.getByRole("button", { name: "Reset demo clock" })).toHaveCount(0, { timeout: 20_000 });
    await expect(pending.getByRole("row", { name: /Priya Caterers/ })).toBeVisible();
    await shot(page, "r5-e2e-05-dashboard-after-reset");
    const priya = (await pending.getByRole("row", { name: /Priya Caterers/ }).textContent()) ?? "";
    expect(priya, "INV-0414 is due tomorrow again after Reset").not.toMatch(/-\d+ days? overdue/);
  });
});
