/**
 * Desk watchlist E2E (task 3288): the real backend, the real compiled dashboard and a real Chromium. A desk with two
 * assets on a SHORT window (the real one is often short, with the dock open): a mouse click on the second asset must
 * reach it (the positions panel used to cover it), the chart and the quote change, the row is highlighted, the arrow
 * keys move the focus, and the desk remembers the asset after a reload.
 *
 *   node e2e/desks-focus.e2e.mjs [es|en]        (both locales when omitted)
 */
import assert from "node:assert/strict";
import { reloadWithHandoff, runSuite, until } from "./lib.mjs";

await runSuite("desks-focus", { script: "desks-serve.py", signedIn: true }, async ({ page, server, msg }) => {
  await server.api("/api/desks", { method: "POST", body: JSON.stringify({ name: "Dos activos", focus: "x", capital: 5000, template: "minimal", assets: ["NQ=F", "AAPL"] }) });
  await page.setViewportSize({ width: 1100, height: 700 });
  await page.goto(server.base);
  await page.getByRole("button", { name: msg("nav.desks") }).first().click();
  await page.getByRole("button", { name: msg("desks.card.open", { name: "Dos activos" }) }).click();
  const list = page.getByLabel(msg("desks.terminal.list"));
  await list.waitFor();
  const title = page.locator(".dterm-chart .dterm-tab").first();
  const asset = (symbol) => list.getByRole("button", { name: symbol, exact: true });
  await until(async () => (await title.innerText()).includes("NQ=F"), "the first asset in the chart");

  // 1. a real mouse click at the coordinates of the second asset lands on it (nothing covers it)
  const box = await asset("AAPL").boundingBox();
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
  await until(async () => (await title.innerText()).includes("AAPL"), "the chart of the second asset");
  assert.equal(await asset("AAPL").getAttribute("aria-pressed"), "true");
  assert.equal(await asset("NQ=F").getAttribute("aria-pressed"), "false");
  await list.getByText(msg("desks.terminal.quote", { symbol: "AAPL" })).waitFor();
  assert.ok(/\bon\b/.test((await asset("AAPL").locator("xpath=ancestor::tr").getAttribute("class")) ?? ""), "the row is highlighted");

  // 2. the keyboard: arrow up goes back to the first asset, arrow down and Enter to the second
  await asset("AAPL").focus();
  await page.keyboard.press("ArrowUp");
  await until(async () => (await title.innerText()).includes("NQ=F"), "arrow up picks the asset above");
  assert.equal(await asset("NQ=F").getAttribute("aria-pressed"), "true");
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await until(async () => (await title.innerText()).includes("AAPL"), "arrow down and Enter pick the asset below");

  // 3. the desk remembers it: after a reload the second asset is still the one in focus
  await reloadWithHandoff(page);
  await page.getByRole("button", { name: msg("nav.desks") }).first().click();
  await page.getByRole("button", { name: msg("desks.card.open", { name: "Dos activos" }) }).click();
  await list.waitFor();
  await until(async () => (await title.innerText()).includes("AAPL"), "the remembered asset after a reload");
  assert.equal(await asset("AAPL").getAttribute("aria-pressed"), "true");
});
