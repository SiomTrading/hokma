/**
 * First-run E2E: the five steps of the first time (Claude, folder, database, name, done) against the real backend
 * with an empty working folder and no profile, then a reload to check that everything stays.
 *
 *   node e2e/first-run.e2e.mjs [es|en]        (both locales when omitted)
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { runSuite, until } from "./lib.mjs";

await runSuite("first-run", { script: "firstrun-serve.py", signedIn: true }, async ({ page, server, root, msg }) => {
  const OWNER = "Ana E2E";
  const FUND = "Fondo E2E";
  const work = path.join(root, "my-fund");
  await page.goto(server.base);

  const dialog = page.getByRole("dialog", { name: msg("firstrun.dialog") });
  const next = () => dialog.getByRole("button", { name: msg("firstrun.next"), exact: true }).click();
  const heading = (key) => dialog.getByRole("heading", { name: msg(key) }).waitFor();
  const stepsLabel = dialog.getByLabel(msg("firstrun.steps"));

  // 0. the modal opens by itself and shows the real Claude state (the fake CLI answers as a connected Pro plan)
  await dialog.waitFor();
  assert.equal((await server.api("/api/setup")).completed, false);
  await heading("firstrun.claude.title");
  await dialog.getByText(msg("firstrun.claude.connectedPlan", { plan: "Pro" })).waitFor();
  for (const key of ["claude", "folder", "database", "name", "done"]) {
    assert.ok((await stepsLabel.innerText()).includes(msg(`firstrun.step.${key}`)), `step ${key} listed`);
  }
  await next();

  // 1. folder: type a path that does not exist yet; the backend creates it and starts Git there
  await heading("firstrun.folder.title");
  await dialog.getByLabel(msg("firstrun.folder.chosen")).fill(work);
  await next();
  await heading("firstrun.db.title");
  assert.ok(fs.existsSync(path.join(work, ".git")), "the chosen folder is a Git repository");
  assert.equal((await server.api("/api/workdir")).path, work);

  // 2. database: local by default
  await dialog.getByText(msg("firstrun.db.local")).first().waitFor();
  await next();
  await heading("firstrun.name.title");

  // reload before the name is saved: the modal comes back from the start and the folder already chosen stays
  await page.reload();
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
  await next();

  // 4. done: real summary of what was set up
  await dialog.getByText(work).waitFor();
  await dialog.getByText(msg("firstrun.db.sqliteName")).waitFor();
  await dialog.getByText(msg("firstrun.done.manualReady")).waitFor();
  await dialog.getByRole("button", { name: msg("firstrun.open") }).waitFor();
  const saved = await server.api("/api/setup");
  assert.equal(saved.owner.owner_name, OWNER);
  assert.equal(saved.owner.fund_name, FUND);
  assert.equal(saved.completed, false, "not complete until the last button");

  await dialog.getByRole("button", { name: msg("firstrun.open") }).click();
  await dialog.waitFor({ state: "detached" });

  // 5. after finishing: persisted in the base and in the folder, and still there after another reload
  const done = await server.api("/api/setup");
  assert.equal(done.completed, true);
  assert.equal(done.profile.owner_name, OWNER);
  assert.equal(done.profile.fund_name, FUND);
  assert.equal(done.profile.workdir, work);
  assert.equal(done.profile.database_engine, "sqlite");
  assert.ok(fs.existsSync(path.join(work, "HOKMA.md")), "the manual is written in the folder");
  assert.ok(fs.readFileSync(path.join(work, "HOKMA.md"), "utf8").includes(OWNER));
  await page.reload();
  await page.locator("aside").first().getByText(FUND).first().waitFor();
  assert.equal(await page.getByRole("dialog", { name: msg("firstrun.dialog") }).count(), 0, "the modal does not return once done");
  assert.equal((await server.api("/api/setup")).completed, true);
});
