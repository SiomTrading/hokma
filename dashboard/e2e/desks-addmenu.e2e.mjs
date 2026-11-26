/**
 * "+ Add" menu of the desk bar E2E (task 3420, bug 1264): the menu of the missing panels must be drawn above everything and be
 * clickable at any window size. The bar of the desk is a pane that clips what overflows it, so a menu that hangs from the bar
 * is cut. The suite opens it at 1440x940 and at 2540x1350 and asks the browser (elementsFromPoint) what is on top of every
 * option, then adds "Company" and checks the tab shows up. It also opens the options menu of a desk card.
 *
 *   node e2e/desks-addmenu.e2e.mjs [es|en]        (both locales when omitted)
 */
import assert from "node:assert/strict";
import { runSuite, until } from "./lib.mjs";

await runSuite("desks-addmenu", { script: "desks-serve.py", signedIn: true }, async ({ page, server, msg }) => {
  const made = await server.api("/api/desks", { method: "POST", body: JSON.stringify({ name: "Mesa menu", focus: "x", capital: 5000, template: "minimal", assets: ["NQ=F"] }) });
  const deskId = made.id;
  await page.goto(server.base);
  await page.getByRole("button", { name: msg("nav.desks") }).first().click();
  await page.getByRole("button", { name: msg("desks.card.open", { name: "Mesa menu" }) }).click();
  await page.getByRole("tab", { name: msg("desks.tab.terminal"), exact: true }).waitFor();

  /** What the browser draws on top at the center of each option of the open menu, and whether it is inside the window. */
  const probe = () =>
    page.evaluate((label) => {
      const menu = document.querySelector('ul[aria-label="' + label + '"]');
      if (!menu) return { open: false, options: [] };
      const options = [...menu.querySelectorAll("button")].map((button) => {
        const r = button.getBoundingClientRect();
        const x = r.left + r.width / 2;
        const y = r.top + r.height / 2;
        const top = document.elementsFromPoint(x, y)[0];
        return {
          text: button.textContent,
          onTop: top === button || button.contains(top),
          inside: r.left >= 0 && r.top >= 0 && r.right <= window.innerWidth && r.bottom <= window.innerHeight,
          disabled: button.disabled,
        };
      });
      return { open: true, options };
    }, msg("desks.panels.addLabel"));

  for (const [width, height] of [
    [1440, 940],
    [2540, 1350],
  ]) {
    await page.setViewportSize({ width, height });
    const add = page.getByRole("button", { name: msg("desks.panels.add"), exact: true });
    await add.waitFor();
    await add.click();
    const seen = await until(async () => {
      const p = await probe();
      return p.open && p.options.length > 0 ? p : null;
    }, `the menu open at ${width}x${height}`);
    for (const option of seen.options) {
      assert.ok(option.inside, `${width}x${height}: "${option.text}" is inside the window`);
      if (!option.disabled) assert.ok(option.onTop, `${width}x${height}: "${option.text}" is on top and can be clicked`);
    }
    assert.ok(seen.options.some((o) => !o.disabled), "at least one option can be added");
    await page.keyboard.press("Escape");
    await until(async () => !(await probe()).open, "the menu closed with Escape");
  }

  // adding "Company" with the real mouse: the tab appears and the desk saves the panel
  await page.setViewportSize({ width: 1440, height: 940 });
  await page.getByRole("button", { name: msg("desks.panels.add"), exact: true }).click();
  await page.getByRole("list", { name: msg("desks.panels.addLabel") }).getByRole("button", { name: new RegExp("^" + msg("desks.tab.company")) }).click();
  await page.getByRole("tab", { name: msg("desks.tab.company"), exact: true }).waitFor();
  assert.ok(await page.getByRole("tab", { name: msg("desks.tab.company"), exact: true }).isVisible());
  await until(async () => JSON.stringify(await server.api(`/api/desks/${deskId}`)).includes("company"), "the Company panel saved in the desk");

  // the options menu of a desk card is also on top
  await page.getByRole("button", { name: msg("nav.desks") }).first().click();
  await page.getByRole("button", { name: msg("desks.card.options", { name: "Mesa menu" }) }).click();
  const card = await page.evaluate((label) => {
    const menu = document.querySelector('[role="menu"][aria-label="' + label + '"]');
    const item = menu?.querySelector('[role="menuitem"]');
    if (!item) return null;
    const r = item.getBoundingClientRect();
    const top = document.elementsFromPoint(r.left + r.width / 2, r.top + r.height / 2)[0];
    return top === item || item.contains(top);
  }, msg("desks.card.options", { name: "Mesa menu" }));
  assert.equal(card, true, "the card menu option is on top");
});
