/**
 * Desk watchlist reorder E2E (task 3289): the real backend, the real compiled dashboard and a real Chromium. The student
 * drags an asset of "En seguimiento" with the mouse to a new place, then moves another one with Alt + arrow; each order
 * is saved in the desk (the backend answers it, also for the agents) and survives a reload.
 *
 *   node e2e/desks-reorder.e2e.mjs [es|en]        (both locales when omitted)
 */
import assert from "node:assert/strict";
import { reloadWithHandoff, runSuite, until } from "./lib.mjs";

await runSuite("desks-reorder", { script: "desks-serve.py", signedIn: true }, async ({ page, server, msg }) => {
  const made = await server.api("/api/desks", { method: "POST", body: JSON.stringify({ name: "Tres activos", focus: "x", capital: 5000, template: "minimal", assets: ["NQ=F", "AAPL", "MSFT"] }) });
  const deskId = made.id;
  await page.setViewportSize({ width: 1100, height: 760 });
  await page.goto(server.base);
  const open = async () => {
    await page.getByRole("button", { name: msg("nav.desks") }).first().click();
    await page.getByRole("button", { name: msg("desks.card.open", { name: "Tres activos" }) }).click();
    await page.getByLabel(msg("desks.terminal.list")).waitFor();
  };
  await open();
  const list = page.getByLabel(msg("desks.terminal.list"));
  const order = async () => list.locator("button.dterm-asset").allInnerTexts();
  const saved = async () => (await server.api(`/api/desks/${deskId}`)).assets.map((a) => a.symbol);
  assert.deepEqual(await order(), ["NQ=F", "AAPL", "MSFT"]);

  // 1. drag the last asset onto the first one with the real mouse: it goes to the top and the desk saves it
  await list.locator("tr[data-symbol='MSFT']").dragTo(list.locator("tr[data-symbol='NQ=F']"));
  await until(async () => (await order()).join() === "MSFT,NQ=F,AAPL", "the dragged asset in its new place");
  await until(async () => (await saved()).join() === "MSFT,NQ=F,AAPL", "the order saved in the desk");

  // 2. the keyboard: Alt + arrow down moves the focused asset one place and keeps the focus on it
  await list.getByRole("button", { name: "MSFT", exact: true }).focus();
  await page.keyboard.press("Alt+ArrowDown");
  await until(async () => (await order()).join() === "NQ=F,MSFT,AAPL", "Alt + arrow down moves the asset");
  await until(async () => (await saved()).join() === "NQ=F,MSFT,AAPL", "the keyboard order saved in the desk");
  assert.equal(await page.evaluate(() => document.activeElement?.textContent), "MSFT");

  // 3. after a reload the desk shows the saved order, and so do the agents (desk_show reads the same list)
  await reloadWithHandoff(page);
  await open();
  assert.deepEqual(await order(), ["NQ=F", "MSFT", "AAPL"]);
  assert.deepEqual(await saved(), ["NQ=F", "MSFT", "AAPL"]);
});
