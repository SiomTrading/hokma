/**
 * Documents E2E (Library): paste a link, find the documents, mark two, watch each row go from waiting to added, then a row the
 * site refuses that offers "open in the browser". Real backend and real browser; the "site" is a local server that
 * documents-serve.py wires in, so nothing leaves the machine.
 *
 *   node e2e/documents.e2e.mjs [es|en]        (both locales when omitted)
 */
import assert from "node:assert/strict";
import { runSuite } from "./lib.mjs";

await runSuite("documents", { script: "documents-serve.py" }, async ({ page, server, msg }) => {
  await page.goto(server.base);
  const menu = page.getByRole("complementary", { name: msg("nav.menu") });
  await menu.getByRole("button", { name: msg("nav.library"), exact: true }).click();
  await page.getByTestId("library-screen").waitFor();

  const links = page.getByTestId("documents-links");
  const url = (name) => "http://" + server.info.host + "/" + name;
  const rowOf = (title) => links.getByRole("list", { name: msg("docs.link.list") }).getByRole("listitem").filter({ hasText: title });
  const documents = async () => (await server.api("/api/library/documents")).documents;

  // 1. paste the link of a page and find its documents
  await links.getByLabel(msg("docs.link.title")).fill(url("papers"));
  await links.getByRole("button", { name: msg("docs.link.search"), exact: true }).click();
  for (const title of ["Paper Alpha", "Paper Beta", "Paper Locked"]) await rowOf(title).waitFor();

  // 2. mark two and download: each row shows its own progress until it is added
  await links.getByRole("checkbox", { name: msg("docs.link.select", { name: "Paper Alpha" }) }).check();
  await links.getByRole("checkbox", { name: msg("docs.link.select", { name: "Paper Beta" }) }).check();
  await links.getByRole("button", { name: msg("docs.link.download", { n: 2 }) }).click();
  await rowOf("Paper Alpha").getByText(msg("docs.link.state.running"), { exact: true }).waitFor();
  await rowOf("Paper Beta").getByText(msg("docs.link.state.waiting"), { exact: true }).waitFor();
  await rowOf("Paper Alpha").getByText(msg("docs.link.state.done"), { exact: true }).waitFor();
  await rowOf("Paper Beta").getByText(msg("docs.link.state.done"), { exact: true }).waitFor();

  // 3. both are in the Library document list, indexed
  const added = page.getByRole("list", { name: msg("docs.list") });
  await added.getByText(/alpha/i).first().waitFor();
  await added.getByText(/beta/i).first().waitFor();
  const docs = await documents();
  assert.equal(docs.length, 2, "two documents were added");
  assert.ok(docs.every((d) => d.status === "ready" && d.chunk_count >= 1 && String(d.source_url).startsWith("http://" + server.info.host)));

  // 4. a site that refuses the download: the row fails with the plain-language reason and offers the browser
  await links.getByRole("checkbox", { name: msg("docs.link.select", { name: "Paper Locked" }) }).check();
  await links.getByRole("button", { name: msg("docs.link.download", { n: 1 }) }).click();
  const locked = rowOf("Paper Locked");
  await locked.getByText(msg("docs.link.state.failed"), { exact: true }).waitFor();
  await locked.getByText(msg("docs.link.reason.blocked")).waitFor();
  const open = locked.getByRole("link", { name: msg("docs.link.openNamed", { name: "Paper Locked" }) });
  assert.equal(await open.getAttribute("href"), url("locked.pdf"));
  assert.equal(await open.getAttribute("target"), "_blank");
  assert.match((await open.getAttribute("rel")) ?? "", /noopener/);
  assert.equal((await documents()).length, 2, "the refused one was not added");
});
