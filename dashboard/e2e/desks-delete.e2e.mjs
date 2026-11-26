/**
 * Delete a money desk E2E (task 3286): the real backend (e2e/desks-serve.py), the real compiled dashboard and a real
 * Chromium. Delete one desk from its Settings (danger zone, typing its name) and another from the menu of its Fund card;
 * the folder goes to the Trash, the other desk stays, and the app is back on the Fund. No UI text lives here.
 *
 *   node e2e/desks-delete.e2e.mjs [es|en]        (both locales when omitted)
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import { runSuite, until } from "./lib.mjs";

await runSuite("desks-delete", { script: "desks-serve.py", signedIn: true }, async ({ page, server, msg }) => {
  const make = (name) =>
    server.api("/api/desks", { method: "POST", body: JSON.stringify({ name, focus: "x", capital: 5000, template: "minimal", assets: [] }) });
  const list = async () => (await server.api("/api/desks")).desks;
  const trashed = async () => (await server.api("/api/trash")).items;
  const first = await make("Mesa Uno");
  const second = await make("Mesa Dos");
  await page.goto(server.base);

  // 1. Settings of the first desk: the button stays off until the exact name is typed
  await page.getByRole("button", { name: msg("nav.desks") }).first().click();
  await page.getByRole("button", { name: msg("desks.card.open", { name: "Mesa Uno" }) }).click();
  await page.getByRole("tab", { name: msg("desks.tab.config"), exact: true }).click();
  await page.getByRole("button", { name: msg("desks.delete.button"), exact: true }).click();
  const dialog = page.getByRole("alertdialog", { name: msg("desks.delete.title") });
  const confirm = dialog.getByRole("button", { name: msg("desks.delete.confirm"), exact: true });
  assert.equal(await confirm.isDisabled(), true, "disabled without the name");
  const field = dialog.getByLabel(msg("desks.delete.type", { name: "Mesa Uno" }));
  await field.fill("mesa uno");
  assert.equal(await confirm.isDisabled(), true, "disabled with a different case");
  await field.fill("Mesa Uno");
  await confirm.click();
  await dialog.waitFor({ state: "detached" });

  // 2. back on the Fund: the first desk is gone from the cards and the menu, the second stays
  await page.getByRole("heading", { name: msg("desks.fund.tab"), level: 1 }).waitFor();
  await page.getByRole("button", { name: msg("desks.card.open", { name: "Mesa Dos" }) }).waitFor();
  assert.equal(await page.getByRole("button", { name: msg("desks.card.open", { name: "Mesa Uno" }) }).count(), 0);
  assert.equal(await page.getByRole("button", { name: new RegExp("^Mesa Uno, ") }).count(), 0, "gone from the menu");
  assert.deepEqual((await list()).map((d) => d.name), ["Mesa Dos"]);
  const items = await until(async () => {
    const found = (await trashed()).filter((i) => i.name === "mesa-uno" || String(i.original_path ?? "").includes("mesa-uno"));
    return found.length ? found : null;
  }, "the folder in the Trash");
  assert.ok(items.length >= 1);
  assert.ok(!fs.existsSync(server.home + "/projects/mesa-uno"), "the folder left its place");
  assert.ok(first.id !== second.id);

  // 3. the menu of the card of the second desk
  await page.getByRole("button", { name: msg("desks.card.options", { name: "Mesa Dos" }) }).click();
  await page.getByRole("menuitem", { name: msg("desks.delete.button"), exact: true }).click();
  const again = page.getByRole("alertdialog", { name: msg("desks.delete.title") });
  await again.getByLabel(msg("desks.delete.type", { name: "Mesa Dos" })).fill("Mesa Dos");
  await again.getByRole("button", { name: msg("desks.delete.confirm"), exact: true }).click();
  await page.getByText(msg("desks.fund.empty.title")).waitFor();
  assert.deepEqual(await list(), []);
});
