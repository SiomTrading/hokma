/**
 * First-run E2E: the five steps of the first time (Claude, folder, database, name, done) against the real backend
 * with an empty working folder and no profile, then a reload to check that everything stays.
 *
 *   node e2e/first-run.e2e.mjs [es|en]        (both locales when omitted)
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { reloadWithHandoff, runSuite, translator, until } from "./lib.mjs";

await runSuite("first-run", { script: "firstrun-serve.py", signedIn: true }, async ({ page, server, root, msg }) => {
  const OWNER = "Ana E2E";
  const FUND = "Fondo E2E";
  const work = path.join(root, "my-fund");
  await page.goto(server.base);

  const dialog = page.getByRole("dialog", { name: msg("firstrun.dialog") });
  const next = () => dialog.getByRole("button", { name: msg("firstrun.next"), exact: true }).click();
  const heading = (key) => dialog.getByRole("heading", { name: msg(key) }).waitFor();
  const stepsLabel = dialog.getByLabel(msg("firstrun.steps"));
  // C5: no step of the first run has a key field or a provider selector
  const forbidden = ["Alpaca", "Finnhub", "Coinbase", "Databento", "Massive"];
  for (const other of ["en", "es"]) {
    const T = translator(other).M;
    for (const key of ["connections.field.key", "connections.field.token", "connections.field.keyId", "connections.field.secret", "connections.key.save"]) forbidden.push(T[key]);
  }
  const noKeys = async (where) => {
    assert.equal(await dialog.locator('input[type="password"]').count(), 0, `${where}: no password field`);
    // what the owner can type into or press: labels and buttons (the preview of the manual is prose, not a field)
    const text = (await dialog.locator("label, button, select, [role=combobox]").allInnerTexts()).join("\n");
    for (const word of forbidden) assert.ok(!text.includes(word), `${where}: "${word}" is not part of the first run`);
  };

  // 0. the modal opens by itself and shows the real Claude state (the fake CLI answers as a connected Pro plan)
  await dialog.waitFor();
  assert.equal((await server.api("/api/setup")).completed, false);
  await heading("firstrun.claude.title");
  await dialog.getByText(msg("firstrun.claude.connectedPlan", { plan: "Pro" })).waitFor();
  for (const key of ["claude", "folder", "database", "name", "done"]) {
    assert.ok((await stepsLabel.innerText()).includes(msg(`firstrun.step.${key}`)), `step ${key} listed`);
  }
  await noKeys("claude step");
  await next();

  // 1. folder: type a path that does not exist yet; the backend creates it and starts Git there
  await heading("firstrun.folder.title");
  await dialog.getByLabel(msg("firstrun.folder.chosen")).fill(work);
  await noKeys("folder step");
  await next();
  await heading("firstrun.db.title");
  assert.ok(fs.existsSync(path.join(work, ".git")), "the chosen folder is a Git repository");
  assert.equal((await server.api("/api/workdir")).path, work);

  // 2. database: local by default
  await dialog.getByText(msg("firstrun.db.local")).first().waitFor();
  await noKeys("database step");
  await next();
  await heading("firstrun.name.title");

  // reload before the name is saved: the modal comes back from the start and the folder already chosen stays
  await reloadWithHandoff(page);
  await dialog.waitFor();
  await heading("firstrun.claude.title");
  await next();
  assert.equal(await dialog.getByLabel(msg("firstrun.folder.chosen")).inputValue(), work);
  await next();
  await next();
  await heading("firstrun.name.title");

  // 3. name and fund, with the live preview of the manual
  const preview = dialog.getByLabel(msg("firstrun.name.preview"));
  await dialog.getByLabel(msg("firstrun.name.owner"), { exact: true }).fill(OWNER);
  await dialog.getByLabel(msg("firstrun.name.fund")).fill(FUND);
  await until(async () => (await preview.innerText()).includes(OWNER), "owner name inside the manual preview");
  await noKeys("name step");
  await next();

  // 4. done: real summary of what was set up
  await dialog.getByText(work).waitFor();
  await dialog.getByText(msg("firstrun.db.sqliteName")).waitFor();
  await dialog.getByText(msg("firstrun.done.manualReady")).waitFor();
  await dialog.getByRole("button", { name: msg("firstrun.open") }).waitFor();
  await noKeys("done step");
  const saved = await server.api("/api/setup");
  assert.equal(saved.owner.owner_name, OWNER);
  assert.equal(saved.owner.fund_name, FUND);
  assert.equal(saved.completed, false, "not complete until the last button");
  // P1: the new installation shows the "no asking" switch already on, with nothing about keys or providers
  const modeSwitch = dialog.getByTestId("firstrun-mode").getByRole("switch", { name: msg("permissions.mode.auto") });
  await modeSwitch.waitFor();
  assert.equal(await modeSwitch.getAttribute("aria-checked"), "true");
  assert.equal(await dialog.getByTestId("firstrun-mode").locator("input").count(), 0, "no key or provider field in the card");

  await dialog.getByRole("button", { name: msg("firstrun.open") }).click();
  await dialog.waitFor({ state: "detached" });

  // 5. after finishing: persisted in the base and in the folder, and still there after another reload
  const done = await server.api("/api/setup");
  assert.equal(done.completed, true);
  assert.equal(done.profile.owner_name, OWNER);
  assert.equal(done.profile.fund_name, FUND);
  assert.equal(done.profile.workdir, work);
  assert.equal(done.profile.database_engine, "sqlite");
  assert.equal(done.profile.permission_mode, "auto", "a new installation starts in 'do everything without asking'");
  assert.equal(done.profile.permission_mode_intro_seen, true);
  assert.ok(fs.existsSync(path.join(work, "HOKMA.md")), "the manual is written in the folder");
  assert.ok(fs.readFileSync(path.join(work, "HOKMA.md"), "utf8").includes(OWNER));
  await reloadWithHandoff(page);
  await page.locator("aside").first().getByText(FUND).first().waitFor();
  assert.equal(await page.getByRole("dialog", { name: msg("firstrun.dialog") }).count(), 0, "the modal does not return once done");
  assert.equal((await server.api("/api/setup")).completed, true);
});
