import { expect, test } from "@playwright/test";
import { shot, watchErrors } from "./helpers";

// These call the real Claude (and Sarvam) APIs, so answers vary in wording: assert on grounded facts.
test.describe("M6 Ask Hisaab", () => {
  test.setTimeout(90_000);

  test("typed Hinglish question answers with the payments behind it", async ({ page }) => {
    const errors = watchErrors(page);
    await page.goto("/");
    const panel = page.getByRole("complementary", { name: "Ask Hisaab" });
    await expect(panel.getByText("Hindi · English · Hinglish")).toHaveCount(0);
    await panel.getByLabel("Type a question").fill("Ram ne pichhle 4 mahine mein kitna diya?");
    await panel.getByRole("button", { name: "Send question" }).click();
    const answer = panel.locator("p", { hasText: "12,100" }).first();
    await expect(answer).toBeVisible({ timeout: 45_000 });
    await expect(panel.getByText(/121/).first()).toBeVisible();
    await expect(panel.getByRole("link", { name: /Ram/ }).first()).toBeVisible(); // payer card
    await expect(panel.getByRole("button", { name: /Spoken back in Hinglish/ })).toBeVisible();
    await shot(page, "m6", "01-ask-typed-ram");
    expect(errors).toEqual([]);
  });

  test("a reminder request needs a confirm tap before anything is sent", async ({ page }) => {
    await page.goto("/");
    const panel = page.getByRole("complementary", { name: "Ask Hisaab" });
    await panel.getByLabel("Type a question").fill("Sharma Traders ko reminder bhej do");
    await panel.getByRole("button", { name: "Send question" }).click();
    const confirm = panel.getByRole("button", { name: "Confirm: Send reminder" });
    await expect(confirm).toBeVisible({ timeout: 45_000 });
    await expect(panel.getByText("Needs your OK")).toBeVisible();
    await shot(page, "m6", "02-ask-confirm-reminder");
    await confirm.click();
    await expect(panel.getByText(/Reminder emailed to accounts@sharmatraders.in/)).toBeVisible({ timeout: 20_000 });
    await shot(page, "m6", "03-ask-reminder-sent");
  });

  test("outstanding dues come back as a table", async ({ page }) => {
    await page.goto("/");
    const panel = page.getByRole("complementary", { name: "Ask Hisaab" });
    await panel.getByRole("button", { name: "Kiske payment pending hain?" }).click();
    await expect(panel.getByText(/11,700/).first()).toBeVisible({ timeout: 45_000 });
    await expect(panel.getByRole("link", { name: /Sharma Traders/ })).toBeVisible();
    await shot(page, "m6", "04-ask-pending");
  });
});
