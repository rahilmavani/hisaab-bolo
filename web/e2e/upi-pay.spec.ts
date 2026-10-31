import { expect, test, type Page } from "@playwright/test";
import { reseed, shot, watchErrors } from "./helpers";

// Paying the shop's own UPI ID (MERCHANT_UPI_ID in the API's .env, mock mode): the pay page's UPI button and
// QR, then "Mark as paid" on the invoice, since Paytm sends no notification for such payments.

async function newInvoice(
  page: Page,
  customer: string,
  item: string,
  rupees: string,
) {
  await page.goto("/collections");
  await page.getByRole("button", { name: "New invoice" }).click();
  const dialog = page.getByRole("dialog", { name: "New invoice" });
  await dialog.getByRole("searchbox").fill(customer);
  await dialog
    .getByRole("button", { name: new RegExp(customer) })
    .first()
    .click();
  await dialog.getByLabel("Item 1 description").fill(item);
  await dialog.getByLabel("Item 1 price in rupees").fill(rupees);
  await dialog.getByRole("button", { name: /^Create/ }).click();
  await expect(page).toHaveURL(/\/collections\/[0-9a-f-]{36}$/);
  const heading = page.getByRole("heading", { name: /^Invoice INV-\d{4}$/ });
  await expect(heading).toBeVisible();
  const number = ((await heading.textContent()) ?? "").replace("Invoice ", "");
  const link =
    (await page
      .getByRole("link", { name: "Open payment link" })
      .getAttribute("href")) ?? "";
  return { url: page.url(), number, link };
}

test.describe("UPI payments to the shop's own UPI ID", () => {
  test.describe.configure({ timeout: 90_000 });
  test.afterAll(() => reseed());

  test("the pay page opens the UPI app with the amount, and the merchant marks the invoice paid", async ({
    page,
    context,
    browser,
  }) => {
    const errors = watchErrors(page);
    const inv = await newInvoice(
      page,
      "Kavita Verma",
      "Diwali hamper order",
      "2450",
    );

    const payer = await context.newPage();
    await payer.goto(inv.link);
    await expect(payer.getByText("₹2,450").first()).toBeVisible();
    const upiButton = payer.getByRole("link", {
      name: /Pay ₹2,450 with UPI app/,
    });
    test.skip(
      !(await upiButton.isVisible()),
      "the API has no MERCHANT_UPI_ID, so the page offers the test payment only",
    );

    const href = (await upiButton.getAttribute("href")) ?? "";
    const params = new URL(href.replace("upi://pay", "https://upi.invalid/pay"))
      .searchParams;
    expect(href.startsWith("upi://pay?")).toBe(true);
    expect(params.get("am")).toBe("2450.00");
    expect(params.get("cu")).toBe("INR");
    expect(params.get("tn")).toContain(inv.number);
    expect(params.get("pa")).toMatch(/^[\w.-]+@[\w]+$/);
    expect(params.get("tr")).toBeNull(); // no merchant-only fields for a personal UPI ID
    await expect(
      payer.getByRole("img", { name: /UPI QR code to pay ₹2,450/ }),
    ).toBeVisible();
    await expect(
      payer.getByRole("button", { name: "Pay ₹2,450 (test)" }),
    ).toBeVisible();
    // No screenshot of this page: it shows the shop's real UPI ID, and evidence/ is committed.

    // A customer (not signed in) can't simulate a payment: with a real UPI ID that would mark the invoice
    // paid without money moving.
    // newContext() in Playwright Test inherits the project's signed-in state unless told otherwise.
    const anon = await browser.newContext({
      storageState: { cookies: [], origins: [] },
    });
    const customer = await anon.newPage();
    await customer.goto(inv.link);
    await expect(
      customer.getByRole("link", { name: /Pay ₹2,450 with UPI app/ }),
    ).toBeVisible();
    await expect(
      customer.getByRole("button", { name: /\(test\)|Simulate/ }),
    ).toHaveCount(0);
    await anon.close();

    // The customer paid in their UPI app: the shop sees the money and confirms it.
    await page.goto(inv.url);
    await page.getByRole("button", { name: "Mark as paid" }).click();
    await expect(
      page.getByText(`Mark ${inv.number} paid (₹2,450)?`),
    ).toBeVisible();
    await page.getByRole("button", { name: "Yes, it's paid" }).click();
    await expect(page.getByTestId("invoice-status")).toHaveText(
      "Paid · marked by you",
      { timeout: 15_000 },
    );
    await shot(page, "m9", "01-invoice-marked-paid");

    // The pay page no longer offers to pay.
    await payer.reload();
    await expect(payer.getByRole("link", { name: /with UPI app/ })).toHaveCount(
      0,
    );
    expect(errors).toEqual([]);
  });
});
