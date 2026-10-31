/**
 * Tester M3: dashboard UI against ARCHITECTURE.md (demo moment 3: "KPIs, trend, peak hours,
 * customer search and a payer profile"; Low-friction ledger: lost-regular alert, slow-day
 * banner, merchant description edits) and the deck's slide 4.
 *
 * Read-only checks run as the demo merchant (storageState). Everything that writes signs in as
 * a throwaway merchant created here and deleted afterwards.
 */
import { execFileSync } from "node:child_process";
import { join } from "node:path";
import { devices, expect, test, type Page, type Response } from "@playwright/test";
import { demoCredentials, watchErrors } from "./helpers";

const API_DIR = join(__dirname, "../../api");

type Summary = {
  now: string;
  date: string;
  kpis: { collected_paise: number; payments: number; new_payers: number; repeat_share: number; change_vs_last_week: number | null };
  trend: { bucket: string; total_paise: number; txn_count: number }[];
  heatmap: { dow: number; hour: number; total_paise: number; txn_count: number }[];
  lost_regulars: { id: string; display_name: string; last_paid_at: string; days_since_last: number }[];
  slow_day: { is_slow: boolean; drop_pct: number } | null;
};

const inr = (paise: number) =>
  new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: paise % 100 ? 2 : 0 }).format(paise / 100);

async function loadDashboard(page: Page): Promise<Summary> {
  const resp = page.waitForResponse((r: Response) => r.url().includes("/dashboard/summary") && r.request().method() === "GET");
  await page.goto("/");
  const body = (await (await resp).json()) as Summary;
  await expect(page.getByRole("heading", { name: "Today’s hisaab" })).toBeVisible();
  return body;
}

function istDate(iso: string): string {
  return new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Kolkata" }).format(new Date(iso));
}

test.describe("Tester M3: dashboard reads the API faithfully", () => {
  test("KPI tiles show the API numbers", async ({ page }) => {
    const errors = watchErrors(page);
    const d = await loadDashboard(page);
    const glance = page.getByRole("region", { name: "Today at a glance" });
    await expect(glance).toContainText(inr(d.kpis.collected_paise));
    await expect(glance).toContainText(`${d.kpis.payments}`);
    await expect(glance).toContainText(`${d.kpis.new_payers} new payer`);
    await expect(glance).toContainText(`${Math.round(d.kpis.repeat_share * 100)}%`);
    if (d.slow_day?.is_slow) await expect(page.getByRole("status").first()).toContainText(`${d.slow_day.drop_pct}% below`);
    expect(errors).toEqual([]);
  });

  test("7-day trend tooltip shows each day's total and count", async ({ page }) => {
    const d = await loadDashboard(page);
    const svg = page.getByRole("img", { name: /Collections by day/ });
    const box = (await svg.boundingBox())!;
    const n = d.trend.length;
    for (const i of [0, Math.floor(n / 2), n - 1]) {
      const x = box.x + 44 + (i / (n - 1)) * (box.width - 56);
      await page.mouse.move(x, box.y + box.height / 2);
      const tip = page.locator("div.pointer-events-none.absolute");
      await expect(tip).toContainText(inr(d.trend[i].total_paise));
      await expect(tip).toContainText(`${d.trend[i].txn_count} payment`);
    }
  });

  test("trend y-axis middle tick is half of the top tick", async ({ page }) => {
    await loadDashboard(page);
    for (const range of ["7D", "30D", "90D"]) {
      await page.getByRole("button", { name: range }).click();
      await page.waitForTimeout(600);
      const labels = await page.locator("svg[aria-label^='Collections by'] text.fill-faint").allTextContents();
      const value = (s: string) => {
        const m = s.replace("₹", "").match(/^([\d.]+)([KL]?)$/)!;
        return parseFloat(m[1]) * (m[2] === "L" ? 100_000 : m[2] === "K" ? 1_000 : 1);
      };
      const [zero, mid, top] = labels.map(value);
      expect(zero).toBe(0);
      expect(mid * 2, `${range} axis labels ${labels.join(" / ")}`).toBe(top);
    }
  });

  test("30D and 90D x-axis labels do not overlap", async ({ page }) => {
    await loadDashboard(page);
    for (const range of ["30D", "90D"]) {
      await page.getByRole("button", { name: range }).click();
      await page.waitForTimeout(800);
      const boxes = await page.locator("svg[aria-label^='Collections by'] text.fill-muted").evaluateAll((els) =>
        els
          .filter((e) => e.getAttribute("y") === String(Number((e.closest("svg") as SVGSVGElement).getAttribute("height")) - 8))
          .map((e) => {
            const r = e.getBoundingClientRect();
            return { text: e.textContent, left: r.left, right: r.right };
          }),
      );
      for (let i = 1; i < boxes.length; i++) {
        expect(boxes[i].left, `${range}: "${boxes[i - 1].text}" overlaps "${boxes[i].text}"`).toBeGreaterThanOrEqual(boxes[i - 1].right);
      }
    }
  });

  test("peak-hours hover reads the heatmap cell", async ({ page }) => {
    const d = await loadDashboard(page);
    const section = page.locator("section", { has: page.getByRole("heading", { name: "Peak hours" }) });
    const busiest = [...d.heatmap].sort((a, b) => b.total_paise - a.total_paise)[0];
    const cell = section.locator("div.contents").nth(busiest.dow - 1).locator("> div").nth(1 + busiest.hour - 6);
    await cell.hover();
    await expect(section.locator("[aria-live=polite]")).toContainText(
      `${inr(busiest.total_paise)} from ${busiest.txn_count} payments`,
    );
  });

  test("Gone quiet 'last paid N days ago' agrees with the profile's Last paid date", async ({ page }) => {
    const d = await loadDashboard(page);
    expect(d.lost_regulars.length).toBeGreaterThan(0);
    const p = d.lost_regulars[0];
    const calendarDays = Math.round(
      (Date.parse(istDate(d.now)) - Date.parse(istDate(p.last_paid_at))) / 86_400_000,
    );
    const quiet = page.locator("section", { has: page.getByRole("heading", { name: "Gone quiet" }) });
    await expect(quiet).toContainText(`${p.display_name} · last paid ${calendarDays} days ago`);
  });
});

test.describe("Tester M3: search, profile and dialogs (demo, read-only)", () => {
  // Results keep the previous list while the next query loads, so wait for this query's response.
  const searchFor = async (page: Page, q: string) => {
    const resp = page.waitForResponse((r) => r.url().includes(`/payers?q=${encodeURIComponent(q)}&`));
    await page.getByRole("searchbox").first().fill(q);
    await resp;
    await page.waitForTimeout(150);
  };

  test("customer search finds Ram by phone typed the Indian way", async ({ page }) => {
    await page.goto("/customers");
    await searchFor(page, "+91 98765 43210");
    await expect(page.getByRole("link", { name: /Ram · ram@paytm/ })).toBeVisible({ timeout: 2000 });
  });

  test("search shows the deck's Ram card for name, UPI ID and phone", async ({ page }) => {
    await page.goto("/");
    for (const q of ["ram", "raam", "ram@paytm", "9876543210"]) {
      await searchFor(page, q);
      const row = page.getByRole("link", { name: /Ram · ram@paytm/ });
      await expect(row).toBeVisible();
      await expect(row).toContainText(/₹100 daily, ~8:\d\d AM · 121 payments since \d+ \w+/);
    }
  });

  test("a malformed customer URL shows the not-found message, not raw JSON", async ({ page }) => {
    await page.goto("/customers/not-a-uuid");
    const alert = page.getByRole("alert").filter({ hasNotText: /^$/ }).and(page.locator(".panel"));
    await expect(alert).toBeVisible();
    await expect(alert).not.toContainText("uuid_parsing");
    await expect(alert).toContainText("doesn't exist");
  });

  test("merge dialog focuses its search box when it opens", async ({ page }) => {
    await page.goto("/customers");
    await page.getByRole("searchbox").fill("meena");
    await page.getByRole("link", { name: /Meena Kapoor/ }).click();
    await page.getByRole("button", { name: "Merge duplicate" }).click();
    const dialog = page.getByRole("dialog", { name: /Merge into/ });
    await expect(dialog.getByRole("searchbox")).toBeFocused();
  });

  test("closing the merge dialog on the confirm step forgets the pick", async ({ page }) => {
    await page.goto("/customers");
    await page.getByRole("searchbox").fill("meena");
    await page.getByRole("link", { name: /Meena Kapoor/ }).click();
    await page.getByRole("button", { name: "Merge duplicate" }).click();
    const dialog = page.getByRole("dialog", { name: /Merge into/ });
    await dialog.getByRole("searchbox").fill("anita");
    await dialog.getByRole("button", { name: /Anita Desai/ }).click();
    await expect(dialog.getByRole("button", { name: "Merge Anita Desai" })).toBeVisible();
    await dialog.getByRole("button", { name: "Close" }).click();
    await expect(dialog).toBeHidden();
    await page.getByRole("button", { name: "Merge duplicate" }).click();
    await expect(dialog.getByRole("searchbox")).toBeVisible();
    await expect(dialog.getByRole("button", { name: "Merge Anita Desai" })).toHaveCount(0);
    await dialog.getByRole("button", { name: "Close" }).click();
  });

  test("Show all payments toggles the full list", async ({ page }) => {
    await page.goto("/customers");
    await page.getByRole("searchbox").fill("ram@paytm");
    await page.getByRole("link", { name: /Ram · ram@paytm/ }).click();
    const payments = page.locator("section", { has: page.getByRole("heading", { name: "Payments" }) });
    await expect(payments.locator("tbody tr")).toHaveCount(15);
    await payments.getByRole("button", { name: "Show all 121 payments" }).click();
    await expect(payments.locator("tbody tr")).toHaveCount(121);
  });
});

test.describe("Tester M3: phone widths", () => {
  for (const name of ["Pixel 7", "iPhone 13"] as const) {
    test(`dashboard and payer profile do not scroll sideways on ${name}`, async ({ browser }) => {
      const { defaultBrowserType: _ignored, ...dev } = devices[name];
      const ctx = await browser.newContext({ ...dev, storageState: "e2e/.auth/demo.json" });
      const page = await ctx.newPage();
      const width = dev.viewport!.width;
      for (const path of ["/", "/customers"]) {
        await page.goto(path);
        await page.getByRole("heading").first().waitFor();
        await page.waitForTimeout(800);
        const m = await page.evaluate(() => ({
          layout: window.innerWidth,
          widest: Math.max(0, ...[...document.querySelectorAll("main *")].map((e) => e.getBoundingClientRect().right)),
        }));
        expect(m.layout, `${path}: layout viewport grew past the ${width}px screen`).toBe(width);
        expect(m.widest, `${path}: content runs to ${Math.round(m.widest)}px`).toBeLessThanOrEqual(width + 1);
      }
      await page.goto("/customers");
      await page.getByRole("searchbox").fill("ram@paytm");
      await page.getByRole("link", { name: /Ram · ram@paytm/ }).click();
      await expect(page.getByRole("heading", { name: "Ram", exact: true })).toBeVisible();
      await page.waitForTimeout(800);
      const layout = await page.evaluate(() => window.innerWidth);
      expect(layout, "payer profile: layout viewport grew past the screen").toBe(width);
      await ctx.close();
    });
  }
});

test.describe("Tester M3: sign-in", () => {
  test("next= only redirects within the app", async ({ browser }) => {
    const ctx = await browser.newContext({ storageState: { cookies: [], origins: [] } });
    const page = await ctx.newPage();
    const { email, password } = demoCredentials();
    await page.goto("/login?next=//127.0.0.1:8000/docs");
    await page.getByLabel("Email").fill(email);
    await page.getByLabel("Password").fill(password);
    await page.getByRole("button", { name: "Sign in" }).click();
    await page.waitForLoadState("networkidle");
    expect(new URL(page.url()).origin, `landed on ${page.url()}`).toBe("http://localhost:3000");
    await ctx.close();
  });

  test("next= to an app page lands there after sign-in, and sign-out returns to login", async ({ browser }) => {
    const ctx = await browser.newContext({ storageState: { cookies: [], origins: [] } });
    const page = await ctx.newPage();
    const { email, password } = demoCredentials();
    await page.goto("/customers");
    await expect(page).toHaveURL(/\/login\?next=%2Fcustomers/);
    await page.getByLabel("Email").fill(email);
    await page.getByLabel("Password").fill("definitely-wrong");
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByRole("alert").filter({ hasText: "match" })).toContainText("don't match");
    await page.getByLabel("Password").fill(password);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page).toHaveURL(/\/customers$/);
    await page.getByRole("button", { name: /Sign out/ }).first().click();
    await expect(page).toHaveURL(/\/login/);
    await page.goto("/");
    await expect(page).toHaveURL(/\/login/);
    await ctx.close();
  });
});

// ---------------------------------------------------------------------------
// Writes: a throwaway merchant with its own login.
// ---------------------------------------------------------------------------
type Throwaway = { email: string; password: string; merchant_id: string; user_id: string; a: string; b: string };

const SETUP_PY = `
import json, uuid, psycopg
from tests.conftest import create_user
from app.config import get_settings
s = get_settings()
email = f"tester-e2e-{uuid.uuid4().hex[:8]}@hisaabbolo.test"; pw = "pw-" + uuid.uuid4().hex
uid = create_user(s, email, pw)
with psycopg.connect(s.database_url, autocommit=True) as c:
    m = c.execute("insert into merchants (name, owner_user_id) values ('Tester E2E Store', %s) returning id", (uid,)).fetchone()[0]
    a = c.execute("insert into payers (merchant_id, display_name, email) values (%s, 'Asha Test', 'asha@example.com') returning id", (m,)).fetchone()[0]
    b = c.execute("insert into payers (merchant_id, display_name, phone, notes) values (%s, 'Asha T (UPI)', '9000000001', 'from upi') returning id", (m,)).fetchone()[0]
    c.execute("insert into payer_identifiers (payer_id, merchant_id, kind, value) values (%s,%s,'vpa','asha@okaxis'), (%s,%s,'vpa','asha.t@ybl'), (%s,%s,'phone','9000000001')", (a, m, b, m, b, m))
    for i, p in enumerate([a, a, b]):
        c.execute("insert into transactions (merchant_id, payer_id, amount_paise, paid_at, source, description, description_by) values (%s, %s, %s, app_now() - %s * interval '1 day', 'seed', 'Groceries', 'ai')", (m, p, 25000 + i * 100, i + 1))
print(json.dumps({"email": email, "password": pw, "merchant_id": str(m), "user_id": str(uid), "a": str(a), "b": str(b)}))
`;

const TEARDOWN_PY = (t: Throwaway) => `
import psycopg
from tests.conftest import delete_user
from app.config import get_settings
s = get_settings()
with psycopg.connect(s.database_url, autocommit=True) as c:
    c.execute("delete from merchants where id = %s", ("${t.merchant_id}",))
delete_user(s, "${t.user_id}")
`;

function py(code: string): string {
  return execFileSync("uv", ["run", "python", "-c", code], { cwd: API_DIR, encoding: "utf8" }).trim().split("\n").pop()!;
}

test.describe("Tester M3: edits on a throwaway merchant", () => {
  test.describe.configure({ mode: "serial" });
  let t: Throwaway;
  let page: Page;

  test.beforeAll(async ({ browser }) => {
    t = JSON.parse(py(SETUP_PY));
    const ctx = await browser.newContext({ storageState: { cookies: [], origins: [] }, viewport: { width: 1440, height: 1000 } });
    page = await ctx.newPage();
    await page.goto("/login");
    await page.getByLabel("Email").fill(t.email);
    await page.getByLabel("Password").fill(t.password);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByRole("heading", { name: "Today’s hisaab" })).toBeVisible();
  });

  test.afterAll(async () => {
    await page?.context().close();
    if (t) py(TEARDOWN_PY(t));
  });

  test("the throwaway merchant only sees its own customers", async () => {
    await page.goto("/customers");
    await expect(page.locator('a[href^="/customers/"]')).toHaveCount(2);
    await expect(page.getByText("Tester E2E Store")).toBeVisible();
  });

  test("editing details saves, normalises and adds identifiers", async () => {
    const errors = watchErrors(page);
    await page.goto(`/customers/${t.a}`);
    await page.getByRole("button", { name: "Edit details" }).click();
    const dialog = page.getByRole("dialog", { name: "Edit customer" });
    await dialog.getByLabel("Phone").fill("9123456780");
    await dialog.getByLabel("GSTIN").fill("27abcde1234f1z5");
    await dialog.getByLabel("Email").fill("Asha.New@Example.com");
    await dialog.getByLabel("Notes").fill("Prefers evening delivery");
    await dialog.getByRole("button", { name: "Save changes" }).click();
    await expect(dialog).toBeHidden();
    await expect(page.getByText("GSTIN 27ABCDE1234F1Z5")).toBeVisible();
    const ids = page.locator("section", { has: page.getByRole("heading", { name: "How we recognise them" }) });
    await expect(ids).toContainText("9123456780");
    await expect(ids).toContainText("asha.new@example.com");
    await expect(page.getByText("Prefers evening delivery")).toBeVisible();
    await page.reload();
    await expect(page.getByText("GSTIN 27ABCDE1234F1Z5")).toBeVisible();
    expect(errors).toEqual([]);
  });

  test("inline description edit replaces the AI text and survives reload", async () => {
    await page.goto(`/customers/${t.a}`);
    await expect(page.getByLabel("Written by Hisaab AI").first()).toBeVisible();
    await page.getByRole("button", { name: /Edit description for/ }).first().click();
    const input = page.getByRole("textbox", { name: "Payment description" });
    await input.fill("Diwali sweets box");
    await input.press("Enter");
    await expect(page.getByText("Diwali sweets box")).toBeVisible();
    await page.reload();
    const row = page.getByRole("row", { name: /Diwali sweets box/ });
    await expect(row).toBeVisible();
    await expect(row.getByLabel("Written by Hisaab AI")).toHaveCount(0);
  });

  test("merging a duplicate folds history in and the duplicate disappears", async () => {
    await page.goto(`/customers/${t.a}`);
    await page.getByRole("button", { name: "Merge duplicate" }).click();
    const dialog = page.getByRole("dialog", { name: "Merge into Asha Test" });
    await dialog.getByRole("searchbox").fill("asha");
    await dialog.getByRole("button", { name: /Asha T \(UPI\)/ }).click();
    await expect(dialog).toContainText("1 payment (₹252)");
    await dialog.getByRole("button", { name: "Merge Asha T (UPI)" }).click();
    await expect(dialog).toBeHidden();
    const tiles = page.locator("section[aria-labelledby=payer-name]");
    await expect(tiles).toContainText("₹753");
    const ids = page.locator("section", { has: page.getByRole("heading", { name: "How we recognise them" }) });
    await expect(ids).toContainText("asha.t@ybl");
    await page.goto(`/customers/${t.b}`);
    await expect(page).toHaveURL(new RegExp(`/customers/${t.a}$`));
    await page.goto("/customers");
    await expect(page.locator('a[href^="/customers/"]')).toHaveCount(1);
  });
});
