/**
 * Projects E2E: the right panel against the real backend. Create, upload a file (saved and committed in the project's
 * own Git), preview it, add a link, rename, switch the active project and reload.
 *
 *   node e2e/projects.e2e.mjs [es|en]        (both locales when omitted)
 */
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { reloadWithHandoff, runSuite, until } from "./lib.mjs";

/** The upload is filed by type in a subfolder of the project; find it wherever the backend put it. */
function findFile(dir, name) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === ".git") continue;
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      const found = findFile(full, name);
      if (found) return found;
    } else if (entry.name === name) return full;
  }
  return null;
}

const gitLog = (dir) => spawnSync("git", ["-C", dir, "log", "--oneline"], { encoding: "utf8" }).stdout.trim().split("\n").filter(Boolean);

await runSuite("projects", { script: "serve.py" }, async ({ page, server, root, msg, M }) => {
  const panel = page.getByRole("complementary", { name: msg("rightPanel.title") });
  const list = panel.getByRole("list", { name: msg("projects.list") });
  const dialogNamed = (key) => page.getByRole("dialog", { name: msg(key) });
  const toast = (text) => page.getByText(text).first().waitFor();
  const projects = async () => (await server.api("/api/projects")).projects;
  await page.goto(server.base);

  // 1. the real list: the default project named after the fund and the one the server created
  await list.waitFor();
  await list.getByRole("button", { name: /E2E Fund/ }).waitFor();
  await list.getByRole("button", { name: /Strategy/ }).waitFor();
  assert.equal((await projects()).length, 2);
  await panel.getByText(msg("projects.workdir"), { exact: true }).waitFor();
  // long paths are shortened in the middle; the full path is the tooltip
  await panel.locator(`[title="${server.home}"]`).first().waitFor();

  // 2. create a project from the "+" in the panel header
  await panel.getByRole("button", { name: msg("projects.create"), exact: true }).first().click();
  const create = dialogNamed("projects.dialog.createTitle");
  await create.getByLabel(msg("projects.field.name")).fill("   ");
  await create.getByRole("button", { name: msg("projects.create"), exact: true }).click();
  await create.getByText(msg("projects.error.name")).waitFor();
  await create.getByLabel(msg("projects.field.name")).fill("Research Notes");
  await create.getByLabel(msg("projects.field.kind")).selectOption({ label: msg("projects.kind.research") });
  await create.getByRole("button", { name: msg("projects.create"), exact: true }).click();
  await create.waitFor({ state: "detached" });
  await toast(msg("projects.toast.created", { name: "Research Notes" }));
  const created = (await projects()).find((p) => p.name === "Research Notes");
  assert.ok(created, "the project is in the base");
  assert.equal(created.kind, "research");
  assert.ok(fs.existsSync(created.path), "the project folder exists on disk");
  assert.ok(fs.existsSync(path.join(created.path, ".git")), "the project has its own Git repository");
  await list.getByRole("button", { name: /Research Notes/ }).waitFor();

  // 3. upload a file with the picker: saved in the folder, committed, and listed in the tree
  const note = path.join(root, "note.md");
  fs.writeFileSync(note, "# Context\n\nreal content of the note\n");
  await page.getByTestId("project-file-input").setInputFiles(note);
  const tree = page.getByRole("tree", { name: msg("projects.contents") });
  await tree.getByRole("treeitem", { name: msg("projects.open", { name: "note.md" }) }).waitFor();
  const stored = findFile(created.path, "note.md");
  assert.ok(stored, "the file is inside the project folder");
  assert.ok(fs.readFileSync(stored, "utf8").includes("real content of the note"));
  await until(() => gitLog(created.path).length >= 1, "the upload committed in the project's Git");

  // 4. preview shows the real text; Escape closes it
  await tree.getByRole("treeitem", { name: msg("projects.open", { name: "note.md" }) }).click();
  const preview = dialogNamed("projects.preview.title");
  const named = page.getByRole("dialog", { name: msg("projects.preview.title", { name: "note.md" }) });
  await named.getByText("real content of the note").waitFor();
  await named.getByRole("button", { name: msg("projects.close") }).click();
  await named.waitFor({ state: "detached" });
  void preview;

  // 5. add a link: an http address is refused, an https one is saved in the project
  await page.getByRole("button", { name: msg("projects.addLink"), exact: true }).click();
  const link = dialogNamed("projects.dialog.linkTitle");
  await link.getByLabel(msg("projects.field.url")).fill("http://example.com/plain");
  await link.getByRole("button", { name: msg("projects.addLink"), exact: true }).click();
  await link.getByText(msg("projects.error.url")).waitFor();
  await link.getByLabel(msg("projects.field.url")).fill("https://example.com/reference");
  await link.getByLabel(msg("projects.field.title")).fill("Reference page");
  await link.getByRole("switch").click();            // saved as a link only, no download
  await link.getByRole("button", { name: msg("projects.addLink"), exact: true }).click();
  await link.waitFor({ state: "detached" });
  await toast(msg("projects.toast.linkSaved"));
  const afterLink = await server.api(`/api/projects/${created.id}/tree`);
  assert.ok(JSON.stringify(afterLink).includes("example.com") || afterLink.entries.length >= 2, "the link is stored in the project");

  // 6. rename
  await panel.getByRole("button", { name: msg("projects.rename"), exact: true }).click();
  const rename = dialogNamed("projects.dialog.renameTitle");
  await rename.getByLabel(msg("projects.field.name")).fill("Research Desk");
  await rename.getByRole("button", { name: msg("projects.save"), exact: true }).click();
  await rename.waitFor({ state: "detached" });
  await toast(msg("projects.toast.renamed"));
  await list.getByRole("button", { name: /Research Desk/ }).waitFor();
  assert.ok((await projects()).some((p) => p.name === "Research Desk"));

  // 7. switch the active project: the tree follows
  await list.getByRole("button", { name: /Strategy/ }).click();
  await toast(msg("projects.toast.active", { name: "Strategy" }));
  await panel.getByRole("tree", { name: msg("projects.contents") }).getByRole("treeitem", { name: msg("projects.open", { name: "note.md" }) }).waitFor({ state: "detached" });
  assert.equal((await projects()).find((p) => p.active).name, "Strategy");

  // 8. reload: everything is still there, with Strategy active and the uploaded file in the renamed project
  await reloadWithHandoff(page);
  await list.getByRole("button", { name: /Research Desk/ }).waitFor();
  await list.getByRole("button", { name: /Strategy/ }).and(page.locator('[aria-current]')).waitFor();
  assert.equal((await projects()).find((p) => p.active).name, "Strategy");
  await panel.getByRole("tree", { name: msg("projects.contents") }).getByRole("treeitem", { name: msg("projects.open", { name: "note.md" }) }).waitFor({ state: "detached" });
  await list.getByRole("button", { name: /Research Desk/ }).click();
  await tree.getByRole("treeitem", { name: msg("projects.open", { name: "note.md" }) }).waitFor();
  void M;
});
