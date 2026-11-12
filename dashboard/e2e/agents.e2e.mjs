/**
 * Agents, scheduled tasks and usage E2E (D7): the real backend (e2e/agents-serve.py), the real compiled dashboard, a
 * real Chromium and the fake `claude` of backend/tests/engine_fixtures on PATH. Runs the whole flow in es and en.
 *
 *   node e2e/agents.e2e.mjs [es|en]        (both locales when omitted)
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
  const files = ["chat.ts", "core.ts", "agents.ts"];
  for (const file of files) {
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
    await sleep(150);
  }
  throw new Error(`timeout waiting for ${what}${last ? `: ${last.message}` : ""}`);
}

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
  const child = spawn("uv", ["run", "--project", repo, "python", path.join(here, "agents-serve.py"), dist, String(port)], {
    cwd: repo,
    env: {
      ...process.env, HOKMA_HOME: home, HOKMA_DATA_DIR: data, FAKE_CLAUDE_LOG: fakeLog, HOKMA_SCHEDULER_TICK: "0.5",
      PATH: `${bin}:${process.env.PATH}`,
    },
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
  return { child, port, token: info.token, home, data, fakeLog };
}

const fakeEntries = (log, event) =>
  fs.existsSync(log) ? fs.readFileSync(log, "utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l)).filter((e) => !event || e.event === event) : [];

/** Read-only look at the owner's database, to prove what the screens did. */
function sql(server, query) {
  const code = "import sqlite3,sys,json\nc=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True)\nprint(json.dumps(c.execute(sys.argv[2]).fetchall()))";
  const r = spawnSync("python3", ["-c", code, path.join(server.data, "hokma.db"), query], { encoding: "utf8" });
  assert.equal(r.status, 0, r.stderr);
  return JSON.parse(r.stdout);
}

async function scenario(chromium, locale, dist) {
  const M = loadMessages(locale);
  const msg = (key, params = {}) => M[key].replace(/\{(\w+)\}/g, (_, k) => params[k]);
  const root = fs.mkdtempSync(path.join(os.tmpdir(), `hokma-agents-e2e-${locale}-`));
  const server = await startServer(root, dist);
  const launch = { headless: true, ...(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}) };
  const browser = await chromium.launch(launch);
  const agentFile = (dir, slug) => path.join(server.home, ...dir.split("/"), `${slug}.md`);
  const api = async (p) => (await fetch(`http://127.0.0.1:${server.port}${p}`, { headers: { "X-Hokma-Token": server.token } })).json();
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
    await page.goto(`http://127.0.0.1:${server.port}/`);

    const openSettings = async (section) => {
      await page.getByRole("button", { name: M["nav.settings"] }).click();
      const dialog = page.getByRole("dialog", { name: M["settings.title"] });
      await dialog.getByRole("button", { name: M[`settings.${section}`] }).click();
      return dialog;
    };
    const closeSettings = () => page.getByRole("button", { name: M["settings.close"] }).click();

    // 1. Settings > Agents: the real list and the real count
    let dialog = await openSettings("agents");
    await dialog.getByText(msg("agents.count", { active: 7, total: 10 })).waitFor();
    assert.deepEqual((await api("/api/agents")).total, 10);

    // 1b. each agent has its own tint (from tokens), stable per agent, in dark and light
    const tints = () => dialog.locator(".ag .glyph").evaluateAll((els) => els.map((el) => getComputedStyle(el).color));
    const dark = await tints();
    assert.ok(new Set(dark).size >= 3, `agents share too few tints: ${dark}`);
    if (process.env.E2E_SHOT_DIR) await page.screenshot({ path: path.join(process.env.E2E_SHOT_DIR, `agents-dark-${locale}.png`) });
    await page.evaluate(() => document.querySelector(".hk").classList.add("light"));
    const light = await tints();
    assert.notDeepEqual(light, dark);
    assert.ok(new Set(light).size >= 3);
    if (process.env.E2E_SHOT_DIR) await page.screenshot({ path: path.join(process.env.E2E_SHOT_DIR, `agents-light-${locale}.png`) });
    await page.evaluate(() => document.querySelector(".hk").classList.remove("light"));
    assert.deepEqual(await tints(), dark);

    // 2. the model pill cycles and writes the base and the file
    await dialog.getByRole("button", { name: msg("agents.model.cycle", { name: "Vera", model: M["agents.model.sonnet"] }) }).click();
    // Opus asks for a confirmation first because it spends the weekly limit faster
    const opusAsk = page.getByRole("dialog", { name: M["agents.opus.title"] });
    await opusAsk.waitFor();
    await opusAsk.getByRole("button", { name: M["agents.opus.confirm"] }).click();
    await dialog.getByRole("button", { name: msg("agents.model.cycle", { name: "Vera", model: M["agents.model.opus"] }) }).waitFor();
    assert.deepEqual(sql(server, "SELECT model_tier FROM agents WHERE slug = 'vera'"), [["deep"]]);
    for (const file of [agentFile("agents", "vera"), agentFile(".claude/agents", "vera")]) {
      assert.match(fs.readFileSync(file, "utf8"), /^model: opus$/m);
    }

    // 3. turn an agent on and off: the file moves and the count follows
    assert.ok(fs.existsSync(agentFile(".claude/agents.off", "axel")) && !fs.existsSync(agentFile(".claude/agents", "axel")));
    await dialog.getByRole("switch", { name: msg("agents.toggle", { name: "Axel" }) }).click();
    await dialog.getByText(msg("agents.count", { active: 8, total: 10 })).waitFor();
    assert.ok(fs.existsSync(agentFile(".claude/agents", "axel")) && !fs.existsSync(agentFile(".claude/agents.off", "axel")));
    await dialog.getByRole("switch", { name: msg("agents.toggle", { name: "Axel" }) }).click();
    await dialog.getByText(msg("agents.count", { active: 7, total: 10 })).waitFor();
    assert.ok(fs.existsSync(agentFile(".claude/agents.off", "axel")) && !fs.existsSync(agentFile(".claude/agents", "axel")));

    // 4. create my own agent
    await dialog.getByRole("button", { name: M["agents.create"] }).click();
    const form = page.getByRole("dialog", { name: M["agents.create.title"] });
    await form.getByLabel(M["agents.create.name"]).fill("Chart Reader");
    await form.getByLabel(M["agents.create.description"]).fill("Reads charts and describes them.");
    await form.getByLabel(M["agents.create.instructions"]).fill("Describe the chart you are given.");
    await form.getByRole("button", { name: M["agents.model.haiku"] }).click();
    await form.getByRole("button", { name: M["agents.create.submit"] }).click();
    await dialog.getByText("Chart Reader", { exact: true }).waitFor();
    await dialog.getByText(msg("agents.count", { active: 8, total: 11 })).waitFor();
    for (const file of [agentFile("agents", "chart-reader"), agentFile(".claude/agents", "chart-reader")]) {
      const text = fs.readFileSync(file, "utf8");
      assert.match(text, /^model: haiku$/m);
      assert.match(text, /Describe the chart you are given\./);
    }
    assert.deepEqual(sql(server, "SELECT is_custom, status FROM agents WHERE slug = 'chart-reader'"), [[1, "active"]]);

    // 5. ...and it is available to /agent in the chat
    await closeSettings();
    await page.reload();                                   // the chat reads its agent list when it opens
    const box = page.getByRole("textbox", { name: M["chat.composer.label"] });
    await box.waitFor();
    await box.fill(`${M["chat.cmd.agent"]} chart`);
    await page.getByRole("menuitem", { name: /chart-reader/ }).click();
    await page.getByText(msg("chat.agentValue", { name: "chart-reader" }), { exact: true }).waitFor();
    await box.fill("describe this");
    await box.press("Enter");
    await page.getByRole("button", { name: M["chat.composer.send"] }).waitFor({ timeout: 20000 });
    assert.ok(fakeEntries(server.fakeLog, "start").at(-1).argv.join(" ").includes("--agent chart-reader"));

    // 6. usage against the cap: no cap yet, then the cap the owner sets, with the tokens already spent
    dialog = await openSettings("models");
    await dialog.getByText(M["models.unset"]).waitFor();
    await dialog.getByLabel(M["models.cap.label"]).fill("100000");
    await dialog.getByRole("button", { name: M["models.cap.save"] }).click();
    const used = (await api("/api/usage/weekly")).used;
    assert.ok(used >= 38000, "the seeded message and the chat turn are counted");
    const pct = Math.round((used * 100) / 100000);
    await dialog.getByText(msg("models.pct", { pct }), { exact: true }).waitFor();
    assert.equal((await api("/api/usage/weekly")).pct, Math.round(((used * 1000) / 100000)) / 10);
    await dialog.getByText(M["models.note"]).waitFor();
    await closeSettings();
    await page.locator("footer, .statusbar, [class*=status]").getByText(`${pct}%`).first().waitFor();

    // 7. a one-time task one minute from now: create, confirm, run, see the result and the approval
    await page.getByRole("button", { name: M["nav.tasks"] }).first().click();
    await page.getByText(M["tasks.empty.title"]).waitFor();
    await page.getByRole("button", { name: M["tasks.new"] }).click();
    const job = page.getByRole("dialog", { name: M["tasks.form.create"] });
    await job.getByLabel(M["tasks.form.name"]).fill("Minute task");
    await job.getByLabel(M["tasks.form.prompt"]).fill("Say pong.");
    await job.getByRole("button", { name: M["tasks.form.kind.once"] }).click();
    const when = await page.evaluate(() => {
      const d = new Date(Date.now() + 60 * 1000);
      const pad = (n) => String(n).padStart(2, "0");
      return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
    });
    await job.getByLabel(M["tasks.form.date"]).fill(when);
    await job.getByRole("button", { name: M["tasks.form.submit.create"] }).click();
    await page.getByText("Minute task", { exact: true }).waitFor();
    assert.deepEqual(sql(server, "SELECT is_active, approval_id FROM scheduled_jobs"), [[0, null]]);

    await page.getByRole("switch", { name: msg("tasks.toggle", { name: "Minute task" }) }).click();
    const confirm = page.getByRole("dialog", { name: msg("tasks.confirm.title", { name: "Minute task" }) });
    await confirm.getByRole("button", { name: M["tasks.confirm.yes"] }).click();
    await until(() => sql(server, "SELECT is_active FROM scheduled_jobs")[0][0] === 1, "job on", 5000);
    const approvals = sql(server, "SELECT subject_type, verdict, decided_by_kind, decided_by, channel FROM approvals WHERE subject_type = 'scheduled_job'");
    assert.deepEqual(approvals, [["scheduled_job", "approved", "human", "E2E Owner", "dashboard"]]);

    await page.getByText(new RegExp(msg("tasks.last", { status: M["tasks.status.done"] })), { exact: false }).waitFor({ timeout: 100000 });
    await page.locator("span.p").getByText("pong", { exact: true }).waitFor();
    const [[taskStatus, kind]] = sql(server, "SELECT status, created_by_kind FROM tasks WHERE title = 'Minute task'");
    assert.deepEqual([taskStatus, kind], ["done", "scheduler"]);
    const [[tin, tout, outcome]] = sql(server, "SELECT tokens_in, tokens_out, outcome FROM task_runs");
    assert.ok(tin > 0 && tout > 0 && outcome === "completed", "the run kept the tokens the engine reported");
    assert.equal(sql(server, "SELECT is_active FROM scheduled_jobs")[0][0], 0, "a one-time task turns itself off");
    assert.ok(sql(server, "SELECT count(*) FROM journal_entries WHERE kind = 'action'")[0][0] >= 1, "journal entry");

    // 8. the meter moved with the run
    const after = (await api("/api/usage/weekly")).used;
    assert.ok(after >= used + tin + tout);
    assert.deepEqual(errors, [], "no uncaught page errors");
    console.log(`ok ${locale}`);
  } finally {
    await browser.close();
    server.child.kill("SIGTERM");
    await sleep(300);
  }
}

const locales = process.argv[2] ? [process.argv[2]] : ["es", "en"];
const { chromium } = loadPlaywright();
const dist = fs.mkdtempSync(path.join(os.tmpdir(), "hokma-agents-e2e-dist-"));
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
