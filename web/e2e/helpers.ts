import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { Page } from "@playwright/test";

/** Demo login comes from env or the repo's .env.example (a local test account created by the seed). */
export function demoCredentials(): { email: string; password: string } {
  const env: Record<string, string> = {};
  for (const line of readFileSync(
    join(__dirname, "../../.env.example"),
    "utf8",
  ).split("\n")) {
    const m = line.match(/^([A-Z_]+)=(.*)$/);
    if (m) env[m[1]] = m[2];
  }
  return {
    email: process.env.DEMO_MERCHANT_EMAIL ?? env.DEMO_MERCHANT_EMAIL,
    password: process.env.DEMO_MERCHANT_PASSWORD ?? env.DEMO_MERCHANT_PASSWORD,
  };
}

export const EVIDENCE = join(__dirname, "../../evidence");

/** Restore the seeded story after a spec that pays, invoices or schedules things. */
export function reseed() {
  if (process.env.E2E_SKIP_SEED) return; // keep data someone is using (like global-setup)
  execFileSync("uv", ["run", "python", "-m", "app.seed"], {
    cwd: join(__dirname, "../../api"),
    stdio: "ignore",
  });
}

export async function shot(page: Page, milestone: string, name: string) {
  await page.screenshot({
    path: join(EVIDENCE, milestone, `${name}.png`),
    fullPage: true,
  });
}

/** Collects console errors and failed API calls so each test can assert there were none. */
export function watchErrors(page: Page) {
  const errors: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() === "error") errors.push(`console: ${msg.text()}`);
  });
  page.on("pageerror", (err) => errors.push(`pageerror: ${err.message}`));
  page.on("response", (res) => {
    if (res.url().includes(":8000") && res.status() >= 400)
      errors.push(`api ${res.status()}: ${res.url()}`);
  });
  return errors;
}
