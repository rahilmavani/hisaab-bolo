import { expect, test as setup } from "@playwright/test";
import { demoCredentials } from "./helpers";

setup("sign in as the demo merchant", async ({ page }) => {
  const { email, password } = demoCredentials();
  await page.goto("/login");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Today’s hisaab" })).toBeVisible();
  await page.context().storageState({ path: "e2e/.auth/demo.json" });
});
