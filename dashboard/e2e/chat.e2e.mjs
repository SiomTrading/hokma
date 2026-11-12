/**
 * Chat E2E (D5): the real backend (hokma start equivalent, e2e/serve.py), the real compiled dashboard, a real
 * Chromium and the fake `claude` of backend/tests/engine_fixtures on PATH. Runs the whole flow in es and en.
 *
 *   node e2e/chat.e2e.mjs [es|en]        (both locales when omitted)
 *
 * Needs the `playwright` package (dev dependency of dashboard/). If it is not installed here, point
 * PLAYWRIGHT_DIR to a folder whose node_modules has it. CHROMIUM_PATH overrides the browser binary.
 */
import { spawn, spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";

const here = path.dirname(fileURLToPath(import.meta.url));
const dashboard = path.resolve(here, "..");
const repo = path.resolve(dashboard, "..");

function loadPlaywright() {
  const roots = [process.env.PLAYWRIGHT_DIR, dashboard].filter(Boolean);
  for (const root of roots) {
    try {
      return createRequire(path.join(root, "noop.js"))("playwright");
    } catch {
      /* try the next place */
    }
  }
  throw new Error("The playwright package was not found. Install it in dashboard/ or set PLAYWRIGHT_DIR.");
}

/** Texts come from the message files, so this file carries no UI text of its own. */
function loadMessages(locale) {
  const out = {};
  for (const file of ["chat.ts", "core.ts"]) {
    const source = fs.readFileSync(path.join(dashboard, "src/i18n/messages", file), "utf8");
    const start = source.indexOf(`  ${locale}: {`);
    const other = locale === "es" ? "  en: {" : "  es: {";
    const otherAt = source.indexOf(other);
    const block = locale === "es" ? source.slice(start, otherAt > start ? otherAt : undefined) : source.slice(start);
    for (const m of block.matchAll(/^\s+"([\w.]+)":\s*("(?:[^"\\]|\\.)*"),?$/gm)) out[m[1]] = JSON.parse(m[2]);
  }
  return out;
}

const free = () =>
  new Promise((resolve) => {
    const s = net.createServer().listen(0, "127.0.0.1", () => {
      const { port } = s.address();
      s.close(() => resolve(port));
    });
  });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function until(fn, what, ms = 15000) {
  const end = Date.now() + ms;
  let last;
  while (Date.now() < end) {
    try {
      const v = await fn();
      if (v) return v;
    } catch (e) {
      last = e;
    }
    await sleep(100);
  }
  throw new Error(`timeout waiting for ${what}${last ? `: ${last.message}` : ""}`);
}

// 1x1 PNG
const PNG = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==", "base64");
const PDF = Buffer.from("%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n");

function buildDashboard(outDir) {
  const r = spawnSync("npx", ["vite", "build", "--outDir", outDir, "--emptyOutDir"], { cwd: dashboard, encoding: "utf8" });
  if (r.status !== 0) throw new Error(`vite build failed:\n${r.stdout}\n${r.stderr}`);
}

async function startServer(root, dist) {
  const home = path.join(root, "home");
  const data = path.join(root, "data");
  const bin = path.join(root, "bin");
  for (const d of [home, data, bin]) fs.mkdirSync(d, { recursive: true });
  const fakeLog = path.join(root, "fake.log");
  const claude = path.join(bin, "claude");
  fs.writeFileSync(
    claude,
    `#!/usr/bin/env python3\nimport os, runpy\nos.environ.setdefault("FAKE_CLAUDE_LOG", ${JSON.stringify(fakeLog)})\nrunpy.run_path(${JSON.stringify(path.join(repo, "backend/tests/engine_fixtures/fake_claude.py"))}, run_name="__main__")\n`,
  );
  fs.chmodSync(claude, 0o755);
  const port = await free();
  const child = spawn("uv", ["run", "--project", repo, "python", path.join(here, "serve.py"), dist, String(port)], {
    cwd: repo,
    env: { ...process.env, HOKMA_HOME: home, HOKMA_DATA_DIR: data, FAKE_CLAUDE_LOG: fakeLog, PATH: `${bin}:${process.env.PATH}` },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stderr = "";
  child.stderr.on("data", (d) => (stderr += d));
  const info = await new Promise((resolve, reject) => {
    let buf = "";
    child.stdout.on("data", (d) => {
      buf += d;
      const line = buf.split("\n").find((l) => l.startsWith("{"));
      if (line) resolve(JSON.parse(line));
    });
    child.on("exit", (code) => reject(new Error(`server exited (${code}):\n${stderr}`)));
  });
  await until(async () => (await fetch(`http://127.0.0.1:${port}/health`)).ok, "server health");
  return { child, port, token: info.token, home, fakeLog };
}

const fakeEntries = (log, event) =>
  fs.existsSync(log) ? fs.readFileSync(log, "utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l)).filter((e) => !event || e.event === event) : [];

async function scenario(chromium, locale, dist) {
  const M = loadMessages(locale);
  const msg = (key, params = {}) => M[key].replace(/\{(\w+)\}/g, (_, k) => params[k]);
  const root = fs.mkdtempSync(path.join(os.tmpdir(), `hokma-chat-e2e-${locale}-`));
  const server = await startServer(root, dist);
  const launch = { headless: true, ...(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}) };
  const browser = await chromium.launch(launch);
  try {
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 940 } });
    await ctx.addInitScript(([token, loc]) => {
      if (!window.name && window.location.protocol.startsWith("http")) {                           // the way a handoff gives a fresh tab its session, never a storage
        const hp = new URLSearchParams(window.location.hash.slice(1));                          // the address carries #/chat/... now
        hp.delete("t");
        hp.delete("o");
        const u = window.location.pathname + window.location.search + (hp.toString() ? "#" + hp.toString() : "");
        window.name = "hokma-handoff:" + JSON.stringify({ o: window.location.origin, u, t: token });
      }
      localStorage.setItem("hokma.locale", loc);
    }, [server.token, locale]);
    const page = await ctx.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    const base = `http://127.0.0.1:${server.port}/`;
    await page.goto(base);

    // chat screens stay mounted in the background (a second conversation tab): everything is read from the one in front
    const front = page.locator(".screen-host:not([hidden])");
    const box = front.locator("#chat-message");
    const users = front.getByTestId("user-message");
    const answers = front.getByTestId("assistant-message");
    const expectCount = async (loc, n, what) => until(async () => (await loc.count()) === n, `${what} = ${n}`);
    const turn = async (text) => {
      const before = await users.count();
      await box.fill(text);
      await box.press("Enter");
      // a normal message shows up as a user bubble at once; slash commands do not
      if (!text.startsWith("/")) await until(async () => (await users.count()) > before, "the user bubble");
    };
    const idle = () => front.getByRole("button", { name: msg("chat.composer.send") }).waitFor({ timeout: 20000 });

    // 1. write and receive a streamed answer
    await box.waitFor();
    await turn("hello there");
    await expectCount(users, 1, "user bubbles");
    await until(async () => (await answers.last().innerText()).includes("pong"), "assistant answer");
    await idle();
    assert.equal(fakeEntries(server.fakeLog, "start").length, 1);
    assert.ok((await page.locator("aside .conv").first().innerText()).includes("hello there"), "conversation listed in the left menu");

    // 2. stop in the middle of a turn
    await turn("long one [slow]");
    await front.getByRole("button", { name: msg("chat.composer.stop") }).click();
    await front.getByText(msg("chat.stopped"), { exact: true }).waitFor({ timeout: 20000 });
    await idle();

    // 3. approve and reject a permission
    await turn("write [perm]");
    const card = front.getByTestId("permission-card");
    await card.waitFor({ timeout: 20000 });
    await card.getByRole("button", { name: msg("chat.perm.approve") }).click();
    await front.getByTestId("permission-done").getByText(msg("chat.perm.allowed")).waitFor();
    await idle();
    // the recorded permission request has a fixed id, so the rejection runs in a second conversation (Ctrl Shift O)
    await page.keyboard.press("Control+Shift+O");
    await front.getByText(msg("chat.empty.title")).waitFor();
    await turn("again [perm]");
    await front.getByTestId("permission-card").waitFor({ timeout: 20000 });
    await front.getByTestId("permission-card").getByRole("button", { name: msg("chat.perm.reject") }).click();
    await front.getByTestId("permission-done").getByText(msg("chat.perm.denied")).waitFor();
    await idle();
    const answersBeforeFiles = await answers.count();

    // 4. attach a PNG and a PDF by clicking, and see the preview
    const input = front.getByTestId("file-input");
    await input.setInputFiles([
      { name: "chart.png", mimeType: "image/png", buffer: PNG },
      { name: "plan.pdf", mimeType: "application/pdf", buffer: PDF },
    ]);
    const pending = front.getByTestId("pending-attachment");
    await expectCount(pending, 2, "pending attachments");
    await until(async () => (await pending.first().locator("img").count()) === 1, "image preview");
    await until(async () => !(await pending.allInnerTexts()).some((text) => text.includes(msg("chat.composer.uploading"))), "upload end");
    await turn("see these");
    await idle();
    const sent = users.last();
    assert.ok((await sent.innerText()).includes("chart.png") && (await sent.innerText()).includes("plan.pdf"));
    assert.equal(await answers.count() > answersBeforeFiles, true);

    // "Save to the project" on the attachment and on the answer (a file in the project folder and a Git commit)
    await sent.getByRole("button", { name: msg("chat.save") }).first().click();
    await until(() => fs.existsSync(path.join(server.home, "projects", "principal", "imagenes", "chart.png")), "saved image on disk");
    await answers.last().getByRole("button", { name: msg("chat.save") }).click();
    await until(() => fs.existsSync(path.join(server.home, "projects", "principal", "documentos")) && fs.readdirSync(path.join(server.home, "projects", "principal", "documentos")).some((f) => f.endsWith(".md")), "saved answer on disk");

    // 5. paste an image from the clipboard
    await box.focus();
    await page.evaluate((b64) => {
      const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
      const data = new DataTransfer();
      data.items.add(new File([bytes], "pasted.png", { type: "image/png" }));
      document.querySelector(".screen-host:not([hidden]) #chat-message").dispatchEvent(new ClipboardEvent("paste", { clipboardData: data, bubbles: true, cancelable: true }));
    }, PNG.toString("base64"));
    await expectCount(pending, 1, "pasted attachment");
    await pending.first().getByRole("button", { name: msg("chat.composer.removeAttachment", { name: "pasted.png" }) }).click();
    await expectCount(pending, 0, "pending after remove");

    // 6. /model opus changes the pill and the next turn uses opus
    await turn(`${M["chat.cmd.model"]} opus`);
    // Opus spends the weekly limit faster: nothing changes until the owner confirms
    const opusAsk = page.getByRole("dialog", { name: msg("chat.opus.title") });
    await opusAsk.waitFor();
    await opusAsk.getByRole("button", { name: msg("chat.opus.confirm") }).click();
    await front.getByRole("button", { name: msg("chat.model.pill", { name: msg("chat.model.opus") }) }).waitFor();
    await turn("with opus");
    await idle();
    assert.ok(fakeEntries(server.fakeLog, "start").at(-1).argv.join(" ").includes("--model opus"), "next turn launched with opus");

    // model menu from the pill
    await front.getByRole("button", { name: msg("chat.model.pill", { name: msg("chat.model.opus") }) }).click();
    await page.getByRole("menuitem", { name: new RegExp(msg("chat.model.haiku")) }).click();
    await front.getByRole("button", { name: msg("chat.model.pill", { name: msg("chat.model.haiku") }) }).waitFor();

    // 7. /project changes the pill and the working folder of the engine
    await box.fill(`${M["chat.cmd.project"]} Str`);
    await page.getByRole("menuitem", { name: "Strategy" }).click();
    await front.locator(".chat-head").getByText("Strategy", { exact: true }).waitFor();
    await turn("in the other project");
    await idle();
    assert.equal(fakeEntries(server.fakeLog, "start").at(-1).cwd, path.join(server.home, "projects", "strategy"));

    // 8. /agent shows the agent in the header and the turn goes to it
    await box.fill(`${M["chat.cmd.agent"]} risk`);
    await page.getByRole("menuitem", { name: /risk-desk/ }).click();
    await front.locator(".chat-head").getByText(msg("chat.agentValue", { name: "risk-desk" }), { exact: true }).waitFor();
    await turn("as the agent");
    await idle();
    assert.ok(fakeEntries(server.fakeLog, "start").at(-1).argv.join(" ").includes("--agent risk-desk"));

    // 9. search finds by content; an unrelated word finds nothing
    const search = page.getByLabel(msg("sidebar.search"));
    await search.fill("pong");
    await page.locator("aside .sec", { hasText: msg("chat.list.results") }).waitFor();
    assert.ok((await page.locator("aside .conv").count()) >= 1);
    await search.fill("zzzzqqq");
    await page.getByText(msg("chat.list.noResults"), { exact: true }).waitFor();
    await search.fill("");

    // 10. pin moves it to Pinned and survives a reload
    await page.locator("aside .conv", { hasText: "hello there" }).hover();
    await page.getByRole("button", { name: msg("chat.list.options", { title: "hello there" }) }).click();
    await page.getByRole("menuitem", { name: msg("chat.list.pin") }).click();
    await page.locator("aside .sec", { hasText: msg("chat.list.pinned") }).waitFor();
    await page.reload();
    await page.locator("aside .sec", { hasText: msg("chat.list.pinned") }).waitFor();
    const pinnedRow = page.locator("aside .conv", { hasText: "hello there" });
    await pinnedRow.click();

    // 11. history when a stored conversation is opened
    await users.first().waitFor({ timeout: 15000 });
    assert.ok((await users.first().innerText()).includes("hello there"));
    assert.ok((await answers.first().innerText()).includes("pong"));

    // 12. Ctrl Shift O starts a clean conversation
    await page.keyboard.press("Control+Shift+O");
    await until(async () => (await users.count()) === 0, "fresh chat");

    // 13. reconnection: cut the socket on the page side and see the app recover on its own
    await page.evaluate(() => window.dispatchEvent(new Event("offline")));
    assert.deepEqual(errors, [], "no uncaught page errors");
    console.log(`ok ${locale}`);
  } catch (error) {
    if (process.env.E2E_DEBUG_DIR) {
      const page = browser.contexts()[0]?.pages()[0];
      if (page) {
        await page.screenshot({ path: path.join(process.env.E2E_DEBUG_DIR, `fail-${locale}.png`) }).catch(() => undefined);
        fs.writeFileSync(path.join(process.env.E2E_DEBUG_DIR, `fail-${locale}.txt`), await page.locator("body").innerText().catch(() => ""));
      }
      if (fs.existsSync(server.fakeLog)) fs.copyFileSync(server.fakeLog, path.join(process.env.E2E_DEBUG_DIR, `fake-${locale}.log`));
    }
    throw error;
  } finally {
    await browser.close();
    server.child.kill("SIGTERM");
    await sleep(300);
  }
}

const locales = process.argv[2] ? [process.argv[2]] : ["es", "en"];
const { chromium } = loadPlaywright();
const dist = fs.mkdtempSync(path.join(os.tmpdir(), "hokma-chat-e2e-dist-"));
buildDashboard(dist);
let failed = false;
for (const locale of locales) {
  try {
    await scenario(chromium, locale, dist);
  } catch (error) {
    failed = true;
    console.error(`FAIL ${locale}:`, error.stack || error);
  }
}
process.exit(failed ? 1 : 0);
