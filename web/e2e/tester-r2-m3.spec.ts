/**
 * Tester round 2, M3 dashboard UI, against ARCHITECTURE.md (demo moment 3: "KPIs, trend, peak hours,
 * customer search and a payer profile"; Decisions/Identity: merging moves identifiers; Risks: trigram
 * search) and deck slide 4 (dashboard + "Ram · ram@paytm / ₹100 daily, ~8:30 AM · 121 payments" card).
 *
 * Runs as the demo merchant. Nothing here writes: conflicts are refused by the API, the merge
 * dialog is only previewed and cancelled, and the inline edit is cancelled with Esc.
 */
import { expect, test, type Page, type Request } from "@playwright/test";
import { watchErrors, EVIDENCE } from "./helpers";
import { join } from "node:path";

type Payer = { id: string; display_name: string; txn_count: number; total_paise: number };

async function openPayer(page: Page, query: string, name: string) {
  await page.goto("/customers");
  await page.getByRole("searchbox").fill(query);
  await page.getByRole("link", { name: new RegExp(`^${name.split(" ")[0]}`) }).first().click();
  await expect(page.getByRole("heading", { name, exact: true })).toBeVisible();
}

function apiCalls(page: Page, method: string, part: string): Request[] {
  const seen: Request[] = [];
  page.on("request", (r) => {
    if (r.method() === method && r.url().includes(":8000") && r.url().includes(part)) seen.push(r);
  });
  return seen;
}

/** Type a query and wait for the API answer to *this* query (results from the last query linger while typing). */
async function search(page: Page, q: string) {
  const resp = page.waitForResponse((r) => r.url().includes(`/payers?q=${encodeURIComponent(q.trim())}&`) && r.request().method() === "GET");
  await page.getByRole("searchbox").fill(q);
  const body = (await (await resp).json()) as { payers: Payer[] };
  await page.waitForTimeout(150);
  return body.payers;
}

const inr = (paise: number) =>
  new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: paise % 100 ? 2 : 0 }).format(paise / 100);

test.describe("R2: phone widths (390 and 412) never scroll sideways", () => {
  for (const width of [390, 412]) {
    test(`all M3 pages fit ${width}px, including a B2B payer with a GSTIN`, async ({ browser }) => {
      test.setTimeout(120_000);
      const ctx = await browser.newContext({
        storageState: "e2e/.auth/demo.json",
        viewport: { width, height: 860 },
        isMobile: true,
        hasTouch: true,
        deviceScaleFactor: 2,
      });
      const page = await ctx.newPage();
      const bad: string[] = [];
      const check = async (label: string) => {
        await page.waitForTimeout(900);
        const m = await page.evaluate(() => ({ sw: document.documentElement.scrollWidth, iw: window.innerWidth }));
        if (m.sw > width || m.iw !== width) bad.push(`${label}: scrollWidth ${m.sw}, innerWidth ${m.iw}`);
      };
      await page.goto("/");
      await expect(page.getByRole("heading", { name: "Today’s hisaab" })).toBeVisible();
      await check("dashboard");
      await page.goto("/customers");
      await expect(page.getByRole("link").filter({ hasText: "Sharma Traders" })).toBeVisible();
      await check("customers");
      for (const [q, name] of [["ram@paytm", "Ram"], ["sharmatraders@icici", "Sharma Traders"], ["meena", "Meena Kapoor"]]) {
        await openPayer(page, q, name);
        await check(name);
        if (bad.length && name === "Sharma Traders") {
          await page.screenshot({ path: join(EVIDENCE, "m3", `r2-pw-FAIL-${width}-sharma-profile-scrolls-sideways.png`) });
        }
      }
      await ctx.close();
      expect(bad, bad.join("\n")).toEqual([]);
    });
  }
});

test.describe("R2: customer search", () => {
  test("typing a phone the Indian way (+91 98765…) finds Ram before the number is finished", async ({ page }) => {
    await page.goto("/");
    await search(page, "+91 98765 43210");
    await expect(page.getByRole("link", { name: /Ram · ram@paytm/ })).toBeVisible();
    const partial = await search(page, "+91 98765");
    await page.screenshot({ path: join(EVIDENCE, "m3", "r2-pw-search-plus91-partial.png") });
    expect(partial.map((p) => p.display_name)).toContain("Ram");
    await expect(page.getByRole("link", { name: /Ram · ram@paytm/ })).toBeVisible();
  });

  test("names, UPI IDs and phones in several formats all find the right customer", async ({ page }) => {
    const errors = watchErrors(page);
    await page.goto("/customers");
    for (const [q, name] of [
      ["Raam", "Ram"], ["RAM@PAYTM", "Ram"], ["098765-43210", "Ram"], ["(+91) 98765 43210", "Ram"],
      ["Sarma Traders", "Sharma Traders"], ["accounts@sharmatraders.in", "Sharma Traders"], ["98200 12345", "Sharma Traders"],
      ["Mena Kapur", "Meena Kapoor"], ["meena.kapoor@ybl", "Meena Kapoor"],
    ]) {
      const found = await search(page, q);
      expect(found[0]?.display_name, q).toBe(name);
      await expect(page.locator('main a[href^="/customers/"]').first(), q).toContainText(name);
    }
    expect(errors).toEqual([]);
  });
});

test.describe("R2: dashboard on desktop reads like deck slide 4", () => {
  test.use({ viewport: { width: 1440, height: 1000 } });

  test("KPI row, weekday trend, search card with Regular badge, no console or API errors", async ({ page }) => {
    const errors = watchErrors(page);
    await page.goto("/");
    const glance = page.getByRole("region", { name: "Today at a glance" });
    for (const label of ["Collected today", "Payments", "Repeat customers", "of today’s volume"]) {
      await expect(glance.getByText(label, { exact: true }).first()).toBeVisible();
    }
    await expect(glance.getByText(/new payers?$/)).toBeVisible();
    await expect(page.getByRole("img", { name: /Collections by day, 7 points/ })).toBeVisible();
    await page.getByRole("searchbox").fill("ram");
    const row = page.getByRole("link", { name: /Ram · ram@paytm/ });
    await expect(row).toContainText(/₹100 daily, ~8:30 AM · 121 payments since \d+ \w+/);
    await expect(row.getByText("Regular", { exact: true })).toBeVisible();
    await page.screenshot({ path: join(EVIDENCE, "m3", "r2-pw-desktop-dashboard-search-ram.png") });
    expect(errors).toEqual([]);
  });

  test("30D and 90D tooltips show the API's first and last points", async ({ page }) => {
    await page.goto("/");
    for (const [label, days, grain] of [["30D", 30, "day"], ["90D", 91, "week"]] as const) {
      const resp = page.waitForResponse((r) => r.url().includes(`/dashboard/trend?days=${days}&grain=${grain}`));
      await page.getByRole("button", { name: label }).click();
      const pts = ((await (await resp).json()) as { points: { bucket: string; total_paise: number; txn_count: number }[] }).points;
      const svg = page.getByRole("img", { name: new RegExp(`Collections by ${grain}, ${pts.length} points`) });
      await expect(svg).toBeVisible();
      const box = (await svg.boundingBox())!;
      for (const i of [0, pts.length - 1]) {
        const x = box.x + 44 + (i / (pts.length - 1)) * (box.width - 56);
        await page.mouse.move(x, box.y + box.height / 2);
        const tip = page.locator("div.pointer-events-none.absolute");
        await expect(tip, `${label} point ${i}`).toContainText(inr(pts[i].total_paise));
        await expect(tip, `${label} point ${i}`).toContainText(`${pts[i].txn_count} payment`);
      }
    }
  });
});

test.describe("R2: payer profile edits that must not write", () => {
  test("phone or email owned by another customer is refused with a merge hint and nothing changes", async ({ page }) => {
    const patches = apiCalls(page, "PATCH", "/payers/");
    await openPayer(page, "anita", "Anita Desai");
    const before = await page.locator("section").first().innerText();
    await page.getByRole("button", { name: "Edit details" }).click();
    const dialog = page.getByRole("dialog", { name: "Edit customer" });
    await dialog.getByLabel("Phone").fill("+91 98765 43210");
    await dialog.getByRole("button", { name: "Save changes" }).click();
    await expect(dialog.getByRole("alert")).toContainText("9876543210 already belongs to Ram");
    await expect(dialog.getByRole("alert")).toContainText("merge");
    await dialog.getByLabel("Phone").fill("");
    await dialog.getByLabel("Email").fill("ACCOUNTS@SharmaTraders.in");
    await dialog.getByRole("button", { name: "Save changes" }).click();
    await expect(dialog.getByRole("alert")).toContainText("already belongs to Sharma Traders");
    await dialog.getByRole("button", { name: "Cancel" }).click();
    await expect(dialog).toBeHidden();
    expect(patches.length).toBe(2);
    await page.reload();
    await expect(page.getByRole("heading", { name: "Anita Desai", exact: true })).toBeVisible();
    expect(await page.locator("section").first().innerText()).toBe(before);
  });

  test("merge dialog previews what moves, Back and Esc cancel, no merge request is sent", async ({ page }) => {
    const merges = apiCalls(page, "POST", "/merge");
    const errors = watchErrors(page);
    await openPayer(page, "anita", "Anita Desai");
    await page.getByRole("button", { name: "Merge duplicate" }).click();
    const dialog = page.getByRole("dialog", { name: "Merge into Anita Desai" });
    const resp = page.waitForResponse((r) => r.url().includes("/payers?q=meena"));
    await dialog.getByRole("searchbox").fill("meena");
    const meena = ((await (await resp).json()) as { payers: Payer[] }).payers[0];
    await dialog.getByRole("button", { name: /Meena Kapoor/ }).click();
    await expect(dialog).toContainText(`Meena Kapoor will be folded into Anita Desai`);
    await expect(dialog).toContainText(`${meena.txn_count} payments (${inr(meena.total_paise)})`);
    await page.screenshot({ path: join(EVIDENCE, "m3", "r2-pw-merge-preview-meena-into-anita.png") });
    await dialog.getByRole("button", { name: "Back" }).click();
    await expect(dialog.getByRole("searchbox")).toBeVisible();
    await page.keyboard.press("Escape");
    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
    expect(merges).toEqual([]);
    await page.goto("/customers");
    await page.getByRole("searchbox").fill("meena");
    await expect(page.getByRole("link", { name: /Meena Kapoor/ })).toBeVisible();
    expect(errors).toEqual([]);
  });

  test("Esc cancels an inline description edit without a PATCH", async ({ page }) => {
    const patches = apiCalls(page, "PATCH", "/transactions/");
    await openPayer(page, "ram@paytm", "Ram");
    const firstRow = page.locator("tbody tr").first();
    const original = await firstRow.innerText();
    await firstRow.getByRole("button", { name: /Edit description/ }).click();
    const input = page.getByRole("textbox", { name: "Payment description" });
    await input.fill("Should not be saved");
    await input.press("Escape");
    await expect(input).toBeHidden();
    expect(await firstRow.innerText()).toBe(original);
    expect(patches).toEqual([]);
  });
});

test.describe("R2: trend labels stay legible", () => {
  test.use({ viewport: { width: 1440, height: 1000 } });

  test("the 'so far' note never sits on top of today's x-axis label (e.g. early morning, ₹0 so far)", async ({ page }) => {
    await page.goto("/");
    const svg = page.getByRole("img", { name: /Collections by day, 7 points/ });
    await expect(svg).toBeVisible();
    const boxes = await svg.evaluate((el) => {
      const texts = [...el.querySelectorAll("text")];
      const soFar = texts.find((t) => t.textContent === "so far");
      const lastLabel = texts.filter((t) => t.textContent !== "so far" && !t.textContent!.startsWith("₹")).at(-1);
      const r = (t: Element | undefined) => (t ? t.getBoundingClientRect().toJSON() : null);
      return { soFar: r(soFar), last: r(lastLabel), lastText: lastLabel?.textContent };
    });
    test.skip(!boxes.soFar, "today is not drawn as a partial bucket");
    const a = boxes.soFar!, b = boxes.last!;
    const overlap = a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
    if (overlap) await page.screenshot({ path: join(EVIDENCE, "m3", "r2-pw-FAIL-trend-so-far-overlaps-axis-label.png"), clip: { x: b.left - 260, y: b.top - 80, width: 300, height: 110 } });
    expect(overlap, `"so far" ${JSON.stringify(a)} overlaps "${boxes.lastText}" ${JSON.stringify(b)}`).toBe(false);
  });
});
