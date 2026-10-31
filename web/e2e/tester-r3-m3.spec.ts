import { expect, test, type Page } from "@playwright/test";
import { demoCredentials, shot, watchErrors } from "./helpers";

/** Tester round 3, M3 regression: login redirect, phone-format search, phone widths, merge dialog. */

async function noSidewaysScroll(page: Page) {
  const { sw, cw } = await page.evaluate(() => ({
    sw: document.documentElement.scrollWidth,
    cw: document.documentElement.clientWidth,
  }));
  expect(sw, `page is ${sw}px wide in a ${cw}px viewport`).toBeLessThanOrEqual(cw);
}

test.describe("tester r3 · M3 regression", () => {
  test("signed-out deep link to an invoice goes to login and back", async ({ browser }) => {
    const ctx = await browser.newContext({ storageState: { cookies: [], origins: [] } });
    const page = await ctx.newPage();
    await page.goto("/collections");
    await expect(page).toHaveURL(/\/login\?next=%2Fcollections$/);
    const { email, password } = demoCredentials();
    await page.getByLabel("Email").fill(email);
    await page.getByLabel("Password").fill(password);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page).toHaveURL(/\/collections$/);
    await expect(page.getByRole("heading", { name: "Collections" })).toBeVisible();
    await page.goto("/login");
    await expect(page).toHaveURL(/\/$/);
    await ctx.close();
  });

  for (const q of ["+91 98765 43210", "098765-43210", "919876543210", "(98765) 43210", "Ram"]) {
    test(`dashboard search "${q}" finds Ram first`, async ({ page }) => {
      const errors = watchErrors(page);
      await page.goto("/");
      await page.getByRole("searchbox").fill(q);
      const first = page.locator('main a[href^="/customers/"]').first();
      await expect(first).toBeVisible();
      await expect(first).toContainText("ram@paytm");
      await expect(first).toContainText("₹100 daily");
      expect(errors).toEqual([]);
    });
  }

  for (const [name, width, height] of [
    ["390", 390, 844],
    ["412", 412, 915],
  ] as const) {
    test(`dashboard, customers and Sharma's profile fit ${name}px`, async ({ browser }) => {
      const ctx = await browser.newContext({
        storageState: "e2e/.auth/demo.json",
        viewport: { width, height },
        isMobile: true,
        hasTouch: true,
      });
      const page = await ctx.newPage();
      await page.goto("/");
      await expect(page.getByRole("heading", { name: "Today’s hisaab" })).toBeVisible();
      await page.waitForLoadState("networkidle");
      await noSidewaysScroll(page);
      await shot(page, "m3", `r3-dashboard-${name}`);
      await page.goto("/customers");
      await page.waitForLoadState("networkidle");
      await noSidewaysScroll(page);
      await page.getByRole("searchbox").fill("Sharma");
      await page.getByRole("link", { name: /Sharma Traders/ }).first().click();
      await expect(page.getByRole("heading", { name: "Sharma Traders" })).toBeVisible();
      await page.waitForLoadState("networkidle");
      await noSidewaysScroll(page);
      await shot(page, "m3", `r3-profile-sharma-${name}`);
      await ctx.close();
    });
  }

  test("merge dialog searches other customers and never offers the same payer", async ({ page }) => {
    await page.goto("/customers");
    await page.getByRole("searchbox").fill("Ram");
    await page.getByRole("link", { name: /Ram.*ram@paytm/ }).first().click();
    await page.getByRole("button", { name: "Merge duplicate" }).click();
    const dialog = page.getByRole("dialog", { name: "Merge into Ram" });
    await expect(dialog).toBeVisible();
    await dialog.getByRole("searchbox").fill("Ram");
    await page.waitForTimeout(600);
    await expect(dialog.getByRole("button", { name: /ram@paytm/ })).toHaveCount(0);
    await dialog.getByRole("searchbox").fill("Ravi");
    await expect(dialog.getByRole("button", { name: /Ravi Menon/ })).toBeVisible();
    await shot(page, "m3", "r3-merge-dialog");
    // The first Esc clears the native search input; the next one closes the dialog.
    await page.keyboard.press("Escape");
    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
  });
});
