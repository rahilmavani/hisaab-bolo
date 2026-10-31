import { expect, test, type Page } from "@playwright/test";
import { shot } from "./helpers";

// Mobile emulation widens window.innerWidth when content overflows, so compare against the device width.
async function overflow(page: Page) {
  const width = page.viewportSize()!.width;
  return page.evaluate((w) => document.documentElement.scrollWidth - w, width);
}

test("dashboard and a customer profile fit a phone screen without sideways scroll", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Today’s hisaab" })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Main" }).last()).toBeVisible();
  await expect(page.getByText(/last paid 12 days ago/)).toBeVisible();
  expect(await overflow(page)).toBeLessThanOrEqual(0);
  await shot(page, "m3", "08-mobile-dashboard");

  await page.goto("/customers");
  await page.getByRole("searchbox").fill("ram");
  await page.getByRole("link", { name: /Ram · ram@paytm/ }).click();
  await expect(page.getByRole("heading", { name: "Ram", exact: true })).toBeVisible();
  expect(await overflow(page)).toBeLessThanOrEqual(0);
  await shot(page, "m3", "09-mobile-profile");
});
