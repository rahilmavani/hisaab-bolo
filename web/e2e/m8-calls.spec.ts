import { mkdirSync } from "node:fs";
import { join } from "node:path";
import { expect, test } from "@playwright/test";
import { EVIDENCE, reseed, shot, watchErrors } from "./helpers";

// Needs the API in CALL_MODE=mock: the fake customer picks up, asks for the link (the agent emails
// it mid-call) and promises to pay today, about 4 seconds after the call starts.
test.describe.configure({ mode: "serial" });
test.beforeAll(() => mkdirSync(join(EVIDENCE, "m8"), { recursive: true }));
test.afterAll(() => reseed());

test("merchant calls Sharma about INV-0412 and sees the outcome live", async ({ page }) => {
  const errors = watchErrors(page);
  await page.goto("/collections");
  const row = page.getByRole("row").filter({ hasText: "INV-0412" }).first();
  await expect(row).toBeVisible();
  await expect(page.getByRole("button", { name: "Call", exact: true }).first()).toBeVisible();
  await shot(page, "m8", "01-collections-call-buttons");

  await row.getByRole("link", { name: "Sharma Traders" }).click();
  await expect(page.getByRole("heading", { name: "Invoice INV-0412" })).toBeVisible();
  await page.getByRole("button", { name: "Call customer" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Calling Sharma Traders" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Calling…" })).toBeDisabled();

  // The outcome arrives on its own (realtime, or the 4-second poll while a call rings).
  const call = page.getByTestId("call-history-item").last();
  await expect(call).toContainText("Answered", { timeout: 15_000 });
  await expect(call).toContainText("Promised to pay by");
  await expect(call.getByTestId("call-link-sent")).toContainText("Payment link emailed during the call");
  await expect(call).toContainText("asked for the payment link");
  await call.getByText("Transcript").click();
  await expect(call).toContainText("link bhej do");
  await shot(page, "m8", "02-invoice-call-card");

  await page.goto("/");
  const promises = page.locator("section", { has: page.getByRole("heading", { name: "Promised on calls" }) });
  await expect(promises).toContainText("Sharma Traders");
  await expect(promises).toContainText("Promised today");
  await expect(promises).toContainText("Link sent during the call");
  await promises.scrollIntoViewIfNeeded();
  await shot(page, "m8", "03-dashboard-promised-on-calls");

  await page.goto("/collections");
  await expect(page.getByRole("row").filter({ hasText: "INV-0412" }).first()).toContainText("Called");
  expect(errors).toEqual([]);
});
