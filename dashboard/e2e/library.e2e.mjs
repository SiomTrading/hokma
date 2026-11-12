/**
 * Library E2E: the Library screen in the menu next to charts. The library endpoints answer from the browser
 * (contract of the Library API) so the screen is exercised on its own: the preparing band with its bar, the
 * "Try again" notice, upload, list with origin label, remove, and no activation control anywhere.
 *
 *   node e2e/library.e2e.mjs [es|en]        (both locales when omitted)
 */
import assert from "node:assert/strict";
import { runSuite } from "./lib.mjs";

await runSuite("library", { script: "serve.py" }, async ({ page, server, msg }) => {
  const state = { folders: [], nextFolder: 1, model: { state: "downloading", percent: 40, bytes: 40, total: 100 }, docs: [], nextId: 1, projectId: null };
  const json = (route, body, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

  await page.route("**/api/library/model/retry", (route) => {
    if (state.model.state !== "failed") return json(route, { detail: "model_not_failed" }, 409);
    state.model = { state: "downloading", percent: 0, bytes: 0, total: 100 };
    return json(route, state.model);
  });
  await page.route("**/api/library/model", (route) => json(route, state.model));
  const pathOf = (id) => {
    const parts = [];
    for (let cur = id; cur != null; ) {
      const f = state.folders.find((x) => x.id === cur);
      parts.unshift(f.name);
      cur = f.parent_id;
    }
    return parts.join("/");
  };
  const below = (id) => [id, ...state.folders.filter((f) => f.parent_id === id).flatMap((f) => below(f.id))];
  await page.route(/\/api\/library\/folders(\/\d+)?(\?.*)?$/, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const id = Number(url.pathname.split("/").pop());
    if (request.method() === "GET") {
      const parent = url.searchParams.get("parent_id");
      return json(route, { folders: state.folders.filter((f) => f.parent_id === (parent == null ? null : Number(parent))) });
    }
    if (request.method() === "POST") {
      const sent = request.postDataJSON();
      const folder = { id: state.nextFolder++, parent_id: sent.parent_id, name: sent.name };
      state.folders.push(folder);
      return json(route, folder, 201);
    }
    if (request.method() === "PATCH") {
      state.folders.find((f) => f.id === id).name = request.postDataJSON().name;
      return json(route, state.folders.find((f) => f.id === id));
    }
    const ids = below(id);
    const documents = state.docs.filter((d) => ids.includes(d.folder)).length;
    const subfolders = ids.length - 1;
    if ((documents || subfolders) && url.searchParams.get("confirm") !== "true") {
      return json(route, { detail: { code: "folder_not_empty", message: "full", documents, subfolders } }, 409);
    }
    state.folders = state.folders.filter((f) => !ids.includes(f.id));
    state.docs = state.docs.filter((d) => !ids.includes(d.folder));
    return route.fulfill({ status: 204 });
  });
  await page.route(/\/api\/library\/documents(\/\d+)?(\?.*)?$/, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const id = Number(url.pathname.split("/").pop());
    if (request.method() === "GET") {
      const folder = url.searchParams.get("folder_id");
      return json(route, { documents: state.docs.filter((d) => folder == null || d.folder === Number(folder)) });
    }
    if (request.method() === "PATCH") {
      const doc = state.docs.find((d) => d.id === id);
      doc.folder = request.postDataJSON().library_folder_id;
      doc.library_path = doc.folder == null ? "" : pathOf(doc.folder);
      return json(route, doc);
    }
    if (request.method() === "DELETE") {
      state.docs = state.docs.filter((d) => d.id !== id);
      return route.fulfill({ status: 204 });
    }
    const sentFolder = url.searchParams.get("folder_id");
    const folder = sentFolder ? Number(sentFolder) : null;
    const doc = {
      folder, id: state.nextId++, name: "notes.txt", format: "txt", status: "indexing", status_detail: null, chunk_count: 0,
      size_bytes: 12, source_url: null, description: null, origin_project_id: state.projectId, library_path: folder == null ? "" : pathOf(folder),
    };
    state.docs.push(doc);
    return json(route, doc, 201);
  });

  await page.goto(server.base);
  const projects = (await server.api("/api/projects")).projects;
  state.projectId = projects[0].id;
  const projectName = projects[0].name;

  // 1. the menu entry sits right after charts and opens the screen
  const menu = page.getByRole("complementary", { name: msg("nav.menu") });
  const labels = (await menu.getByRole("button").allInnerTexts()).map((n) => n.trim());
  assert.equal(labels.indexOf(msg("nav.library")), labels.indexOf(msg("nav.chart")) + 1, "Library follows charts in the menu");
  await menu.getByRole("button", { name: msg("nav.library"), exact: true }).click();
  const screen = page.getByTestId("library-screen");
  await screen.waitFor();
  await screen.getByText(msg("library.help")).waitFor();

  // 2. the search is preparing by itself: bar and text, nothing to press
  const band = page.getByTestId("library-model-preparing");
  await band.getByText(msg("library.model.preparing")).waitFor();
  assert.equal(await page.getByRole("switch").count(), 0, "no switch");

  // 3. a failed preparation shows one notice with one button; pressing it starts again
  state.model = { state: "failed", percent: 10, bytes: 10, total: 100 };
  const failed = page.getByTestId("library-model-failed");
  await failed.waitFor({ timeout: 8000 });
  await failed.getByRole("button", { name: msg("library.model.retry"), exact: true }).click();
  await band.waitFor();

  // 4. ready: the band and its text disappear and nothing is left behind
  state.model = { state: "ready", percent: 100, bytes: 100, total: 100 };
  await band.waitFor({ state: "detached", timeout: 8000 });
  assert.equal(await page.getByText(msg("library.model.preparing")).count(), 0);

  // 5. upload a file: it lands in the list with its origin label, indexing first and then ready
  await screen.getByText(msg("library.empty.title")).waitFor();
  await page.getByTestId("documents-file-input").setInputFiles({ name: "notes.txt", mimeType: "text/plain", buffer: Buffer.from("hello world") });
  const list = page.getByRole("list", { name: msg("docs.list") });
  const row = list.getByRole("listitem").filter({ hasText: "notes.txt" });
  await row.waitFor();
  await row.getByText(msg("library.origin.from", { project: projectName })).waitFor();
  await row.getByText(msg("docs.status.indexing"), { exact: true }).waitFor();
  state.docs[0].status = "ready";
  state.docs[0].chunk_count = 1;
  await row.getByText(msg("docs.status.ready"), { exact: true }).waitFor({ timeout: 8000 });

  // 6. remove it
  await row.getByRole("button", { name: msg("docs.removeNamed", { name: "notes.txt" }) }).click();
  await screen.getByText(msg("library.empty.title")).waitFor();
  assert.equal(state.docs.length, 0);

  // 7. folders: three levels, path, back by the path, upload into the folder in view
  const newFolder = async (name) => {
    await screen.getByRole("button", { name: msg("library.folder.new"), exact: true }).click();
    await screen.getByRole("textbox", { name: msg("library.folder.name") }).fill(name);
    await screen.getByRole("button", { name: msg("library.folder.create"), exact: true }).click();
    await screen.getByRole("list", { name: msg("library.folders.list") }).getByRole("button", { name, exact: true }).click();
  };
  for (const name of ["Papers", "2026", "Q1"]) await newFolder(name);
  const crumbs = page.getByRole("navigation", { name: msg("library.crumbs.label") });
  assert.deepEqual((await crumbs.getByRole("button").allInnerTexts()).map((x) => x.trim()), [msg("library.title"), "Papers", "2026", "Q1"]);
  await page.getByTestId("documents-file-input").setInputFiles({ name: "notes.txt", mimeType: "text/plain", buffer: Buffer.from("hello") });
  await list.getByRole("listitem").filter({ hasText: "notes.txt" }).waitFor();
  assert.equal(state.docs[0].folder, 3, "the upload landed in the folder in view");
  await crumbs.getByRole("button", { name: "Papers", exact: true }).click();
  await screen.getByRole("list", { name: msg("library.folders.list") }).getByRole("button", { name: "2026", exact: true }).waitFor();
  assert.equal(await list.getByRole("listitem").filter({ hasText: "notes.txt" }).count(), 0, "a document of a deeper level is not listed here");

  // 8. drag a document from the root onto a folder and onto the path
  await crumbs.getByRole("button", { name: msg("library.title"), exact: true }).click();
  await page.getByTestId("documents-file-input").setInputFiles({ name: "notes.txt", mimeType: "text/plain", buffer: Buffer.from("again") });
  const rootRow = list.getByRole("listitem").filter({ hasText: "notes.txt" });
  await rootRow.waitFor();
  await rootRow.dragTo(page.getByTestId("library-folder").filter({ hasText: "Papers" }));
  await rootRow.waitFor({ state: "detached" });
  assert.equal(state.docs.find((d) => d.library_path === "Papers")?.folder, 1);

  // 9. rename from the menu, then remove with the counts to confirm
  const folderRow = page.getByTestId("library-folder").filter({ hasText: "Papers" });
  await folderRow.getByRole("button", { name: msg("library.folder.menu", { name: "Papers" }) }).click();
  await page.getByRole("menuitem", { name: msg("library.folder.rename") }).click();
  await page.getByRole("textbox", { name: msg("library.folder.renameField", { name: "Papers" }) }).fill("Articles");
  await page.getByRole("button", { name: msg("library.folder.save"), exact: true }).click();
  const renamed = page.getByTestId("library-folder").filter({ hasText: "Articles" });
  await renamed.waitFor();
  await renamed.getByRole("button", { name: msg("library.folder.menu", { name: "Articles" }) }).click();
  await page.getByRole("menuitem", { name: msg("library.folder.remove") }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByText(msg("library.folder.confirm.body", {
    documents: msg("library.folder.count.documents", { n: 2 }),
    subfolders: msg("library.folder.count.subfolders", { n: 2 }),
  })).waitFor();
  await dialog.getByRole("button", { name: msg("library.folder.remove"), exact: true }).click();
  await renamed.waitFor({ state: "detached" });
  assert.equal(state.folders.length, 0);
  assert.equal(state.docs.length, 0);
});
