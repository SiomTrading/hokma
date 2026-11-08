/**
 * Permissions E2E (P1, P2, P7, P8): an installation that came from an earlier version, against the real backend, the
 * real compiled dashboard and the fake `claude`.
 *
 *   node e2e/permissions.e2e.mjs [es|en]        (both locales when omitted)
 *
 * P1  the pill says "ask first" and the one-time notice is in the chat
 * P2  "Turn on" goes through the modal, the pill and Settings follow, the choice survives a reload, and the fake
 *     CLI is launched with `--permission-mode bypassPermissions` or `default` according to the mode
 * P7  what an agent moved to the trash is listed in Settings > Trash and "Restore" brings it back identical;
 *     "Empty trash" asks first
 * P8  the DOM of Settings has no switch for the always-on protections
 * Also the "Blocked" card in the chat (live and after a reload) and the restore points of the project.
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { reloadWithHandoff, runSuite, until } from "./lib.mjs";

const starts = (log) =>
  fs.existsSync(log) ? fs.readFileSync(log, "utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l)).filter((e) => e.event === "start") : [];

await runSuite("permissions", { script: "permissions-serve.py" }, async ({ page, server, msg, M }) => {
  const project = path.join(server.home, "projects", "strategy");
  const box = page.locator("#chat-message");
  const pill = page.getByTestId("permission-pill");
  const idle = () => page.getByRole("button", { name: msg("chat.composer.send") }).waitFor({ timeout: 20000 });
  const turn = async (text) => {
    await box.fill(text);
    await box.press("Enter");
  };
  const openSettings = async (section) => {
    await page.getByRole("button", { name: M["nav.settings"] }).click();
    const dialog = page.getByRole("dialog", { name: msg("settings.title") });
    await dialog.getByRole("button", { name: msg(`settings.${section}`) }).click();
    return dialog;
  };
  const closeSettings = () => page.getByRole("button", { name: msg("settings.close") }).click();
  const mode = async () => (await server.api("/api/profile/permission-mode")).mode;

  await page.goto(server.base);
  await box.waitFor();

  // P1. migrated installation: "ask first" and the one-time notice
  await pill.waitFor();
  assert.equal(await pill.getAttribute("data-mode"), "ask");
  assert.ok((await pill.innerText()).includes(msg("permissions.pill.ask")));
  assert.equal(await mode(), "ask");
  const notice = page.getByTestId("permission-notice");
  await notice.waitFor();
  assert.ok((await notice.innerText()).includes(msg("permissions.notice.title")));

  // a turn in "ask" is launched with the old flag
  await turn("hello there");
  await idle();
  assert.ok(starts(server.fakeLog).at(-1).argv.join(" ").includes("--permission-mode default"), "ask launches with default");

  // P2. "Turn on" asks once in a short modal, then the pill, the server and the next launch follow
  await notice.getByRole("button", { name: msg("permissions.notice.turnOn") }).click();
  const modal = page.getByRole("dialog", { name: msg("permissions.modal.title") });
  await modal.waitFor();
  assert.ok((await modal.innerText()).includes(msg("permissions.modal.body")));
  assert.equal(await mode(), "ask", "nothing changes before the owner confirms");
  await modal.getByRole("button", { name: msg("permissions.modal.confirm") }).click();
  await until(async () => (await pill.getAttribute("data-mode")) === "auto", "the pill follows");
  assert.equal(await mode(), "auto");
  assert.equal((await server.api("/api/profile/permission-mode")).intro_seen, true);
  await notice.waitFor({ state: "detached" });

  await turn("second turn");
  await idle();
  assert.ok(starts(server.fakeLog).at(-1).argv.join(" ").includes("--permission-mode bypassPermissions"), "auto launches with bypassPermissions");

  // the choice survives a reload, and the notice does not come back
  await reloadWithHandoff(page);
  await pill.waitFor();
  assert.equal(await pill.getAttribute("data-mode"), "auto");
  assert.equal(await page.getByTestId("permission-notice").count(), 0);

  // one click in the pill menu, no modal the second time
  await pill.click();
  await page.getByRole("menuitem", { name: new RegExp(msg("permissions.mode.ask")) }).click();
  await until(async () => (await mode()) === "ask", "ask saved from the pill");
  assert.equal(await page.getByRole("dialog").count(), 0);

  // P2/P8. Settings > General > Permissions: the mode switch and the protections without any switch
  let dialog = await openSettings("general");
  const section = dialog.getByRole("region", { name: msg("permissions.title") });
  await section.waitFor();
  const list = section.getByTestId("protections");
  await list.waitFor();
  assert.equal(await list.getByRole("listitem").count(), 7, "seven protections");
  for (const rule of ["delete", "truncate", "hokma_internal", "force_push", "outside_workdir", "secrets", "real_money"]) {
    assert.ok((await list.innerText()).includes(msg(`protections.${rule}`)), `${rule} listed`);
  }
  assert.equal(await list.locator("input, button, select, [role=switch], [role=checkbox]").count(), 0, "no control inside the protections");
  assert.equal(await section.getByRole("switch").count(), 1, "the only switch of the section is the mode");
  const toggle = section.getByRole("switch", { name: msg("permissions.mode.auto") });
  assert.equal(await toggle.getAttribute("aria-checked"), "false");
  await toggle.click();
  await until(async () => (await mode()) === "auto", "auto saved from Settings");
  await until(async () => (await toggle.getAttribute("aria-checked")) === "true", "the switch follows the saved mode");
  await closeSettings();
  await until(async () => (await pill.getAttribute("data-mode")) === "auto", "the pill follows Settings");

  // P7. the file the agent moved to the trash is listed, and Restore brings it back identical
  assert.ok(!fs.existsSync(path.join(project, "notas.md")), "the agent's move took it out of the folder");
  dialog = await openSettings("trash");
  const item = dialog.getByTestId("trash-item");
  await item.waitFor();
  assert.equal(await item.count(), 1);
  const text = await item.innerText();
  assert.ok(text.includes("notas.md") && text.includes(path.join(project, "notas.md")) && text.includes("the owner asked to clean up"));
  assert.ok(text.includes(msg("trash.project", { name: "Strategy" })));
  assert.ok((await dialog.getByTestId("trash-total").innerText()).length > 0);
  await item.getByRole("button", { name: msg("trash.restore") }).click();
  await until(() => fs.existsSync(path.join(project, "notas.md")), "the file is back");
  assert.equal(fs.readFileSync(path.join(project, "notas.md"), "utf8"), "original notes\n");
  await dialog.getByText(msg("trash.empty.none")).waitFor();
  assert.equal((await server.api("/api/trash")).count, 0);

  // with nothing in the trash the "Empty trash" button is off
  assert.equal(await dialog.getByRole("button", { name: msg("trash.empty.button") }).isDisabled(), true);

  // restore points: the project has one, and "Restore all deleted" brings back the file that disappeared
  // (the engine already took one for the main project before the "auto" turns: pick the project of the trash item)
  await dialog.getByLabel(msg("points.project")).selectOption({ label: "Strategy" });
  const point = dialog.getByTestId("restore-point");
  await point.waitFor();
  assert.equal(await point.count(), 1);
  assert.ok(!fs.existsSync(path.join(project, "informes", "viejo.md")));
  await point.getByRole("button", { name: msg("points.viewChanges") }).click();
  const changes = dialog.getByTestId("point-changes");
  await changes.waitFor();
  assert.ok((await changes.innerText()).includes("informes/viejo.md"));
  await changes.getByRole("button", { name: msg("points.restoreFile") }).first().click();
  await until(() => fs.existsSync(path.join(project, "informes", "viejo.md")), "the missing file is restored");
  assert.equal(fs.readFileSync(path.join(project, "informes", "viejo.md"), "utf8"), "old report\n");
  await closeSettings();

  // the "Blocked" card: live, with "View in Trash", and still there after a reload
  await turn("please delete the notes [block]");
  const card = page.getByTestId("block-card");
  await card.waitFor({ timeout: 20000 });
  await idle();
  const cardText = await card.innerText();
  assert.ok(cardText.includes(msg("block.title.delete")), "title of the rule");
  assert.ok(cardText.includes(msg("block.wanted", { target: "rm informes/viejo.md" })), "what it tried");
  assert.ok(cardText.includes(msg("block.why.delete")), "why in one line");
  await card.getByRole("button", { name: msg("block.viewTrash") }).click();
  await page.getByRole("dialog", { name: msg("settings.title") }).getByRole("heading", { name: msg("settings.trash"), exact: true }).waitFor();
  await closeSettings();
  assert.equal((await server.api("/api/guard-events")).events.length >= 1, true, "the refusal is stored");

  await reloadWithHandoff(page);
  await page.locator("aside .conv", { hasText: "please delete the notes" }).first().click();
  await page.getByTestId("block-card").waitFor({ timeout: 20000 });
  assert.ok((await page.getByTestId("block-card").innerText()).includes(msg("block.title.delete")), "the card persists");
});
